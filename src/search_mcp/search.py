"""Web search backend.

Tavily (https://tavily.com) — AI-agent-native search API: 1,000 free credits/mo
(no card), basic search = 1 credit. Chosen over Brave Search API (2,000 free
queries/mo) because Tavily returns LLM-ready structured results (title / url /
content) via an official, maintained Python SDK, and its per-call cost
(~$0.006-0.008 basic) fits the connector's unit economics. The thin
``search_web`` seam below is the swap point if a second backend is added later.
"""

from __future__ import annotations

import os

from tavily import TavilyClient

MAX_RESULTS = 10
_SNIPPET_CHARS = 2000


class SearchError(Exception):
    """Raised when a web search cannot be completed."""


def _api_key() -> str:
    key = os.environ.get("SEARCH_API_KEY", "").strip()
    if not key:
        raise SearchError(
            "search backend not configured: set the SEARCH_API_KEY environment variable"
        )
    return key


def search_web(query: str, count: int = 5) -> list[dict]:
    """Search the web; return a list of {title, url, snippet} dicts.

    Uses Tavily basic search (1 credit per call). Raises SearchError on
    misconfiguration or backend failure. The API key is never included in
    error messages or logs.
    """
    query = (query or "").strip()
    if not query:
        raise SearchError("search: query must not be empty")
    try:
        count = max(1, min(int(count), MAX_RESULTS))
    except (TypeError, ValueError) as exc:
        raise SearchError(f"search: invalid count ({exc})") from exc

    client = TavilyClient(api_key=_api_key())
    try:
        resp = client.search(query, max_results=count, search_depth="basic")
    except Exception as exc:  # Tavily SDK errors -> SearchError; never leak the key
        raise SearchError(f"search: backend request failed ({type(exc).__name__})") from exc

    results: list[dict] = []
    for item in resp.get("results", []) or []:
        results.append(
            {
                "title": str(item.get("title", ""))[:300],
                "url": str(item.get("url", ""))[:2048],
                "snippet": str(item.get("content", ""))[:_SNIPPET_CHARS],
            }
        )
    return results
