"""FastMCP server: web search + URL fetch tools over streamable HTTP."""

from __future__ import annotations

from fastmcp import FastMCP

from . import fetch as _fetch
from . import search as _search

mcp = FastMCP(
    "search-mcp",
    instructions=(
        "Web search and fetch tools. "
        "search_web: query string + result count -> list of {title, url, snippet}. "
        "fetch_url: fetch a public http(s) URL -> main text content. "
        "Private/loopback hosts are rejected."
    ),
)


@mcp.tool(name="search_web")
def search_web(query: str, count: int = 5) -> list[dict]:
    """Search the web and return titles, URLs, and snippets.

    Args:
        query: The search query.
        count: Maximum results to return (1-10, default 5).
    """
    return _search.search_web(query, count=count)


@mcp.tool(name="fetch_url")
def fetch_url(url: str) -> str:
    """Fetch a public http(s) URL and return its main text content as plain text.

    Args:
        url: The http(s) URL to fetch. Private/loopback hosts are rejected.
    """
    return _fetch.fetch_url(url)
