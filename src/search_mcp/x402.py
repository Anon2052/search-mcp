"""x402 (USDC on Base; opt-in Solana leg) metering middleware.

Real facilitator verification on every leg. The Solana leg is opt-in via
``X402_NETWORKS`` (SOLANA_PORT_PLAN.md) and changes nothing when unset:
the default remains the Base-only behavior this module has always had.

Per-call micropayments in the x402 v1 "exact" scheme: callers attach an
``X-Payment`` header carrying a base64-encoded signed EIP-3009 USDC
authorization. The server builds the payment requirements server-side from
env, runs local sanity checks, verifies the authorization with an x402
facilitator (``POST /verify``), serves the request, then settles
(``POST /settle``).

Payments are OFF by default: ``x402_effective_enabled()`` requires
``X402_ENABLED=true`` AND ``X402_PAY_TO`` AND ``X402_FACILITATOR_URL`` to all
be set. If ``X402_ENABLED=true`` but config is incomplete, the middleware
logs an ERROR and stays disabled (fail closed).

Facilitator URL default is https://x402.org/facilitator, which serves
Base Sepolia only — NOT mainnet. For Base mainnet, set
X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402 with
CDP_API_KEY_ID + CDP_API_KEY_SECRET (or X402_FACILITATOR_API_KEY for
providers that use API-key auth).

Stripe subscriber keys (see ``billing.py``) bypass per-call charges when
valid.

Free tier: when x402 is on, anonymous callers (no subscriber key, no
X-Payment) draw from a monthly free credit bucket keyed by client IP
(``FREE_MONTHLY_CREDITS``, default 50 — Scott's call, aligned across all
three connectors 2026-10-21; kill switch ``FREE_TIER_ENABLED``). Same
credit math as subscribers (``billing.credits_for_price_usdc`` on the
single per-tool price map). Exhaustion -> HTTP 402 ``free_tier_exhausted``
— 429 stays exclusive to the subscriber cap. Abuse backstop is the
existing 60/min per-IP rate limit; IP rotation is an accepted v1
limitation.
"""

from __future__ import annotations

import base64
import binascii
import copy
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from decimal import Decimal, InvalidOperation

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("search_mcp.x402")

# Circle's canonical USDC on Base (6 decimals).
USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
NETWORK = "base"
SCHEME = "exact"
X402_VERSION = 1
MAX_TIMEOUT_SECONDS = 300

# Solana leg (opt-in via X402_NETWORKS). SOLANA_NETWORK is the x402 v1
# plain network name (SOLANA_PORT_PLAN §2); the CAIP-2 form
# (solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp) is a v2 identifier. Plan open
# question 1 — confirm the exact string against CDP /supported — stays
# open until the devnet round-trip (§4d); "solana" is the plan default.
SOLANA_NETWORK = "solana"
# Circle's canonical USDC SPL mint on Solana mainnet (6 decimals).
USDC_SOLANA_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
KNOWN_NETWORKS = (NETWORK, SOLANA_NETWORK)

# Default facilitator: Base Sepolia testnet only. Documented loudly wherever
# referenced — it cannot verify mainnet authorizations.
DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"

FACILITATOR_TIMEOUT_SECONDS = 15.0

# Paths that never require payment (health checks, docs). Billing routes are
# exempt separately in the middleware: Stripe's webhooks cannot attach an
# X-Payment header, and requiring per-call payment for checkout would be
# circular for new subscribers.
EXEMPT_PREFIXES = (
    # Landing page: the matcher is exact-or-prefix+"/", so "/" exempts ONLY
    # the bare root path — it does not widen to any other route.
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/legal",
    "/support",
    "/demo-walkthrough.mp4",
    "/.well-known",
)

# Always-paid route prefix (2026-10-24 paid-route batch): POSTs under this
# prefix NEVER draw from the monthly free-credit bucket — without a valid
# X-Payment they fall through to the 402 challenge. Directory checkers
# (PayAPI preflight) and router dispatch (Agent402) must see a 402 to
# price a route, and the free tier answers them 200 instead. Normal
# routes keep the free tier unchanged; only /paid/* aliases skip it.
PAID_ROUTE_PREFIX = "/paid/"

# REST route -> tool name (per-repo map).
REST_PRICE_MAP = {
    "/search": "search_web",
    "/fetch": "fetch_url",
    # Always-paid alias: same tool as /search, never free-tiered.
    "/paid/search": "search_web",
}

# Proposed per-tool price defaults (USDC). Business decision — Scott's call.
# Overridable per tool with X402_PRICE_<TOOLNAME>_USDC env vars.
PER_TOOL_DEFAULTS_USDC = {
    "search_web": Decimal("0.01"),
    "fetch_url": Decimal("0.01"),
}

# Fallback for anything paid that doesn't map to a known tool.
DEFAULT_PRICE_USDC = Decimal("0.01")

# JSON-RPC methods that stay free: discovery must work so directory scanners
# (Smithery et al.) keep functioning.
EXEMPT_JSONRPC_METHODS = {
    "initialize",
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "ping",
}


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def x402_enabled_flag() -> bool:
    return _env_flag("X402_ENABLED")


def x402_pay_to() -> str:
    return os.environ.get("X402_PAY_TO", "").strip()


def x402_pay_to_solana() -> str:
    return os.environ.get("X402_PAY_TO_SOLANA", "").strip()


def facilitator_url() -> str:
    return os.environ.get("X402_FACILITATOR_URL", DEFAULT_FACILITATOR_URL).strip()


def solana_facilitator_url() -> str:
    """Facilitator for the Solana leg: dedicated override, else the shared one."""
    return os.environ.get("X402_FACILITATOR_URL_SOLANA", "").strip() or facilitator_url()


def x402_asset_solana() -> str:
    return os.environ.get("X402_ASSET_SOLANA", USDC_SOLANA_MINT).strip() or USDC_SOLANA_MINT


def requested_networks() -> list[str]:
    """Networks named by X402_NETWORKS. Unset = ["base"] (today's behavior)."""
    raw = os.environ.get("X402_NETWORKS", "").strip()
    if not raw:
        return [NETWORK]
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return names or [NETWORK]


# Cached facilitator Solana fee payer, filled lazily by
# resolve_solana_fee_payer() from GET {facilitator}/supported (plan §3b).
_solana_fee_payer_cache: str | None = None


def solana_fee_payer() -> str | None:
    """The facilitator's Solana fee-payer key: X402_SOLANA_FEE_PAYER env
    override, else the /supported-discovered value. None = unresolved."""
    override = os.environ.get("X402_SOLANA_FEE_PAYER", "").strip()
    if override:
        return override
    return _solana_fee_payer_cache


def x402_effective_enabled() -> bool:
    """True only when x402 is fully and VALIDLY configured. Fail closed otherwise."""
    if not x402_enabled_flag():
        return False
    problems = x402_config_problems()
    if problems:
        for problem in problems:
            log.error(
                "x402 misconfigured: %s — x402 metering DISABLED (fail closed).",
                problem,
            )
        return False
    return True


_PAY_TO_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _https_url_problem(label: str, url: str) -> str | None:
    if not url:
        return f"{label} is empty"
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme != "https" or not parts.hostname:
        return (
            f"{label}={url!r} must be an https URL "
            "(http would leak facilitator credentials in cleartext)"
        )
    return None


def leg_config_problems(network: str) -> list[str]:
    """Config problems for ONE network leg. Empty list = leg is servable."""
    problems: list[str] = []
    if network == NETWORK:
        pay_to = x402_pay_to()
        if not pay_to:
            problems.append("X402_PAY_TO is empty")
        elif not _PAY_TO_RE.match(pay_to):
            problems.append(f"X402_PAY_TO={pay_to!r} is not a valid 0x Ethereum address")
        problem = _https_url_problem("X402_FACILITATOR_URL", facilitator_url())
        if problem:
            problems.append(problem)
    elif network == SOLANA_NETWORK:
        pay_to = x402_pay_to_solana()
        if not pay_to:
            problems.append("X402_PAY_TO_SOLANA is empty")
        elif not _BASE58_RE.match(pay_to):
            problems.append(f"X402_PAY_TO_SOLANA={pay_to!r} is not a valid base58 Solana address")
        override = os.environ.get("X402_FACILITATOR_URL_SOLANA", "").strip()
        if override:
            problem = _https_url_problem("X402_FACILITATOR_URL_SOLANA", override)
            if problem:
                problems.append(problem)
    else:
        problems.append(f"unknown x402 network {network!r} in X402_NETWORKS")
    return problems


def valid_networks() -> list[str]:
    """Requested networks whose leg config is complete.

    Per-leg fail-closed (plan §4b.5): a leg with incomplete config is
    dropped with an ERROR log; the remaining legs keep working.
    """
    valid: list[str] = []
    for name in requested_networks():
        problems = leg_config_problems(name)
        if problems:
            for problem in problems:
                log.error("x402 %s leg dropped: %s", name, problem)
        else:
            valid.append(name)
    return valid


def x402_config_problems() -> list[str]:
    """Fail-closed config validation (M-5). Empty list = safe to enable.

    A typo'd payTo would otherwise verify-and-settle real USDC to a dead
    address (service delivered, revenue burned, no error ever raised), and
    an http:// facilitator URL would send facilitator credentials in
    cleartext. Validation is per-leg: one bad leg disables x402 entirely
    only when NO valid leg remains (in which case every leg's problems are
    surfaced so the operator sees exactly what to fix).
    """
    problems: list[str] = []
    if not x402_enabled_flag():
        return problems  # disabled by flag: nothing to validate
    if not valid_networks():
        for name in requested_networks():
            problems.extend(leg_config_problems(name))
        if not problems:
            problems.append("no x402 network legs configured")
    return problems


def network_configs() -> dict[str, dict]:
    """Per-network config table for every currently servable leg (§4b.1)."""
    configs: dict[str, dict] = {}
    for name in valid_networks():
        if name == NETWORK:
            configs[name] = {
                "network_id": NETWORK,
                "asset": USDC_BASE_ADDRESS,
                "pay_to": x402_pay_to(),
                "decimals": USDC_DECIMALS,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        elif name == SOLANA_NETWORK:
            fee_payer = solana_fee_payer()
            if not fee_payer:
                # §4b.5: without a fee payer the client cannot build the
                # transaction — the leg is unservable, drop it loudly.
                log.error(
                    "x402 solana leg dropped: fee payer unresolved (set "
                    "X402_SOLANA_FEE_PAYER or make the facilitator "
                    "/supported endpoint reachable)"
                )
                continue
            configs[name] = {
                "network_id": SOLANA_NETWORK,
                "asset": x402_asset_solana(),
                "pay_to": x402_pay_to_solana(),
                "decimals": USDC_DECIMALS,
                "extra": {"feePayer": fee_payer},
            }
    return configs


def _price_env_name(tool_name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9]", "_", tool_name).upper()
    return f"X402_PRICE_{sanitized}_USDC"


def price_for_tool_usdc(tool_name: str) -> Decimal:
    """Resolve the per-call price for a tool.

    Precedence: X402_PRICE_<TOOLNAME>_USDC env override, then the baked
    per-tool default, then the generic X402_PRICE_USDC. Invalid values log a
    warning and fall back — we always charge a default, never zero.
    """
    fallback = PER_TOOL_DEFAULTS_USDC.get(tool_name, DEFAULT_PRICE_USDC)
    env_name = _price_env_name(tool_name)
    raw = os.environ.get(env_name)
    if raw is not None:
        try:
            value = Decimal(raw.strip())
            if value <= 0:
                raise InvalidOperation
            return value
        except (InvalidOperation, AttributeError):
            log.warning(
                "Invalid %s=%r — falling back to default %s (fail closed).",
                env_name,
                raw,
                fallback,
            )
            return fallback
    return fallback


def default_price_usdc() -> Decimal:
    try:
        value = Decimal(os.environ.get("X402_PRICE_USDC", "0.015").strip())
        if value <= 0:
            raise InvalidOperation
        return value
    except (InvalidOperation, AttributeError):
        log.warning("Invalid X402_PRICE_USDC — falling back to 0.015 (fail closed).")
        return Decimal("0.015")


def usdc_to_atomic(amount_usdc: Decimal) -> int:
    return int((amount_usdc * (10**USDC_DECIMALS)).to_integral_value())


def _base_config() -> dict:
    """The Base leg's config, built from env whether or not it is servable."""
    return {
        "network_id": NETWORK,
        "asset": USDC_BASE_ADDRESS,
        "pay_to": x402_pay_to(),
        "decimals": USDC_DECIMALS,
        "extra": {"name": "USD Coin", "version": "2"},
    }


# ---------------------------------------------------------------------------
# Bazaar discovery metadata (x402 v1 outputSchema + v2 extensions.bazaar).
# ---------------------------------------------------------------------------
# Two cataloging surfaces fed by the single set of tables below:
#
# * x402 v1 (the protocol these servers speak): the facilitator's bazaar
#   extractor reads discovery metadata from ``outputSchema`` INSIDE each
#   paymentRequirements object — ``input`` must carry ``type: "http"`` and
#   the HTTP ``method``; for body endpoints the example request body rides
#   in ``bodyFields`` (reference: x402 Python SDK, x402/extensions/bazaar —
#   "V1 Support" — and the v1 facilitator extractor contract).
# * x402 v2: the same metadata as a top-level ``extensions.bazaar`` block on
#   the 402 body, emitted in the facilitator-facing form the SDK's
#   ``declare_discovery_extension`` produces after resource-server
#   enrichment (``info.input`` carries the real method and validates
#   against the sibling JSON Schema; reference: the same SDK's
#   resource_service.py / types.py, mirrored field-for-field below).
#
# Purely additive: existing x402 clients ignore both keys, and no existing
# field changes value.

_BAZAAR_SCHEMA_URL = "https://json-schema.org/draft/2020-12/schema"

_SEARCH_RESULT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "snippet": {"type": "string"},
    },
    "required": ["title", "url", "snippet"],
}

_SEARCH_EXAMPLE_RESULT = {
    "title": "Example result title",
    "url": "https://example.com/article",
    "snippet": "Example result snippet.",
}

#: Per-tool discovery metadata for the MCP surface. ``input_schema`` mirrors
#: the FastMCP-generated argument schema of each tool in mcp_server.py;
#: ``output_schema`` / ``output_example`` describe the tool's return value.
_BAZAAR_TOOLS: dict[str, dict] = {
    "search_web": {
        "description": "Search the web and return titles, URLs, and snippets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
        "example_args": {"query": "x402 protocol news", "count": 5},
        "output_schema": {"type": "array", "items": _SEARCH_RESULT_ITEM_SCHEMA},
        "output_example": [_SEARCH_EXAMPLE_RESULT],
    },
    "fetch_url": {
        "description": "Fetch a public http(s) URL and return its main text content as plain text.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "example_args": {"url": "https://example.com/article"},
        "output_schema": {"type": "string"},
        "output_example": "Example page text.",
    },
}

#: Per-route discovery metadata for the REST surface. Body examples and body
#: schemas mirror the pydantic request models in rest_app.py; output shapes
#: mirror the route handlers' return values.
_BAZAAR_ROUTES: dict[str, dict] = {
    "/search": {
        "tool": "search_web",
        "body_type": "json",
        "body_example": {"query": "x402 protocol news", "count": 5},
        "body_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "results": {"type": "array", "items": _SEARCH_RESULT_ITEM_SCHEMA},
            },
            "required": ["results"],
        },
        "output_example": {"results": [_SEARCH_EXAMPLE_RESULT]},
    },
    "/fetch": {
        "tool": "fetch_url",
        "body_type": "json",
        "body_example": {"url": "https://example.com/article"},
        "body_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "output_example": {"text": "Example page text."},
    },
}


def _bazaar_target(resource: str, tool_name: str | None) -> tuple[str, dict, str] | None:
    """Resolve a requirements resource to its bazaar metadata table entry.

    Returns ``(kind, meta, tool_name)`` — kind ``"rest"`` (meta from
    ``_BAZAAR_ROUTES``) or ``"mcp"`` (meta from ``_BAZAAR_TOOLS``) — or
    None when the resource is not a catalogable endpoint.
    """
    path = urllib.parse.urlsplit(resource).path or resource
    route = _BAZAAR_ROUTES.get(path)
    if route is not None:
        return "rest", route, route["tool"]
    if (path == "/mcp" or path.startswith("/mcp/")) and tool_name in _BAZAAR_TOOLS:
        return "mcp", _BAZAAR_TOOLS[tool_name], str(tool_name)
    return None


def _bazaar_output_schema(resource: str, tool_name: str | None = None) -> dict | None:
    """x402 v1 ``outputSchema`` for one resource (None = not catalogable).

    For MCP tools/call resources the endpoint is HTTP, so the input is the
    http form whose ``bodyFields`` carry the JSON-RPC envelope example —
    exactly what the v1 bazaar extractor turns into a catalog entry.
    """
    target = _bazaar_target(resource, tool_name)
    if target is None:
        return None
    kind, meta, name = target
    if kind == "rest":
        body: dict = copy.deepcopy(meta["body_example"])
    else:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": copy.deepcopy(meta["example_args"])},
        }
    return {
        "input": {
            "type": "http",
            "method": "POST",
            "discoverable": True,
            "bodyType": meta.get("body_type", "json"),
            "bodyFields": body,
        },
        "output": copy.deepcopy(meta["output_schema"]),
    }


def _bazaar_extension(resource: str, tool_name: str | None = None) -> dict | None:
    """x402 v2 ``extensions.bazaar`` block for one resource (None = skip).

    Mirrors the x402 SDK's ``declare_discovery_extension`` output in its
    served (post-enrichment) state: ``info.input`` carries the real HTTP
    method / tool name and validates against the sibling ``schema``.
    """
    target = _bazaar_target(resource, tool_name)
    if target is None:
        return None
    kind, meta, name = target
    if kind == "rest":
        info_input: dict = {
            "type": "http",
            "method": "POST",
            "bodyType": meta["body_type"],
            "body": copy.deepcopy(meta["body_example"]),
        }
        input_schema_block: dict = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "http"},
                "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                "body": copy.deepcopy(meta["body_schema"]),
            },
            "required": ["type", "method", "bodyType", "body"],
            "additionalProperties": False,
        }
        output_example_schema: dict = {"type": "object"}
    else:
        info_input = {
            "type": "mcp",
            "toolName": name,
            "description": meta["description"],
            "transport": "streamable-http",
            "inputSchema": copy.deepcopy(meta["input_schema"]),
            "example": copy.deepcopy(meta["example_args"]),
        }
        input_schema_block = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "mcp"},
                "toolName": {"type": "string"},
                "inputSchema": {"type": "object"},
                "description": {"type": "string"},
                "transport": {"type": "string", "enum": ["streamable-http", "sse"]},
                "example": {"type": "object"},
            },
            "required": ["type", "toolName", "inputSchema"],
            "additionalProperties": False,
        }
        output_example_schema = {}
    return {
        "info": {
            "input": info_input,
            "output": {"type": "json", "example": copy.deepcopy(meta["output_example"])},
        },
        "schema": {
            "$schema": _BAZAAR_SCHEMA_URL,
            "type": "object",
            "properties": {
                "input": input_schema_block,
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": output_example_schema,
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }


def _bazaar_extension_for_requirements(requirements: dict) -> dict | None:
    """``extensions.bazaar`` for a challenge built from these requirements.

    The tool name for /mcp challenges is recovered from the tools/call
    envelope the requirements builder embedded in ``outputSchema`` — the
    requirements dict is the single carrier, so every challenge variant
    (payment_required / payment_invalid / free_tier_exhausted) is covered
    without threading new parameters through the response helpers.
    """
    tool_name: str | None = None
    output_schema = requirements.get("outputSchema")
    if isinstance(output_schema, dict):
        body = (output_schema.get("input") or {}).get("bodyFields")
        if isinstance(body, dict):
            params = body.get("params")
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                tool_name = params["name"]
    return _bazaar_extension(requirements.get("resource", ""), tool_name)


def _requirements_for_config(
    config: dict,
    price_usdc: Decimal,
    resource: str,
    description: str,
    tool_name: str | None = None,
) -> dict:
    requirements = {
        "scheme": SCHEME,
        "network": config["network_id"],
        "maxAmountRequired": str(usdc_to_atomic(price_usdc)),
        "resource": resource,
        "description": description,
        "payTo": config["pay_to"],
        "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
        "asset": config["asset"],
        # CDP facilitator schema-validates v1 requirements: mimeType is
        # REQUIRED (400 without it, verified live 2026-10-20); extra carries
        # the USDC EIP-712 domain on Base / the fee payer on Solana.
        "mimeType": "application/json",
        "extra": dict(config["extra"]),
    }
    # Bazaar discovery metadata (additive; see the bazaar block above).
    output_schema = _bazaar_output_schema(resource, tool_name)
    if output_schema is not None:
        requirements["outputSchema"] = output_schema
    return requirements


def build_payment_requirements(
    price_usdc: Decimal,
    resource: str,
    description: str,
    tool_name: str | None = None,
) -> dict:
    """Server-side payment requirements for the x402 v1 exact scheme (Base leg)."""
    return _requirements_for_config(_base_config(), price_usdc, resource, description, tool_name)


def build_requirements_list(
    price_usdc: Decimal,
    resource: str,
    description: str,
    tool_name: str | None = None,
) -> list[dict]:
    """One requirements entry per servable network leg (plan §4b.2)."""
    return [
        _requirements_for_config(config, price_usdc, resource, description, tool_name)
        for config in network_configs().values()
    ]


def decode_x_payment(raw: str) -> dict:
    """Decode the X-Payment header: base64 -> JSON v1 payment payload."""
    try:
        padded = raw.strip() + "=" * (-len(raw.strip()) % 4)
        data = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"X-Payment is not valid base64: {exc}") from exc
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"X-Payment is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("X-Payment payload must be a JSON object")
    return payload


def sanity_check_payload(payload: dict, requirements: dict, now: float | None = None) -> str | None:
    """Local sanity checks before calling the facilitator.

    Returns an error string when the payload is rejected, else None.
    """
    now = int(now if now is not None else time.time())
    if payload.get("x402Version") != X402_VERSION:
        return "unsupported x402Version"
    if payload.get("scheme") != SCHEME:
        return "unsupported scheme"
    if payload.get("network") != NETWORK:
        return "unsupported network"
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return "missing payload"
    auth = inner.get("authorization")
    if not isinstance(auth, dict):
        return "missing authorization"
    if str(auth.get("to", "")).lower() != requirements["payTo"].lower():
        return "authorization recipient mismatch"
    try:
        value = int(str(auth.get("value", "0")))
    except (TypeError, ValueError):
        return "invalid authorization value"
    if value < int(requirements["maxAmountRequired"]):
        return "authorization value below required amount"
    try:
        valid_after = int(str(auth.get("validAfter", "0")))
        valid_before = int(str(auth.get("validBefore", "0")))
    except (TypeError, ValueError):
        return "invalid authorization validity window"
    if not (valid_after <= now < valid_before):
        return "authorization outside validity window"
    # M-1: bound the authorization lifetime so the replay-cache TTL always
    # covers it. Without this, a payer could submit an authorization valid
    # for days while the seen-signature cache evicts after 300s, enabling
    # pay-once/replay-for-free (the facilitator only rejects the spent nonce
    # at /settle, which happens AFTER the response is served).
    window = valid_before - valid_after
    if window <= 0 or window > MAX_TIMEOUT_SECONDS:
        return "authorization validity window exceeds maximum"
    signature = inner.get("signature")
    if not isinstance(signature, str) or not signature.startswith("0x"):
        return "missing signature"
    # The asset is not client-asserted in v1: we set it in the requirements and
    # the facilitator validates the signature against the USDC contract.
    return None


def sanity_check_solana_payload(payload: dict, requirements: dict) -> str | None:
    """Local sanity checks for a Solana x402 payload (plan §3b).

    Deliberately light: the payload carries a partially-signed transaction,
    not EIP-3009 fields, and deserializing it locally would need a Solana
    codec (new dependency — deferred hardening follow-up). Recipient and
    amount validation is the facilitator's job here, same trust anchor as
    on Base; what we check locally is shape only.
    """
    if payload.get("x402Version") != X402_VERSION:
        return "unsupported x402Version"
    if payload.get("scheme") != SCHEME:
        return "unsupported scheme"
    if payload.get("network") != requirements["network"]:
        return "unsupported network"
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return "missing payload"
    transaction = inner.get("transaction")
    if not isinstance(transaction, str) or not transaction.strip():
        return "missing transaction"
    try:
        # Standard base64 per spec; tolerate the URL-safe alphabet too.
        raw = base64.b64decode(transaction.strip(), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return "invalid transaction encoding"
    if not raw:
        return "invalid transaction encoding"
    return None


def sanity_check_for_network(payload: dict, requirements: dict) -> str | None:
    """Dispatch the local sanity checks on the requirements' network."""
    if requirements["network"] == SOLANA_NETWORK:
        return sanity_check_solana_payload(payload, requirements)
    return sanity_check_payload(payload, requirements)


def replay_key_for(payload: dict, requirements: dict) -> str:
    """Anti-replay claim key: the 0x signature on EVM; on Solana the base64
    transaction string itself (the spec's recommended mitigation, §3b).
    The "solana:" prefix keeps the shared claim table collision-free."""
    inner = payload["payload"]
    if requirements["network"] == SOLANA_NETWORK:
        return "solana:" + inner["transaction"]
    return inner["signature"]


# ---------------------------------------------------------------------------
# Anti-replay: in-memory set of seen signatures with TTL.
# ---------------------------------------------------------------------------

_seen_signatures: dict[str, float] = {}
_seen_lock = threading.Lock()


def seen_replay(signature: str, ttl_seconds: int = MAX_TIMEOUT_SECONDS) -> bool:
    """True if this signature was already used (replay). Records new ones.

    M-1: claims persist in the billing SQLite DB — they survive restarts and
    are shared across workers, closing the in-memory cache's gaps (a deploy
    used to wipe the cache, and the 300s TTL was shorter than an unbounded
    authorization window). The claim is atomic, so concurrent duplicates
    across the facilitator-verify gap collapse to a single serve. Falls back
    to the in-memory dict when the DB is unreachable (availability first;
    still correct within this process).
    """
    try:
        from .billing import claim_x402_signature

        return claim_x402_signature(signature, time.time() + ttl_seconds)
    except Exception:  # noqa: BLE001 — DB failure must not break payments
        log.warning("x402 replay-cache DB unavailable; using in-memory fallback")
    now = time.time()
    with _seen_lock:
        for sig, expires in list(_seen_signatures.items()):
            if expires <= now:
                del _seen_signatures[sig]
        if signature in _seen_signatures:
            return True
        _seen_signatures[signature] = now + ttl_seconds
        return False


# ---------------------------------------------------------------------------
# CDP JWT minting (Coinbase CDP mainnet facilitator auth).
# ---------------------------------------------------------------------------


def _load_ed25519_private_key(key_secret: str):
    """Load an Ed25519 private key from PEM, raw base64, raw hex, or bytes."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    secret = key_secret.strip()
    if "BEGIN" in secret and "PRIVATE KEY" in secret:
        return serialization.load_pem_private_key(secret.encode(), password=None)
    raw: bytes
    if re.fullmatch(r"[0-9a-fA-F]{64}", secret):
        raw = bytes.fromhex(secret)
    else:
        padded = secret + "=" * (-len(secret) % 4)
        raw = base64.urlsafe_b64decode(padded)
    if len(raw) == 64:
        raw = raw[:32]  # CDP exports seed || pubkey (64B); from_private_bytes wants the 32B seed
    key = Ed25519PrivateKey.from_private_bytes(raw)
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_cdp_jwt(
    key_id: str, key_secret: str, now: int | None = None, uris: list[str] | None = None
) -> str:
    """Mint a CDP EdDSA JWT for facilitator auth.

    Claim set verified live against the CDP facilitator 2026-10-20: CDP
    requires the ``uris`` claim scoping the token to the exact endpoints
    (``POST <host><path>/verify`` etc.) — without it the API returns 401.
    """
    private_key = _load_ed25519_private_key(key_secret)
    now = now if now is not None else int(time.time())
    header = {
        "alg": "EdDSA",
        "typ": "JWT",
        "kid": key_id,
        "nonce": secrets.token_hex(16),
    }
    claims = {
        "sub": key_id,
        "iss": "coinbase-cloud",
        "nbf": now - 30,
        "exp": now + 120,
    }
    if uris:
        claims["uris"] = list(uris)
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    ).encode()
    signature = private_key.sign(signing_input)
    return signing_input.decode() + "." + _b64url(signature)


# ---------------------------------------------------------------------------
# Facilitator client (x402 v1 HTTP API).
# ---------------------------------------------------------------------------


class FacilitatorUnavailable(Exception):
    """The facilitator could not be reached or answered badly."""


def _cdp_uris() -> list[str]:
    """Endpoint URIs the CDP JWT is scoped to (CDP requires the uris claim)."""
    stripped = facilitator_url().split("://", 1)[-1].rstrip("/")
    return [f"POST {stripped}/verify", f"POST {stripped}/settle"]


def _facilitator_auth_headers() -> dict[str, str]:
    """Auth headers for the facilitator, env-driven and pluggable."""
    api_key = os.environ.get("X402_FACILITATOR_API_KEY", "").strip()
    if api_key:
        return {"X-API-Key": api_key}
    key_id = os.environ.get("CDP_API_KEY_ID", "").strip()
    key_secret = os.environ.get("CDP_API_KEY_SECRET", "").strip()
    if key_id and key_secret:
        return {"Authorization": f"Bearer {mint_cdp_jwt(key_id, key_secret, uris=_cdp_uris())}"}
    # No auth configured (works for https://x402.org/facilitator testnet).
    return {}


def _facilitator_url_for(requirements: dict) -> str:
    """Per-network facilitator routing (plan §4b.4): the Solana leg uses
    X402_FACILITATOR_URL_SOLANA when set, else the shared facilitator."""
    if requirements.get("network") == SOLANA_NETWORK:
        return solana_facilitator_url()
    return facilitator_url()


async def resolve_solana_fee_payer() -> str | None:
    """Best-effort feePayer discovery from GET {facilitator}/supported (§3b).

    Cached on success. Any failure returns None (the Solana leg then drops
    with an ERROR log) — discovery must never crash a request.
    """
    global _solana_fee_payer_cache
    known = solana_fee_payer()
    if known:
        return known
    url = solana_facilitator_url().rstrip("/") + "/supported"
    try:
        async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=_facilitator_auth_headers())
        data = resp.json()
    except Exception:
        log.warning("x402 solana fee payer discovery failed: %s unreachable", url)
        return None
    kinds = data.get("kinds") if isinstance(data, dict) else None
    if isinstance(kinds, list):
        for kind in kinds:
            if not isinstance(kind, dict):
                continue
            if kind.get("scheme") != SCHEME:
                continue
            network = str(kind.get("network", ""))
            if not network.startswith(SOLANA_NETWORK):
                continue
            extra = kind.get("extra") or {}
            fee_payer = extra.get("feePayer")
            if isinstance(fee_payer, str) and fee_payer:
                _solana_fee_payer_cache = fee_payer
                return fee_payer
    log.warning("x402 facilitator /supported lists no Solana fee payer")
    return None


async def facilitator_verify(payment_payload: dict, requirements: dict) -> dict:
    """POST {facilitator}/verify. Raises FacilitatorUnavailable on failure."""
    body = {
        "x402Version": X402_VERSION,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements,
    }
    try:
        async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _facilitator_url_for(requirements).rstrip("/") + "/verify",
                json=body,
                headers=_facilitator_auth_headers(),
            )
    except httpx.HTTPError as exc:
        raise FacilitatorUnavailable(f"facilitator /verify unreachable: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise FacilitatorUnavailable(f"facilitator /verify bad response: {exc}") from exc
    if not isinstance(data, dict) or "isValid" not in data:
        raise FacilitatorUnavailable("facilitator /verify malformed response")
    return data


async def facilitator_settle(payment_payload: dict, requirements: dict) -> dict:
    """POST {facilitator}/settle. Raises FacilitatorUnavailable on failure."""
    body = {
        "x402Version": X402_VERSION,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements,
    }
    try:
        async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _facilitator_url_for(requirements).rstrip("/") + "/settle",
                json=body,
                headers=_facilitator_auth_headers(),
            )
    except httpx.HTTPError as exc:
        raise FacilitatorUnavailable(f"facilitator /settle unreachable: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise FacilitatorUnavailable(f"facilitator /settle bad response: {exc}") from exc
    if not isinstance(data, dict):
        raise FacilitatorUnavailable("facilitator /settle malformed response")
    return data


def _settle_tx_hash(result: dict) -> str | None:
    for key in ("transaction", "txHash", "transactionHash", "hash"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# 402 challenge + middleware.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Canonical 402 challenge body.
# ---------------------------------------------------------------------------
# This exact shape is shared by all IOConnex MCP servers (search-mcp,
# doc-convert-mcp, price-tracker-mcp): the same ``error`` codes, the same
# ``accepts`` fields, and ``reason`` on every payment rejection. ``accepts``
# is derived from the x402 ``paymentRequirements`` so the two can never drift
# apart. Only OUR extension fields are normalized here — the
# ``paymentRequirements`` object itself stays x402-protocol-compliant.

CHALLENGE_MESSAGE = (
    "This endpoint requires an x402 USDC micropayment on Base. "
    "Send an X-Payment header with a base64-encoded signed EIP-3009 "
    "authorization (x402 v1 'exact' scheme)."
)

CHALLENGE_MESSAGE_MULTI = (
    "This endpoint requires an x402 USDC micropayment (Base or Solana). "
    "Send an X-Payment header with a base64-encoded x402 v1 'exact' "
    "scheme payment for one of the accepted networks."
)

FACILITATOR_DOWN_MESSAGE = "Payment verification is temporarily unavailable; try again."


def _accepts_entry(requirements: dict) -> dict:
    amount_atomic = int(requirements["maxAmountRequired"])
    return {
        "asset": "USDC",
        "asset_contract": requirements["asset"],
        "network": requirements["network"],
        "amount_usdc": str(Decimal(amount_atomic) / 10**USDC_DECIMALS),
        "amount_atomic": str(amount_atomic),
        "pay_to": requirements["payTo"],
        "scheme": requirements["scheme"],
    }


def challenge_content(
    error: str,
    requirements: dict | list[dict],
    reason: str | None = None,
    message: str | None = None,
) -> dict:
    """Build the canonical 402 body for an x402 payment rejection.

    ``error`` is one of ``payment_required`` / ``payment_invalid`` /
    ``duplicate_payment`` (or a repo-specific 402 code like
    ``free_tier_exhausted``). ``reason`` carries the human/machine-readable
    detail when known; ``message`` overrides the default challenge text when
    the caller has something more specific to say.

    ``requirements`` is a single requirements dict (the original shape,
    unchanged) or a list of per-network dicts. A one-entry list collapses
    to the single shape; with more than one network, ``accepts`` becomes
    an ARRAY of per-network options and ``paymentRequirements`` the
    matching array (plan §4b.3).
    """
    if isinstance(requirements, list):
        if len(requirements) == 1:
            requirements = requirements[0]
        else:
            content_multi: dict = {
                "error": error,
                "message": message or CHALLENGE_MESSAGE_MULTI,
                "accepts": [_accepts_entry(entry) for entry in requirements],
                # Real x402 clients need this to build a valid payment.
                "paymentRequirements": requirements,
            }
            # Bazaar discovery metadata (additive; one block describes the
            # resource — identical across network legs).
            extension_multi = _bazaar_extension_for_requirements(requirements[0])
            if extension_multi is not None:
                content_multi["extensions"] = {"bazaar": extension_multi}
            if reason:
                content_multi["reason"] = reason
            return content_multi
    content: dict = {
        "error": error,
        "message": message or CHALLENGE_MESSAGE,
        "accepts": _accepts_entry(requirements),
        # Real x402 clients need this to build a valid payment.
        "paymentRequirements": requirements,
    }
    # Bazaar discovery metadata (additive; see the bazaar block above).
    extension = _bazaar_extension_for_requirements(requirements)
    if extension is not None:
        content["extensions"] = {"bazaar": extension}
    if reason:
        content["reason"] = reason
    return content


def payment_required_response(
    requirements: dict | list[dict], reason: str | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content=challenge_content("payment_required", requirements, reason),
    )


def payment_invalid_response(requirements: dict | list[dict], reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content=challenge_content("payment_invalid", requirements, reason),
    )


def free_tier_exhausted_response(usage: dict, requirements: dict | list[dict]) -> JSONResponse:
    """HTTP 402 when the monthly free credit bucket is exhausted.

    Deliberately 402, NOT 429: 429 stays exclusive to the subscriber
    fair-use cap (``subscriber_monthly_cap_exceeded``). The body carries
    the same canonical x402 payment challenge so the caller can pay per
    call immediately, plus the free-tier usage fields.
    """
    content = challenge_content(
        "free_tier_exhausted",
        requirements,
        message=(
            f"Free tier exhausted: {usage['credits_used']} of {usage['free_limit']} "
            f"free credits used this month (resets {usage['resets_at']}). "
            "Pay per call with x402 or subscribe for $9/mo ($10 of calls included)."
        ),
    )
    content.update(
        {
            "credits_used": usage["credits_used"],
            "free_limit": usage["free_limit"],
            "resets_at": usage["resets_at"],
        }
    )
    return JSONResponse(status_code=402, content=content)


_CLOUDFLARE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # IPv4
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        # IPv6
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def _peer_is_cloudflare(peer: str) -> bool:
    """True when the direct TCP peer is a Cloudflare edge IP."""
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in _CLOUDFLARE_NETWORKS)


def client_ip(request: Request) -> str:
    """Client IP for the monthly free credit bucket, proxy-aware (M-3).

    Mirrors ``rest_app._client_ip_from_scope`` (kept in step deliberately —
    the middleware cannot import the app module). Render terminates TLS at
    its proxy and appends the original client address to
    ``X-Forwarded-For``; the first entry is trusted ONLY when the direct
    TCP peer is a non-global address (i.e. we really are behind the
    proxy). A client connecting directly always has a global peer address,
    so a spoofed XFF from a direct connection is ignored — the bucket
    cannot be reset at will by rotating the header. An unparseable peer
    (the TestClient's ``"testclient"``) means a test harness, where XFF is
    trusted as before.

    When the domain is proxied through Cloudflare, the direct TCP peer is
    a Cloudflare edge IP and ``CF-Connecting-IP`` carries the real client
    address — trusted ONLY for verifiably Cloudflare peers, so spoofing
    from any other peer is impossible. The bucket stores only the SHA-256
    of this value, never the raw IP.
    """
    peer = request.client.host if request.client else ""
    if _peer_is_cloudflare(peer):
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            try:
                ipaddress.ip_address(cf_ip)
                return cf_ip
            except ValueError:
                pass  # garbage header: fall through to existing logic
    xff = request.headers.get("x-forwarded-for", "")
    try:
        behind_proxy = not ipaddress.ip_address(peer).is_global
    except ValueError:
        behind_proxy = True  # test harness (e.g. TestClient): no real peer
    if behind_proxy and xff:
        first = xff.split(",")[0].strip()
        if first:
            try:
                ipaddress.ip_address(first)
                return first
            except ValueError:
                pass  # garbage XFF value: fall through to the peer
    return peer or "unknown"


async def _resolve_price(request: Request) -> Decimal | None:
    """Resolve the price for this request. None = exempt (serve without pay)."""
    path = request.url.path
    tool = REST_PRICE_MAP.get(path)
    if tool is not None:
        return price_for_tool_usdc(tool)
    if path == "/mcp" or path.startswith("/mcp/"):
        try:
            data = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Unparseable body -> default price, never a 500.
            return default_price_usdc()
        if not isinstance(data, dict):
            return default_price_usdc()
        method = data.get("method", "")
        if method == "tools/call":
            name = (data.get("params") or {}).get("name", "")
            return price_for_tool_usdc(str(name))
        if method in EXEMPT_JSONRPC_METHODS or str(method).startswith("notifications/"):
            return None
        return default_price_usdc()
    return default_price_usdc()


async def _mcp_tool_name(request: Request) -> str | None:
    """Tool name for an MCP tools/call POST (best-effort; None otherwise).

    Mirrors the body parse in ``_resolve_price``; used only to attach the
    right bazaar discovery metadata to the payment challenge.
    """
    try:
        data = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("method") != "tools/call":
        return None
    params = data.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return name if isinstance(name, str) else None


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Require a facilitator-verified X-Payment on paid paths when enabled."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN202
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in EXEMPT_PREFIXES):
            return await call_next(request)
        if path == "/billing" or path.startswith("/billing/"):
            # Stripe checkout/webhooks/status must work without x402:
            # Stripe's servers cannot attach an X-Payment header, and
            # charging per-call for checkout would be circular for new
            # subscribers. Subscriber keys bypass x402 below.
            return await call_next(request)
        if request.method == "GET" and path.startswith("/mcp"):
            # MCP discovery/info GETs stay free.
            return await call_next(request)
        if not x402_effective_enabled():
            return await call_next(request)
        if request.method != "POST":
            return await call_next(request)

        # Stripe subscriber bypass (invalid keys fall through to x402, not a
        # hard error). Valid subscriber keys spend credits from the monthly
        # fair-use cap — the credit cost derives from the same x402 price
        # map used below (free paths cost 0 and are never gated). x402
        # per-call payers are never capped.
        from .billing import check_subscriber_usage, credits_for_price_usdc

        sub_key = request.headers.get("x-subscriber-key", "").strip()
        if sub_key:
            price = await _resolve_price(request)
            credit_cost = 0.0 if price is None else credits_for_price_usdc(price)
            verdict = check_subscriber_usage(sub_key, credit_cost)
            if verdict is not None:
                if not verdict["ok"]:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "subscriber_monthly_cap_exceeded",
                            "message": (
                                f"Pro monthly fair-use credit cap reached "
                                f"({verdict['credits_used']} of "
                                f"{verdict['credit_limit']} credits used). "
                                f"Usage resets on {verdict['resets_at']}. "
                                "Per-call x402 payment still works for extra "
                                "usage: resend without X-Subscriber-Key and "
                                "include a signed X-Payment header."
                            ),
                            "credits_used": verdict["credits_used"],
                            "credit_limit": verdict["credit_limit"],
                            "resets_at": verdict["resets_at"],
                        },
                    )
                return await call_next(request)

        price = await _resolve_price(request)
        if price is None:
            return await call_next(request)

        # Solana fee payer discovery is lazy (plan §3b): resolve it before
        # building the challenge so the Solana leg can be offered at all.
        if SOLANA_NETWORK in requested_networks() and not solana_fee_payer():
            await resolve_solana_fee_payer()

        # Bazaar metadata for /mcp challenges is per-tool; resolve the
        # tools/call name (None for REST paths and non-call methods).
        tool_name = await _mcp_tool_name(request) if path.startswith("/mcp") else None
        requirements_list = build_requirements_list(
            price,
            resource=str(request.url),
            description=f"{REST_PRICE_MAP.get(path, 'mcp_tool_call')} on search-mcp",
            tool_name=tool_name,
        )
        if not requirements_list:
            # x402 is enabled but no leg is servable right now (e.g. the
            # Solana fee payer could not be resolved): fail closed rather
            # than serve a paid endpoint for free.
            log.error("x402 enabled but no servable network legs — failing closed")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "payment_verification_unavailable",
                    "message": FACILITATOR_DOWN_MESSAGE,
                },
            )
        requirements_by_network = {entry["network"]: entry for entry in requirements_list}

        raw = request.headers.get("x-payment")
        if not raw:
            # Free tier: anonymous callers draw from the monthly per-IP
            # credit bucket before the 402 challenge (see billing.py).
            # Denied/exhausted calls never consume credits; the deduction
            # happens here, before serving, exactly like the subscriber cap.
            from .billing import (
                credits_for_price_usdc,
                free_tier_enabled,
                record_free_tier_usage,
            )

            # Always-paid aliases never draw from the free bucket: without
            # a valid X-Payment they fall through to the 402 challenge
            # below, so directory checkers and router dispatch see a price.
            if free_tier_enabled() and not path.startswith(PAID_ROUTE_PREFIX):
                verdict = record_free_tier_usage(client_ip(request), credits_for_price_usdc(price))
                if verdict["allowed"]:
                    return await call_next(request)
                return free_tier_exhausted_response(verdict, requirements_list)
            return payment_required_response(requirements_list)
        try:
            payload = decode_x_payment(raw)
        except ValueError as exc:
            return payment_required_response(requirements_list, reason=str(exc))

        # Select the requirements for the network the client actually paid
        # on (plan §4b.4); an unknown network is rejected outright.
        network = payload.get("network")
        requirements = requirements_by_network.get(network) if isinstance(network, str) else None
        if requirements is None and network is None:
            # No network asserted: fall back to the Base requirements so the
            # EVM sanity checks report the mismatch, as before multi-network.
            requirements = requirements_by_network.get(NETWORK)
        if requirements is None:
            return payment_invalid_response(requirements_list, "unsupported network")

        reason = sanity_check_for_network(payload, requirements)
        if reason:
            return payment_required_response(requirements_list, reason=reason)

        if seen_replay(replay_key_for(payload, requirements)):
            return JSONResponse(
                status_code=402,
                content=challenge_content(
                    "duplicate_payment", requirements_list, "payment already used"
                ),
            )

        try:
            verification = await facilitator_verify(payload, requirements)
        except FacilitatorUnavailable as exc:
            # Fail closed: never serve paid content without verification.
            log.error("x402 facilitator unavailable: %s", exc)
            return JSONResponse(
                status_code=503,
                content={
                    "error": "payment_verification_unavailable",
                    "message": FACILITATOR_DOWN_MESSAGE,
                },
            )
        if not verification.get("isValid"):
            return payment_invalid_response(
                requirements,
                str(verification.get("invalidReason") or "facilitator rejected payment"),
            )

        response = await call_next(request)

        # Settle after serving. A settle failure is logged loudly (money may
        # be lost) but must NOT fail the already-served request.
        try:
            settled = await facilitator_settle(payload, requirements)
            tx_hash = _settle_tx_hash(settled)
            if settled.get("success") and tx_hash:
                log.info(
                    "x402 settled: tx=%s payer=%s amount_atomic=%s",
                    tx_hash,
                    verification.get("payer"),
                    requirements["maxAmountRequired"],
                )
            else:
                log.error(
                    "x402 settle FAILED (payment served but may not be collected): %s",
                    settled.get("errorReason") or settled,
                )
        except Exception:  # noqa: BLE001
            log.exception("x402 settle FAILED (payment served but may not be collected)")
        return response


# ---------------------------------------------------------------------------
# x402 discovery manifest (served at /.well-known/x402.json).
# ---------------------------------------------------------------------------

# Honest status strings for the discovery manifest. NEVER advertise live or
# accepting payments unless x402_effective_enabled() is True.
DISCOVERY_STATUS_ENABLED = "enabled"
DISCOVERY_STATUS_DISABLED = "implemented_but_disabled"
DISCOVERY_NOTE_DISABLED = (
    "x402 payments are implemented but DISABLED in production. "
    "They go live after business entity setup; no payments are accepted at "
    "this time."
)
DISCOVERY_NOTE_ENABLED = "x402 payments are LIVE: per-call USDC micropayments are accepted."


def discovery_manifest() -> dict:
    """Build the public x402 discovery manifest.

    Derived entirely from this module's constants — the single source of
    truth for prices (PER_TOOL_DEFAULTS_USDC / DEFAULT_PRICE_USDC) and
    asset/network identifiers (NETWORK, USDC_*). Nothing is hardcoded here.
    ``pay_to`` is null unless X402_PAY_TO is set — we never invent an address.

    ``networks`` lists one entry per network named by X402_NETWORKS with
    that leg's asset and payTo (null when unset, same honesty rule). The
    top-level fields keep describing the primary (Base) leg, unchanged.
    """
    enabled = x402_effective_enabled()
    pay_to = x402_pay_to() or None
    networks: list[dict] = []
    for name in requested_networks():
        if name == NETWORK:
            networks.append(
                {
                    "network": NETWORK,
                    "asset": "USDC",
                    "asset_contract": USDC_BASE_ADDRESS,
                    "asset_decimals": USDC_DECIMALS,
                    "pay_to": pay_to,
                }
            )
        elif name == SOLANA_NETWORK:
            networks.append(
                {
                    "network": SOLANA_NETWORK,
                    "asset": "USDC",
                    "asset_contract": x402_asset_solana(),
                    "asset_decimals": USDC_DECIMALS,
                    "pay_to": x402_pay_to_solana() or None,
                }
            )
    return {
        "x402_status": (DISCOVERY_STATUS_ENABLED if enabled else DISCOVERY_STATUS_DISABLED),
        "note": DISCOVERY_NOTE_ENABLED if enabled else DISCOVERY_NOTE_DISABLED,
        "protocol": "x402",
        "version": X402_VERSION,
        "scheme": SCHEME,
        "network": NETWORK,
        "asset": "USDC",
        "asset_contract": USDC_BASE_ADDRESS,
        "asset_decimals": USDC_DECIMALS,
        "pay_to": pay_to,
        "networks": networks,
        "prices_usdc": {tool: str(price) for tool, price in PER_TOOL_DEFAULTS_USDC.items()},
        "default_price_usdc": str(DEFAULT_PRICE_USDC),
        "facilitator_default": DEFAULT_FACILITATOR_URL,
        "facilitator_note": (
            "Default facilitator serves Base Sepolia testnet only; "
            "Base mainnet needs the CDP facilitator URL + keys."
        ),
        "free_endpoints": [
            "/health",
            "/.well-known/x402",
            "/.well-known/x402.json",
            "/legal/terms",
            "/legal/privacy",
            "/legal/refunds",
        ],
        "documentation": "/docs",
    }


def warn_if_payments_live() -> None:
    """Startup warnings: loud when real money is on, errors when misconfigured."""
    if x402_enabled_flag() and not x402_effective_enabled():
        log.error(
            "X402_ENABLED=true but X402_PAY_TO/X402_FACILITATOR_URL are incomplete — "
            "x402 metering DISABLED (fail closed). Set all three to enable."
        )
    if x402_effective_enabled():
        servable = valid_networks()
        if NETWORK in servable:
            if facilitator_url() == DEFAULT_FACILITATOR_URL:
                log.warning(
                    "X402 payments LIVE against %s — Base Sepolia TESTNET ONLY. "
                    "Mainnet needs X402_FACILITATOR_URL=%s + CDP keys.",
                    DEFAULT_FACILITATOR_URL,
                    CDP_FACILITATOR_URL,
                )
            else:
                log.warning(
                    "X402 payments LIVE — real USDC charges on Base. Confirm AZ LLC is formed."
                )
        if SOLANA_NETWORK in servable:
            if solana_facilitator_url() == DEFAULT_FACILITATOR_URL:
                log.warning(
                    "X402 Solana leg LIVE against %s — that default facilitator is "
                    "Base Sepolia only; Solana needs the CDP facilitator + keys.",
                    DEFAULT_FACILITATOR_URL,
                )
            else:
                log.warning(
                    "X402 payments LIVE — real USDC charges on Solana. Confirm AZ LLC is formed."
                )
    from .billing import stripe_effective_enabled, stripe_enabled_flag

    if stripe_enabled_flag() and not stripe_effective_enabled():
        log.error(
            "STRIPE_ENABLED=true but STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET/"
            "STRIPE_PRICE_ID incomplete — Stripe billing DISABLED (fail closed)."
        )
    if stripe_effective_enabled():
        log.warning("Stripe billing LIVE — checkout/webhooks enabled.")
