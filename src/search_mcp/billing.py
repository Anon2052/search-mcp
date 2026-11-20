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
    SUBSCRIBER_MONTHLY_CREDIT_LIMIT (default "1000"): fair-use credit cap
        on calls authenticated with X-Subscriber-Key, per calendar month
        (UTC). 1 credit = $0.01 of x402 list price, so 1000 credits = $10 of
        API calls bundled with the $9/mo Pro plan (Scott's call on the
        number; override with the env var). x402 per-call payers are not
        capped.

Free tier: when x402 is enabled, anonymous callers (no subscriber key, no
X-Payment) draw from a monthly free credit bucket keyed by client IP
(``FREE_MONTHLY_CREDITS``, default 50 credits = $0.50 of calls at list
price; kill switch ``FREE_TIER_ENABLED``, default on). Aligned across all
three connectors — Scott's call, 2026-10-21: directory scanners and
reviewers cannot pay per call, so the bucket is what lets them (and new
users) evaluate the tools. Exhaustion -> HTTP 402 ``free_tier_exhausted``
(429 stays exclusive to the subscriber cap). IPs are stored as SHA-256
hashes, never raw.

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
from decimal import Decimal

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
    credits    REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);

-- M-1: persistent x402 anti-replay claims. Survives restarts and is shared
-- across workers, closing the gaps of the old in-memory signature cache.
CREATE TABLE IF NOT EXISTS x402_seen_signatures (
    signature  TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);

-- Free tier: monthly anonymous credit bucket, keyed by SHA-256 of the
-- client IP (never the raw IP). See record_free_tier_usage.
CREATE TABLE IF NOT EXISTS free_tier_usage (
    ip_hash    TEXT PRIMARY KEY,
    month      TEXT NOT NULL,
    credits    REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);
"""

# Default lives on the Render persistent-disk mount (see render.yaml `disk:`).
# A redeploy would wipe ./data on Render's ephemeral filesystem.
_DISK_DB_PATH = "/var/lib/search-mcp/billing.db"

# Fair-use credit cap for Pro subscribers: 1000 credits = $10 of API calls
# at x402 list price, bundled with the $9/mo plan. Scott's call to change.
_DEFAULT_MONTHLY_CREDIT_LIMIT = 1000.0

def _placeholder_hash(customer_id: str) -> str:
    """Unique placeholder key_hash per customer (M-4).

    The old single constant placeholder collided on the key_hash PRIMARY KEY
    when two customers checked out before either minted a key: the second
    webhook 500'd and Stripe retried forever. A per-customer placeholder keeps
    the upsert's ON CONFLICT(customer_id) target collision-free.
    """
    return "pending:" + hashlib.sha256(customer_id.encode("utf-8")).hexdigest()


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
    _migrate_usage_to_credits(conn)
    return conn


def _migrate_usage_to_credits(conn: sqlite3.Connection) -> None:
    """One-way schema migration: subscriber_usage.calls -> credits.

    The flat per-call cap was never enabled in production, so no real usage
    data is at risk; the migration just keeps dev/staging DBs created by the
    old code working with the credit-based cap.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(subscriber_usage)")}
    if "calls" in cols and "credits" not in cols:
        conn.execute("ALTER TABLE subscriber_usage ADD COLUMN credits REAL NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE subscriber_usage DROP COLUMN calls")
        conn.commit()
        log.info("migrated subscriber_usage.calls -> credits")


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
            (_placeholder_hash(customer_id), customer_id, email, _now_iso(), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def claim_x402_signature(signature: str, expires_at: float) -> bool:
    """Atomically claim an x402 payment signature (M-1 anti-replay).

    Returns True when the signature was already claimed and has not expired
    (a replay); False when this call claimed it. The claim is atomic
    (INSERT PRIMARY KEY): concurrent duplicate submissions across the
    facilitator-verify gap collapse to a single serve. Expired rows are
    pruned opportunistically. An expired row can never mask a live
    authorization: the sanity check bounds the validity window to
    <= MAX_TIMEOUT_SECONDS <= the claim TTL, so a row only expires after
    its authorization is already invalid.
    """
    conn = _db()
    try:
        try:
            conn.execute(
                "INSERT INTO x402_seen_signatures (signature, expires_at) VALUES (?, ?)",
                (signature, expires_at),
            )
        except sqlite3.IntegrityError:
            return True
        conn.execute(
            "DELETE FROM x402_seen_signatures WHERE expires_at <= ?",
            (time.time(),),
        )
        conn.commit()
        return False
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


def credits_for_price_usdc(price_usdc: Decimal | float | str) -> float:
    """Credit cost of one call: 1 credit = $0.01 of x402 list price.

    Takes the same Decimal price the x402 middleware charges (see
    ``x402.price_for_tool_usdc``) — the x402 price map stays the single
    source of truth; this only converts dollars to credits. $0.025 -> 2.5
    credits, $0.01 -> 1.0 credit.
    """
    price = price_usdc if isinstance(price_usdc, Decimal) else Decimal(str(price_usdc))
    return float(round(price / Decimal("0.01"), 1))


def _monthly_credit_limit() -> float:
    raw = os.environ.get("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "").strip()
    if not raw:
        return _DEFAULT_MONTHLY_CREDIT_LIMIT
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        log.warning(
            "invalid SUBSCRIBER_MONTHLY_CREDIT_LIMIT=%r; using default %g",
            raw,
            _DEFAULT_MONTHLY_CREDIT_LIMIT,
        )
        return _DEFAULT_MONTHLY_CREDIT_LIMIT


def _next_month_start_iso() -> str:
    """First instant of next month, UTC, ISO-8601 — derived from the
    patchable _current_month_utc() so tests control it."""
    year, month = (int(part) for part in _current_month_utc().split("-"))
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return f"{year:04d}-{month:02d}-01T00:00:00Z"


def check_subscriber_usage(token: str, credit_cost: float) -> dict | None:
    """Validate a subscriber key and charge one call's credit cost against
    the monthly fair-use credit cap.

    ``credit_cost`` is the credits for this call (see
    credits_for_price_usdc, which converts the x402 price map's dollars to
    credits). Free paths pass 0: they are never gated and never counted.

    Returns None when the key is invalid/unknown (the caller falls through
    to the normal x402 flow, not a hard error). Otherwise returns a verdict
    dict:
        {"ok": True,  "credit_limit": L, "credits_used": n, "credit_cost": c}
        {"ok": False, "credit_limit": L, "credits_used": n, "credit_cost": c,
         "resets_at": "..."}                                     (cap hit; NOT charged)
    The counter resets on the calendar-month boundary (UTC). The cap applies
    to subscriber-key auth only — x402 per-call payers never touch this.
    """
    if not token:
        return None
    key_hash = _hash_key(token)
    month = _current_month_utc()
    limit = _monthly_credit_limit()
    conn = _db()
    try:
        sub = conn.execute(
            "SELECT 1 FROM subscribers WHERE key_hash = ? AND status = 'active'",
            (key_hash,),
        ).fetchone()
        if sub is None:
            return None
        row = conn.execute(
            "SELECT month, credits FROM subscriber_usage WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        used = float(row["credits"]) if row is not None and row["month"] == month else 0.0
        if credit_cost <= 0:
            # Free path (discovery etc.): no charge, never gated.
            return {
                "ok": True,
                "credit_limit": limit,
                "credits_used": round(used, 1),
                "credit_cost": 0.0,
            }
        if used + credit_cost > limit:
            return {
                "ok": False,
                "credit_limit": limit,
                "credits_used": round(used, 1),
                "credit_cost": credit_cost,
                "resets_at": _next_month_start_iso(),
            }
        # Atomic upsert: reset the counter when the stored month is stale.
        # In ON CONFLICT DO UPDATE, right-hand sides see the OLD row, so the
        # CASE compares the stored month against the incoming one.
        conn.execute(
            """
            INSERT INTO subscriber_usage (key_hash, month, credits, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key_hash) DO UPDATE SET
                month = excluded.month,
                credits = CASE
                    WHEN subscriber_usage.month = excluded.month
                    THEN subscriber_usage.credits + excluded.credits
                    ELSE excluded.credits
                END,
                updated_at = excluded.updated_at
            """,
            (key_hash, month, credit_cost, _now_iso()),
        )
        conn.commit()
        return {
            "ok": True,
            "credit_limit": limit,
            "credits_used": round(used + credit_cost, 1),
            "credit_cost": credit_cost,
        }
    finally:
        conn.close()


def get_subscriber_credit_usage(token: str) -> dict | None:
    """Current credit usage for a subscriber key. None if invalid/unknown.

    Read-only: never charges the cap and never 429s, so the /billing/usage
    endpoint can serve it freely.
    """
    if not token:
        return None
    key_hash = _hash_key(token)
    month = _current_month_utc()
    conn = _db()
    try:
        sub = conn.execute(
            "SELECT 1 FROM subscribers WHERE key_hash = ? AND status = 'active'",
            (key_hash,),
        ).fetchone()
        if sub is None:
            return None
        row = conn.execute(
            "SELECT month, credits FROM subscriber_usage WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        used = float(row["credits"]) if row is not None and row["month"] == month else 0.0
        return {
            "credits_used": round(used, 1),
            "credit_limit": _monthly_credit_limit(),
            "resets_at": _next_month_start_iso(),
            "period": month,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Free tier: monthly anonymous credit bucket keyed by client IP.
#
# Business decision — Scott's call (aligned across search / doc-convert /
# price-tracker, 2026-10-21). Default 50 credits = $0.50 of API calls at
# x402 list prices (1 credit = $0.01). Same credit math as the subscriber
# cap: ``credits_for_price_usdc`` is the single conversion — billing never
# prices a call itself; the x402 price map does.
_DEFAULT_FREE_MONTHLY_CREDITS = 50.0


def free_tier_enabled() -> bool:
    """Kill switch: ``FREE_TIER_ENABLED=false``/``0`` skips the free path
    entirely (anonymous calls fall straight to the x402 402 challenge)."""
    return _env_flag("FREE_TIER_ENABLED", "true")


def _free_monthly_credit_limit() -> float:
    """Resolved monthly free credit bucket: env override -> baked-in
    default. Zero is a legitimate value (no free calls); negative or
    non-numeric values fall back to the default."""
    raw = os.environ.get("FREE_MONTHLY_CREDITS", "").strip()
    if not raw:
        return _DEFAULT_FREE_MONTHLY_CREDITS
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        log.warning(
            "invalid FREE_MONTHLY_CREDITS=%r; using default %g",
            raw,
            _DEFAULT_FREE_MONTHLY_CREDITS,
        )
        return _DEFAULT_FREE_MONTHLY_CREDITS
    return value


def record_free_tier_usage(ip: str, credit_cost: float) -> dict:
    """Check + deduct free-tier credits for one anonymous call.

    Keyed by SHA-256 of the client IP (never the raw IP), calendar month
    UTC. Calls whose credit cost would push usage over the bucket are
    rejected and deduct NOTHING. Zero-cost calls are always allowed, even
    at a zero credit limit.

    Returns ``{"allowed": bool, "credits_used": float, "free_limit":
    float, "resets_at": str}``.

    Concurrency: the read-modify-write rides SQLite's single-writer lock,
    so cap enforcement under heavy concurrency is best-effort — the same
    note as the subscriber cap. Abuse backstop is the 60/min per-IP rate
    limit, not this bucket; IP rotation is an accepted v1 limitation.
    """
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()
    month = _current_month_utc()
    limit = _free_monthly_credit_limit()
    conn = _db()
    try:
        row = conn.execute(
            "SELECT month, credits FROM free_tier_usage WHERE ip_hash = ?",
            (ip_hash,),
        ).fetchone()
        used = float(row["credits"]) if row is not None and row["month"] == month else 0.0
        verdict = {
            "credits_used": round(used, 1),
            "free_limit": limit,
            "resets_at": _next_month_start_iso(),
        }
        if credit_cost <= 0:
            return {"allowed": True, **verdict}
        if used + credit_cost > limit:
            return {"allowed": False, **verdict}
        conn.execute(
            """
            INSERT INTO free_tier_usage (ip_hash, month, credits, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip_hash) DO UPDATE SET
                month = excluded.month,
                credits = CASE
                    WHEN free_tier_usage.month = excluded.month
                    THEN free_tier_usage.credits + excluded.credits
                    ELSE excluded.credits
                END,
                updated_at = excluded.updated_at
            """,
            (ip_hash, month, credit_cost, _now_iso()),
        )
        conn.commit()
        return {"allowed": True, **{**verdict, "credits_used": round(used + credit_cost, 1)}}
    finally:
        conn.close()


def get_free_tier_usage(ip: str) -> dict:
    """Read-only free-bucket peek for an IP — never deducts, never gates."""
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()
    month = _current_month_utc()
    used = 0.0
    conn = _db()
    try:
        row = conn.execute(
            "SELECT month, credits FROM free_tier_usage WHERE ip_hash = ?",
            (ip_hash,),
        ).fetchone()
        if row is not None and row["month"] == month:
            used = float(row["credits"])
    finally:
        conn.close()
    return {
        "credits_used": round(used, 1),
        "free_limit": _free_monthly_credit_limit(),
        "resets_at": _next_month_start_iso(),
        "period": month,
    }


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


@router.get("/usage")
async def usage(request: Request) -> JSONResponse:
    """Credit usage for the caller's subscriber key (X-Subscriber-Key).

    Free: the x402 middleware exempts /billing/*, and this endpoint never
    charges or 429-gates — it only reads.
    """
    if not stripe_effective_enabled():
        return _not_found()
    token = request.headers.get("x-subscriber-key", "").strip()
    info = get_subscriber_credit_usage(token)
    if info is None:
        return JSONResponse(status_code=401, content={"error": "invalid_subscriber_key"})
    return JSONResponse(status_code=200, content=info)


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
