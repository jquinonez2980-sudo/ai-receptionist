"""Chat-session logging: platform_api/chat_log.py's upsert helper and
platform_api/chats.py's GET /platform/chats route.

Two layers, same split as test_knowledge_db.py:
  * Route + upsert-SQL shape against a recording fake DB (FakeConn/FakeEngine,
    no live Postgres needed).
  * A self-cleaning round trip against the REAL Railway DB (skipped unless
    DATABASE_URL is set — e.g. `railway run pytest evals/test_chat_log.py -v`).

Run: PYTHONUTF8=1 pytest evals/test_chat_log.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "sk-test-unit")

import platform_api.chat_log as chat_log
import platform_api.chats as chats
from evals.test_signup import FakeConn, FakeEngine

SECRET = "test-platform-secret"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": "otro-nivel"}
TID = "otro-nivel"


def use(monkeypatch, conn):
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))
    return conn


# ── derive_outcome ────────────────────────────────────────────────────────────


def test_derive_outcome_none_without_tools():
    assert chat_log.derive_outcome(None) is None
    assert chat_log.derive_outcome(set()) is None


def test_derive_outcome_booked_takes_precedence():
    assert chat_log.derive_outcome({"book_appointment", "escalate_to_human"}) == "booked"


def test_derive_outcome_escalated():
    assert chat_log.derive_outcome({"escalate_to_human"}) == "escalated"
    assert chat_log.derive_outcome({"transfercall"}) == "escalated"


def test_derive_outcome_unrelated_tool_is_none():
    assert chat_log.derive_outcome({"search_knowledge_base"}) is None


# ── record_chat_turn (fake DB) ────────────────────────────────────────────────


def test_record_chat_turn_noop_without_database_url(monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    chat_log.record_chat_turn("otro-nivel", "otro-nivel:t1")  # must not raise


def test_record_chat_turn_upserts_with_correct_params(monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("INSERT INTO chat_sessions", [])]))
    chat_log.record_chat_turn("otro-nivel", "otro-nivel:t1", {"book_appointment"})

    ins = conn.sql_containing("INSERT INTO chat_sessions")
    assert len(ins) == 1
    sql, params = ins[0]
    assert "ON CONFLICT (tenant_id, thread_id)" in sql
    assert params["tenant_id"] == "otro-nivel"
    assert params["thread_id"] == "otro-nivel:t1"
    assert params["messages_per_turn"] == 2
    assert params["outcome"] == "booked"


def test_record_chat_turn_ensures_tenant_row_first(monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("INSERT INTO chat_sessions", [])]))
    chat_log.record_chat_turn("otro-nivel", "otro-nivel:t1")
    order = [sql for sql, _ in conn.executed]
    assert any("INSERT INTO tenants" in s for s in order)
    assert order.index(next(s for s in order if "INSERT INTO tenants" in s)) < order.index(
        next(s for s in order if "INSERT INTO chat_sessions" in s)
    )


def test_record_chat_turn_swallows_db_errors(monkeypatch):
    class Boom:
        def begin(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("platform_db.get_engine", lambda: Boom())
    chat_log.record_chat_turn("otro-nivel", "otro-nivel:t1")  # must not raise


def test_record_chat_turn_without_tools_leaves_outcome_null(monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("INSERT INTO chat_sessions", [])]))
    chat_log.record_chat_turn("otro-nivel", "otro-nivel:t1", set())
    params = conn.sql_containing("INSERT INTO chat_sessions")[0][1]
    assert params["outcome"] is None


# ── GET /platform/chats (fake DB) ─────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(chats.router)
    return TestClient(app)


def chat_row(**over):
    row = {
        "id": uuid.uuid4(), "thread_id": "otro-nivel:t1", "channel": "web",
        "started_at": None, "last_at": None, "message_count": 2,
        "outcome": None, "summary": None,
    }
    row.update(over)
    return row


def test_route_requires_platform_secret(client, monkeypatch):
    use(monkeypatch, FakeConn())
    r = client.get("/platform/chats", headers={"X-Tenant-Id": TID})
    assert r.status_code == 401


def test_route_requires_a_tenant(client, monkeypatch):
    use(monkeypatch, FakeConn())
    r = client.get("/platform/chats", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


def test_no_db_is_503_not_an_empty_list(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.get("/platform/chats", headers=HEADERS)
    assert r.status_code == 503


def test_list_returns_rows_shaped_like_calls_minus_recording(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[
        ("count(*)", [(1,)]),
        ("FROM chat_sessions", [chat_row(outcome="booked", message_count=4)]),
    ]))
    body = client.get("/platform/chats", headers=HEADERS).json()

    assert body["total"] == 1
    row = body["chats"][0]
    assert row["thread_id"] == "otro-nivel:t1"
    assert row["outcome"] == "booked"
    assert row["message_count"] == 4
    assert "recording_url" not in row
    assert "transcript" not in row


def test_list_scopes_to_the_tenant(client, monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("count(*)", [(0,)]), ("FROM chat_sessions", [])]))
    client.get("/platform/chats", headers=HEADERS)
    sql, params = conn.sql_containing("FROM chat_sessions")[0]
    assert "tenant_id = :tenant_id" in sql
    assert params["tenant_id"] == TID


def test_invalid_outcome_is_400(client, monkeypatch):
    use(monkeypatch, FakeConn())
    r = client.get("/platform/chats?outcome=bogus", headers=HEADERS)
    assert r.status_code == 400


def test_invalid_date_is_400(client, monkeypatch):
    use(monkeypatch, FakeConn())
    r = client.get("/platform/chats?from_date=not-a-date", headers=HEADERS)
    assert r.status_code == 400


def test_outcome_filter_is_applied(client, monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("count(*)", [(0,)]), ("FROM chat_sessions", [])]))
    client.get("/platform/chats?outcome=escalated", headers=HEADERS)
    sql, params = conn.sql_containing("FROM chat_sessions")[0]
    assert "outcome = :outcome" in sql
    assert params["outcome"] == "escalated"


# ── Real Railway DB round trip (self-cleaning) ────────────────────────────────
# Skipped unless DATABASE_URL is set, e.g.:
#   railway run pytest evals/test_chat_log.py -v -k real_db

pytestmark_real_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set — no live Postgres to test against"
)


@pytestmark_real_db
def test_real_db_record_then_list_round_trip(monkeypatch):
    """Insert two turns for a throwaway thread, confirm the row accumulates
    message_count and outcome correctly via the real /platform/chats route,
    then delete the row so the test leaves no trace."""
    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    assert engine is not None, "DATABASE_URL set but get_engine() returned None"

    tenant_id = "otro-nivel"
    thread_id = f"otro-nivel:test-{uuid.uuid4().hex[:12]}"

    try:
        chat_log.record_chat_turn(tenant_id, thread_id, set())
        chat_log.record_chat_turn(tenant_id, thread_id, {"book_appointment"})

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT message_count, outcome FROM chat_sessions "
                    "WHERE tenant_id = :tid AND thread_id = :thread"
                ),
                {"tid": tenant_id, "thread": thread_id},
            ).mappings().first()

        assert row is not None
        assert row["message_count"] == 4
        assert row["outcome"] == "booked"

        secret = os.environ.get("PLATFORM_API_SECRET", SECRET)
        monkeypatch.setenv("PLATFORM_API_SECRET", secret)
        app = FastAPI()
        app.include_router(chats.router)
        client = TestClient(app)
        body = client.get(
            "/platform/chats",
            headers={"X-Platform-Secret": secret, "X-Tenant-Id": tenant_id},
        ).json()
        matched = [c for c in body["chats"] if c["thread_id"] == thread_id]
        assert len(matched) == 1
        assert matched[0]["message_count"] == 4
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM chat_sessions WHERE tenant_id = :tid AND thread_id = :thread"),
                {"tid": tenant_id, "thread": thread_id},
            )
