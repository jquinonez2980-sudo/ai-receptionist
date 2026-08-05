"""GET /platform/chats/{chat_id} — chat transcript detail.

Two layers:
  * _transcript_from_checkpoints is a pure function over LangChain message
    objects — no DB, no network. Tests the reconstruction logic directly:
    ordering, cross-checkpoint dedup, role filtering, empty-content skip.
  * Route + fail-closed tenant scoping against a recording fake DB
    (FakeConn/FakeEngine), with a stub checkpointer standing in for the real
    LangGraph Postgres one.

A real round trip against two actual production otro-nivel chat sessions
was run manually during development (see the PR/session notes) rather than
captured here — constructing a valid LangGraph checkpoint blob from scratch
for a fixture isn't worth it when SQL-shape tests already cover the DB half
and this file covers the transcript-reconstruction half directly.

Run: PYTHONUTF8=1 pytest evals/test_chats_detail.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage

os.environ.setdefault("OPENAI_API_KEY", "sk-test-unit")

import platform_api.chats as chats
from evals.test_signup import FakeConn, FakeEngine

SECRET = "test-platform-secret"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": "otro-nivel"}
TID = "otro-nivel"
CHAT_ID = "d684ef4f-9200-4f4c-aceb-462a89df7e96"


def use(monkeypatch, conn):
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))
    return conn


def ckpt(ts: str, messages: list) -> "ChatCheckpointTuple":
    return ChatCheckpointTuple(checkpoint={"ts": ts, "channel_values": {"messages": messages}})


class ChatCheckpointTuple:
    """Minimal stand-in for langgraph's CheckpointTuple — only .checkpoint is read."""

    def __init__(self, checkpoint):
        self.checkpoint = checkpoint


# ── _transcript_from_checkpoints (pure logic) ─────────────────────────────────


def test_basic_ordering():
    h = HumanMessage(content="hi", id="m1")
    a = AIMessage(content="hello!", id="m2")
    out = chats._transcript_from_checkpoints([ckpt("t1", [h, a])])
    assert out == [
        {"role": "user", "content": "hi", "timestamp": "t1"},
        {"role": "assistant", "content": "hello!", "timestamp": "t1"},
    ]


def test_dedup_across_checkpoints_keeps_first_seen_timestamp():
    """add_messages accumulates — a later checkpoint's message list is a
    superset of an earlier one's. Each message must appear exactly once,
    stamped with the checkpoint it FIRST appeared in."""
    h = HumanMessage(content="hi", id="m1")
    a1 = AIMessage(content="hello!", id="m2")
    h2 = HumanMessage(content="book me tuesday", id="m3")
    out = chats._transcript_from_checkpoints([
        ckpt("t1", [h, a1]),
        ckpt("t2", [h, a1, h2]),  # m1/m2 repeated, m3 new
    ])
    assert [m["content"] for m in out] == ["hi", "hello!", "book me tuesday"]
    assert out[0]["timestamp"] == "t1"
    assert out[2]["timestamp"] == "t2"


def test_removed_messages_still_appear_once_seen():
    """graph._compress_node issues a RemoveMessage for older turns once
    they're folded into conversation_summary. The transcript must still show
    them (recorded the first time they appeared) — a compressed message is
    not the same as a message that never happened."""
    h = HumanMessage(content="hi", id="m1")
    a = AIMessage(content="hello!", id="m2")
    out = chats._transcript_from_checkpoints([
        ckpt("t1", [h, a]),
        ckpt("t2", [RemoveMessage(id="m1"), RemoveMessage(id="m2")]),  # compressed away
    ])
    assert [m["content"] for m in out] == ["hi", "hello!"]


def test_tool_and_system_messages_excluded():
    h = HumanMessage(content="book me", id="m1")
    tool_call_ai = AIMessage(content="", id="m2", tool_calls=[{"name": "list_available_slots", "args": {}, "id": "c1"}])
    tm = ToolMessage(content="9am, 10am", id="m3", tool_call_id="c1")
    sm = SystemMessage(content="internal note", id="m4")
    final = AIMessage(content="Here are the times...", id="m5")
    out = chats._transcript_from_checkpoints([ckpt("t1", [h, tool_call_ai, tm, sm, final])])
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert [m["content"] for m in out] == ["book me", "Here are the times..."]


def test_empty_ai_content_skipped():
    """A pure tool-call AIMessage (no user-visible text) must not produce an
    empty bubble in the transcript."""
    h = HumanMessage(content="hi", id="m1")
    empty_ai = AIMessage(content="", id="m2", tool_calls=[{"name": "x", "args": {}, "id": "c1"}])
    out = chats._transcript_from_checkpoints([ckpt("t1", [h, empty_ai])])
    assert len(out) == 1
    assert out[0]["content"] == "hi"


def test_content_block_list_format_extracted():
    """AIMessage.content can be a list of content blocks, not just a string."""
    a = AIMessage(content=[{"type": "text", "text": "part one "}, {"type": "text", "text": "part two"}], id="m1")
    out = chats._transcript_from_checkpoints([ckpt("t1", [a])])
    assert out[0]["content"] == "part one part two"


def test_no_checkpoints_is_empty_transcript():
    assert chats._transcript_from_checkpoints([]) == []


# ── GET /platform/chats/{chat_id} (fake DB + stub checkpointer) ───────────────


class StubCheckpointer:
    """Async stand-in for the real LangGraph checkpointer — alist() yields
    canned CheckpointTuples in newest-first order, matching the real API."""

    def __init__(self, tuples_oldest_first: list):
        self._tuples = list(reversed(tuples_oldest_first))

    async def alist(self, config, *, filter=None, before=None, limit=None):
        for t in self._tuples[: limit or len(self._tuples)]:
            yield t


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(chats.router)
    return TestClient(app)


def session_row(**over):
    row = {
        "id": uuid.UUID(CHAT_ID),
        "thread_id": "otro-nivel:fbfa8476-a3c5-4214-b563-ad52ee7797b9",
        "channel": "web",
        "started_at": datetime(2026, 8, 4, 13, 50, 20, tzinfo=timezone.utc),
        "last_at": datetime(2026, 8, 4, 13, 50, 26, tzinfo=timezone.utc),
        "message_count": 2,
        "outcome": None,
        "summary": None,
    }
    row.update(over)
    return row


def set_graph_checkpointer(monkeypatch, checkpointer):
    import graph as _graph_module

    class FakeGraph:
        pass

    fake = FakeGraph()
    fake.checkpointer = checkpointer
    monkeypatch.setattr(_graph_module, "graph", fake)


def test_requires_platform_secret(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM chat_sessions", [session_row()])]))
    r = client.get(f"/platform/chats/{CHAT_ID}", headers={"X-Tenant-Id": TID})
    assert r.status_code == 401


def test_requires_a_tenant(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM chat_sessions", [session_row()])]))
    r = client.get(f"/platform/chats/{CHAT_ID}", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


def test_malformed_chat_id_is_400(client, monkeypatch):
    use(monkeypatch, FakeConn())
    r = client.get("/platform/chats/not-a-uuid", headers=HEADERS)
    assert r.status_code == 400


def test_unknown_chat_id_is_404(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM chat_sessions", [])]))
    r = client.get(f"/platform/chats/{CHAT_ID}", headers=HEADERS)
    assert r.status_code == 404


def test_tenant_scoped_in_the_query_fails_closed_on_mismatch(client, monkeypatch):
    """A real row that belongs to a DIFFERENT tenant must 404 exactly like an
    unknown id — the WHERE clause scopes by tenant_id, not just id, so the
    fake DB (which doesn't itself enforce the WHERE) simulates the mismatch
    case by returning no row, same as the real DB would."""
    conn = use(monkeypatch, FakeConn(rules=[("FROM chat_sessions", [])]))
    r = client.get(f"/platform/chats/{CHAT_ID}", headers=HEADERS)
    assert r.status_code == 404
    sql, params = conn.sql_containing("FROM chat_sessions")[0]
    assert "tenant_id = :tenant_id" in sql
    assert params["tenant_id"] == TID


def test_no_db_is_503(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.get(f"/platform/chats/{CHAT_ID}", headers=HEADERS)
    assert r.status_code == 503


def test_full_response_shape_with_transcript(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM chat_sessions", [session_row()])]))
    h = HumanMessage(content="What are your hours?", id="m1")
    a = AIMessage(content="We're open 9-5.", id="m2")
    set_graph_checkpointer(monkeypatch, StubCheckpointer([ckpt("2026-08-04T13:50:20+00:00", [h, a])]))

    r = client.get(f"/platform/chats/{CHAT_ID}", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TID
    assert body["id"] == CHAT_ID
    assert body["thread_id"] == "otro-nivel:fbfa8476-a3c5-4214-b563-ad52ee7797b9"
    assert body["message_count"] == 2
    assert body["messages"] == [
        {"role": "user", "content": "What are your hours?", "timestamp": "2026-08-04T13:50:20+00:00"},
        {"role": "assistant", "content": "We're open 9-5.", "timestamp": "2026-08-04T13:50:20+00:00"},
    ]


def test_missing_checkpointer_returns_empty_messages_not_an_error(client, monkeypatch):
    """Degraded but non-fatal: metadata still loads even if the checkpointer
    can't be reached (e.g. process hasn't finished startup)."""
    use(monkeypatch, FakeConn(rules=[("FROM chat_sessions", [session_row()])]))
    import graph as _graph_module

    class FakeGraph:
        checkpointer = None

    monkeypatch.setattr(_graph_module, "graph", FakeGraph())

    r = client.get(f"/platform/chats/{CHAT_ID}", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["messages"] == []
