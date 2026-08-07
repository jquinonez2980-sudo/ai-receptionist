"""GET /platform/analytics (dashboard Analytics page — light v1,
docs/ESMI_DASHBOARD_UX.md Section 5.5).

Reuses the same recording-fake-DB harness as test_overview.py — one query
against `calls`, bucketed in Python. What matters here:

  * Auth (platform secret + tenant header), same as every /platform/* route.
  * Tenant isolation: the query is scoped by tenant_id, and a different
    tenant's rows never leak into these buckets.
  * Empty tenant -> a fully zero-filled series, never a 500.
  * volume_by_day is zero-filled for every day in the window, ascending.
  * language_mix buckets en/es/unspecified (None or an unexpected value)
    correctly, additively.

Run: PYTHONUTF8=1 pytest evals/test_analytics.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.analytics as an
from evals.test_signup import FakeConn, FakeEngine

SECRET = "test-platform-secret"
TID = "otro-nivel"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": TID}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(an.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_tenant_cache():
    """load_tenant() caches for 5 minutes — reset so each test's FakeConn
    (no tenant_configs rule) deterministically falls through to the real
    tenants/otro-nivel/config.json rather than reusing a cached instance."""
    from tenants import clear_tenant_cache

    clear_tenant_cache(TID)
    yield
    clear_tenant_cache(TID)


def use(monkeypatch, conn):
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))
    return conn


# ── auth ──────────────────────────────────────────────────────────────────


def test_requires_platform_secret(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM calls", [])]))
    r = client.get("/platform/analytics", headers={"X-Tenant-Id": TID})
    assert r.status_code == 401


def test_requires_tenant(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM calls", [])]))
    r = client.get("/platform/analytics", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


def test_no_db_is_503(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.get("/platform/analytics", headers=HEADERS)
    assert r.status_code == 503


# ── tenant isolation ─────────────────────────────────────────────────────────


def test_query_is_scoped_to_the_tenant(client, monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("FROM calls", [])]))
    client.get("/platform/analytics", headers=HEADERS)
    sql, params = conn.sql_containing("FROM calls")[0]
    assert "tenant_id = :tid" in sql
    assert params["tid"] == TID


# ── empty tenant ──────────────────────────────────────────────────────────────


def test_empty_tenant_is_a_fully_zero_filled_series_not_a_500(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM calls", [])]))
    r = client.get("/platform/analytics", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 14
    assert len(body["volume_by_day"]) == 14
    assert all(day["count"] == 0 for day in body["volume_by_day"])
    assert body["language_mix"] == {"en": 0, "es": 0, "unknown": 0}


# ── volume_by_day ─────────────────────────────────────────────────────────────


def test_volume_by_day_is_zero_filled_and_ascending(client, monkeypatch):
    now = datetime.now(timezone.utc)
    conn = use(monkeypatch, FakeConn(rules=[
        ("FROM calls", [
            (now - timedelta(days=1), "en"),
            (now - timedelta(days=1), "es"),
            (now - timedelta(days=3), "en"),
        ]),
    ]))
    body = client.get("/platform/analytics", headers=HEADERS).json()

    days = body["volume_by_day"]
    assert len(days) == 14
    dates = [d["date"] for d in days]
    assert dates == sorted(dates), "must be ascending, oldest first"

    by_date = {d["date"]: d["count"] for d in days}
    total = sum(by_date.values())
    assert total == 3
    # Exactly two non-zero days (yesterday=2, three days ago=1); the rest 0.
    nonzero = [c for c in by_date.values() if c > 0]
    assert sorted(nonzero) == [1, 2]


def test_volume_by_day_ignores_calls_outside_the_window(client, monkeypatch):
    now = datetime.now(timezone.utc)
    conn = use(monkeypatch, FakeConn(rules=[
        ("FROM calls", [(now - timedelta(days=1), "en")]),
    ]))
    client.get("/platform/analytics", headers=HEADERS)
    # The SQL itself bounds the window (window_start param); confirm the
    # handler passes the 14-day cutoff through to the query.
    sql, params = conn.sql_containing("FROM calls")[0]
    assert "started_at >= :window_start" in sql
    assert (now - params["window_start"]).days in (13, 14)


def test_volume_by_day_null_started_at_is_skipped(client, monkeypatch):
    """A malformed row (should never happen — started_at is NOT NULL in the
    schema) must not crash the bucketer."""
    use(monkeypatch, FakeConn(rules=[("FROM calls", [(None, "en")])]))
    r = client.get("/platform/analytics", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert all(d["count"] == 0 for d in r.json()["volume_by_day"])


# ── language_mix ──────────────────────────────────────────────────────────────


def test_language_mix_buckets_en_es_and_unknown(client, monkeypatch):
    now = datetime.now(timezone.utc)
    use(monkeypatch, FakeConn(rules=[
        ("FROM calls", [
            (now, "en"),
            (now, "en"),
            (now, "es"),
            (now, None),
            (now, "fr"),  # unexpected value — still "unknown"
        ]),
    ]))
    body = client.get("/platform/analytics", headers=HEADERS).json()
    assert body["language_mix"] == {"en": 2, "es": 1, "unknown": 2}


# ── pure bucketing logic (no DB) ──────────────────────────────────────────────


def test_language_mix_pure_function_is_additive():
    rows = [(None, "en"), (None, "es"), (None, "es"), (None, None)]
    assert an._language_mix(rows) == {"en": 1, "es": 2, "unknown": 1}
