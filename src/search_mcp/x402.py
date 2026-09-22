"""x402 (USDC on Base) metering middleware.

STUB — structural validation only. This module implements the HTTP plumbing for
x402 per-request micropayments (402 responses, X-Payment header parsing, price
configuration) but does NOT verify payments on-chain. Before enabling
``X402_ENABLED=true`` in production, ``verify_payment_stub`` must be replaced
with real verification against an x402 facilitator (EIP-3009 authorization
validation on Base). The server logs a loud warning at startup while the stub
is active.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, InvalidOperation

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("search_mcp.x402")

ASSET = "USDC"
NETWORK = "base"

# Paths that never require payment (health checks, docs).
EXEMPT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def x402_enabled() -> bool:
    return _env_flag("X402_ENABLED")


def x402_price_usdc() -> Decimal:
    try:
        return Decimal(os.environ.get("X402_PRICE_USDC", "0.015"))
    except InvalidOperation:
        return Decimal("0.015")


def x402_pay_to() -> str:
    return os.environ.get("X402_PAY_TO", "").strip()


def verify_payment_stub(payment: dict, price: Decimal, pay_to: str) -> bool:
    """Structural validation of an X-Payment payload.

    Returns True only if the payload is well-formed AND names the right asset,
    network, recipient, and a sufficient amount. This is NOT on-chain
    verification — replace with facilitator-backed verification before
    production use.
    """
    try:
        if payment.get("asset") != ASSET:
            return False
        if payment.get("network") != NETWORK:
            return False
        if not pay_to or str(payment.get("pay_to", "")).lower() != pay_to.lower():
            return False
        amount = Decimal(str(payment.get("amount", "0")))
        if amount < price:
            return False
        if not str(payment.get("payer", "")).startswith("0x"):
            return False
        if not str(payment.get("signature", "")).startswith("0x"):
            return False
        return True
    except (InvalidOperation, AttributeError, TypeError):
        return False


def payment_required_response(price: Decimal, pay_to: str) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content={
            "error": "payment_required",
            "message": (
                "This endpoint requires an x402 USDC micropayment. "
                "Send an X-Payment header with a signed EIP-3009 authorization."
            ),
            "accepts": {
                "asset": ASSET,
                "network": NETWORK,
                "amount": str(price),
                "pay_to": pay_to or "<set X402_PAY_TO>",
                "scheme": "x402-stub",
            },
            "note": (
                "STUB: payment verification is structural only. "
                "Do not enable in production without facilitator-backed verification."
            ),
        },
    )


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Require an X-Payment header on paid paths when X402_ENABLED=true."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in EXEMPT_PREFIXES):
            return await call_next(request)
        if path.startswith("/mcp") and request.method == "GET":
            # Allow MCP discovery/info GETs; tool calls are POST.
            return await call_next(request)
        if not x402_enabled():
            return await call_next(request)

        raw = request.headers.get("x-payment")
        if not raw:
            return payment_required_response(x402_price_usdc(), x402_pay_to())
        try:
            payment = json.loads(raw)
        except json.JSONDecodeError:
            return payment_required_response(x402_price_usdc(), x402_pay_to())
        if not isinstance(payment, dict) or not verify_payment_stub(
            payment, x402_price_usdc(), x402_pay_to()
        ):
            return payment_required_response(x402_price_usdc(), x402_pay_to())
        return await call_next(request)


def warn_if_stub_enabled() -> None:
    if x402_enabled():
        log.warning(
            "X402_ENABLED=true but payment verification is a STUB "
            "(structural checks only, no on-chain verification). "
            "Replace verify_payment_stub before production use."
        )
