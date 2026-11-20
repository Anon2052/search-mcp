"""Tests for the REST API: /health, /search, /fetch, rate limiting, x402 hook."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from search_mcp import fetch as fetch_mod
from search_mcp import search as search_mod
from search_mcp.rest_app import create_app


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    """M-1: the replay table is persistent — point every test at a scratch
    DB so claims can never leak between tests (or touch the real path)."""
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


@pytest.fixture()
def stubbed_backends(monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "tvly-test")

    class FakeTavily:
        def __init__(self, api_key=None):
            pass

        def search(self, query, max_results=5, search_depth="basic"):
            return {
                "results": [
                    {"title": "T", "url": "https://example.com/1", "content": "s1"},
                ]
            }

    monkeypatch.setattr(search_mod, "TavilyClient", FakeTavily)
    monkeypatch.setattr(fetch_mod, "fetch_url", lambda url, timeout=20: "clean page text")


@pytest.fixture()
def client(stubbed_backends):
    return TestClient(create_app())


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["x402"] == "disabled"
    assert body["stripe"] == "disabled"


def test_search_endpoint(client):
    r = client.post("/search", json={"query": "hello", "count": 3})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0]["title"] == "T"
    assert results[0]["url"] == "https://example.com/1"


def test_search_validates_body(client):
    r = client.post("/search", json={"query": "", "count": 3})
    assert r.status_code == 422
    r = client.post("/search", json={"query": "q", "count": 99})
    assert r.status_code == 422


def test_fetch_endpoint(client):
    r = client.post("/fetch", json={"url": "https://example.com/"})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "clean page text"


def test_fetch_failure_returns_422(stubbed_backends, monkeypatch):
    from search_mcp.fetch import FetchError

    def boom(url, timeout=20):
        raise FetchError("URL: host 'x' resolves to a non-public IP (127.0.0.1)")

    monkeypatch.setattr(fetch_mod, "fetch_url", boom)
    c = TestClient(create_app())
    r = c.post("/fetch", json={"url": "http://127.0.0.1/"})
    assert r.status_code == 422
    assert r.json()["error"] == "fetch_failed"


def test_fetch_invalid_url_returns_503(stubbed_backends, monkeypatch):
    """httpx.InvalidURL is NOT an httpx.HTTPError subclass, so without an
    explicit mapping it escapes fetch_url() as an unhandled 500. Upstream
    fetch failures must be 503 instead."""
    import httpx

    def boom(url, timeout=20):
        raise httpx.InvalidURL("Invalid port: '99999'")

    monkeypatch.setattr(fetch_mod, "fetch_url", boom)
    c = TestClient(create_app())
    r = c.post("/fetch", json={"url": "https://example.com/"})
    assert r.status_code == 503
    assert r.json()["error"] == "fetch_failed"


def test_search_backend_failure_returns_502(stubbed_backends, monkeypatch):
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    c = TestClient(create_app())
    r = c.post("/search", json={"query": "q"})
    assert r.status_code == 502
    assert r.json()["error"] == "search_failed"


def test_rate_limiting(stubbed_backends):
    limited = TestClient(create_app(rate_limit="2/minute"))
    for _ in range(2):
        assert limited.get("/health").status_code == 200  # /health not rate-limited
    codes = set()
    for _ in range(4):
        r = limited.post("/search", json={"query": "q"})
        codes.add(r.status_code)
    assert 429 in codes, f"expected a 429, got {codes}"
    assert limited.get("/health").status_code == 200


# ------------------------------------------------------------- x402 hook


def _enable_x402(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", "0x000000000000000000000000000000000000dEaD")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://x402.test/facilitator")
    monkeypatch.setenv("X402_PRICE_USDC", "0.015")


def _x402_header(value_atomic=25000, **auth_over):
    """Build a base64 v1 X-Payment header (signature not verified locally).

    Each header gets a unique random signature so the anti-replay set can't
    leak between tests. (A per-module counter looks unique but restarts in
    every test module, so counter values collide across modules in one
    pytest run.)
    """
    import base64
    import secrets
    import time

    sig = auth_over.pop("signature", "0x" + secrets.token_hex(32))
    now = int(time.time())
    auth = {
        "from": "0xPayer000000000000000000000000000000000001",
        "to": "0x000000000000000000000000000000000000dEaD",
        "value": str(value_atomic),
        "validAfter": str(now - 60),
        # M-1: the authorization window must fit within MAX_TIMEOUT_SECONDS
        # (300s); wider windows are rejected by the sanity check.
        "validBefore": str(now + 240),
        "nonce": "0x" + secrets.token_hex(32),
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


def _fake_facilitator(monkeypatch, valid=True, unreachable=False):
    import search_mcp.x402 as x402_mod

    async def fake_verify(payment_payload, requirements):  # noqa: ANN001, ANN202
        if unreachable:
            raise x402_mod.FacilitatorUnavailable("down")
        return {
            "isValid": valid,
            "invalidReason": None if valid else "bad signature",
            "payer": "0xPayer000000000000000000000000000000000001",
        }

    async def fake_settle(payment_payload, requirements):  # noqa: ANN001, ANN202
        if unreachable:
            raise x402_mod.FacilitatorUnavailable("down")
        return {"success": True, "transaction": "0xTxHash1", "network": "base"}

    monkeypatch.setattr(x402_mod, "facilitator_verify", fake_verify)
    monkeypatch.setattr(x402_mod, "facilitator_settle", fake_settle)


def test_x402_disabled_by_default_passes_through(client):
    r = client.post("/search", json={"query": "q"})
    assert r.status_code == 200


def test_x402_enabled_blocks_without_payment(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    monkeypatch.setenv("FREE_TIER_ENABLED", "false")  # raw payment path
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"})
    assert r.status_code == 402
    body = r.json()
    assert body["error"] == "payment_required"
    assert body["accepts"]["asset"] == "USDC"
    assert body["accepts"]["network"] == "base"
    assert body["accepts"]["scheme"] == "exact"
    reqs = body["paymentRequirements"]
    assert reqs["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert reqs["network"] == "base"
    # search_web default 0.01 USDC -> 10000 atomic units.
    assert reqs["maxAmountRequired"] == "10000"


def test_x402_enabled_health_stays_free(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    xc = TestClient(create_app())
    r = xc.get("/health")
    assert r.status_code == 200
    assert r.json()["x402"] == "enabled"
    assert r.json()["stripe"] == "disabled"


def test_x402_enabled_rejects_malformed_payment(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers={"X-Payment": "not-base64!!!"})
    assert r.status_code == 402
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header(value_atomic=1))  # underpriced
    assert r.status_code == 402


def test_x402_enabled_verifies_and_serves(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=True)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header())
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["title"] == "T"


def test_x402_enabled_rejects_facilitator_denial(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _fake_facilitator(monkeypatch, valid=False)
    xc = TestClient(create_app())
    r = xc.post("/search", json={"query": "q"}, headers=_x402_header())
    assert r.status_code == 402
    assert r.json()["error"] == "payment_invalid"


# ------------------------------------------------------------- MCP mount paths


def _mcp_ping_body():
    return {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def _mcp_result(response):
    # Streamable HTTP answers as SSE: extract the JSON payload of the message event.
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            import json

            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no data event in MCP response: {response.text!r}")


def test_mcp_bare_path_does_not_redirect(stubbed_backends):
    # Bare /mcp (no trailing slash) must be served directly: many MCP clients
    # don't follow the 307 Starlette's mount would otherwise issue.
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post("/mcp", json=_mcp_ping_body())
    assert r.status_code != 307, f"bare /mcp 307-redirected to {r.headers.get('location')}"
    assert _mcp_result(r)["result"] == {}


def test_mcp_trailing_slash_path_still_works(stubbed_backends):
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post("/mcp/", json=_mcp_ping_body())
    assert _mcp_result(r)["result"] == {}
