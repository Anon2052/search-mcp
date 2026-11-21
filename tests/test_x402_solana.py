"""Solana x402 leg tests (SOLANA_PORT_PLAN §4c).

The Solana leg is opt-in via X402_NETWORKS; with it unset, behavior is the
Base-only behavior covered by test_payments.py. Facilitator calls are
monkeypatched (same pattern as test_payments.py); fee-payer discovery is
stubbed except in the tests that exercise it against a fake httpx client.

All Solana addresses here are SYNTHETIC base58 strings for tests only —
the real pay-to address is env-config (X402_PAY_TO_SOLANA), never code.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets

import pytest
from fastapi.testclient import TestClient

import search_mcp.x402 as x402_mod
from search_mcp import fetch as fetch_mod
from search_mcp import search as search_mod
from search_mcp.rest_app import create_app

# Synthetic base58 (alphabet-checked: no 0/O/I/l), 41-42 chars.
SOL_PAY_TO = "So1anaPayToTest1111111111111111111111111111"
FEE_PAYER = "FeePayerTestSo1ana111111111111111111111111"
BASE_PAY_TO = "0x000000000000000000000000000000000000dEaD"

_REAL_RESOLVE = x402_mod.resolve_solana_fee_payer


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    """M-1: the replay table is persistent — point every test at a scratch DB."""
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


@pytest.fixture(autouse=True)
def _no_network_discovery(monkeypatch):
    """Keep the suite hermetic: fee-payer discovery is stubbed to 'no
    resolution' unless a test re-enables the real resolver, and the
    discovered-value cache never leaks between tests."""

    async def _stub_resolve():
        return None

    monkeypatch.setattr(x402_mod, "resolve_solana_fee_payer", _stub_resolve)
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


def _enable_multi(monkeypatch, **over):
    env = {
        "X402_ENABLED": "true",
        "X402_PAY_TO": BASE_PAY_TO,
        "X402_FACILITATOR_URL": "https://x402.test/facilitator",
        "X402_PRICE_USDC": "0.015",
        "X402_NETWORKS": "base,solana",
        "X402_PAY_TO_SOLANA": SOL_PAY_TO,
        "X402_SOLANA_FEE_PAYER": FEE_PAYER,
        "FREE_TIER_ENABLED": "false",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _solana_payload(tx: bytes | None = None, **over):
    if tx is None:
        tx = b"\x01\x02\x03" + secrets.token_bytes(16)
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "solana",
        "payload": {"transaction": base64.b64encode(tx).decode()},
    }
    payload.update(over)
    return payload


def _header(payload_dict):
    raw = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    return {"X-Payment": raw}


def _fake_solana_facilitator(monkeypatch, valid=True, unreachable=False):
    calls = {"verify": [], "settle": []}

    async def fake_verify(payment_payload, requirements):
        calls["verify"].append((payment_payload, requirements))
        if unreachable:
            raise x402_mod.FacilitatorUnavailable("down")
        return {"isValid": valid, "invalidReason": None if valid else "bad tx", "payer": "payer"}

    async def fake_settle(payment_payload, requirements):
        calls["settle"].append((payment_payload, requirements))
        if unreachable:
            raise x402_mod.FacilitatorUnavailable("down")
        return {"success": True, "transaction": "Base58SettleSig111", "network": "solana"}

    monkeypatch.setattr(x402_mod, "facilitator_verify", fake_verify)
    monkeypatch.setattr(x402_mod, "facilitator_settle", fake_settle)
    return calls


# ------------------------------------------------- §4c.1 multi-network 402


def test_402_multi_network_accepts(stubbed_backends, monkeypatch):
    _enable_multi(monkeypatch)
    xc = TestClient(create_app())
    body = xc.post("/search", json={"query": "q"}).json()

    accepts = body["accepts"]
    assert isinstance(accepts, list) and len(accepts) == 2
    requirements = body["paymentRequirements"]
    assert isinstance(requirements, list) and len(requirements) == 2

    by_network = {entry["network"]: entry for entry in requirements}
    solana = by_network["solana"]
    assert solana["scheme"] == "exact"
    assert solana["asset"] == x402_mod.USDC_SOLANA_MINT
    assert solana["payTo"] == SOL_PAY_TO
    assert solana["extra"]["feePayer"] == FEE_PAYER
    # /search is priced at 0.01 USDC; 6 decimals on Solana too.
    assert solana["maxAmountRequired"] == "10000"
    assert by_network["base"]["asset"] == x402_mod.USDC_BASE_ADDRESS

    accepts_by_network = {entry["network"]: entry for entry in accepts}
    sol_accepts = accepts_by_network["solana"]
    assert sol_accepts["asset_contract"] == x402_mod.USDC_SOLANA_MINT
    assert sol_accepts["pay_to"] == SOL_PAY_TO
    assert sol_accepts["amount_atomic"] == "10000"
    assert sol_accepts["amount_usdc"] == "0.01"


def test_requirements_list_builder(monkeypatch):
    _enable_multi(monkeypatch)
    from decimal import Decimal

    entries = x402_mod.build_requirements_list(Decimal("0.01"), "https://x.test/search", "desc")
    assert [entry["network"] for entry in entries] == ["base", "solana"]
    # The Base entry must stay byte-identical to the single-network builder.
    assert entries[0] == x402_mod.build_payment_requirements(
        Decimal("0.01"), "https://x.test/search", "desc"
    )


def test_solana_only_challenge_collapses_to_single_shape(stubbed_backends, monkeypatch):
    _enable_multi(monkeypatch, X402_NETWORKS="solana")
    monkeypatch.delenv("X402_PAY_TO", raising=False)
    xc = TestClient(create_app())
    body = xc.post("/search", json={"query": "q"}).json()
    assert isinstance(body["accepts"], dict)
    assert body["accepts"]["network"] == "solana"
    assert isinstance(body["paymentRequirements"], dict)
    assert body["paymentRequirements"]["extra"]["feePayer"] == FEE_PAYER


# ------------------------------------------------- §4c.2 payload sanity


def _solana_requirements():
    return {"network": "solana", "payTo": SOL_PAY_TO, "maxAmountRequired": "10000"}


def test_solana_sanity_checks():
    requirements = _solana_requirements()
    assert x402_mod.sanity_check_solana_payload(_solana_payload(), requirements) is None

    missing_tx = _solana_payload()
    missing_tx["payload"] = {}
    assert x402_mod.sanity_check_solana_payload(missing_tx, requirements) == "missing transaction"

    # An EVM-shaped payload (signature + authorization) is not a Solana payment.
    evm_shaped = _solana_payload()
    evm_shaped["payload"] = {"signature": "0xabc", "authorization": {"value": "10000"}}
    assert x402_mod.sanity_check_solana_payload(evm_shaped, requirements) == "missing transaction"

    bad_b64 = _solana_payload()
    bad_b64["payload"] = {"transaction": "!!! not base64 !!!"}
    assert (
        x402_mod.sanity_check_solana_payload(bad_b64, requirements)
        == "invalid transaction encoding"
    )

    empty_tx = _solana_payload()
    empty_tx["payload"] = {"transaction": "   "}
    assert x402_mod.sanity_check_solana_payload(empty_tx, requirements) == "missing transaction"

    wrong_network = _solana_payload(network="base")
    assert (
        x402_mod.sanity_check_solana_payload(wrong_network, requirements) == "unsupported network"
    )


def test_unknown_network_rejected(stubbed_backends, monkeypatch):
    _enable_multi(monkeypatch)
    _fake_solana_facilitator(monkeypatch)
    xc = TestClient(create_app())
    payload = _solana_payload(network="bitcoin")
    resp = xc.post("/search", json={"query": "q"}, headers=_header(payload))
    assert resp.status_code == 402
    body = resp.json()
    assert body["error"] == "payment_invalid"
    assert body["reason"] == "unsupported network"


# ------------------------------------------------- §4c.3 replay


def test_solana_replay(stubbed_backends, monkeypatch):
    _enable_multi(monkeypatch)
    _fake_solana_facilitator(monkeypatch)
    xc = TestClient(create_app())
    headers = _header(_solana_payload(tx=b"\x07\x08\x09" + secrets.token_bytes(8)))

    first = xc.post("/search", json={"query": "q"}, headers=headers)
    assert first.status_code == 200
    second = xc.post("/search", json={"query": "q"}, headers=headers)
    assert second.status_code == 402
    assert second.json()["error"] == "duplicate_payment"


# ------------------------------------------------- §4c.4 facilitator flow


def test_solana_facilitator_flow(stubbed_backends, monkeypatch):
    _enable_multi(monkeypatch)
    calls = _fake_solana_facilitator(monkeypatch)
    xc = TestClient(create_app())
    payload = _solana_payload()
    resp = xc.post("/search", json={"query": "q"}, headers=_header(payload))
    assert resp.status_code == 200

    assert len(calls["verify"]) == 1 and len(calls["settle"]) == 1
    sent_payload, sent_requirements = calls["verify"][0]
    assert sent_payload == payload
    assert sent_requirements["network"] == "solana"
    assert sent_requirements["payTo"] == SOL_PAY_TO
    assert calls["settle"][0][1]["network"] == "solana"


def test_solana_facilitator_down_fails_closed(stubbed_backends, monkeypatch):
    _enable_multi(monkeypatch)
    _fake_solana_facilitator(monkeypatch, unreachable=True)
    xc = TestClient(create_app())
    resp = xc.post("/search", json={"query": "q"}, headers=_header(_solana_payload()))
    assert resp.status_code == 503
    assert resp.json()["error"] == "payment_verification_unavailable"


def test_facilitator_verify_envelope_and_solana_url(monkeypatch):
    """The real verify call posts the v1 envelope to the SOLANA facilitator
    URL when the requirements are for the Solana leg (§4b.4)."""
    _enable_multi(
        monkeypatch,
        X402_FACILITATOR_URL_SOLANA="https://solana-fac.test/facilitator",
    )
    captured = {}

    class FakeResp:
        def json(self):
            return {"isValid": True, "payer": "payer"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr(x402_mod.httpx, "AsyncClient", FakeClient)
    requirements = {"network": "solana", "payTo": SOL_PAY_TO}
    payload = _solana_payload()
    result = asyncio.run(x402_mod.facilitator_verify(payload, requirements))
    assert result["isValid"] is True
    assert captured["url"] == "https://solana-fac.test/facilitator/verify"
    assert captured["json"] == {
        "x402Version": 1,
        "paymentPayload": payload,
        "paymentRequirements": requirements,
    }


# ------------------------------------------------- §4c.5 misconfigured leg


def test_solana_leg_dropped_when_misconfigured(stubbed_backends, monkeypatch, caplog):
    _enable_multi(monkeypatch)
    monkeypatch.delenv("X402_PAY_TO_SOLANA")
    xc = TestClient(create_app())
    with caplog.at_level(logging.ERROR):
        resp = xc.post("/search", json={"query": "q"})
    assert resp.status_code == 402
    body = resp.json()
    # Base keeps serving with the original single-network shape.
    assert isinstance(body["accepts"], dict)
    assert body["accepts"]["network"] == "base"
    assert body["paymentRequirements"]["network"] == "base"
    assert any(
        "solana" in record.message and "X402_PAY_TO_SOLANA" in record.message
        for record in caplog.records
        if record.levelno >= logging.ERROR
    )


# ------------------------------------------------- §4c.6 discovery manifest


def test_discovery_manifest_multi_network(monkeypatch):
    _enable_multi(monkeypatch)
    xc = TestClient(create_app())
    manifest = xc.get("/.well-known/x402.json").json()
    networks = {entry["network"]: entry for entry in manifest["networks"]}
    assert set(networks) == {"base", "solana"}
    assert networks["solana"]["asset_contract"] == x402_mod.USDC_SOLANA_MINT
    assert networks["solana"]["pay_to"] == SOL_PAY_TO
    assert networks["base"]["asset_contract"] == x402_mod.USDC_BASE_ADDRESS
    assert networks["base"]["pay_to"] == BASE_PAY_TO
    # Top-level fields still describe the primary (Base) leg.
    assert manifest["network"] == "base"
    assert manifest["pay_to"] == BASE_PAY_TO


def test_discovery_manifest_default_lists_base_only(monkeypatch):
    monkeypatch.delenv("X402_NETWORKS", raising=False)
    monkeypatch.delenv("X402_PAY_TO", raising=False)
    xc = TestClient(create_app())
    manifest = xc.get("/.well-known/x402.json").json()
    assert [entry["network"] for entry in manifest["networks"]] == ["base"]
    assert manifest["networks"][0]["pay_to"] is None


# ------------------------------------------------- fee payer discovery (§3b)


def test_fee_payer_discovery_from_supported(monkeypatch):
    _enable_multi(monkeypatch)
    monkeypatch.delenv("X402_SOLANA_FEE_PAYER")
    monkeypatch.setattr(x402_mod, "resolve_solana_fee_payer", _REAL_RESOLVE)

    class FakeResp:
        def json(self):
            return {
                "kinds": [
                    {"scheme": "exact", "network": "base", "extra": {}},
                    {"scheme": "exact", "network": "solana", "extra": {"feePayer": FEE_PAYER}},
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            assert url == "https://x402.test/facilitator/supported"
            return FakeResp()

    monkeypatch.setattr(x402_mod.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(x402_mod.resolve_solana_fee_payer()) == FEE_PAYER
    assert x402_mod.solana_fee_payer() == FEE_PAYER
    # With the fee payer resolved, the Solana leg becomes servable.
    configs = x402_mod.network_configs()
    assert configs["solana"]["extra"] == {"feePayer": FEE_PAYER}


def test_solana_leg_dropped_when_fee_payer_unresolved(monkeypatch):
    _enable_multi(monkeypatch)
    monkeypatch.delenv("X402_SOLANA_FEE_PAYER")
    # Discovery stub (autouse) resolves nothing -> leg must drop out.
    configs = x402_mod.network_configs()
    assert set(configs) == {"base"}
