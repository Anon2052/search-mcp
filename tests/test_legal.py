"""Tests for the public legal-document endpoints (/legal/*).

These URLs are the ones submitted to Stripe onboarding: they must be
public (200 with no auth), never x402-metered, never rate-limited, and the
content must match the canonical (filled-in) documents verbatim.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from search_mcp.legal_docs import PRIVACY_POLICY, REFUND_POLICY, TERMS_OF_SERVICE
from search_mcp.rest_app import create_app

_LEGAL = (
    ("/legal/terms", TERMS_OF_SERVICE, "# Terms of Service"),
    ("/legal/privacy", PRIVACY_POLICY, "# Privacy Policy"),
    ("/legal/refunds", REFUND_POLICY, "# Refund Policy"),
)


def test_legal_routes_serve_verbatim_markdown():
    xc = TestClient(create_app())
    for path, doc, heading in _LEGAL:
        r = xc.get(path)
        assert r.status_code == 200, f"{path}: got {r.status_code}"
        assert "text/markdown" in r.headers["content-type"]
        # Verbatim: exact match to the canonical filled-in document.
        assert r.text == doc
        assert heading in r.text
        assert "IOConnex LLC" in r.text
        # No placeholders may remain in the filled-in docs.
        for placeholder in ("[LLC NAME]", "[DATE]", "[CONTACT EMAIL]"):
            assert placeholder not in r.text


def test_legal_routes_never_402_when_x402_enabled(monkeypatch):
    """Even with X402_ENABLED=true the legal routes must not 402."""
    for k, v in {
        "X402_ENABLED": "true",
        "X402_PAY_TO": "0xAbC123",
        "X402_FACILITATOR_URL": "https://x402.test/facilitator",
    }.items():
        monkeypatch.setenv(k, v)
    xc = TestClient(create_app())
    for path, _doc, _heading in _LEGAL:
        r = xc.get(path)
        assert r.status_code == 200, f"{path}: got {r.status_code}"
        assert "text/markdown" in r.headers["content-type"]


def test_legal_routes_not_rate_limited():
    """Legal routes carry no limiter; hammering stays 200, never 429."""
    xc = TestClient(create_app(rate_limit="5/minute"))
    for _ in range(10):
        r = xc.get("/legal/terms")
        assert r.status_code == 200, f"got {r.status_code}"
