"""Regression tests for the pre-launch security fixes (H-1..H-8, M-1..M-5).

Each test pins one audit finding so a future refactor can't silently
re-open it. H-1/H-2/H-3 live in tests/test_fetch.py; the middleware-level
fixes are covered here.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

import search_mcp.billing as billing_mod
import search_mcp.x402 as x402_mod
from search_mcp import rest_app as rest_app_mod
from search_mcp.rest_app import create_app

VALID_PAY_TO = "0x000000000000000000000000000000000000dEaD"


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


def _enable_x402(monkeypatch, **over):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", over.get("pay_to", VALID_PAY_TO))
    monkeypatch.setenv(
        "X402_FACILITATOR_URL", over.get("facilitator", "https://x402.test/facilitator")
    )
    monkeypatch.setenv("FREE_TIER_ENABLED", "0")
    for k, v in over.get("env", {}).items():
        monkeypatch.setenv(k, v)


def _x402_header(*, valid_after=None, valid_before=None):
    import secrets

    now = int(time.time())
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": {
            "signature": "0x" + secrets.token_hex(32),
            "authorization": {
                "from": "0xPayer000000000000000000000000000000000001",
                "to": VALID_PAY_TO,
                "value": "10000",
                "validAfter": str(now - 60 if valid_after is None else valid_after),
                "validBefore": str(now + 240 if valid_before is None else valid_before),
                "nonce": "0x" + secrets.token_hex(32),
            },
        },
    }
    raw = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"X-Payment": raw}


def _fake_facilitator(monkeypatch, valid=True):
    async def verify(payment_payload, requirements):
        return {"isValid": valid, "invalidReason": None, "payer": "0xPayer"}

    async def settle(payment_payload, requirements):
        return {"success": True, "transaction": "0xTx", "network": "base"}

    monkeypatch.setattr(x402_mod, "facilitator_verify", verify)
    monkeypatch.setattr(x402_mod, "facilitator_settle", settle)


# ------------------------------------------------------------------ M-1


def test_m1_oversize_validity_window_rejected(monkeypatch):
    """M-1: an authorization window wider than MAX_TIMEOUT_SECONDS (300s)
    is rejected at the sanity check, before any facilitator call."""
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True)
    xc = TestClient(create_app())
    now = int(time.time())
    # 360s window: the old test fixture shape, now non-compliant.
    r = xc.post(
        "/search",
        json={"query": "q"},
        headers=_x402_header(valid_after=now - 60, valid_before=now + 300),
    )
    assert r.status_code == 402
    assert r.json()["reason"] == "authorization validity window exceeds maximum"


def test_m1_replay_claim_is_atomic_and_persistent(monkeypatch, tmp_path):
    """M-1: the replay claim is an atomic DB INSERT — the second claim of
    the same signature fails even across app instances (process restarts)."""
    db = str(tmp_path / "replay.db")
    monkeypatch.setenv("BILLING_DB_PATH", db)
    sig = "0x" + "ab" * 32
    # Returns True when the signature was already claimed (a replay),
    # False when this call claimed it.
    assert billing_mod.claim_x402_signature(sig, time.time() + 300) is False
    assert billing_mod.claim_x402_signature(sig, time.time() + 300) is True
    # A "new process" (fresh import state) still sees the claim: it is on disk.
    import sqlite3

    rows = sqlite3.connect(db).execute("SELECT COUNT(*) FROM x402_seen_signatures").fetchone()
    assert rows[0] == 1


# ------------------------------------------------------------------ M-4


def test_m4_placeholder_hashes_are_unique_per_customer():
    """M-4: two customers checking out before minting keys must not collide
    on the key_hash primary key (the old constant placeholder did)."""
    h1 = billing_mod._placeholder_hash("cus_aaa")
    h2 = billing_mod._placeholder_hash("cus_bbb")
    assert h1 != h2
    assert h1.startswith("pending:") and len(h1) == len("pending:") + 64


# ------------------------------------------------------------------ M-5


def test_m5_bad_pay_to_fails_closed(monkeypatch):
    """M-5: a malformed X402_PAY_TO disables x402 instead of letting paid
    endpoints run unprotected (or crash on a bad address)."""
    _enable_x402(monkeypatch, pay_to="0xAbC123")
    assert x402_mod.x402_config_problems() != []
    assert x402_mod.x402_effective_enabled() is False


def test_m5_non_https_facilitator_fails_closed(monkeypatch):
    """M-5: a non-HTTPS facilitator URL disables x402 (payment auth must
    never travel over plaintext)."""
    _enable_x402(monkeypatch, facilitator="http://x402.test/facilitator")
    assert x402_mod.x402_config_problems() != []
    assert x402_mod.x402_effective_enabled() is False


def test_m5_valid_config_stays_enabled(monkeypatch):
    _enable_x402(monkeypatch)
    assert x402_mod.x402_config_problems() == []
    assert x402_mod.x402_effective_enabled() is True


# ------------------------------------------------------------------ H-5


def test_h5_mcp_mount_is_rate_limited(monkeypatch):
    """H-5: the /mcp mount (invisible to SlowAPI's default_limits) is
    throttled by the pure-ASGI wrapper: over the per-minute budget -> 429."""
    monkeypatch.setenv("SEARCH_API_KEY", "tvly-test")
    xc = TestClient(create_app(rate_limit="3/minute"))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with xc:
        statuses = [xc.post("/mcp", json=body).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    assert statuses[4] == 429


# ------------------------------------------------------------------ M-3


def _scope(*, peer, xff=None, cf_ip=None):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    if cf_ip is not None:
        headers.append((b"cf-connecting-ip", cf_ip.encode("latin-1")))
    return {"headers": headers, "client": (peer, 12345)}


def test_m3_spoofed_xff_ignored_on_direct_connection():
    """M-3: a direct client (global peer IP) cannot rotate rate-limit /
    billing buckets by spoofing X-Forwarded-For."""
    key = rest_app_mod._client_ip_from_scope(_scope(peer="8.8.8.8", xff="10.9.9.9"))
    assert key == "8.8.8.8"


def test_m3_xff_honored_behind_proxy():
    """M-3: behind Render's proxy (non-global peer) the first XFF entry is
    the real client IP."""
    key = rest_app_mod._client_ip_from_scope(_scope(peer="10.0.0.5", xff="8.8.8.8, 10.0.0.5"))
    assert key == "8.8.8.8"


def test_m3_garbage_xff_falls_back_to_peer():
    key = rest_app_mod._client_ip_from_scope(_scope(peer="10.0.0.5", xff="not-an-ip"))
    assert key == "10.0.0.5"


# ------------------------------------------------- Cloudflare proxy flip


def test_cf_connecting_ip_honored_from_cloudflare_peer():
    """CF-Connecting-IP is trusted when the TCP peer is a Cloudflare edge
    IP (proxied DNS). 172.64.1.10 is inside 172.64.0.0/13."""
    key = rest_app_mod._client_ip_from_scope(_scope(peer="172.64.1.10", cf_ip="8.8.8.8"))
    assert key == "8.8.8.8"


def test_cf_connecting_ip_honored_from_cloudflare_ipv6_peer():
    """Same fast path for a Cloudflare IPv6 edge peer (2606:4700::/32)."""
    key = rest_app_mod._client_ip_from_scope(_scope(peer="2606:4700:0:0:0:0:0:1", cf_ip="1.1.1.1"))
    assert key == "1.1.1.1"


def test_cf_connecting_ip_ignored_from_non_cloudflare_peer():
    """A spoofed CF-Connecting-IP from a direct (non-Cloudflare) peer is
    ignored — falls back to existing direct-connection logic (XFF ignored)."""
    key = rest_app_mod._client_ip_from_scope(_scope(peer="8.8.8.8", cf_ip="1.2.3.4"))
    assert key == "8.8.8.8"


def test_cf_connecting_ip_garbage_falls_back_to_peer():
    """Garbage CF-Connecting-IP from a Cloudflare peer falls back to the
    peer instead of poisoning the rate-limit bucket."""
    key = rest_app_mod._client_ip_from_scope(_scope(peer="172.64.1.10", cf_ip="not-an-ip"))
    assert key == "172.64.1.10"


def test_cf_peer_without_cf_header_ignores_xff():
    """Cloudflare peer with no CF-Connecting-IP: XFF is NOT trusted (first
    XFF entry is client-spoofable through Cloudflare) — falls back to the
    peer, same as any global peer."""
    key = rest_app_mod._client_ip_from_scope(_scope(peer="172.64.1.10", xff="8.8.8.8"))
    assert key == "172.64.1.10"
