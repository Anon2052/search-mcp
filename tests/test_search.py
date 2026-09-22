"""Tests for the Tavily search backend: mapping, clamping, key handling."""

from __future__ import annotations

import pytest

from search_mcp import search as search_mod
from search_mcp.search import SearchError, search_web

TEST_KEY = "tvly-test-key-abc123"


class FakeTavily:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.calls = []

    def search(self, query, max_results=5, search_depth="basic"):
        self.calls.append(
            {"query": query, "max_results": max_results, "search_depth": search_depth}
        )
        return {
            "results": [
                {
                    "title": "Example One",
                    "url": "https://example.com/1",
                    "content": "snippet one",
                },
                {
                    "title": "Example Two",
                    "url": "https://example.com/2",
                    "content": "snippet two",
                },
            ]
        }


@pytest.fixture()
def fake_client(monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", TEST_KEY)
    fake = FakeTavily()

    def factory(api_key=None):
        fake.api_key = api_key
        return fake

    monkeypatch.setattr(search_mod, "TavilyClient", factory)
    return fake


def test_search_maps_results(fake_client):
    results = search_web("hello world", count=2)
    assert results == [
        {"title": "Example One", "url": "https://example.com/1", "snippet": "snippet one"},
        {"title": "Example Two", "url": "https://example.com/2", "snippet": "snippet two"},
    ]
    call = fake_client.calls[0]
    assert call["query"] == "hello world"
    assert call["max_results"] == 2
    assert call["search_depth"] == "basic"  # basic = 1 credit per call


def test_search_uses_configured_key(fake_client):
    search_web("q")
    assert fake_client.api_key == TEST_KEY


def test_count_clamped_to_max(fake_client):
    search_web("q", count=50)
    assert fake_client.calls[0]["max_results"] == 10


def test_count_clamped_to_min(fake_client):
    search_web("q", count=0)
    assert fake_client.calls[0]["max_results"] == 1


def test_empty_query_rejected(fake_client):
    with pytest.raises(SearchError, match="must not be empty"):
        search_web("   ")


def test_missing_key_raises_without_leaking(monkeypatch):
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    with pytest.raises(SearchError, match="SEARCH_API_KEY"):
        search_web("q")


def test_backend_failure_wrapped_and_key_not_leaked(monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", TEST_KEY)

    class Boom:
        def __init__(self, api_key=None):
            pass

        def search(self, *a, **k):
            raise RuntimeError(f"upstream exploded for {TEST_KEY}")

    monkeypatch.setattr(search_mod, "TavilyClient", Boom)
    with pytest.raises(SearchError, match="backend request failed") as excinfo:
        search_web("q")
    assert TEST_KEY not in str(excinfo.value)


def test_empty_results_list(fake_client, monkeypatch):
    class Empty(FakeTavily):
        def search(self, *a, **k):
            return {"results": []}

    monkeypatch.setattr(search_mod, "TavilyClient", lambda api_key=None: Empty(api_key))
    assert search_web("q") == []
