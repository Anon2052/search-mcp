"""Free tier + usage analytics tests.

Free tier: anonymous monthly credit bucket keyed by hashed client IP, same
credit math as subscribers (single x402 price map), 402 (not 429) on
exhaustion, FREE_TIER_ENABLED kill switch, denied calls never charged.

Analytics: usage_daily rollups by (date, tool, plan_type), GET /billing/stats
behind ADMIN_KEY, days clamping, un-metered endpoint.
"""

from __future__ import annotations

import base64
import json
import sqlite3
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


@pytest.fixture()
def db_path(monkeypatch, tmp_path):
    """Point the billing DB at a per-test sqlite file."""
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))
    return tmp_path / "billing.db"


def _enable_x402(monkeypatch, **over):
    """x402 on with the free tier at its DEFAULT (enabled) — unlike the
    helpers in test_rest/test_payments, which pin it off."""
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
        "BILLING_DB_PATH": str(tmp_path / "billing.db"),
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


_sig_counter = 1000


def _x402_header(value_atomic=25000, **auth_over):
    global _sig_counter
    _sig_counter += 1
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
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": {"signature": sig, "authorization": auth},
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return {"X-Payment": raw}


def _fake_facilitator(monkeypatch, valid=True):
    async def fake_verify(payment_payload, requirements):  # noqa: ANN001, ANN202
        return {
            "isValid": valid,
            "invalidReason": None if valid else "insufficient funds",
            "payer": "0xPayer000000000000000000000000000000000001",
        }

    async def fake_settle(payment_payload, requirements):  # noqa: ANN001, ANN202
        return {"success": True, "transaction": "0xTxHash1", "network": "base"}

    monkeypatch.setattr(x402_mod, "facilitator_verify", fake_verify)
    monkeypatch.setattr(x402_mod, "facilitator_settle", fake_settle)


def _subscriber_token(customer_id):
    billing_mod.upsert_subscriber_placeholder(customer_id, f"{customer_id}@x.io")
    return billing_mod.rotate_subscriber_key(customer_id)


def _rows(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
    finally:
        conn.close()


# ------------------------------------------------------------- free tier


def test_free_tier_serves_anonymous_and_deducts_per_price(stubbed_backends, monkeypatch, db_path):
    _enable_x402(monkeypatch)
    xc = TestClient(create_app())
    headers = {"X-Forwarded-For": "203.0.113.7"}

    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    # search_web = $0.025 -> 2.5 credits deducted from the free bucket.
    rows = _rows(db_path, "free_usage")
    assert len(rows) == 1
    assert rows[0][2] == 2.5  # credits column
    # Raw IPs are never stored — only the hash.
    assert "203.0.113.7" not in str(rows)

    r = xc.post("/fetch", json={"url": "https://example.com/"}, headers=headers)
    assert r.status_code == 200
    # fetch_url = $0.01 -> 1.0 more credit: 3.5 total.
    assert _rows(db_path, "free_usage")[0][2] == 3.5


def test_free_tier_exhaustion_402_shape(stubbed_backends, monkeypatch, db_path):
    _enable_x402(monkeypatch, FREE_MONTHLY_CREDITS="2.5")
    xc = TestClient(create_app())
    headers = {"X-Forwarded-For": "203.0.113.8"}

    # One search = 2.5 credits: exactly the bucket.
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200

    r = xc.post("/search", json={"query": "q"}, headers=headers)
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["error"] == "free_tier_exhausted"  # NOT 429
    assert body["credits_used"] == 2.5
    assert body["free_limit"] == 2.5
    assert body["resets_at"].endswith("-01T00:00:00Z")
    assert "$9/mo" in body["upgrade_hint"]
    assert "x402" in body["message"] and "$9/mo" in body["message"]
    # The denied call is never charged: usage stays at the limit.
    assert _rows(db_path, "free_usage")[0][2] == 2.5


def test_free_tier_month_reset(stubbed_backends, monkeypatch, db_path):
    _enable_x402(monkeypatch, FREE_MONTHLY_CREDITS="2.5")
    monkeypatch.setattr(billing_mod, "_current_month_utc", lambda: "2026-08")
    xc = TestClient(create_app())
    headers = {"X-Forwarded-For": "203.0.113.9"}

    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    r = xc.post("/search", json={"query": "q"}, headers=headers)
    assert r.status_code == 402
    assert r.json()["resets_at"] == "2026-09-01T00:00:00Z"

    # Next month (UTC): the bucket resets and the IP works again.
    monkeypatch.setattr(billing_mod, "_current_month_utc", lambda: "2026-09")
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    rows = _rows(db_path, "free_usage")
    assert len(rows) == 1
    assert rows[0][1] == "2026-09"  # month column
    assert rows[0][2] == 2.5


def test_free_tier_ip_keying_independent_buckets(stubbed_backends, monkeypatch, db_path):
    _enable_x402(monkeypatch, FREE_MONTHLY_CREDITS="2.5")
    xc = TestClient(create_app())

    assert (
        xc.post("/search", json={"query": "q"}, headers={"X-Forwarded-For": "10.0.0.1"}).status_code
        == 200
    )
    # First IP exhausted...
    r = xc.post("/search", json={"query": "q"}, headers={"X-Forwarded-For": "10.0.0.1"})
    assert r.status_code == 402
    # ...second IP unaffected (XFF first entry wins).
    r = xc.post(
        "/search",
        json={"query": "q"},
        headers={"X-Forwarded-For": "10.0.0.2, 70.0.0.1"},
    )
    assert r.status_code == 200, r.text


def test_free_tier_disabled_falls_straight_to_402(stubbed_backends, monkeypatch, db_path):
    _enable_x402(monkeypatch, FREE_TIER_ENABLED="0")
    xc = TestClient(create_app())

    r = xc.post("/search", json={"query": "q"}, headers={"X-Forwarded-For": "203.0.113.10"})
    assert r.status_code == 402, r.text
    assert r.json()["error"] == "payment_required"  # plain x402 challenge
    # The free path was skipped entirely — the bucket table was never touched.
    try:
        rows = _rows(db_path, "free_usage")
    except sqlite3.OperationalError:
        rows = []
    assert rows == []


def test_free_tier_default_and_invalid_limit(monkeypatch):
    monkeypatch.delenv("FREE_MONTHLY_CREDITS", raising=False)
    assert billing_mod._free_monthly_limit() == 50.0
    monkeypatch.setenv("FREE_MONTHLY_CREDITS", "bogus")
    assert billing_mod._free_monthly_limit() == 50.0
    monkeypatch.setenv("FREE_MONTHLY_CREDITS", "-5")
    assert billing_mod._free_monthly_limit() == 50.0
    monkeypatch.setenv("FREE_MONTHLY_CREDITS", "200")
    assert billing_mod._free_monthly_limit() == 200.0


def test_subscriber_path_unaffected_by_free_tier(stubbed_backends, monkeypatch, tmp_path):
    """Valid subscriber keys still hit the subscriber cap (429), never the
    free bucket — the 429 code/body are untouched."""
    _enable_x402(monkeypatch)  # free tier default ON
    _enable_stripe(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "5")
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_ft1")

    xc = TestClient(create_app())
    headers = {"X-Subscriber-Key": token}
    # Two searches = 2 x 2.5 = 5.0 credits: exactly the subscriber cap.
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200

    r = xc.post("/search", json={"query": "q"}, headers=headers)
    assert r.status_code == 429, r.text
    assert r.json()["error"] == "subscriber_monthly_cap_exceeded"
    # The subscriber path never touches the free bucket.
    assert _rows(tmp_path / "billing.db", "free_usage") == []


def test_x402_paid_path_unaffected_by_free_tier(stubbed_backends, monkeypatch, db_path):
    """Per-call x402 payers are exempt from the free bucket and uncapped."""
    _enable_x402(monkeypatch, FREE_MONTHLY_CREDITS="0.5")  # tiny bucket
    _fake_facilitator(monkeypatch, valid=True)
    xc = TestClient(create_app())
    headers = dict(_x402_header())
    headers["X-Forwarded-For"] = "203.0.113.11"

    # A 2.5-credit search would blow the 0.5 free bucket — but the X-Payment
    # path never consults it.
    r = xc.post("/search", json={"query": "q"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _rows(db_path, "free_usage") == []
    # Recorded as an x402 call at list-price credits.
    rows = _rows(db_path, "usage_daily")
    assert len(rows) == 1
    assert rows[0][1:] == ("search_web", "x402", 1, 2.5)


def test_exhausted_free_bucket_subscriber_still_works(stubbed_backends, monkeypatch, tmp_path):
    """Buckets are independent: an exhausted anonymous IP doesn't affect a
    subscriber key on the same request path."""
    _enable_x402(monkeypatch, FREE_MONTHLY_CREDITS="2.5")
    _enable_stripe(monkeypatch, tmp_path)
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_ft2")

    xc = TestClient(create_app())
    ipf = {"X-Forwarded-For": "203.0.113.12"}
    assert xc.post("/search", json={"query": "q"}, headers=ipf).status_code == 200
    assert xc.post("/search", json={"query": "q"}, headers=ipf).status_code == 402

    sub_headers = {"X-Subscriber-Key": token, "X-Forwarded-For": "203.0.113.12"}
    r = xc.post("/search", json={"query": "q"}, headers=sub_headers)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------- analytics


def test_record_usage_rollup_across_plan_types(db_path):
    billing_mod.record_usage("search_web", "free", 2.5)
    billing_mod.record_usage("search_web", "free", 2.5)
    billing_mod.record_usage("search_web", "subscriber", 2.5)
    billing_mod.record_usage("search_web", "x402", 2.5)

    rows = {
        (r[1], r[2]): (r[3], r[4])
        for r in _rows(db_path, "usage_daily")  # (tool, plan) -> (calls, credits)
    }
    assert rows[("search_web", "free")] == (2, 5.0)
    assert rows[("search_web", "subscriber")] == (1, 2.5)
    assert rows[("search_web", "x402")] == (1, 2.5)


def test_record_usage_multi_tool_separation(db_path):
    billing_mod.record_usage("search_web", "free", 2.5)
    billing_mod.record_usage("fetch_url", "free", 1.0)
    billing_mod.record_usage("fetch_url", "x402", 1.0)

    tools = {r[1] for r in _rows(db_path, "usage_daily")}
    assert tools == {"search_web", "fetch_url"}


def test_record_usage_day_boundary_utc(monkeypatch, db_path):
    monkeypatch.setattr(billing_mod, "_today_utc", lambda: "2026-09-22")
    billing_mod.record_usage("search_web", "free", 2.5)
    monkeypatch.setattr(billing_mod, "_today_utc", lambda: "2026-09-23")
    billing_mod.record_usage("search_web", "free", 2.5)

    dates = sorted(r[0] for r in _rows(db_path, "usage_daily"))
    assert dates == ["2026-09-22", "2026-09-23"]


def test_record_usage_zero_credit(db_path):
    """Zero-credit outcomes (free methods on a paid path) are recorded as 0."""
    billing_mod.record_usage("search_web", "subscriber", 0.0)
    rows = _rows(db_path, "usage_daily")
    assert len(rows) == 1
    assert rows[0][3] == 1  # calls
    assert rows[0][4] == 0.0  # credits


def test_record_usage_rejects_bad_plan_type(db_path):
    with pytest.raises(ValueError):
        billing_mod.record_usage("search_web", "bogus", 1.0)


def _enable_admin(monkeypatch, key="test-admin-key"):
    monkeypatch.setenv("ADMIN_KEY", key)
    return key


def test_stats_endpoint_shape(monkeypatch, db_path):
    key = _enable_admin(monkeypatch)
    billing_mod.record_usage("search_web", "free", 2.5)
    billing_mod.record_usage("search_web", "free", 2.5)
    billing_mod.record_usage("fetch_url", "x402", 1.0)

    xc = TestClient(create_app())
    r = xc.get("/billing/stats", headers={"X-Admin-Key": key})
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["days"]) == 30  # default window
    today = next(d for d in body["days"] if d["date"] == billing_mod._today_utc())
    sw = today["tools"]["search_web"]
    assert sw["free"] == {"calls": 2, "credits": 5.0}
    assert sw["subscriber"] == {"calls": 0, "credits": 0.0}
    assert sw["x402"] == {"calls": 0, "credits": 0.0}
    assert today["tools"]["fetch_url"]["x402"] == {"calls": 1, "credits": 1.0}
    assert body["totals"]["calls"] == 3
    assert body["totals"]["credits"] == 6.0
    assert body["totals"]["tools"]["search_web"]["free"] == {"calls": 2, "credits": 5.0}


def test_stats_401_without_or_with_wrong_key(monkeypatch, db_path):
    _enable_admin(monkeypatch, "correct-key")
    xc = TestClient(create_app())

    r = xc.get("/billing/stats")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"

    r = xc.get("/billing/stats", headers={"X-Admin-Key": "wrong-key"})
    assert r.status_code == 401

    # Query-param fallback works too.
    r = xc.get("/billing/stats", params={"admin_key": "correct-key"})
    assert r.status_code == 200, r.text


def test_stats_401_when_admin_key_unset(monkeypatch, db_path):
    """Fail closed: no ADMIN_KEY configured means nothing is authorized."""
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    xc = TestClient(create_app())
    assert xc.get("/billing/stats", headers={"X-Admin-Key": "anything"}).status_code == 401
    assert xc.get("/billing/stats", params={"admin_key": "anything"}).status_code == 401


def test_stats_days_clamping(monkeypatch, db_path):
    key = _enable_admin(monkeypatch)
    xc = TestClient(create_app())

    def day_count(days_param):
        r = xc.get("/billing/stats", headers={"X-Admin-Key": key}, params={"days": days_param})
        assert r.status_code == 200, r.text
        return len(r.json()["days"])

    assert day_count("0") == 1
    assert day_count("99999") == 365
    assert day_count("bogus") == 30
    assert day_count("7") == 7


def test_stats_endpoint_is_unmetered(stubbed_backends, monkeypatch, db_path):
    """GET /billing/stats never consumes quota and never records itself."""
    _enable_x402(monkeypatch, FREE_MONTHLY_CREDITS="2.5")
    key = _enable_admin(monkeypatch)
    xc = TestClient(create_app())
    headers = {"X-Admin-Key": key, "X-Forwarded-For": "203.0.113.13"}

    r = xc.get("/billing/stats", headers=headers)
    assert r.status_code == 200, r.text
    # No usage rows, no free-bucket row for the caller.
    assert _rows(db_path, "usage_daily") == []
    assert _rows(db_path, "free_usage") == []


def test_anonymous_free_calls_recorded_as_free(stubbed_backends, monkeypatch, db_path):
    """Anonymous calls served from the free bucket roll up as plan_type=free
    with the credits actually deducted."""
    _enable_x402(monkeypatch)
    xc = TestClient(create_app())
    headers = {"X-Forwarded-For": "203.0.113.14"}

    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    rows = _rows(db_path, "usage_daily")
    assert len(rows) == 1
    assert rows[0][1:] == ("search_web", "free", 1, 2.5)


def test_metering_disabled_records_free_zero_credit(stubbed_backends, monkeypatch, db_path):
    """With x402 off entirely, served calls still roll up as free/0 so
    pre-launch traffic stays visible."""
    xc = TestClient(create_app())  # x402 not enabled
    assert xc.post("/search", json={"query": "q"}).status_code == 200
    rows = _rows(db_path, "usage_daily")
    assert len(rows) == 1
    assert rows[0][1:] == ("search_web", "free", 1, 0.0)


def test_subscriber_calls_recorded_as_subscriber(stubbed_backends, monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    _fake_facilitator(monkeypatch, valid=True)
    token = _subscriber_token("cus_ft3")

    xc = TestClient(create_app())
    headers = {"X-Subscriber-Key": token}
    assert xc.post("/search", json={"query": "q"}, headers=headers).status_code == 200
    rows = _rows(tmp_path / "billing.db", "usage_daily")
    assert len(rows) == 1
    assert rows[0][1:] == ("search_web", "subscriber", 1, 2.5)
