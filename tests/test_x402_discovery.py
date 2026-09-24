"""Tests for the x402 discovery manifest: GET /.well-known/x402.json.

The endpoint is public metadata: 200 + valid JSON with no auth, no
subscriber key, and never a 402 — even when x402 is enabled. Prices must
match the single source of truth (the x402 module price map) exactly, and
the manifest must NEVER claim live/accepting payments while disabled.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from search_mcp import x402 as x402_mod
from search_mcp.rest_app import create_app


@pytest.fixture()
def client(monkeypatch):
    # Hermetic env: no payment config set, so x402 stays disabled.
    for var in (
        "X402_ENABLED",
        "X402_PAY_TO",
        "X402_FACILITATOR_URL",
        "X402_PRICE_SEARCH_WEB_USDC",
        "X402_PRICE_FETCH_URL_USDC",
        "X402_PRICE_USDC",
    ):
        monkeypatch.delenv(var, raising=False)
    return TestClient(create_app())


def test_discovery_returns_200_json(client):
    r = client.get("/.well-known/x402.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert isinstance(body, dict)


def test_discovery_needs_no_auth_key_or_payment(client):
    # No x-subscriber-key, no X-Payment — and nothing to send anyway.
    r = client.get("/.well-known/x402.json")
    assert r.status_code == 200
    assert r.json()["x402_status"] == x402_mod.DISCOVERY_STATUS_DISABLED


def test_discovery_prices_match_price_map_exactly(client):
    manifest = client.get("/.well-known/x402.json").json()
    expected = {tool: str(price) for tool, price in x402_mod.PER_TOOL_DEFAULTS_USDC.items()}
    assert manifest["prices_usdc"] == expected
    assert manifest["default_price_usdc"] == str(x402_mod.DEFAULT_PRICE_USDC)


def test_discovery_does_not_claim_live_payments(client):
    manifest = client.get("/.well-known/x402.json").json()
    assert manifest["x402_status"] == "implemented_but_disabled"
    assert manifest["x402_status"] != x402_mod.DISCOVERY_STATUS_ENABLED
    blob = str(manifest).lower()
    assert "accepting payments" not in blob
    assert "payments are live" not in blob
    assert "disabled" in str(manifest["note"]).lower()
    assert "no payments are accepted" in str(manifest["note"]).lower()


def test_discovery_asset_and_network_from_module(client):
    manifest = client.get("/.well-known/x402.json").json()
    assert manifest["scheme"] == x402_mod.SCHEME
    assert manifest["version"] == x402_mod.X402_VERSION
    assert manifest["network"] == x402_mod.NETWORK
    assert manifest["asset"] == "USDC"
    assert manifest["asset_contract"] == x402_mod.USDC_BASE_ADDRESS
    assert manifest["asset_decimals"] == x402_mod.USDC_DECIMALS


def test_discovery_pay_to_null_when_unset(client):
    manifest = client.get("/.well-known/x402.json").json()
    assert manifest["pay_to"] is None


def test_discovery_pay_to_reflects_env(monkeypatch):
    monkeypatch.delenv("X402_ENABLED", raising=False)
    monkeypatch.delenv("X402_FACILITATOR_URL", raising=False)
    monkeypatch.setenv("X402_PAY_TO", "0x000000000000000000000000000000000000dEaD")
    client = TestClient(create_app())
    manifest = client.get("/.well-known/x402.json").json()
    assert manifest["pay_to"] == "0x000000000000000000000000000000000000dEaD"
    # Still honest about disabled state: enabling needs all three vars.
    assert manifest["x402_status"] == "implemented_but_disabled"


def test_discovery_free_endpoints_listed(client):
    manifest = client.get("/.well-known/x402.json").json()
    assert "/health" in manifest["free_endpoints"]
    assert "/.well-known/x402.json" in manifest["free_endpoints"]
    assert "/legal/terms" in manifest["free_endpoints"]


def test_discovery_never_402_when_x402_enabled(monkeypatch):
    # Even with full x402 config enabled, discovery must not 402-challenge.
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", "0x000000000000000000000000000000000000dEaD")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://x402.org/facilitator")
    client = TestClient(create_app())
    r = client.get("/.well-known/x402.json")
    assert r.status_code == 200
    manifest = r.json()
    assert manifest["x402_status"] == x402_mod.DISCOVERY_STATUS_ENABLED
    assert manifest["pay_to"] == "0x000000000000000000000000000000000000dEaD"
