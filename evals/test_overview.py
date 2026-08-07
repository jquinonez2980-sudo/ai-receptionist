"""Web-chats KPI on GET /platform/overview (chat_sessions join, additive).

Three layers:
  * Pure logic: _chat_bucket_stats window/fallback rules — no DB.
  * Route + fail-soft against a recording fake DB (FakeConn/FakeEngine) —
    confirms the new web_chats key is additive and that a chat_sessions read
    failure degrades to 0 rather than 500ing the whole Overview.
  * A self-cleaning round trip against the REAL Railway DB (skipped unless
    DATABASE_URL is set — e.g. `railway run pytest evals/test_overview.py -v`).

Run: PYTHONUTF8=1 pytest evals/test_overview.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "sk-test-unit")

import platform_api.overview as overview
from evals.test_signup import FakeConn, FakeEngine

SECRET = "test-platform-secret"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": "otro-nivel"}
TID = "otro-nivel"


@pytest.fixture(autouse=True)
def _fresh_tenant_cache():
    """load_tenant() caches for 5 minutes — reset it so each test's FakeConn
    (which has no tenant_configs rule) deterministically falls through to the
    real tenants/otro-nivel/config.json rather than reusing another test's
    cached TenantConfig instance."""
    from tenants import clear_tenant_cache

    clear_tenant_cache(TID)
    yield
    clear_tenant_cache(TID)


def use(monkeypatch, conn):
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))
    return conn


# ── _chat_bucket_stats (pure logic, no DB) ────────────────────────────────────


def test_chat_bucket_stats_counts_in_window():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, tzinfo=timezone.utc)
    rows = [
        (datetime(2026, 7, 2, tzinfo=timezone.utc), datetime(2026, 7, 3, tzinfo=timezone.utc), None),
        (datetime(2026, 6, 20, tzinfo=timezone.utc), datetime(2026, 6, 25, tzinfo=timezone.utc), None),  # out
    ]
    assert overview._chat_bucket_stats(rows, start, end) == {"web_chats": 1}


def test_chat_bucket_stats_prefers_last_at_over_started_at():
    """A session that started before the window but got a new message inside
    it must count as current activity — per spec."""
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, tzinfo=timezone.utc)
    rows = [(datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 7, 5, tzinfo=timezone.utc), None)]
    assert overview._chat_bucket_stats(rows, start, end) == {"web_chats": 1}


def test_chat_bucket_stats_falls_back_to_started_at_when_last_at_is_null():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, tzinfo=timezone.utc)
    rows = [(datetime(2026, 7, 2, tzinfo=timezone.utc), None, None)]
    assert overview._chat_bucket_stats(rows, start, end) == {"web_chats": 1}


def test_chat_bucket_stats_empty_rows_is_zero():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, tzinfo=timezone.utc)
    assert overview._chat_bucket_stats([], start, end) == {"web_chats": 0}


# ── _chat_rows fail-soft ──────────────────────────────────────────────────────


class _RaisingConn:
    def __init__(self):
        self.rolled_back = False

    def execute(self, *a, **kw):
        raise RuntimeError("relation \"chat_sessions\" does not exist")

    def rollback(self):
        self.rolled_back = True


def test_chat_rows_returns_rows_on_success():
    conn = FakeConn(rules=[("FROM chat_sessions", [(1, 2, "booked")])])
    rows = overview._chat_rows(conn, TID, datetime.now(timezone.utc))
    assert rows == [(1, 2, "booked")]


def test_chat_rows_fail_soft_rolls_back_and_returns_empty():
    conn = _RaisingConn()
    rows = overview._chat_rows(conn, TID, datetime.now(timezone.utc))
    assert rows == []
    assert conn.rolled_back is True


# ── GET /platform/overview (fake DB) ──────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(overview.router)
    return TestClient(app)


def test_overview_adds_web_chats_key_additively(client, monkeypatch):
    now = datetime.now(timezone.utc)
    conn = use(monkeypatch, FakeConn(rules=[
        ("FROM calls", []),
        ("FROM chat_sessions", [
            (now - timedelta(days=1), now - timedelta(hours=1), "booked"),
            (now - timedelta(days=10), now - timedelta(days=9), None),  # prior window
        ]),
    ]))
    body = client.get("/platform/overview", headers=HEADERS).json()

    # Pre-existing keys still present (additive, not a rebuild).
    assert "calls_answered" in body["current"]
    assert "after_hours_calls" in body["current"]
    # New key present in both buckets.
    assert body["current"]["web_chats"] == 1
    assert body["previous"]["web_chats"] == 1
    assert conn.sql_containing("FROM chat_sessions")[0][1]["tid"] == TID


def test_overview_chat_failure_does_not_break_calls_data(client, monkeypatch):
    """Fail-soft contract: a broken chat_sessions read must not 500 the whole
    Overview, and must not touch the calls-derived numbers."""

    class _PartialFailConn(FakeConn):
        def execute(self, stmt, params=None):
            sql = " ".join(str(stmt).split())
            if "chat_sessions" in sql:
                raise RuntimeError("db exploded")
            return super().execute(stmt, params)

        def rollback(self):
            pass

    conn = use(monkeypatch, _PartialFailConn(rules=[("FROM calls", [])]))
    r = client.get("/platform/overview", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current"]["web_chats"] == 0
    assert body["previous"]["web_chats"] == 0
    assert body["current"]["calls_answered"] == 0


def test_overview_language_mix_is_additive_and_buckets_correctly(client, monkeypatch):
    now = datetime.now(timezone.utc)
    conn = use(monkeypatch, FakeConn(rules=[
        ("FROM calls", [
            (now - timedelta(days=1), 60, "booked", "en"),
            (now - timedelta(days=2), 60, "info", "es"),
            (now - timedelta(days=3), 60, "info", None),  # unmapped/pre-migration row
            (now - timedelta(days=3), 60, "info", "fr"),  # unexpected value — still "unknown"
            (now - timedelta(days=10), 60, "info", "en"),  # prior window
        ]),
        ("FROM chat_sessions", []),
    ]))
    body = client.get("/platform/overview", headers=HEADERS).json()

    assert body["current"]["language_mix"] == {"en": 1, "es": 1, "unknown": 2}
    assert body["previous"]["language_mix"] == {"en": 1, "es": 0, "unknown": 0}
    # Pre-existing keys unaffected.
    assert body["current"]["calls_answered"] == 4


def test_overview_recent_activity_merges_calls_and_chats_newest_first(client, monkeypatch):
    now = datetime.now(timezone.utc)
    use(monkeypatch, FakeConn(rules=[
        ("FROM calls", [
            (now - timedelta(hours=1), 60, "booked", "en"),
            (now - timedelta(hours=5), 60, "info", "es"),
        ]),
        ("FROM chat_sessions", [
            (now - timedelta(hours=3), None, "escalated"),
        ]),
    ]))
    body = client.get("/platform/overview", headers=HEADERS).json()

    activity = body["recent_activity"]
    assert [item["type"] for item in activity] == ["call", "chat", "call"]
    assert activity[0]["outcome"] == "booked"
    assert activity[0]["language"] == "en"
    assert activity[1]["type"] == "chat" and activity[1]["language"] is None
    # Strictly newest-first.
    assert activity == sorted(activity, key=lambda x: x["at"], reverse=True)


def test_overview_recent_activity_caps_at_five(client, monkeypatch):
    now = datetime.now(timezone.utc)
    call_rows = [(now - timedelta(minutes=i), 60, "info", "en") for i in range(8)]
    use(monkeypatch, FakeConn(rules=[("FROM calls", call_rows), ("FROM chat_sessions", [])]))
    body = client.get("/platform/overview", headers=HEADERS).json()
    assert len(body["recent_activity"]) == 5


def test_setup_checklist_none_when_tenant_is_active(monkeypatch):
    from tenants import TenantState

    monkeypatch.setattr(
        overview,
        "tenant_state",
        lambda tid: TenantState(onboarding_status="active", account_status="live", plan="managed"),
    )
    assert overview._setup_checklist(TID) is None


def test_setup_checklist_none_when_tenant_unknown(monkeypatch):
    monkeypatch.setattr(overview, "tenant_state", lambda tid: None)
    assert overview._setup_checklist(TID) is None


def test_setup_checklist_reflects_voice_previewed_and_knowledge(monkeypatch):
    from datetime import timezone as tz_mod

    from tenants import TenantState

    monkeypatch.setattr(
        overview,
        "tenant_state",
        lambda tid: TenantState(
            onboarding_status="review", account_status="live", plan="managed",
            onboarding_voice_previewed_at=datetime(2026, 8, 7, tzinfo=tz_mod.utc),
        ),
    )
    conn = FakeConn(rules=[("FROM kb_entries", [(3,)])])
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))

    checklist = overview._setup_checklist(TID)
    assert checklist["onboarding_status"] == "review"
    items = {item["key"]: item["done"] for item in checklist["items"]}
    assert items == {"voice_previewed": True, "knowledge_added": True, "activated": False}


def test_setup_checklist_knowledge_not_added_when_zero_entries(monkeypatch):
    from tenants import TenantState

    monkeypatch.setattr(
        overview,
        "tenant_state",
        lambda tid: TenantState(onboarding_status="submitted", account_status="live", plan="managed"),
    )
    conn = FakeConn(rules=[("FROM kb_entries", [(0,)])])
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))

    checklist = overview._setup_checklist(TID)
    items = {item["key"]: item["done"] for item in checklist["items"]}
    assert items["voice_previewed"] is False
    assert items["knowledge_added"] is False


def test_overview_no_db_is_503(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.get("/platform/overview", headers=HEADERS)
    assert r.status_code == 503


def test_overview_requires_platform_secret(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM calls", []), ("FROM chat_sessions", [])]))
    r = client.get("/platform/overview", headers={"X-Tenant-Id": TID})
    assert r.status_code == 401


# ── Real Railway DB round trip (self-cleaning) ────────────────────────────────
# Skipped unless DATABASE_URL is set, e.g.:
#   railway run pytest evals/test_overview.py -v -k real_db

pytestmark_real_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set — no live Postgres to test against"
)


@pytestmark_real_db
def test_real_db_zero_rows_is_healthy():
    """A tenant_id with no chat_sessions rows in-window must resolve cleanly
    to web_chats=0 via a real round trip to Postgres — no exception, no
    fallback masking a broken query as 'empty'."""
    from platform_db import get_engine

    engine = get_engine()
    assert engine is not None, "DATABASE_URL set but get_engine() returned None"

    now = datetime.now(timezone.utc)
    prev_start = now - timedelta(days=14)
    nonexistent_tenant = f"__overview_test_nonexistent_{uuid.uuid4().hex[:8]}__"

    with engine.connect() as conn:
        rows = overview._chat_rows(conn, nonexistent_tenant, prev_start)
    assert rows == []
    assert overview._chat_bucket_stats(rows, now - timedelta(days=7), now) == {"web_chats": 0}


@pytestmark_real_db
def test_real_db_overview_route_reflects_chat_rows(monkeypatch):
    """Insert two throwaway chat_sessions rows for otro-nivel dated inside the
    current 7-day window, hit the real /platform/overview route, confirm
    web_chats rose by exactly 2 versus a baseline read taken first (delta,
    not an absolute count — otro-nivel is a real tenant that may have its own
    unrelated chat activity), then delete the rows so nothing is left behind."""
    from sqlalchemy import text

    from platform_db import get_engine
    from platform_api.chat_log import record_chat_turn

    engine = get_engine()
    assert engine is not None

    secret = os.environ.get("PLATFORM_API_SECRET", SECRET)
    monkeypatch.setenv("PLATFORM_API_SECRET", secret)
    app = FastAPI()
    app.include_router(overview.router)
    client = TestClient(app)
    headers = {"X-Platform-Secret": secret, "X-Tenant-Id": TID}

    baseline = client.get("/platform/overview", headers=headers).json()
    baseline_web_chats = baseline["current"]["web_chats"]

    thread_ids = [f"otro-nivel:overview-test-{uuid.uuid4().hex[:12]}" for _ in range(2)]
    try:
        for tid in thread_ids:
            record_chat_turn(TID, tid, set())

        body = client.get("/platform/overview", headers=headers).json()
        assert body["current"]["web_chats"] == baseline_web_chats + 2
    finally:
        with engine.begin() as conn:
            for tid in thread_ids:
                conn.execute(
                    text("DELETE FROM chat_sessions WHERE tenant_id = :tid AND thread_id = :thread"),
                    {"tid": TID, "thread": tid},
                )
