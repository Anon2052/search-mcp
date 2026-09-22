"""End-to-end MCP protocol tests via FastMCP's in-memory client."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from search_mcp import fetch as fetch_mod
from search_mcp import search as search_mod
from search_mcp.mcp_server import mcp


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def stubbed_backends(monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "tvly-test")

    class FakeTavily:
        def __init__(self, api_key=None):
            pass

        def search(self, query, max_results=5, search_depth="basic"):
            return {
                "results": [
                    {
                        "title": "MCP Result",
                        "url": "https://example.com/mcp",
                        "content": "mcp snippet",
                    }
                ]
            }

    monkeypatch.setattr(search_mod, "TavilyClient", FakeTavily)
    monkeypatch.setattr(fetch_mod, "fetch_url", lambda url, timeout=20: "fetched body text")


def test_both_tools_registered():
    async def go():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            return {t.name for t in tools}

    assert _run(go()) == {"search_web", "fetch_url"}


def test_tool_descriptions_present():
    async def go():
        async with Client(mcp) as client:
            return await client.list_tools()

    for tool in _run(go()):
        assert tool.description, f"tool {tool.name} has no description"


def test_mcp_search_web_roundtrip(stubbed_backends):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("search_web", {"query": "test query", "count": 3})

    result = _run(go())
    text = "".join(c.text for c in result.content if hasattr(c, "text"))
    assert "MCP Result" in text
    assert "https://example.com/mcp" in text


def test_mcp_fetch_url_roundtrip(stubbed_backends):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("fetch_url", {"url": "https://example.com/"})

    result = _run(go())
    text = "".join(c.text for c in result.content if hasattr(c, "text"))
    assert "fetched body text" in text


def test_mcp_search_error_surfaces_cleanly(stubbed_backends, monkeypatch):
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("search_web", {"query": "q"})

    with pytest.raises(ToolError, match="SEARCH_API_KEY"):
        _run(go())
