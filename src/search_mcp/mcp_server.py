"""FastMCP server: web search + URL fetch tools over streamable HTTP."""

from __future__ import annotations

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import fetch as _fetch
from . import search as _search
from .skills import (
    SKILL_MD,
    SKILL_MIME,
    SKILL_NAME,
    SKILL_URI,
    SkillsExtension,
    skill_frontmatter,
)

mcp = FastMCP(
    "search-mcp",
    instructions=(
        "Web search and fetch tools. "
        "search_web: query string + result count -> list of {title, url, snippet}. "
        "fetch_url: fetch a public http(s) URL -> main text content. "
        "Private/loopback hosts are rejected. "
        f"Skill: {SKILL_URI} (usage instructions for these tools; "
        "SEP-2640: enumerate via skills/list, fetch one via skills/get)."
    ),
)


@mcp.tool(
    name="search_web",
    title="Search the Web",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
def search_web(query: str, count: int = 5) -> list[dict]:
    """Search the web and return titles, URLs, and snippets.

    Args:
        query: The search query.
        count: Maximum results to return (1-10, default 5).
    """
    return _search.search_web(query, count=count)


@mcp.tool(
    name="fetch_url",
    title="Fetch URL as Text",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
def fetch_url(url: str) -> str:
    """Fetch a public http(s) URL and return its main text content as plain text.

    Args:
        url: The http(s) URL to fetch. Private/loopback hosts are rejected.
    """
    return _fetch.fetch_url(url)


@mcp.resource(
    SKILL_URI,
    mime_type=SKILL_MIME,
    name=SKILL_NAME,
    description=skill_frontmatter()["description"],
)
def skill_readme() -> str:
    """SEP-2640 skill payload: usage instructions for this server's tools."""
    return SKILL_MD


# SEP-2640 skills extension: skills/list + skills/get (+ capability declaration).
mcp.add_extension(SkillsExtension())
