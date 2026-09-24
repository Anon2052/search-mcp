"""x402 (USDC on Base) metering middleware with real facilitator verification.

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
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("search_mcp.x402")

# Circle's canonical USDC on Base (6 decimals).
USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA38bD"
USDC_DECIMALS = 6
NETWORK = "base"
SCHEME = "exact"
X402_VERSION = 1
MAX_TIMEOUT_SECONDS = 300

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
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/legal",
    "/.well-known",
)

# REST route -> tool name (per-repo map).
REST_PRICE_MAP = {
    "/search": "search_web",
    "/fetch": "fetch_url",
}

# Proposed per-tool price defaults (USDC). Business decision — Scott's call.
# Overridable per tool with X402_PRICE_<TOOLNAME>_USDC env vars.
PER_TOOL_DEFAULTS_USDC = {
    "search_web": Decimal("0.025"),
    "fetch_url": Decimal("0.01"),
}

# Fallback for anything paid that doesn't map to a known tool.
DEFAULT_PRICE_USDC = Decimal("0.015")

# JSON-RPC methods that stay free: discovery must work so directory scanners
# (Smithery et al.) keep functioning.
EXEMPT_JSONRPC_METHODS = {"initialize", "tools/list", "ping"}


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def x402_enabled_flag() -> bool:
    return _env_flag("X402_ENABLED")


def x402_pay_to() -> str:
    return os.environ.get("X402_PAY_TO", "").strip()


def facilitator_url() -> str:
    return os.environ.get("X402_FACILITATOR_URL", DEFAULT_FACILITATOR_URL).strip()


def x402_effective_enabled() -> bool:
    """True only when x402 is fully configured. Fail closed otherwise."""
    if not x402_enabled_flag():
        return False
    if not x402_pay_to() or not facilitator_url():
        log.error(
            "X402_ENABLED=true but X402_PAY_TO/X402_FACILITATOR_URL incomplete — "
            "x402 metering DISABLED (fail closed)."
        )
        return False
    return True


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


def build_payment_requirements(price_usdc: Decimal, resource: str, description: str) -> dict:
    """Server-side payment requirements for the x402 v1 exact scheme."""
    return {
        "scheme": SCHEME,
        "network": NETWORK,
        "maxAmountRequired": str(usdc_to_atomic(price_usdc)),
        "resource": resource,
        "description": description,
        "payTo": x402_pay_to(),
        "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
        "asset": USDC_BASE_ADDRESS,
    }


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
    signature = inner.get("signature")
    if not isinstance(signature, str) or not signature.startswith("0x"):
        return "missing signature"
    # The asset is not client-asserted in v1: we set it in the requirements and
    # the facilitator validates the signature against the USDC contract.
    return None


# ---------------------------------------------------------------------------
# Anti-replay: in-memory set of seen signatures with TTL.
# ---------------------------------------------------------------------------

_seen_signatures: dict[str, float] = {}
_seen_lock = threading.Lock()


def seen_replay(signature: str, ttl_seconds: int = MAX_TIMEOUT_SECONDS) -> bool:
    """True if this signature was already used (replay). Records new ones."""
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
    key = Ed25519PrivateKey.from_private_bytes(raw)
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_cdp_jwt(key_id: str, key_secret: str, now: int | None = None) -> str:
    """Mint a CDP EdDSA JWT for facilitator auth.

    NOTE: the exact claim set needs a live smoke test with Scott's CDP keys
    before mainnet enablement — mint against the CDP facilitator and confirm
    it accepts the token.
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


def _facilitator_auth_headers() -> dict[str, str]:
    """Auth headers for the facilitator, env-driven and pluggable."""
    api_key = os.environ.get("X402_FACILITATOR_API_KEY", "").strip()
    if api_key:
        return {"X-API-Key": api_key}
    key_id = os.environ.get("CDP_API_KEY_ID", "").strip()
    key_secret = os.environ.get("CDP_API_KEY_SECRET", "").strip()
    if key_id and key_secret:
        return {"Authorization": f"Bearer {mint_cdp_jwt(key_id, key_secret)}"}
    # No auth configured (works for https://x402.org/facilitator testnet).
    return {}


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
                facilitator_url().rstrip("/") + "/verify",
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
                facilitator_url().rstrip("/") + "/settle",
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


def payment_required_response(requirements: dict, reason: str | None = None) -> JSONResponse:
    content: dict = {
        "error": "payment_required",
        "message": (
            "This endpoint requires an x402 USDC micropayment on Base. "
            "Send an X-Payment header with a base64-encoded signed EIP-3009 "
            "authorization (x402 v1 'exact' scheme)."
        ),
        "accepts": {
            "asset": "USDC",
            "asset_contract": requirements["asset"],
            "network": NETWORK,
            "amount_usdc": str(Decimal(requirements["maxAmountRequired"]) / 10**USDC_DECIMALS),
            "amount_atomic": requirements["maxAmountRequired"],
            "pay_to": requirements["payTo"],
            "scheme": "exact",
        },
        # Real x402 clients need this to build a valid payment.
        "paymentRequirements": requirements,
    }
    if reason:
        content["reason"] = reason
    return JSONResponse(status_code=402, content=content)


def payment_invalid_response(reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content={"error": "payment_invalid", "reason": reason},
    )


def free_tier_exhausted_response(verdict: dict) -> JSONResponse:
    """402 for an exhausted anonymous free-tier bucket. Deliberately NOT a
    429: 429 is reserved exclusively for the subscriber monthly cap."""
    return JSONResponse(
        status_code=402,
        content={
            "error": "free_tier_exhausted",
            "message": (
                f"Free tier exhausted ({verdict['credits_used']} of "
                f"{verdict['free_limit']} credits used this month). "
                "Pay per call with x402 (resend with a signed X-Payment header) "
                "or subscribe to Pro ($9/mo includes $10 of calls) for a higher limit."
            ),
            "credits_used": verdict["credits_used"],
            "free_limit": verdict["free_limit"],
            "resets_at": verdict["resets_at"],
            "upgrade_hint": (
                "Pay per call via x402, or subscribe to Pro at $9/mo "
                "(includes $10 of calls) for a higher monthly limit."
            ),
        },
    )


async def _paid_call_info(request: Request) -> tuple[Decimal | None, str]:
    """(price, tool label) for this request.

    price None = exempt/free method (serve without pay). The tool label feeds
    usage analytics. Unparseable bodies get the default price, never a 500.
    """
    path = request.url.path
    tool = REST_PRICE_MAP.get(path)
    if tool is not None:
        return price_for_tool_usdc(tool), tool
    if path == "/mcp" or path.startswith("/mcp/"):
        try:
            data = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return default_price_usdc(), "mcp_unknown"
        if not isinstance(data, dict):
            return default_price_usdc(), "mcp_unknown"
        method = data.get("method", "")
        if method == "tools/call":
            name = str((data.get("params") or {}).get("name", "") or "unknown_tool")
            return price_for_tool_usdc(name), name
        if method in EXEMPT_JSONRPC_METHODS or str(method).startswith("notifications/"):
            return None, str(method)
        return default_price_usdc(), f"mcp:{method}"
    return default_price_usdc(), "unknown"


async def _resolve_price(request: Request) -> Decimal | None:
    """Resolve the price for this request. None = exempt (serve without pay)."""
    price, _ = await _paid_call_info(request)
    return price


def client_ip(request: Request) -> str:
    """Best-effort client IP, Render-aware: first entry of X-Forwarded-For,
    else the direct peer. Used only to key the anonymous free-tier bucket
    (stored hashed); never logged or stored raw."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    peer = request.client
    return peer.host if peer else "unknown"


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Require a facilitator-verified X-Payment on paid paths when enabled."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN202
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in EXEMPT_PREFIXES):
            return await call_next(request)
        if path == "/billing" or path.startswith("/billing/"):
            # Stripe checkout/webhooks/status/stats must work without x402:
            # Stripe's servers cannot attach an X-Payment header, and
            # charging per-call for checkout would be circular for new
            # subscribers. Subscriber keys bypass x402 below. /billing/stats
            # is admin-authenticated and never consumes quota.
            return await call_next(request)
        if request.method == "GET" and path.startswith("/mcp"):
            # MCP discovery/info GETs stay free.
            return await call_next(request)
        if not x402_effective_enabled():
            # Metering off: open access. Still rolled into usage analytics as
            # free/zero-credit so pre-launch traffic stays visible.
            if request.method == "POST":
                from .billing import record_usage

                price, tool = await _paid_call_info(request)
                if price is not None:
                    record_usage(tool, "free", 0.0)
            return await call_next(request)
        if request.method != "POST":
            return await call_next(request)

        # Stripe subscriber bypass (invalid keys fall through to the x402 /
        # free-tier flow, not a hard error). Valid subscriber keys spend
        # credits from the monthly fair-use cap — the credit cost derives from
        # the same x402 price map used below (free paths cost 0 and are never
        # gated). x402 per-call payers are never capped. The 429 code and body
        # below are UNTOUCHED.
        from .billing import (
            check_free_tier_usage,
            check_subscriber_usage,
            credits_for_price_usdc,
            free_tier_enabled_flag,
            record_usage,
        )

        price, tool = await _paid_call_info(request)
        credit_cost = 0.0 if price is None else credits_for_price_usdc(price)

        sub_key = request.headers.get("x-subscriber-key", "").strip()
        if sub_key:
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
                record_usage(tool, "subscriber", credit_cost)
                return await call_next(request)

        if price is None:
            # Free method (discovery etc.): serve without pay, no bucket.
            return await call_next(request)

        requirements = build_payment_requirements(
            price,
            resource=str(request.url),
            description=f"{REST_PRICE_MAP.get(path, 'mcp_tool_call')} on search-mcp",
        )

        raw = request.headers.get("x-payment")
        if raw:
            # x402-paid call: exempt from the free bucket, never capped.
            try:
                payload = decode_x_payment(raw)
            except ValueError as exc:
                return payment_required_response(requirements, reason=str(exc))
            reason = sanity_check_payload(payload, requirements)
            if reason:
                return payment_required_response(requirements, reason=reason)

            signature = payload["payload"]["signature"]
            if seen_replay(signature):
                return JSONResponse(
                    status_code=402,
                    content={"error": "duplicate_payment", "reason": "payment already used"},
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
                        "message": "Payment verification is temporarily unavailable.",
                    },
                )
            if not verification.get("isValid"):
                return payment_invalid_response(
                    str(verification.get("invalidReason") or "facilitator rejected payment")
                )

            response = await call_next(request)
            record_usage(tool, "x402", credit_cost)

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

        # No subscriber key, no payment: the anonymous free tier (on by
        # default; FREE_TIER_ENABLED=false skips it entirely and falls
        # straight to the 402 challenge). Exhausted bucket -> 402 with
        # free_tier_exhausted (NOT 429 — 429 is the subscriber cap only).
        # Denied calls never consume credits.
        if not free_tier_enabled_flag():
            return payment_required_response(requirements)
        free_verdict = check_free_tier_usage(client_ip(request), credit_cost)
        if not free_verdict["ok"]:
            return free_tier_exhausted_response(free_verdict)
        record_usage(tool, "free", credit_cost)
        return await call_next(request)


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
    """
    enabled = x402_effective_enabled()
    pay_to = x402_pay_to() or None
    return {
        "x402_status": DISCOVERY_STATUS_ENABLED if enabled else DISCOVERY_STATUS_DISABLED,
        "note": DISCOVERY_NOTE_ENABLED if enabled else DISCOVERY_NOTE_DISABLED,
        "protocol": "x402",
        "version": X402_VERSION,
        "scheme": SCHEME,
        "network": NETWORK,
        "asset": "USDC",
        "asset_contract": USDC_BASE_ADDRESS,
        "asset_decimals": USDC_DECIMALS,
        "pay_to": pay_to,
        "prices_usdc": {tool: str(price) for tool, price in PER_TOOL_DEFAULTS_USDC.items()},
        "default_price_usdc": str(DEFAULT_PRICE_USDC),
        "facilitator_default": DEFAULT_FACILITATOR_URL,
        "facilitator_note": (
            "Default facilitator serves Base Sepolia testnet only; "
            "Base mainnet needs the CDP facilitator URL + keys."
        ),
        "free_endpoints": [
            "/health",
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
        if facilitator_url() == DEFAULT_FACILITATOR_URL:
            log.warning(
                "X402 payments LIVE against %s — Base Sepolia TESTNET ONLY. "
                "Mainnet needs X402_FACILITATOR_URL=%s + CDP keys.",
                DEFAULT_FACILITATOR_URL,
                CDP_FACILITATOR_URL,
            )
        else:
            log.warning("X402 payments LIVE — real USDC charges on Base. Confirm AZ LLC is formed.")
    from .billing import stripe_effective_enabled, stripe_enabled_flag

    if stripe_enabled_flag() and not stripe_effective_enabled():
        log.error(
            "STRIPE_ENABLED=true but STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET/"
            "STRIPE_PRICE_ID incomplete — Stripe billing DISABLED (fail closed)."
        )
    if stripe_effective_enabled():
        log.warning("Stripe billing LIVE — checkout/webhooks enabled.")
