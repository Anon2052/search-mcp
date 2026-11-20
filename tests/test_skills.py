"""SEP-2640 skills tests: skills/list, skills/get, skill:// resource, capabilities."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import MCPError
from mcp.types import Request
from pydantic import TypeAdapter

from search_mcp.mcp_server import mcp
from search_mcp.skills import (
    EXTENSION_ID,
    SKILL_MD,
    SKILL_MIME,
    SKILL_NAME,
    SKILL_URI,
    SkillsGetParams,
    SkillsListParams,
    skill_entry,
)

RAW_DICT = TypeAdapter(dict)


def _req(method: str, params: dict, params_type):
    """Build a wire-faithful request: the bare Request generic drops params."""
    return Request[params_type, str](method=method, params=params)


def _run(coro):
    return asyncio.run(coro)


def _expected_entry() -> dict:
    raw = SKILL_MD.encode("utf-8")
    return {
        "uri": SKILL_URI,
        "frontmatter": {"name": SKILL_NAME},
        "resources": [
            {
                "uri": SKILL_URI,
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        ],
    }


def test_skills_list_returns_single_entry():
    async def go():
        async with Client(mcp) as client:
            return await client.session.send_request(
                _req("skills/list", {}, SkillsListParams), RAW_DICT
            )

    result = _run(go())
    assert result["resultType"] == "complete"
    assert len(result["skills"]) == 1
    entry = result["skills"][0]
    expected = _expected_entry()
    assert entry["uri"] == expected["uri"] == SKILL_URI
    assert entry["frontmatter"]["name"] == SKILL_NAME
    assert entry["frontmatter"]["description"]
    assert entry["resources"] == expected["resources"]


def test_skills_list_entry_matches_local_entry():
    assert skill_entry()["resources"] == _expected_entry()["resources"]


def test_skill_uri_matches_frontmatter_name():
    entry = skill_entry()
    assert entry["uri"] == f"skill://{entry['frontmatter']['name']}/SKILL.md"


def test_skills_get_roundtrip():
    async def go():
        async with Client(mcp) as client:
            return await client.session.send_request(
                _req("skills/get", {"uri": SKILL_URI}, SkillsGetParams), RAW_DICT
            )

    result = _run(go())
    assert result["resultType"] == "complete"
    assert result["skill"]["uri"] == SKILL_URI
    assert result["skill"]["frontmatter"]["name"] == SKILL_NAME


def test_skills_get_unknown_uri_is_invalid_params():
    async def go():
        async with Client(mcp) as client:
            await client.session.send_request(
                _req("skills/get", {"uri": "skill://nope/SKILL.md"}, SkillsGetParams),
                RAW_DICT,
            )

    with pytest.raises(MCPError) as exc_info:
        _run(go())
    assert exc_info.value.code == -32602


def test_skill_resource_readable_via_resources_read():
    async def go():
        async with Client(mcp) as client:
            return await client.read_resource(SKILL_URI)

    contents = _run(go())
    assert len(contents) == 1
    content = contents[0]
    assert content.text == SKILL_MD
    assert content.mime_type == SKILL_MIME


def test_skill_resource_listed():
    async def go():
        async with Client(mcp) as client:
            return await client.list_resources()

    uris = {str(r.uri) for r in _run(go())}
    assert SKILL_URI in uris


def test_extension_declared_in_capabilities():
    caps = mcp._mcp_server.get_capabilities()
    assert caps.resources is not None  # SEP-2640 requires the resources capability
    assert caps.extensions is not None
    assert EXTENSION_ID in caps.extensions


def test_unknown_method_still_404s():
    async def go():
        async with Client(mcp) as client:
            await client.session.send_request(_req("skills/bogus", {}, SkillsListParams), RAW_DICT)

    with pytest.raises(MCPError) as exc_info:
        _run(go())
    assert exc_info.value.code == -32601
