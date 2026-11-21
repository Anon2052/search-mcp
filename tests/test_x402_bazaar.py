"""Bazaar discovery metadata (manifest-hygiene pass, 2026-10-21).

Covers: the discovery manifest served at BOTH /.well-known/x402 and
/.well-known/x402.json with identical content; x402 v1 ``outputSchema``
inside paymentRequirements; x402 v2 ``extensions.bazaar`` on 402
challenges; and the additive-only guarantee (legacy challenge fields and
free-tier behavior unchanged).

Format references (verified against primary sources 2026-10-21): the x402
Python SDK (x402/extensions/bazaar — ``declare_discovery_extension`` in
resource_service.py, types.py, and the "V1 Support" extractor contract)
and CDP docs "Get discovered (Bazaar)".
"""

from decimal import Decimal

import jsonschema
import pytest
from fastapi.testclient import TestClient

from search_mcp import fetch as fetch_mod
from search_mcp import search as search_mod
from search_mcp import x402 as x402_mod
from search_mcp.rest_app import create_app

BASE_PAY_TO = "0x000000000000000000000000000000000000dEaD"
SOL_PAY_TO = "So1anaPayToTest1111111111111111111111111111"
FEE_PAYER = "FeePayerTestSo1ana111111111111111111111111"


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


@pytest.fixture(autouse=True)
def _no_network_discovery(monkeypatch):
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


def _enable(monkeypatch, **over):
    env = {
        "X402_ENABLED": "true",
        "X402_PAY_TO": BASE_PAY_TO,
        "X402_FACILITATOR_URL": "https://x402.test/facilitator",
        "FREE_TIER_ENABLED": "false",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _validate_bazaar(extension):
    """The extension's info block must validate against its own schema."""
    jsonschema.validate(extension["info"], extension["schema"])
    assert extension["schema"]["required"] == ["input"]


# ------------------------------------------------------------- manifest


def test_manifest_served_at_both_paths_identically(monkeypatch):
    xc = TestClient(create_app())
    bare = xc.get("/.well-known/x402")
    dotted = xc.get("/.well-known/x402.json")
    assert bare.status_code == dotted.status_code == 200
    assert bare.json() == dotted.json()
    assert bare.json()["prices_usdc"]["search_web"] == "0.01"
    assert "/.well-known/x402" in bare.json()["free_endpoints"]
    assert "/.well-known/x402.json" in bare.json()["free_endpoints"]


# ------------------------------------------------------- v1 outputSchema


def test_requirements_carry_output_schema_for_rest_routes():
    req = x402_mod.build_payment_requirements(
        Decimal("0.01"), "https://search.ioconnex.dev/search", "search_web on search-mcp"
    )
    schema = req["outputSchema"]
    assert schema["input"]["type"] == "http"
    assert schema["input"]["method"] == "POST"
    assert schema["input"]["discoverable"] is True
    assert schema["input"]["bodyType"] == "json"
    assert schema["input"]["bodyFields"] == {"query": "x402 protocol news", "count": 5}
    assert schema["output"]["required"] == ["results"]


def test_requirements_carry_output_schema_for_mcp_tool_call():
    req = x402_mod.build_payment_requirements(
        Decimal("0.01"),
        "https://search.ioconnex.dev/mcp",
        "mcp_tool_call on search-mcp",
        tool_name="fetch_url",
    )
    body = req["outputSchema"]["input"]["bodyFields"]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "fetch_url"
    assert body["params"]["arguments"] == {"url": "https://example.com/article"}
    assert req["outputSchema"]["output"] == {"type": "string"}


def test_no_output_schema_for_uncatalogued_resources():
    req = x402_mod.build_payment_requirements(
        Decimal("0.01"), "https://search.ioconnex.dev/other", "other"
    )
    assert "outputSchema" not in req
    req = x402_mod.build_payment_requirements(
        Decimal("0.01"), "https://search.ioconnex.dev/mcp", "mcp_tool_call on search-mcp"
    )
    assert "outputSchema" not in req  # no tool name -> nothing truthful to declare


# ------------------------------------------------------ v2 extensions


def test_extensions_bazaar_http_and_mcp_forms_validate():
    http_ext = x402_mod._bazaar_extension("https://search.ioconnex.dev/search")
    _validate_bazaar(http_ext)
    assert http_ext["info"]["input"]["type"] == "http"
    assert http_ext["info"]["input"]["method"] == "POST"
    assert http_ext["info"]["input"]["body"] == {"query": "x402 protocol news", "count": 5}
    assert http_ext["info"]["output"]["type"] == "json"

    mcp_ext = x402_mod._bazaar_extension("https://search.ioconnex.dev/mcp", "search_web")
    _validate_bazaar(mcp_ext)
    assert mcp_ext["info"]["input"]["type"] == "mcp"
    assert mcp_ext["info"]["input"]["toolName"] == "search_web"
    assert mcp_ext["info"]["input"]["transport"] == "streamable-http"
    assert mcp_ext["info"]["input"]["example"] == {"query": "x402 protocol news", "count": 5}
    assert mcp_ext["info"]["input"]["inputSchema"]["required"] == ["query"]


def test_challenge_carries_extensions_for_rest_route(monkeypatch):
    _enable(monkeypatch)
    xc = TestClient(create_app())
    resp = xc.post("/search", json={"query": "q"})
    assert resp.status_code == 402
    body = resp.json()
    # Legacy challenge fields unchanged.
    assert body["error"] == "payment_required"
    assert body["accepts"]["pay_to"] == BASE_PAY_TO
    assert body["accepts"]["amount_usdc"] == "0.01"
    assert body["paymentRequirements"]["outputSchema"]["input"]["method"] == "POST"
    # New additive block.
    bazaar = body["extensions"]["bazaar"]
    _validate_bazaar(bazaar)
    assert bazaar["info"]["input"]["type"] == "http"
    assert bazaar["info"]["input"]["body"]["query"] == "x402 protocol news"


def test_challenge_carries_extensions_for_mcp_tool_call(monkeypatch):
    _enable(monkeypatch)
    xc = TestClient(create_app())
    resp = xc.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_web", "arguments": {"query": "q"}},
        },
    )
    assert resp.status_code == 402
    bazaar = resp.json()["extensions"]["bazaar"]
    _validate_bazaar(bazaar)
    assert bazaar["info"]["input"]["type"] == "mcp"
    assert bazaar["info"]["input"]["toolName"] == "search_web"


def test_challenge_extensions_multi_network_shape(monkeypatch):
    _enable(
        monkeypatch,
        X402_NETWORKS="base,solana",
        X402_PAY_TO_SOLANA=SOL_PAY_TO,
        X402_SOLANA_FEE_PAYER=FEE_PAYER,
    )
    xc = TestClient(create_app())
    body = xc.post("/fetch", json={"url": "https://example.com/a"}).json()
    assert isinstance(body["accepts"], list) and len(body["accepts"]) == 2
    assert isinstance(body["paymentRequirements"], list)
    assert all("outputSchema" in entry for entry in body["paymentRequirements"])
    _validate_bazaar(body["extensions"]["bazaar"])
    assert body["extensions"]["bazaar"]["info"]["input"]["type"] == "http"


def test_challenge_without_resource_has_no_extensions():
    requirements = {
        "maxAmountRequired": "10000",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "network": "base",
        "payTo": BASE_PAY_TO,
        "scheme": "exact",
    }
    content = x402_mod.challenge_content("payment_required", requirements)
    assert "extensions" not in content


def test_free_tier_behavior_unchanged(stubbed_backends, monkeypatch):
    _enable(monkeypatch, FREE_TIER_ENABLED="true")
    xc = TestClient(create_app())
    resp = xc.post("/search", json={"query": "hello"})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["title"] == "T"
