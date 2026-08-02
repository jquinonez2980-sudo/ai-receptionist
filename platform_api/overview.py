# platform_api/overview.py — GET /platform/overview (dashboard KPI tiles).
#
# Rolling 7-day window vs the prior 7 days, computed in the tenant's own
# timezone. Rolling (not calendar) weeks so the comparison is always
# like-for-like — a Tuesday-morning "this week" vs a full "last week" would
# make every % change meaningless.
#
# Data source: the `calls` table (voice) plus `chat_sessions` (web chat,
# volume-only for v1 — see _chat_bucket_stats). Website bookings join these
# numbers in a later phase.

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from platform_api.security import require_tenant, verify_platform_secret
from tenants import TenantConfig, load_tenant

log = logging.getLogger(__name__)

router = APIRouter()

WINDOW_DAYS = 7

_PRICE_RE = re.compile(r"(\d+(?:\.\d{1,2})?)")


def _is_after_hours(local_dt: datetime, cfg: TenantConfig) -> bool:
    """True when NO location was open at that local time.

    Uses each location's business_days + per-day hours (multi-location: a call
    is only 'after hours' if every shop was closed). Falls back to the
    tenant-level synthesized location for single-location tenants.
    """
    weekday = local_dt.weekday()
    hour = local_dt.hour
    locations = list(cfg.locations.values()) or [cfg.default_location()]
    for loc in locations:
        if weekday in loc.business_days:
            open_h, close_h = loc.hours_for_day(weekday)
            if open_h <= hour < close_h:
                return False
    return True


def _avg_service_price(cfg: TenantConfig) -> Optional[float]:
    """Mean of the parseable service prices ('$40' → 40.0, '$35–$40' → 35.0
    — first number, i.e. the conservative low end). None when the tenant has
    no services map or nothing parses (SaaS-pricing tenants like Orchelix)."""
    prices = []
    for svc in cfg.services.values():
        m = _PRICE_RE.search(svc.price or "")
        if m:
            prices.append(float(m.group(1)))
    if not prices:
        return None
    return sum(prices) / len(prices)


def _bucket_stats(
    rows: list, start, end, cfg: TenantConfig, tz: ZoneInfo, avg_price: Optional[float]
) -> dict:
    calls = booked = escalated = after_hours = 0
    seconds = 0
    for started_at, duration_sec, outcome in rows:
        if started_at is None or not (start <= started_at < end):
            continue
        calls += 1
        seconds += duration_sec or 0
        if outcome == "booked":
            booked += 1
        elif outcome == "escalated":
            escalated += 1
        if _is_after_hours(started_at.astimezone(tz), cfg):
            after_hours += 1
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "calls_answered": calls,
        "appointments_booked": booked,
        "leads_escalated": escalated,
        "after_hours_calls": after_hours,
        "minutes_used": round(seconds / 60.0, 1),
        # Estimate, clearly labeled as such in the UI: bookings × the tenant's
        # average listed service price. None when prices aren't parseable.
        "est_revenue_booked": (
            round(booked * avg_price) if avg_price is not None else None
        ),
    }


def _chat_bucket_stats(rows: list, start, end) -> dict:
    """Volume-only web-chat count for one window (v1 — see module docstring).

    Buckets by last_at when present, else started_at, per spec: a session
    that got a new message today should count as "current" activity even if
    it started last week, matching what a tenant would call "chats this week."
    """
    web_chats = 0
    for started_at, last_at, _outcome in rows:
        effective = last_at or started_at
        if effective is None or not (start <= effective < end):
            continue
        web_chats += 1
    return {"web_chats": web_chats}


def _chat_rows(conn, tenant_id: str, prev_start) -> list:
    """Fail-soft: any error (including a not-yet-migrated/missing table)
    degrades to zero chat rows rather than 500ing the whole Overview —
    chat_sessions is additive and must never be able to break the calls-based
    KPIs that already work."""
    from sqlalchemy import text

    try:
        return conn.execute(
            text(
                "SELECT started_at, last_at, outcome FROM chat_sessions "
                "WHERE tenant_id = :tid "
                "AND COALESCE(last_at, started_at) >= :prev_start"
            ),
            {"tid": tenant_id, "prev_start": prev_start},
        ).all()
    except Exception:
        log.exception(
            "chat_sessions read failed for tenant=%s overview — reporting 0 web chats.",
            tenant_id,
        )
        # Reset the connection's transaction state — a failed statement
        # otherwise leaves it unusable for the rest of this request.
        conn.rollback()
        return []


@router.get("/platform/overview")
def platform_overview(request: Request) -> dict:
    """Tenant KPIs: rolling last-7-days vs the 7 days before that.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy query.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    cfg = load_tenant(tenant_id)
    try:
        tz = ZoneInfo(cfg.business_tz)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    cur_start = now - timedelta(days=WINDOW_DAYS)
    prev_start = now - timedelta(days=2 * WINDOW_DAYS)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT started_at, duration_sec, outcome FROM calls "
                "WHERE tenant_id = :tid AND started_at >= :prev_start"
            ),
            {"tid": tenant_id, "prev_start": prev_start},
        ).all()
        chat_rows = _chat_rows(conn, tenant_id, prev_start)

    avg_price = _avg_service_price(cfg)
    current = _bucket_stats(rows, cur_start, now, cfg, tz, avg_price)
    previous = _bucket_stats(rows, prev_start, cur_start, cfg, tz, avg_price)
    current.update(_chat_bucket_stats(chat_rows, cur_start, now))
    previous.update(_chat_bucket_stats(chat_rows, prev_start, cur_start))
    return {
        "tenant_id": tenant_id,
        "business_tz": cfg.business_tz,
        "window_days": WINDOW_DAYS,
        "current": current,
        "previous": previous,
    }
