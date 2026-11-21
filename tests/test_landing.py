"""Landing page at /: the ioconnex.dev "superpowers" links point at the bare
service root, so it must serve the live-demo page publicly and free — and the
x402 exemption for "/" must stay exact-path (not widen to other routes)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from search_mcp.rest_app import create_app


def test_landing_page_served():
    with TestClient(create_app()) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Web Search" in r.text
    assert "Search the web, live." in r.text


def test_landing_exemption_does_not_widen():
    with TestClient(create_app()) as client:
        r = client.get("/definitely-not-a-route")
    assert r.status_code == 404


def test_support_page_still_served():
    with TestClient(create_app()) as client:
        r = client.get("/support")
    assert r.status_code == 200
