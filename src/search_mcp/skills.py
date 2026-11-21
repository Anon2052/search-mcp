"""SEP-2640 (MCP Skills) support for search-mcp.

Extension id: ``io.modelcontextprotocol/skills`` (Final, spec'd against base
protocol 2026-07-28). Minimal implementation: one skill describing this
server's tool surface, served as a ``skill://`` resource plus the
``skills/list`` and ``skills/get`` methods. Optional
``resources/directory/read`` is deliberately not implemented.
"""

from __future__ import annotations

import hashlib

import yaml
from fastmcp.exceptions import MCPError
from fastmcp.server.extensions import MethodBinding, ServerExtension
from mcp.types import INVALID_PARAMS, RequestParams

SKILL_NAME = "search-mcp"
SKILL_URI = f"skill://{SKILL_NAME}/SKILL.md"
SKILL_MIME = "text/markdown"

EXTENSION_ID = "io.modelcontextprotocol/skills"

SKILL_MD = """\
---
name: search-mcp
description: Search the web and fetch page content as clean text. Use for
  research, fact-checking, and grounding answers in current sources.
---

# search-mcp

Web search (Tavily) plus SSRF-guarded URL fetching, exposed as MCP tools.

## Tools

- `search_web(query, count=5)` — query string + result count (1-10) -> list of
  `{title, url, snippet}`.
- `fetch_url(url)` — fetch a public http(s) URL -> main text content as plain
  text. Private/loopback hosts are rejected.

## Workflow

1. Start with `search_web` to find candidate sources; 3-8 results is typical.
2. Use `fetch_url` on the most promising URLs to read full content — snippets
   are lossy, so synthesize from fetched content, not snippets alone.
3. Cite the URLs you actually read.

## Notes

- `fetch_url` only reaches public hosts; intranet or localhost URLs are
  rejected by design.
- If a fetch fails, fall back to another result rather than retrying the same
  URL repeatedly.
"""


def _skill_bytes() -> bytes:
    return SKILL_MD.encode("utf-8")


def skill_frontmatter() -> dict:
    """Verbatim YAML frontmatter parsed from the skill's SKILL.md."""
    if not SKILL_MD.startswith("---"):
        raise ValueError("SKILL.md must begin with YAML frontmatter")
    _, front, _ = SKILL_MD.split("---", 2)
    data = yaml.safe_load(front)
    if not isinstance(data, dict) or "name" not in data or "description" not in data:
        raise ValueError("SKILL.md frontmatter must define name and description")
    return data


def skill_entry() -> dict:
    """SEP-2640 skill entry: uri + verbatim frontmatter + file manifest."""
    raw = _skill_bytes()
    return {
        "uri": SKILL_URI,
        "frontmatter": skill_frontmatter(),
        "resources": [
            {
                "uri": SKILL_URI,
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        ],
    }


class SkillsListParams(RequestParams):
    cursor: str | None = None


class SkillsGetParams(RequestParams):
    uri: str


async def _handle_skills_list(ctx, params) -> dict:  # noqa: ANN001, ANN202
    return {"resultType": "complete", "skills": [skill_entry()]}


async def _handle_skills_get(ctx, params) -> dict:  # noqa: ANN001, ANN202
    if params.uri != SKILL_URI:
        raise MCPError(INVALID_PARAMS, f"unknown skill uri: {params.uri}")
    return {"resultType": "complete", "skill": skill_entry()}


class SkillsExtension(ServerExtension):
    """SEP-2640 skills extension (``io.modelcontextprotocol/skills``)."""

    identifier = EXTENSION_ID

    def methods(self) -> tuple[MethodBinding, ...]:
        return (
            MethodBinding(
                method="skills/list",
                params_type=SkillsListParams,
                handler=_handle_skills_list,
            ),
            MethodBinding(
                method="skills/get",
                params_type=SkillsGetParams,
                handler=_handle_skills_get,
            ),
        )
