"""Model-free tests for the leads sink (finding 7.1): leads.py's persistence
helpers, and graph.py's _maybe_record_lead wiring into the booker/closer nodes.
No network, no model, no OPENAI_API_KEY needed.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "sk-test-unit")


# ── leads.record_lead ─────────────────────────────────────────────────────────

def test_record_lead_noop_without_database_url():
    import leads

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        leads.record_lead("t1", "default", 90, True)  # must not raise


def test_record_lead_upserts_with_correct_params():
    import leads

    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch.dict(os.environ, {"DATABASE_URL": "postgres://fake/db"}), \
         patch("psycopg.connect", return_value=mock_conn) as mock_connect:
        leads.record_lead("thread-abc", "default", 90, True, contact="jane@x.com", summary="wants intro call")

    assert mock_connect.call_args.args[0] == "postgres://fake/db"
    calls = mock_cursor.execute.call_args_list
    assert len(calls) == 2, "expected a CREATE TABLE + an INSERT/upsert"
    upsert_sql, upsert_params = calls[1].args
    assert "INSERT INTO leads" in upsert_sql
    assert upsert_params["thread_id"] == "thread-abc"
    assert upsert_params["contact"] == "jane@x.com"
    assert upsert_params["qualified"] is True


def test_record_lead_swallows_db_errors():
    import leads

    with patch.dict(os.environ, {"DATABASE_URL": "postgres://fake/db"}), \
         patch("psycopg.connect", side_effect=RuntimeError("connection refused")):
        leads.record_lead("thread-err", "default", 50, True)  # must not raise


# ── leads.list_leads ──────────────────────────────────────────────────────────

def test_list_leads_returns_rows_with_isoformat_timestamp():
    import leads

    fake_rows = [{
        "thread_id": "t1", "tenant_id": "default", "lead_score": 90, "qualified": True,
        "contact": "jane@x.com", "summary": "hot lead", "last_updated": datetime.now(timezone.utc),
    }]
    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = fake_rows
    mock_cursor_cm = MagicMock(__aenter__=AsyncMock(return_value=mock_cursor), __aexit__=AsyncMock(return_value=False))
    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor_cm)
    mock_conn_cm = MagicMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock(return_value=False))

    with patch.dict(os.environ, {"DATABASE_URL": "postgres://fake/db"}), \
         patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=mock_conn_cm)):
        result = asyncio.run(leads.list_leads(limit=10))

    assert len(result) == 1
    assert result[0]["thread_id"] == "t1"
    assert isinstance(result[0]["last_updated"], str)  # not a raw datetime


def test_list_leads_empty_without_database_url():
    import leads

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        assert asyncio.run(leads.list_leads()) == []


# ── graph._maybe_record_lead wiring ───────────────────────────────────────────

def test_booker_node_records_lead_on_successful_booking():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from graph import _make_booker_node

    class FakeBooker:
        def invoke(self, state, config=None):
            prior = state["messages"]
            ai = AIMessage(content="", tool_calls=[{
                "name": "book_appointment", "id": "call_1",
                "args": {"summary": "Intro Call", "start_time": "2026-07-14T10:00:00-04:00",
                          "end_time": "2026-07-14T10:30:00-04:00", "attendee_email": "jane@x.com"},
            }])
            tm = ToolMessage(content="Booked — confirmed for Tuesday, July 14 at 10:00 AM.",
                              name="book_appointment", tool_call_id="call_1")
            return {"messages": prior + [ai, tm]}

    node = _make_booker_node(FakeBooker())
    state = {"messages": [HumanMessage(content="book it")], "appointment_details": None,
             "lead_score": 0, "qualified": False, "next": "booker"}

    with patch("leads.record_lead") as mock_record:
        node(state, config={"configurable": {"thread_id": "t-booker", "tenant_id": "default"}})

    mock_record.assert_called_once()
    kwargs = mock_record.call_args.kwargs
    assert kwargs["thread_id"] == "t-booker"
    assert kwargs["qualified"] is True
    assert kwargs["contact"] == "jane@x.com"


def test_booker_node_does_not_record_lead_on_failed_booking():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from graph import _make_booker_node

    class FakeBooker:
        def invoke(self, state, config=None):
            prior = state["messages"]
            ai = AIMessage(content="", tool_calls=[{
                "name": "book_appointment", "id": "call_1",
                "args": {"summary": "Intro Call", "start_time": "2026-07-14T20:00:00-04:00",
                          "end_time": "2026-07-14T20:30:00-04:00", "attendee_email": "jane@x.com"},
            }])
            tm = ToolMessage(content="That's outside our business hours (9 AM–5 PM).",
                              name="book_appointment", tool_call_id="call_1")
            return {"messages": prior + [ai, tm]}

    node = _make_booker_node(FakeBooker())
    state = {"messages": [HumanMessage(content="book it")], "appointment_details": None,
             "lead_score": 0, "qualified": False, "next": "booker"}

    with patch("leads.record_lead") as mock_record:
        node(state, config={"configurable": {"thread_id": "t-booker-fail", "tenant_id": "default"}})

    mock_record.assert_not_called()


def test_closer_node_records_lead_on_escalation():
    from langchain_core.messages import AIMessage, HumanMessage
    from graph import _make_closer_node

    class FakeCloser:
        def invoke(self, state, config=None):
            prior = state["messages"]
            ai = AIMessage(content="", tool_calls=[{
                "name": "escalate_to_human", "id": "call_2",
                "args": {"reason": "hot lead — budget approved", "user_summary": "Wants Esmi for their dental office."},
            }])
            return {"messages": prior + [ai]}

    node = _make_closer_node(FakeCloser())
    state = {"messages": [HumanMessage(content="we have budget approved")], "appointment_details": None,
             "lead_score": 20, "qualified": False, "next": None}

    with patch("leads.record_lead") as mock_record:
        node(state, config={"configurable": {"thread_id": "t-closer", "tenant_id": "default"}})

    mock_record.assert_called_once()
    kwargs = mock_record.call_args.kwargs
    assert kwargs["thread_id"] == "t-closer"
    assert kwargs["qualified"] is True
    assert kwargs["summary"] == "Wants Esmi for their dental office."
