"""Stripe billing: monthly subscriber plans that bypass per-call x402.

Model: pay-as-you-go via x402 stays for non-subscribers; a Stripe monthly
subscription ("Pro Monthly", price created by Scott in the Stripe dashboard)
issues a subscriber key that bypasses per-call charges (fair use).

Env:
    STRIPE_ENABLED (default "false")
    STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_ID (required)
    STRIPE_SUCCESS_URL, STRIPE_CANCEL_URL (required for checkout)
    BILLING_DB_PATH (default "/var/lib/search-mcp/billing.db" — the Render
        persistent-disk mount; parent dir is created). Override locally with
        e.g. "./data/subscribers.db" when developing outside Render.
    SUBSCRIBER_MONTHLY_CALL_LIMIT (default "1000"): fair-use cap on calls
        authenticated with X-Subscriber-Key, per calendar month (UTC).
        x402 per-call payers are not capped.

Subscriber storage: stdlib sqlite3. Only the SHA-256 hash of a subscriber
key is stored — never the raw token. All endpoints 404 unless Stripe is
effectively enabled (fail closed).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import time

import stripe
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("search_mcp.billing")

router = APIRouter(prefix="/billing")

# Idempotent: CREATE TABLE IF NOT EXISTS runs on every _db() open, so the
# first boot on a fresh Render disk works without a migration step.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    key_hash   TEXT PRIMARY KEY,
    customer_id TEXT UNIQUE,
    email      TEXT,
    status     TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS subscriber_usage (
    key_hash   TEXT PRIMARY KEY,
    month      TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
"""

# Default lives on the Render persistent-disk mount (see render.yaml `disk:`).
# A redeploy would wipe ./data on Render's ephemeral filesystem.
_DISK_DB_PATH = "/var/lib/search-mcp/billing.db"

# Fair-use cap for Pro subscribers: at ~$0.02 avg per-call price, $9/mo
# breaks even around 450 calls; 1000 gives headroom. Scott's call to change.
_DEFAULT_MONTHLY_CALL_LIMIT = 1000

_PLACEHOLDER_HASH = "pending:" + "0" * 56  # replaced by GET /billing/key


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def stripe_enabled_flag() -> bool:
    return _env_flag("STRIPE_ENABLED")


def stripe_effective_enabled() -> bool:
    """True only when Stripe is fully configured. Fail closed otherwise."""
    if not stripe_enabled_flag():
        return False
    return all(
        os.environ.get(name, "").strip()
        for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID")
    )


def _db_path() -> str:
    return os.environ.get("BILLING_DB_PATH", _DISK_DB_PATH)


def _db() -> sqlite3.Connection:
    path = _db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_subscriber_token() -> str:
    return secrets.token_urlsafe(32)


def upsert_subscriber_placeholder(customer_id: str, email: str | None) -> None:
    """Create/refresh a subscriber row (webhook path). The real key is
    minted on GET /billing/key; until then the placeholder cannot bypass."""
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO subscribers (key_hash, customer_id, email, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            ON CONFLICT(customer_id) DO UPDATE SET
                email=excluded.email, status='active', updated_at=excluded.updated_at
            """,
            (_PLACEHOLDER_HASH, customer_id, email, _now_iso(), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def rotate_subscriber_key(customer_id: str, email: str | None = None) -> str | None:
    """Mint a fresh subscriber token for a customer; store only its hash.

    Returns the raw token (shown to the user exactly once) or None when the
    customer has no active subscription.
    """
    conn = _db()
    try:
        row = conn.execute(
            "SELECT status, email FROM subscribers WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        if row is None or row["status"] != "active":
            return None
        token = mint_subscriber_token()
        conn.execute(
            """
            UPDATE subscribers SET key_hash = ?, email = COALESCE(?, email),
                status = 'active', updated_at = ?
            WHERE customer_id = ?
            """,
            (_hash_key(token), email or row["email"], _now_iso(), customer_id),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def set_subscriber_status(customer_id: str, status: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE subscribers SET status = ?, updated_at = ? WHERE customer_id = ?",
            (status, _now_iso(), customer_id),
        )
        conn.commit()
    finally:
        conn.close()


def subscriber_key_valid(token: str) -> bool:
    """Check an X-Subscriber-Key. Invalid/unknown keys -> False (the caller
    falls through to the normal x402 flow, not a hard error)."""
    if not token:
        return False
    conn = _db()
    try:
        row = conn.execute(
            "SELECT 1 FROM subscribers WHERE key_hash = ? AND status = 'active'",
            (_hash_key(token),),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _current_month_utc() -> str:
    """Current calendar month as "YYYY-MM" (UTC). Small and patchable so
    tests can simulate the month boundary."""
    return time.strftime("%Y-%m", time.gmtime())


def _monthly_call_limit() -> int:
    raw = os.environ.get("SUBSCRIBER_MONTHLY_CALL_LIMIT", "").strip()
    if not raw:
        return _DEFAULT_MONTHLY_CALL_LIMIT
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "invalid SUBSCRIBER_MONTHLY_CALL_LIMIT=%r; using default %d",
            raw,
            _DEFAULT_MONTHLY_CALL_LIMIT,
        )
        return _DEFAULT_MONTHLY_CALL_LIMIT


def _next_month_start_iso() -> str:
    """First instant of next month, UTC, ISO-8601 — derived from the
    patchable _current_month_utc() so tests control it."""
    year, month = (int(part) for part in _current_month_utc().split("-"))
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return f"{year:04d}-{month:02d}-01T00:00:00Z"


def check_subscriber_usage(token: str) -> dict | None:
    """Validate a subscriber key and record one call against the monthly
    fair-use cap.

    Returns None when the key is invalid/unknown (the caller falls through
    to the normal x402 flow, not a hard error). Otherwise returns a verdict
    dict:
        {"ok": True,  "limit": N, "used": n}                       (under cap; call counted)
        {"ok": False, "limit": N, "used": n, "resets_at": "..."}   (cap hit; NOT counted)
    The counter resets on the calendar-month boundary (UTC). Cap applies to
    subscriber-key auth only — x402 per-call payers never touch this.
    """
    if not token:
        return None
    key_hash = _hash_key(token)
    month = _current_month_utc()
    limit = _monthly_call_limit()
    conn = _db()
    try:
        sub = conn.execute(
            "SELECT 1 FROM subscribers WHERE key_hash = ? AND status = 'active'",
            (key_hash,),
        ).fetchone()
        if sub is None:
            return None
        row = conn.execute(
            "SELECT month, calls FROM subscriber_usage WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        used = row["calls"] if row is not None and row["month"] == month else 0
        if used >= limit:
            return {"ok": False, "limit": limit, "used": used, "resets_at": _next_month_start_iso()}
        # Atomic upsert: reset the counter when the stored month is stale.
        # In ON CONFLICT DO UPDATE, right-hand sides see the OLD row, so the
        # CASE compares the stored month against the incoming one.
        conn.execute(
            """
            INSERT INTO subscriber_usage (key_hash, month, calls, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(key_hash) DO UPDATE SET
                month = excluded.month,
                calls = CASE
                    WHEN subscriber_usage.month = excluded.month
                    THEN subscriber_usage.calls + 1
                    ELSE 1
                END,
                updated_at = excluded.updated_at
            """,
            (key_hash, month, _now_iso()),
        )
        conn.commit()
        return {"ok": True, "limit": limit, "used": used + 1}
    finally:
        conn.close()


def _ev(obj, key, default=None):
    """Read a field from a Stripe API object or a plain dict.

    stripe-python v12+ resources are not dicts (``.get()`` raises), so this
    tries mapping access first, then attribute access.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except (KeyError, TypeError, IndexError):
        return getattr(obj, key, default)


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found"})


@router.post("/checkout")
async def checkout() -> JSONResponse:
    """Create a Stripe Checkout Session for the monthly plan."""
    if not stripe_effective_enabled():
        return _not_found()
    success_url = os.environ.get("STRIPE_SUCCESS_URL", "").strip()
    cancel_url = os.environ.get("STRIPE_CANCEL_URL", "").strip()
    if not success_url or not cancel_url:
        log.error("STRIPE_SUCCESS_URL/STRIPE_CANCEL_URL not configured — checkout refused.")
        return JSONResponse(status_code=500, content={"error": "billing_misconfigured"})
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": os.environ["STRIPE_PRICE_ID"], "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"service": "search-mcp"},
        )
    except Exception:  # noqa: BLE001
        log.exception("Stripe checkout session creation failed")
        return JSONResponse(status_code=502, content={"error": "checkout_failed"})
    return JSONResponse(status_code=200, content={"checkout_url": _ev(session, "url")})


@router.post("/stripe-webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    if not stripe_effective_enabled():
        return _not_found()
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, os.environ["STRIPE_WEBHOOK_SECRET"])
    except Exception:  # noqa: BLE001
        log.warning("Stripe webhook signature verification failed")
        return JSONResponse(status_code=400, content={"error": "invalid_signature"})

    etype = _ev(event, "type", "")
    obj = _ev(_ev(event, "data"), "object") or {}
    try:
        if etype == "checkout.session.completed":
            customer_id = _ev(obj, "customer")
            details = _ev(obj, "customer_details") or {}
            email = _ev(details, "email") or _ev(obj, "customer_email")
            if customer_id:
                upsert_subscriber_placeholder(str(customer_id), email)
                log.info("subscriber activated (checkout completed)")
            else:
                log.warning("checkout.session.completed without customer id")
        elif etype == "customer.subscription.deleted":
            customer_id = _ev(obj, "customer")
            if customer_id:
                set_subscriber_status(str(customer_id), "canceled")
                log.info("subscriber deactivated (subscription deleted)")
        elif etype == "customer.subscription.updated":
            customer_id = _ev(obj, "customer")
            status = _ev(obj, "status")
            if customer_id and status:
                set_subscriber_status(str(customer_id), str(status))
                log.info("subscriber status synced: %s", status)
    except Exception:  # noqa: BLE001
        log.exception("Stripe webhook handler failed for %s", etype)
        return JSONResponse(status_code=500, content={"error": "webhook_failed"})
    return JSONResponse(status_code=200, content={"received": True})


@router.get("/key")
async def get_key(session_id: str) -> JSONResponse:
    """Exchange a completed checkout session for a subscriber key.

    Validates the session with Stripe, then mints (or rotates) the
    subscriber's key. The raw key is returned exactly once.
    """
    if not stripe_effective_enabled():
        return _not_found()
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=404, content={"error": "session_not_found"})
    if _ev(session, "status") != "complete":
        return JSONResponse(status_code=402, content={"error": "session_not_complete"})
    customer_id = _ev(session, "customer")
    if not customer_id:
        return JSONResponse(status_code=404, content={"error": "session_not_found"})
    details = _ev(session, "customer_details") or {}
    token = rotate_subscriber_key(
        str(customer_id), _ev(details, "email") or _ev(session, "customer_email")
    )
    if token is None:
        # Webhook may not have arrived yet — create the row from the session.
        upsert_subscriber_placeholder(
            str(customer_id), _ev(details, "email") or _ev(session, "customer_email")
        )
        token = rotate_subscriber_key(str(customer_id))
    if token is None:
        return JSONResponse(status_code=500, content={"error": "key_issue_failed"})
    return JSONResponse(status_code=200, content={"subscriber_key": token})


@router.get("/status")
async def billing_status() -> JSONResponse:
    from .x402 import x402_effective_enabled

    return JSONResponse(
        status_code=200,
        content={
            "stripe_enabled": stripe_effective_enabled(),
            "x402_enabled": x402_effective_enabled(),
        },
    )
