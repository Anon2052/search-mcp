"""Tier 1 deferral tests (Card 56): XRP / XLM / HBAR / ALGO must NOT
create a settlement leg.

Verified research (2026-10-27): the Coinbase CDP facilitator supports
mainnets Base, Polygon, Arbitrum, Monad, World, and Solana only, and the
x402.org facilitator is testnet-only for Algorand / Stellar / Hedera /
XRPL. No Tier 1 leg may therefore be built or advertised in this session.

These tests prove the existing code already fails closed for Tier 1
names, using synthetic test values only:

* ``X402_NETWORKS`` naming xrp/xlm/hbar/algo yields accepts for base and
  solana only — no Tier 1 entry is servable.
* The discovery manifest lists no Tier 1 network.
* A payment payload asserting a Tier 1 network is rejected via the
  existing unsupported-network path, before any facilitator call.
* Hypothetical Tier 1 pay-to env vars create no leg.
* Default (Base-only) behaviour is unchanged.

No production code is changed by this file, and no Tier 1 network
string, asset identifier, decimal count, pay-to address, or facilitator
URL is invented here: Tier 1 names appear only as *rejected inputs*.
All pay-to values below are synthetic test-only strings; the Base and
Solana values mirror the synthetic values in test_x402_solana.py.
"""

from __future__ import annotations

import base64
import json
import logging

import pytest
from fastapi.testclient import TestClient

import search_mcp.x402 as x402_mod
from search_mcp import fetch as fetch_mod
from search_mcp import search as search_mod
from search_mcp.rest_app import create_app

# Synthetic test-only values (same convention as test_x402_solana.py).
BASE_PAY_TO = "0x000000000000000000000000000000000000dEaD"
SOL_PAY_TO = "So1anaPayToTest1111111111111111111111111111"
FEE_PAYER = "FeePayerTestSo1ana111111111111111111111111"

TIER1_NETWORKS = ("xrp", "xlm", "hbar", "algo")
TIER1_NETWORKS_ENV = "base,solana,xrp,xlm,hbar,algo"


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    """M-1: the replay table is persistent — point every test at a scratch DB."""
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


@pytest.fixture(autouse=True)
def _no_fee_payer_cache_leak(monkeypatch):
    """The discovered-value cache never leaks between tests."""
    monkeypatch.setattr(x402_mod, "_solana_fee_payer_cache", None)


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


def _enable_tier1_env(monkeypatch, **over):
    env = {
        "X402_ENABLED": "true",
        "X402_PAY_TO": BASE_PAY_TO,
        "X402_FACILITATOR_URL": "https://x402.test/facilitator",
        "X402_PRICE_USDC": "0.015",
        "X402_NETWORKS": TIER1_NETWORKS_ENV,
        "X402_PAY_TO_SOLANA": SOL_PAY_TO,
        "X402_SOLANA_FEE_PAYER": FEE_PAYER,
        "FREE_TIER_ENABLED": "false",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _enable_base_only(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", BASE_PAY_TO)
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://x402.test/facilitator")
    monkeypatch.setenv("X402_PRICE_USDC", "0.015")
    monkeypatch.setenv("FREE_TIER_ENABLED", "false")
    monkeypatch.delenv("X402_NETWORKS", raising=False)
    monkeypatch.delenv("X402_PAY_TO_SOLANA", raising=False)
    monkeypatch.delenv("X402_SOLANA_FEE_PAYER", raising=False)


def _header(payload_dict):
    raw = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    return {"X-Payment": raw}


def _fake_facilitator(monkeypatch):
    """A facilitator that records calls and would accept if ever called.

    Tier 1 tests assert it is NEVER called: rejection happens locally,
    before verification/settlement.
    """
    calls = {"verify": [], "settle": []}

    async def fake_verify(payment_payload, requirements):
        calls["verify"].append((payment_payload, requirements))
        return {"isValid": True, "invalidReason": None, "payer": "payer"}

    async def fake_settle(payment_payload, requirements):
        calls["settle"].append((payment_payload, requirements))
        return {"success": True, "transaction": "TestSettleSig111", "network": "base"}

    monkeypatch.setattr(x402_mod, "facilitator_verify", fake_verify)
    monkeypatch.setattr(x402_mod, "facilitator_settle", fake_settle)
    return calls


# ------------------------------------------------- Tier 1 names are unknown legs


def test_tier1_names_are_unknown_legs(monkeypatch):
    for name in TIER1_NETWORKS:
        problems = x402_mod.leg_config_problems(name)
        assert problems == [f"unknown x402 network {name!r} in X402_NETWORKS"]


def test_tier1_names_dropped_with_error_log(monkeypatch, caplog):
    _enable_tier1_env(monkeypatch)
    with caplog.at_level(logging.ERROR):
        valid = x402_mod.valid_networks()
    assert valid == ["base", "solana"]
    for name in TIER1_NETWORKS:
        assert any(
            name in record.message and "unknown x402 network" in record.message
            for record in caplog.records
            if record.levelno >= logging.ERROR
        )


def test_tier1_names_produce_no_accepts_entry(stubbed_backends, monkeypatch):
    _enable_tier1_env(monkeypatch)
    # network_configs() is the single table the challenge is built from.
    assert set(x402_mod.network_configs()) == {"base", "solana"}
    xc = TestClient(create_app())
    body = xc.post("/search", json={"query": "q"}).json()

    accepts = body["accepts"]
    assert isinstance(accepts, list) and len(accepts) == 2
    assert {entry["network"] for entry in accepts} == {"base", "solana"}
    requirements = body["paymentRequirements"]
    assert isinstance(requirements, list) and len(requirements) == 2
    assert {entry["network"] for entry in requirements} == {"base", "solana"}
    for name in TIER1_NETWORKS:
        assert name not in {entry["network"] for entry in accepts}
        assert name not in {entry["network"] for entry in requirements}


def test_discovery_manifest_excludes_tier1(monkeypatch):
    _enable_tier1_env(monkeypatch)
    xc = TestClient(create_app())
    manifest = xc.get("/.well-known/x402.json").json()
    networks = [entry["network"] for entry in manifest["networks"]]
    assert networks == ["base", "solana"]
    for name in TIER1_NETWORKS:
        assert name not in networks


# ------------------------------------------------- Tier 1 payloads rejected, never settled


@pytest.mark.parametrize("network", TIER1_NETWORKS)
def test_tier1_payload_rejected_unsupported_network(stubbed_backends, monkeypatch, network):
    _enable_tier1_env(monkeypatch)
    calls = _fake_facilitator(monkeypatch)
    xc = TestClient(create_app())
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": network,
        "payload": {"transaction": base64.b64encode(b"\x01\x02\x03test").decode()},
    }
    resp = xc.post("/search", json={"query": "q"}, headers=_header(payload))
    assert resp.status_code == 402
    body = resp.json()
    assert body["error"] == "payment_invalid"
    assert body["reason"] == "unsupported network"
    # Rejected locally: the facilitator was never asked to verify or settle.
    assert calls["verify"] == []
    assert calls["settle"] == []


# ------------------------------------------------- Hypothetical Tier 1 pay-to env creates no leg


def test_hypothetical_tier1_payto_env_creates_no_leg(stubbed_backends, monkeypatch):
    _enable_tier1_env(monkeypatch)
    # Deliberately non-address synthetic placeholders: even if some future
    # code looked for these names, these values could not be a pay-to.
    for name in TIER1_NETWORKS:
        monkeypatch.setenv(
            f"X402_PAY_TO_{name.upper()}", f"SYNTHETIC-TEST-ONLY-{name.upper()}-PAYTO"
        )
    assert set(x402_mod.network_configs()) == {"base", "solana"}
    xc = TestClient(create_app())
    body = xc.post("/search", json={"query": "q"}).json()
    assert {entry["network"] for entry in body["accepts"]} == {"base", "solana"}
    manifest = xc.get("/.well-known/x402.json").json()
    assert [entry["network"] for entry in manifest["networks"]] == ["base", "solana"]


def test_tier1_payto_env_without_tier1_networks_creates_no_leg(monkeypatch):
    _enable_base_only(monkeypatch)
    for name in TIER1_NETWORKS:
        monkeypatch.setenv(
            f"X402_PAY_TO_{name.upper()}", f"SYNTHETIC-TEST-ONLY-{name.upper()}-PAYTO"
        )
    assert set(x402_mod.network_configs()) == {"base"}


# ------------------------------------------------- Default Base-only behaviour unchanged


def test_default_base_only_unchanged(stubbed_backends, monkeypatch):
    _enable_base_only(monkeypatch)
    xc = TestClient(create_app())
    body = xc.post("/search", json={"query": "q"}).json()
    # Single-network challenge shape, exactly as before multi-network.
    assert isinstance(body["accepts"], dict)
    assert body["accepts"]["network"] == "base"
    assert body["accepts"]["pay_to"] == BASE_PAY_TO
    assert isinstance(body["paymentRequirements"], dict)
    assert body["paymentRequirements"]["network"] == "base"
    assert body["paymentRequirements"]["asset"] == x402_mod.USDC_BASE_ADDRESS
    manifest = xc.get("/.well-known/x402.json").json()
    assert [entry["network"] for entry in manifest["networks"]] == ["base"]
    assert manifest["network"] == "base"
    assert manifest["pay_to"] == BASE_PAY_TO
