"""Payments tests: real x402 verification, per-tool pricing, anti-replay,
CDP JWT minting, Stripe checkout/webhook/subscriber keys.

Facilitator calls are monkeypatched; Stripe webhook signatures are built
with HMAC so construct_event runs for real.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import search_mcp.billing as billing_mod
import search_mcp.x402 as x402_mod
from search_mcp import fetch as fetch_mod
from search_mcp import search as search_mod
from search_mcp.rest_app import create_app


@pytest.fixture()
def stubbed_backends(monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "tvly-test")

    class FakeTavily:
        def __init__(self, api_key=None):
            pass

        def search(self, query, max_results=5, search_depth="basic"):
            return {"results": [{"title": "T", "url": "https://example.com/1", "content": "s1"}]}

    monkeypatch.setattr(search_mod, "TavilyClient", FakeTavily)
    monkeypatch.setattr(fetch_mod, "fetch_url", lambda url, timeout=20: "clean page text")


def _enable_x402(monkeypatch, **over):
    env = {
        "X402_ENABLED": "true",
        "X402_PAY_TO": "0xAbC123",
        "X402_FACILITATOR_URL": "https://x402.test/facilitator",
        "X402_PRICE_USDC": "0.015",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _enable_stripe(monkeypatch, tmp_path, **over):
    env = {
        "STRIPE_ENABLED": "true",
        "STRIPE_SECRET_KEY": "sk_test_123",
        "STRIPE_WEBHOOK_SECRET": "whsec_test_123",
        "STRIPE_PRICE_ID": "price_test_123",
        "STRIPE_SUCCESS_URL": "https://example.com/success?session_id={CHECKOUT_SESSION_ID}",
        "STRIPE_CANCEL_URL": "https://example.com/cancel",
        "BILLING_DB_PATH": str(tmp_path / "subs.db"),
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


_sig_counter = 0


def _x402_payload(value_atomic=25000, **auth_over):
    global _sig_counter
    _sig_counter += 1
    # Unique signature per header so the anti-replay set can't leak between tests.
    sig = auth_over.pop("signature", "0x" + f"{_sig_counter:064x}"[-64:])
    now = int(time.time())
    auth = {
        "from": "0xPayer000000000000000000000000000000000001",
        "to": "0xAbC123",
        "value": str(value_atomic),
        "validAfter": str(now - 60),
        "validBefore": str(now + 300),
        "nonce": "0x" + f"{_sig_counter:064x}"[-64:],
    }
    auth.update(auth_over)
    return {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": {"signature": sig, "authorization": auth},
    }


def _x402_header(value_atomic=25000, **auth_over):
    raw = (
        base64.urlsafe_b64encode(json.dumps(_x402_payload(value_atomic, **auth_over)).encode())
        .decode()
        .rstrip("=")
    )
    return {"X-Payment": raw}


def _fake_facilitator(monkeypatch, valid=True, unreachable=False, settle_ok=True):
    async def fake_verify(payment_payload, requirements):  # noqa: ANN001, ANN202
        if unreachable:
            raise x402_mod.FacilitatorUnavailable("down")
        return {
            "isValid": valid,
            "invalidReason": None if valid else "insufficient funds",
            "payer": "0xPayer000000000000000000000000000000000001",
        }

    async def fake_settle(payment_payload, requirements):  # noqa: ANN001, ANN202
        if unreachable:
            raise x402_mod.FacilitatorUnavailable("down")
        if not settle_ok:
            return {"success": False, "errorReason": "nonce already used"}
        return {"success": True, "transaction": "0xTxHash1", "network": "base"}

    monkeypatch.setattr(x402_mod, "facilitator_verify", fake_verify)
    monkeypatch.setattr(x402_mod, "facilitator_settle", fake_settle)


# ------------------------------------------------------------- pricing


def test_per_tool_price_defaults(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"})
    assert r.json()["paymentRequirements"]["maxAmountRequired"] == "25000"  # 0.025
    r = xc.post("/fetch", json={"url": "https://example.com/"})
    assert r.json()["paymentRequirements"]["maxAmountRequired"] == "10000"  # 0.01


def test_per_tool_env_override_only_affects_that_tool(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch, X402_PRICE_SEARCH_WEB_USDC="0.10")
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"})
    assert r.json()["paymentRequirements"]["maxAmountRequired"] == "100000"
    r = xc.post("/fetch", json={"url": "https://example.com/"})
    assert r.json()["paymentRequirements"]["maxAmountRequired"] == "10000"


def test_invalid_price_env_falls_back_to_default(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch, X402_PRICE_FETCH_URL_USDC="banana")
    xc = TestClient(create_app())
    r = xc.post("/fetch", json={"url": "https://example.com/"})
    assert r.json()["paymentRequirements"]["maxAmountRequired"] == "10000"


def test_402_challenge_shape(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"})
    assert r.status_code == 402
    body = r.json()
    assert body["accepts"]["scheme"] == "exact"
    assert "STUB" not in json.dumps(body)
    reqs = body["paymentRequirements"]
    assert reqs["asset"] == x402_mod.USDC_BASE_ADDRESS
    assert reqs["scheme"] == "exact"
    assert reqs["network"] == "base"
    assert reqs["payTo"] == "0xAbC123"
    assert reqs["maxTimeoutSeconds"] == 300


# ------------------------------------------------------------- JSON-RPC discovery


def _mcp_result_text(response):
    assert response.status_code == 200, response.text
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError("no data event in MCP response")


def test_tools_list_stays_free(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    with TestClient(create_app()) as xc:
        r = xc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code != 402
        _mcp_result_text(r)


def test_tools_call_requires_payment(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True)
    with TestClient(create_app()) as xc:
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "fetch_url", "arguments": {"url": "https://example.com/"}},
        }
        # Bare /mcp (no trailing slash) — regression test for the 307 redirect
        # fix: the TestClient follows redirects, and a 307 would replay the
        # X-Payment header to /mcp/, tripping the anti-replay guard
        # (duplicate_payment -> 402) instead of the expected 200.
        r = xc.post("/mcp", json=body)
        assert r.status_code == 402
        assert r.json()["paymentRequirements"]["maxAmountRequired"] == "10000"
        r = xc.post("/mcp", json=body, headers=_x402_header(value_atomic=10000))
        assert r.status_code == 200, r.text


# ------------------------------------------------------------- sanity checks


def test_wrong_recipient_rejected(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header(to="0xEvil999"))
    assert r.status_code == 402
    assert r.json()["reason"] == "authorization recipient mismatch"


def test_underpriced_rejected(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header(value_atomic=1))
    assert r.status_code == 402


def test_expired_rejected(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True)
    xc = TestClient(create_app())
    now = int(time.time())
    r = xc.post(
        "/search",
        json={"query": "q"},
        headers=_x402_header(validAfter=str(now - 600), validBefore=str(now - 10)),
    )
    assert r.status_code == 402
    assert r.json()["reason"] == "authorization outside validity window"


# ------------------------------------------------------------- facilitator behavior


def test_facilitator_denial_surfaces_reason(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=False)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header())
    assert r.status_code == 402
    assert r.json()["error"] == "payment_invalid"
    assert r.json()["reason"] == "insufficient funds"


def test_facilitator_unreachable_503(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, unreachable=True)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header())
    assert r.status_code == 503
    assert r.json()["error"] == "payment_verification_unavailable"


def test_settle_failure_still_serves(stubbed_backends, monkeypatch, caplog):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True, settle_ok=False)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header())
    assert r.status_code == 200, r.text
    assert "settle FAILED" in caplog.text


def test_replay_rejected(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True)
    xc = TestClient(create_app())
    headers = _x402_header(signature="0x" + "ee" * 32)
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    r = xc.post("/search", json={"query": "q"}, headers=headers)
    assert r.status_code == 402
    assert r.json()["error"] == "duplicate_payment"


# ------------------------------------------------------------- CDP JWT


def _fresh_ed25519_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def test_cdp_jwt_structure_and_signature():
    from cryptography.hazmat.primitives import serialization

    private, public = _fresh_ed25519_keypair()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    raw_b64 = (
        base64.urlsafe_b64encode(
            private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
        .decode()
        .rstrip("=")
    )

    for secret in (pem, raw_b64):
        token = x402_mod.mint_cdp_jwt("test-key-id", secret, now=1_700_000_000)
        header_b64, claims_b64, sig_b64 = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        header = json.loads(base64.urlsafe_b64decode(pad(header_b64)))
        claims = json.loads(base64.urlsafe_b64decode(pad(claims_b64)))
        assert header["alg"] == "EdDSA"
        assert header["typ"] == "JWT"
        assert header["kid"] == "test-key-id"
        assert len(header["nonce"]) == 32
        assert claims["sub"] == "test-key-id"
        assert claims["iss"] == "coinbase-cloud"
        assert claims["nbf"] == 1_700_000_000 - 30
        assert claims["exp"] == 1_700_000_000 + 120
        signing_input = f"{header_b64}.{claims_b64}".encode()
        public.verify(base64.urlsafe_b64decode(pad(sig_b64)), signing_input)


# ------------------------------------------------------------- Stripe


def _signed_event(secret: str, event: dict) -> tuple[bytes, str]:
    payload = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, f"t={ts},v1={sig}"


def _completed_event(customer_id="cus_123"):
    return {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "customer": customer_id,
                "customer_details": {"email": "buyer@example.com"},
            }
        },
    }


def test_checkout_disabled_404(stubbed_backends, monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path, STRIPE_ENABLED="false")
    xc = TestClient(create_app())
    assert xc.post("/billing/checkout").status_code == 404


def test_checkout_creates_session(stubbed_backends, monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)

    class FakeSession:
        url = "https://checkout.stripe.com/pay/cs_test_123"

    monkeypatch.setattr(
        billing_mod.stripe.checkout.Session,
        "create",
        classmethod(lambda cls, **kw: FakeSession()),
    )
    xc = TestClient(create_app())
    r = xc.post("/billing/checkout")
    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"] == "https://checkout.stripe.com/pay/cs_test_123"


def test_webhook_completed_creates_subscriber(stubbed_backends, monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    xc = TestClient(create_app())
    payload, sig = _signed_event("whsec_test_123", _completed_event())
    r = xc.post("/billing/stripe-webhook", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200, r.text

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "subs.db"))
    try:
        row = conn.execute(
            "SELECT key_hash, customer_id, email, status FROM subscribers"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    key_hash, customer_id, email, status = row
    assert customer_id == "cus_123"
    assert email == "buyer@example.com"
    assert status == "active"
    # Placeholder hash only — no usable key exists before GET /billing/key.
    assert not billing_mod.subscriber_key_valid("anything")


def test_webhook_bad_signature_400(stubbed_backends, monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    xc = TestClient(create_app())
    r = xc.post(
        "/billing/stripe-webhook",
        content=b"{}",
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert r.status_code == 400


def test_webhook_deleted_deactivates(stubbed_backends, monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    billing_mod.upsert_subscriber_placeholder("cus_9", "x@y.z")
    token = billing_mod.rotate_subscriber_key("cus_9")
    assert billing_mod.subscriber_key_valid(token)

    event = {
        "id": "evt_2",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_1", "customer": "cus_9"}},
    }
    payload, sig = _signed_event("whsec_test_123", event)
    xc = TestClient(create_app())
    r = xc.post("/billing/stripe-webhook", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert not billing_mod.subscriber_key_valid(token)


def test_subscriber_key_bypasses_x402(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    _fake_facilitator(monkeypatch, valid=True)
    billing_mod.upsert_subscriber_placeholder("cus_42", "s@example.com")
    token = billing_mod.rotate_subscriber_key("cus_42")

    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers={"X-Subscriber-Key": token})
    assert r.status_code == 200, r.text
    # Raw token is never stored — only its hash.
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "subs.db"))
    try:
        stored = conn.execute("SELECT key_hash FROM subscribers").fetchone()[0]
    finally:
        conn.close()
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert stored != token
    # Bad key falls through to the x402 flow, not a hard error.
    r = xc.post("/search", json={"query": "q"}, headers={"X-Subscriber-Key": "nope"})
    assert r.status_code == 402


def test_get_key_exchanges_completed_session(stubbed_backends, monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True)
    billing_mod.upsert_subscriber_placeholder("cus_77", "k@example.com")

    class FakeSession(dict):
        pass

    monkeypatch.setattr(
        billing_mod.stripe.checkout.Session,
        "retrieve",
        classmethod(
            lambda cls, session_id: FakeSession(
                {
                    "status": "complete",
                    "customer": "cus_77",
                    "customer_details": {"email": "k@example.com"},
                }
            )
        ),
    )
    xc = TestClient(create_app())
    r = xc.get("/billing/key", params={"session_id": "cs_1"})
    assert r.status_code == 200, r.text
    token = r.json()["subscriber_key"]
    assert billing_mod.subscriber_key_valid(token)
    r = xc.post("/search", json={"query": "q"}, headers={"X-Subscriber-Key": token})
    assert r.status_code == 200


def test_billing_status_no_secrets(stubbed_backends, monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    xc = TestClient(create_app())
    r = xc.get("/billing/status")
    assert r.status_code == 200
    body = r.json()
    assert body == {"stripe_enabled": True, "x402_enabled": False}
    assert "sk_test" not in json.dumps(body)


# --------------------------------- disk path + subscriber fair-use cap


def _subscriber_token(customer_id):
    billing_mod.upsert_subscriber_placeholder(customer_id, f"{customer_id}@x.io")
    return billing_mod.rotate_subscriber_key(customer_id)


def _usage_row(tmp_path):
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "subs.db"))
    try:
        return conn.execute("SELECT month, credits FROM subscriber_usage").fetchone()
    finally:
        conn.close()


def test_db_default_path_is_disk_mount(monkeypatch):
    monkeypatch.delenv("BILLING_DB_PATH", raising=False)
    assert billing_mod._db_path() == "/var/lib/search-mcp/billing.db"


def test_credit_cost_derives_from_price_map(monkeypatch):
    """Credit cost must come from the same price map the x402 middleware
    charges with — no duplicated price table."""
    assert billing_mod.credits_for_price_usdc(x402_mod.price_for_tool_usdc("search_web")) == 2.5
    assert billing_mod.credits_for_price_usdc(x402_mod.price_for_tool_usdc("fetch_url")) == 1.0
    # An env price override flows through to credits automatically.
    monkeypatch.setenv("X402_PRICE_SEARCH_WEB_USDC", "0.05")
    assert billing_mod.credits_for_price_usdc(x402_mod.price_for_tool_usdc("search_web")) == 5.0


def test_old_call_limit_env_is_gone(monkeypatch):
    """SUBSCRIBER_MONTHLY_CALL_LIMIT no longer has any effect."""
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", "999999")
    monkeypatch.delenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", raising=False)
    assert billing_mod._monthly_credit_limit() == 1000.0


def test_invalid_credit_limit_env_falls_back(monkeypatch):
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "bogus")
    assert billing_mod._monthly_credit_limit() == 1000.0
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "-5")
    assert billing_mod._monthly_credit_limit() == 1000.0


def test_subscriber_under_cap_200_and_counted(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "10")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_cap1")

    xc = TestClient(create_app())
    for _ in range(3):
        r = xc.post("/search", json={"query": "q"}, headers={"X-Subscriber-Key": token})
        assert r.status_code == 200, r.text

    # A $0.025 search deducts 2.5 credits: 3 x 2.5 = 7.5.
    month, credits = _usage_row(tmp_path)
    assert credits == 7.5
    assert month == billing_mod._current_month_utc()


def test_fractional_deduction_mixed_tools(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "100")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_frac")
    headers = {"X-Subscriber-Key": token}

    xc = TestClient(create_app())
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    assert (
        xc.post("/fetch", json={"url": "https://example.com/x"}, headers=headers).status_code == 200
    )

    # search_web = 2.5 credits, fetch_url = 1.0 credit.
    assert _usage_row(tmp_path)[1] == 3.5


def test_subscriber_over_cap_429(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "5")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_cap2")

    xc = TestClient(create_app())
    headers = {"X-Subscriber-Key": token}
    # Two searches = 2 x 2.5 = 5.0 credits: exactly the cap.
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200

    r = xc.post("/search", json={"query": "q"}, headers=headers)
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["error"] == "subscriber_monthly_cap_exceeded"
    assert body["credits_used"] == 5.0
    assert body["credit_limit"] == 5.0
    assert body["resets_at"].endswith("-01T00:00:00Z")
    assert "x402" in body["message"]
    assert "X-Payment" in body["message"]
    # The denied call is never charged: usage stays at the cap.
    assert _usage_row(tmp_path)[1] == 5.0


def test_month_rollover_resets_counter(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "2.5")
    monkeypatch.setattr(billing_mod, "_current_month_utc", lambda: "2026-08")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_cap3")

    xc = TestClient(create_app())
    headers = {"X-Subscriber-Key": token}
    # One search = 2.5 credits: exactly the cap.
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    r = xc.post("/search", json={"query": "q"}, headers=headers)
    assert r.status_code == 429, r.text
    assert r.json()["resets_at"] == "2026-09-01T00:00:00Z"

    # Next month (UTC): the counter resets and the key works again.
    monkeypatch.setattr(billing_mod, "_current_month_utc", lambda: "2026-09")
    r = xc.post("/search", json={"query": "q"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _usage_row(tmp_path) == ("2026-09", 2.5)


def test_x402_payers_not_capped(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "2.5")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_cap4")

    xc = TestClient(create_app())
    # Burn the subscriber's whole credit cap...
    assert (
        xc.post("/search", json={"query": "q"}, headers={"X-Subscriber-Key": token}).status_code
        == 200
    )
    # ...but a per-call x402 payer on the same endpoint is unaffected.
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header())
    assert r.status_code == 200, r.text


def test_mcp_subscriber_key_covered_by_cap(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "1")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_cap5")

    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "fetch_url", "arguments": {"url": "https://example.com/"}},
    }
    xc = TestClient(create_app())
    headers = {"X-Subscriber-Key": token}
    # NOTE: the FastMCP app needs the TestClient lifespan context; plain
    # TestClient(...) without `with` leaves the session manager unstarted.
    with xc:
        # fetch_url = $0.01 -> 1.0 credit: exactly the cap.
        r = xc.post("/mcp", json=body, headers=headers)
        assert r.status_code == 200, r.text
        r = xc.post("/mcp", json=body, headers=headers)
        assert r.status_code == 429, r.text
        assert r.json()["error"] == "subscriber_monthly_cap_exceeded"
        assert r.json()["credits_used"] == 1.0


def test_free_paths_cost_zero_and_are_never_gated(stubbed_backends, monkeypatch, tmp_path):
    """tools/list (free in the x402 price map) costs 0 credits and is never
    429-gated, even when the subscriber's credit cap is exhausted."""
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "2.5")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_free")

    xc = TestClient(create_app())
    headers = {"X-Subscriber-Key": token}
    # Burn the whole cap on one search (2.5 credits).
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 429

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with xc:
        r = xc.post("/mcp", json=body, headers=headers)
        assert r.status_code == 200, r.text
    # No credits charged for the free call.
    assert _usage_row(tmp_path)[1] == 2.5


def test_billing_usage_endpoint(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_usage")

    xc = TestClient(create_app())
    headers = {"X-Subscriber-Key": token}
    # Free even with x402 live: billing routes are exempt from 402s.
    r = xc.get("/billing/usage", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "credits_used": 0.0,
        "credit_limit": 1000.0,
        "resets_at": body["resets_at"],
        "period": billing_mod._current_month_utc(),
    }
    assert body["resets_at"].endswith("-01T00:00:00Z")

    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    r = xc.get("/billing/usage", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["credits_used"] == 2.5

    # Unknown key -> 401, not a 402 or 429.
    assert xc.get("/billing/usage", headers={"X-Subscriber-Key": "nope"}).status_code == 401


def test_billing_usage_never_429_gated(stubbed_backends, monkeypatch, tmp_path):
    """GET /billing/usage stays readable even after the cap is exhausted."""
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "2.5")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_usage2")

    xc = TestClient(create_app())
    headers = {"X-Subscriber-Key": token}
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 429

    r = xc.get("/billing/usage", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["credits_used"] == 2.5


def test_billing_usage_404_when_stripe_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("STRIPE_ENABLED", raising=False)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "subs.db"))
    xc = TestClient(create_app())
    r = xc.get("/billing/usage", headers={"X-Subscriber-Key": "whatever"})
    assert r.status_code == 404
