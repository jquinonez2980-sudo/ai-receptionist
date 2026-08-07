# platform_api/analytics.py — GET /platform/analytics (dashboard Analytics
# page, docs/ESMI_DASHBOARD_UX.md Section 5.5, "light v1").
#
# Real, cheap insights from data already in the `calls` table — no new
# tables, no new infra. One query (started_at, language for the last
# WINDOW_DAYS), bucketed in Python the same way platform_api/overview.py
# already buckets calls for its KPI tiles and language_mix. Peak-hours
# heatmap, booking-conversion-rate, and lead-quality-score are real,
# unbuilt work — the frontend shows honest "coming soon" cards for those
# instead of faking them here.

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from platform_api.security import require_tenant, verify_platform_secret
from tenants import load_tenant

log = logging.getLogger(__name__)

router = APIRouter()

WINDOW_DAYS = 14


def _volume_by_day(rows: list, tz: ZoneInfo, window_days: int, today) -> list[dict]:
    """Zero-filled daily call counts, oldest first, so the frontend never has
    to reason about missing days — a quiet day is a real 0, not absent."""
    counts: dict = {}
    for started_at, _language in rows:
        if started_at is None:
            continue
        d = started_at.astimezone(tz).date()
        counts[d] = counts.get(d, 0) + 1

    days = [today - timedelta(days=i) for i in range(window_days - 1, -1, -1)]
    return [{"date": d.isoformat(), "count": counts.get(d, 0)} for d in days]


def _language_mix(rows: list) -> dict:
    mix = {"en": 0, "es": 0, "unknown": 0}
    for _started_at, language in rows:
        mix[language if language in ("en", "es") else "unknown"] += 1
    return mix


@router.get("/platform/analytics")
def platform_analytics(request: Request) -> dict:
    """Call volume trend + language mix over the last WINDOW_DAYS.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy query,
    same convention as every other /platform/* route.
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
    window_start = now - timedelta(days=WINDOW_DAYS)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT started_at, language FROM calls "
                "WHERE tenant_id = :tid AND started_at >= :window_start"
            ),
            {"tid": tenant_id, "window_start": window_start},
        ).all()

    return {
        "tenant_id": tenant_id,
        "business_tz": cfg.business_tz,
        "window_days": WINDOW_DAYS,
        "volume_by_day": _volume_by_day(rows, tz, WINDOW_DAYS, now.date()),
        "language_mix": _language_mix(rows),
    }
