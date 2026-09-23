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
    FREE_TIER_ENABLED (default "true"): kill switch for the anonymous free
        tier. When "false"/"0", the free path is skipped entirely and
        anonymous callers fall straight to the x402 402 challenge.
    FREE_MONTHLY_CREDITS (default "50" — Scott's call): monthly credit
        bucket for anonymous callers, keyed by client IP (hashed; the raw IP
        is never stored). Same credit math as subscribers, from the same
        x402 price map.
    ADMIN_KEY (required for /billing/stats): shared secret for the usage
        analytics endpoint, via X-Admin-Key header (preferred) or
        ?admin_key= query param. Must be set on Render — a long random
        string. Fail closed: unset/empty means the endpoint always 401s.

Subscriber storage: stdlib sqlite3. Only the SHA-256 hash of a subscriber
key is stored — never the raw token. All endpoints 404 unless Stripe is
effectively enabled (fail closed).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
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

-- Anonymous free-tier bucket: one row per hashed client IP per month.
-- Only the SHA-256 of the IP is stored — never the raw IP.
CREATE TABLE IF NOT EXISTS free_usage (
    ip_hash    TEXT PRIMARY KEY,
    month      TEXT NOT NULL,
    credits    REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);

-- Usage analytics rollups: per-day aggregates by tool and plan type.
-- No PII here — IPs are never stored, only aggregate counts.
CREATE TABLE IF NOT EXISTS usage_daily (
    date       TEXT NOT NULL,
    tool       TEXT NOT NULL,
    plan_type  TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    credits    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, tool, plan_type)
);
"""

# Default lives on the Render persistent-disk mount (see render.yaml `disk:`).
# A redeploy would wipe ./data on Render's ephemeral filesystem.
_DISK_DB_PATH = "/var/lib/search-mcp/billing.db"

# Fair-use credit cap for Pro subscribers: 1000 credits = $10 of API calls
# at x402 list price, bundled with the $9/mo plan. Scott's call to change.
_DEFAULT_MONTHLY_CREDIT_LIMIT = 1000.0

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
# Free tier: anonymous monthly credit bucket keyed by client IP.
# ---------------------------------------------------------------------------

# Default anonymous bucket: 50 credits = $0.50 of x402 list-price calls per
# month per IP. Scott's call — override with FREE_MONTHLY_CREDITS.
_DEFAULT_FREE_MONTHLY_CREDITS = 50.0


def free_tier_enabled_flag() -> bool:
    """Kill switch for the anonymous free tier. Default ON; set
    FREE_TIER_ENABLED=false (or 0) to skip the free path entirely — anonymous
    callers then fall straight to the x402 402 challenge."""
    return _env_flag("FREE_TIER_ENABLED", "true")


def _free_monthly_limit() -> float:
    raw = os.environ.get("FREE_MONTHLY_CREDITS", "").strip()
    if not raw:
        return _DEFAULT_FREE_MONTHLY_CREDITS
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        log.warning(
            "invalid FREE_MONTHLY_CREDITS=%r; using default %g",
            raw,
            _DEFAULT_FREE_MONTHLY_CREDITS,
        )
        return _DEFAULT_FREE_MONTHLY_CREDITS


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


def check_free_tier_usage(ip: str, credit_cost: float) -> dict:
    """Charge one call's credit cost against the anonymous free-tier bucket.

    Keyed by client IP — only the SHA-256 of the IP is stored, never the raw
    IP. Same credit math as subscribers (``credits_for_price_usdc`` over the
    x402 price map: the single source of truth). Denied calls are never
    charged. The bucket resets on the calendar-month boundary (UTC).

    Returns a verdict dict:
        {"ok": True,  "credits_used": n, "free_limit": L,
         "credit_cost": c, "resets_at": "..."}
        {"ok": False, ...}   (bucket exhausted; NOT charged)
    """
    ip_hash = _hash_ip(ip or "unknown")
    month = _current_month_utc()
    limit = _free_monthly_limit()
    conn = _db()
    try:
        row = conn.execute(
            "SELECT month, credits FROM free_usage WHERE ip_hash = ?",
            (ip_hash,),
        ).fetchone()
        used = float(row["credits"]) if row is not None and row["month"] == month else 0.0
        if credit_cost > 0 and used + credit_cost > limit:
            return {
                "ok": False,
                "credits_used": round(used, 1),
                "free_limit": limit,
                "credit_cost": credit_cost,
                "resets_at": _next_month_start_iso(),
            }
        if credit_cost > 0:
            # Atomic upsert: reset the counter when the stored month is stale.
            conn.execute(
                """
                INSERT INTO free_usage (ip_hash, month, credits, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ip_hash) DO UPDATE SET
                    month = excluded.month,
                    credits = CASE
                        WHEN free_usage.month = excluded.month
                        THEN free_usage.credits + excluded.credits
                        ELSE excluded.credits
                    END,
                    updated_at = excluded.updated_at
                """,
                (ip_hash, month, credit_cost, _now_iso()),
            )
            conn.commit()
        charged = used + credit_cost if credit_cost > 0 else used
        return {
            "ok": True,
            "credits_used": round(charged, 1),
            "free_limit": limit,
            "credit_cost": credit_cost,
            "resets_at": _next_month_start_iso(),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Usage analytics: per-day rollups by tool and plan type.
#
# No PII: IPs are never stored here, only aggregate counts. Every served
# call that passes the metering hook is recorded — credits actually deducted
# (free/subscriber) or the list-price credit equivalent (x402). Zero-credit
# outcomes (free methods on a paid path) are recorded as 0.
# ---------------------------------------------------------------------------

_PLAN_TYPES = ("free", "subscriber", "x402")
_MAX_STATS_DAYS = 365


def _today_utc() -> str:
    """Current calendar date as "YYYY-MM-DD" (UTC). Small and patchable so
    tests can simulate the day boundary."""
    return time.strftime("%Y-%m-%d", time.gmtime())


def record_usage(tool: str, plan_type: str, credit_cost: float) -> None:
    """Roll one served call into usage_daily.

    Fail-safe by design: analytics must never break serving, so DB errors are
    logged and swallowed. A programming bug (unknown plan_type) still raises
    loudly so tests catch it.
    """
    if plan_type not in _PLAN_TYPES:
        raise ValueError(f"unknown plan_type: {plan_type!r}")
    try:
        conn = _db()
    except Exception:  # noqa: BLE001
        log.warning("usage analytics unavailable (billing db open failed)", exc_info=True)
        return
    try:
        conn.execute(
            """
            INSERT INTO usage_daily (date, tool, plan_type, calls, credits)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(date, tool, plan_type) DO UPDATE SET
                calls = usage_daily.calls + 1,
                credits = ROUND(usage_daily.credits + excluded.credits, 1)
            """,
            (_today_utc(), tool, plan_type, round(float(credit_cost), 1)),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("usage analytics write failed", exc_info=True)
    finally:
        conn.close()


def _empty_plan_rollup() -> dict:
    return {plan: {"calls": 0, "credits": 0.0} for plan in _PLAN_TYPES}


def get_usage_stats(days: int | str = 30) -> dict:
    """Per-day usage rollups plus totals.

    Shape:
        {"days": [{"date", "calls", "credits",
                   "tools": {tool: {"free": {"calls", "credits"},
                                    "subscriber": {...}, "x402": {...}}}}],
         "totals": {"calls", "credits",
                    "tools": {tool: {plan: {"calls", "credits"}}}}}
    ``days`` is clamped to [1, 365]; every day in the window appears even
    with zero traffic.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(_MAX_STATS_DAYS, days))
    today = datetime.date.fromisoformat(_today_utc())
    start = (today - datetime.timedelta(days=days - 1)).isoformat()
    day_list = [(today - datetime.timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    per_day: dict[str, dict] = {d: {} for d in day_list}
    try:
        conn = _db()
    except Exception:  # noqa: BLE001
        log.warning("usage stats unavailable (billing db open failed)", exc_info=True)
        conn = None
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT date, tool, plan_type, calls, credits FROM usage_daily "
                "WHERE date >= ? AND date <= ?",
                (start, today.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            tools = per_day[row["date"]]
            entry = tools.setdefault(row["tool"], _empty_plan_rollup())
            if row["plan_type"] in _PLAN_TYPES:
                entry[row["plan_type"]] = {
                    "calls": row["calls"],
                    "credits": round(float(row["credits"]), 1),
                }
    days_out = []
    totals_tools: dict[str, dict] = {}
    total_calls = 0
    total_credits = 0.0
    for day in day_list:
        tools = per_day[day]
        day_calls = sum(plan["calls"] for entry in tools.values() for plan in entry.values())
        day_credits = round(
            sum(plan["credits"] for entry in tools.values() for plan in entry.values()), 1
        )
        days_out.append({"date": day, "calls": day_calls, "credits": day_credits, "tools": tools})
        total_calls += day_calls
        total_credits = round(total_credits + day_credits, 1)
        for tool, plans in tools.items():
            agg = totals_tools.setdefault(tool, _empty_plan_rollup())
            for plan in _PLAN_TYPES:
                agg[plan]["calls"] += plans[plan]["calls"]
                agg[plan]["credits"] = round(agg[plan]["credits"] + plans[plan]["credits"], 1)
    return {
        "days": days_out,
        "totals": {"calls": total_calls, "credits": total_credits, "tools": totals_tools},
    }


# ---------------------------------------------------------------------------
# Admin auth for /billing/stats.
# ---------------------------------------------------------------------------


def _admin_key_expected() -> str:
    return os.environ.get("ADMIN_KEY", "").strip()


def admin_key_valid(provided: str | None) -> bool:
    """Shared-secret check for /billing/stats. Fail closed: when ADMIN_KEY is
    unset or empty, nothing is ever authorized."""
    expected = _admin_key_expected()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.strip(), expected)


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


@router.get("/stats")
async def usage_stats(request: Request) -> JSONResponse:
    """Usage analytics rollups (see get_usage_stats).

    Auth: ADMIN_KEY shared secret via X-Admin-Key header (preferred) or
    ?admin_key= query param. 401 JSON when missing/mismatched; fail closed
    when ADMIN_KEY is unset. Un-metered: /billing/* is exempt from the x402
    middleware, and this endpoint never touches any quota. Not gated on
    Stripe — ADMIN_KEY is the gate.
    """
    provided = request.headers.get("x-admin-key", "") or request.query_params.get("admin_key", "")
    if not admin_key_valid(provided):
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "Valid X-Admin-Key required."},
        )
    return JSONResponse(
        status_code=200, content=get_usage_stats(request.query_params.get("days", 30))
    )
