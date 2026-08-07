"""Knowledge base moves from the ephemeral filesystem to Postgres.

The bug being fixed: dashboard KB entries were .md files under
tenants/<id>/kb/dashboard/, inside the container's writable layer. The
ai-receptionist service has no Railway volume, so every deploy deleted them —
silently, because the manager kept working right up until the next push.

Two halves are tested here:

  * platform_api/knowledge.py — the CRUD surface, against a recording fake DB
    (no live Postgres in this environment; SQL grammar still needs one real run).
  * tools.py — the retrieval corpus, which is the part that can quietly break
    the agent. The tests that matter most are that a DB read FAILURE degrades
    instead of masquerading as an empty knowledge base, and that DB content is
    folded into the index hash so an edit actually rebuilds.

Run: PYTHONUTF8=1 pytest evals/test_knowledge_db.py -v
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.knowledge as kb
import tools as T
from evals.test_signup import FakeConn, FakeEngine

SECRET = "test-platform-secret"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": "otro-nivel"}
TID = "otro-nivel"
EID = "a" * 32


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(kb.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_publish(monkeypatch):
    """_publish() reaches into tools to drop the FAISS cache; irrelevant here
    and it would try to build an index."""
    monkeypatch.setattr(kb, "_publish", lambda tid: None)


def faq_row(**over):
    row = {
        "id": EID, "kind": "faq", "question": "Do you sell gift cards?",
        "answer": "Yes, at the front desk.", "language": None, "source": None,
        "meta": None, "created_at": None,
    }
    row.update(over)
    return row


def pdf_row(**over):
    row = {
        "id": "b" * 32, "kind": "pdf", "question": None,
        "answer": "extracted text", "source": "knowledge-pdfs/otro-nivel/b.pdf",
        "meta": {"filename": "menu.pdf", "size_bytes": 1234, "pages": 3,
                 "truncated": False},
        "created_at": None,
    }
    row.update(over)
    return row


def use(monkeypatch, conn):
    monkeypatch.setattr(kb, "_db", lambda: FakeEngine(conn))
    return conn


# ── auth / fail-closed ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/platform/knowledge", None),
        ("post", "/platform/knowledge", {"answer": "x"}),
        ("put", f"/platform/knowledge/{EID}", {"answer": "x"}),
        ("delete", f"/platform/knowledge/{EID}", None),
    ],
)
def test_routes_require_the_platform_secret(client, monkeypatch, method, path, body):
    use(monkeypatch, FakeConn())
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(path, headers={"X-Tenant-Id": TID}, **kwargs)
    assert r.status_code == 401


def test_routes_require_a_tenant(client, monkeypatch):
    use(monkeypatch, FakeConn())
    r = client.get("/platform/knowledge", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


def test_default_tenant_is_rejected(client, monkeypatch):
    """Orchelix's own KB is git-managed, not dashboard-managed."""
    use(monkeypatch, FakeConn())
    r = client.get(
        "/platform/knowledge",
        headers={"X-Platform-Secret": SECRET, "X-Tenant-Id": "default"},
    )
    assert r.status_code == 400


def test_no_db_is_503_not_an_empty_list(client, monkeypatch):
    """Fail closed. Reporting "you have no entries" when the DB is unreachable
    would invite a tenant to re-add everything they already have."""
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.get("/platform/knowledge", headers=HEADERS)
    assert r.status_code == 503


# ── list ──────────────────────────────────────────────────────────────────────


def test_list_splits_faq_and_pdf_and_keeps_the_response_shape(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM kb_entries", [faq_row(), pdf_row()])]))
    body = client.get("/platform/knowledge", headers=HEADERS).json()

    assert [e["id"] for e in body["entries"]] == [EID]
    assert body["entries"][0]["question"] == "Do you sell gift cards?"

    assert len(body["pdfs"]) == 1
    pdf = body["pdfs"][0]
    assert pdf["filename"] == "menu.pdf"
    assert pdf["pages"] == 3
    assert pdf["size_bytes"] == 1234
    assert pdf["truncated"] is False
    assert pdf["has_original"] is True, "source set => original archived in R2"


def test_list_scopes_to_the_tenant(client, monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("FROM kb_entries", [])]))
    client.get("/platform/knowledge", headers=HEADERS)
    sql, params = conn.executed[0]
    assert "tenant_id = :tid" in sql
    assert params["tid"] == TID


def test_git_tracked_docs_are_still_counted(client, monkeypatch):
    """The onboarding .md files were never at risk and must keep showing up."""
    use(monkeypatch, FakeConn(rules=[("FROM kb_entries", [])]))
    body = client.get("/platform/knowledge", headers=HEADERS).json()
    assert body["other_docs_count"] > 0


def test_pdf_without_r2_reports_no_original(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM kb_entries", [pdf_row(source=None)])]))
    body = client.get("/platform/knowledge", headers=HEADERS).json()
    assert body["pdfs"][0]["has_original"] is False


def test_pdf_meta_as_json_string_is_parsed(client, monkeypatch):
    """Some drivers hand back jsonb as text."""
    row = pdf_row(meta=json.dumps({"filename": "x.pdf", "pages": 2}))
    use(monkeypatch, FakeConn(rules=[("FROM kb_entries", [row])]))
    body = client.get("/platform/knowledge", headers=HEADERS).json()
    assert body["pdfs"][0]["filename"] == "x.pdf"


# ── language (migration 0010) ─────────────────────────────────────────────────


def test_list_includes_language_when_set(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM kb_entries", [faq_row(language="es")])]))
    body = client.get("/platform/knowledge", headers=HEADERS).json()
    assert body["entries"][0]["language"] == "es"


def test_list_reports_null_language_as_none(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM kb_entries", [faq_row(language=None)])]))
    body = client.get("/platform/knowledge", headers=HEADERS).json()
    assert body["entries"][0]["language"] is None


def test_list_row_missing_language_key_degrades_to_none(client, monkeypatch):
    """Migration-safe: a row shape that predates 0010 (no 'language' key at
    all, not just a null value) must not KeyError."""
    row = faq_row()
    del row["language"]
    use(monkeypatch, FakeConn(rules=[("FROM kb_entries", [row])]))
    body = client.get("/platform/knowledge", headers=HEADERS).json()
    assert body["entries"][0]["language"] is None


# ── add ───────────────────────────────────────────────────────────────────────


def _add_conn(count=0):
    return FakeConn(rules=[
        ("count(*)", [(count,)]),
        ("INSERT INTO kb_entries", [faq_row()]),
    ])


def test_add_writes_a_row(client, monkeypatch):
    conn = use(monkeypatch, _add_conn())
    r = client.post("/platform/knowledge", json={"question": "Q?", "answer": "A."},
                    headers=HEADERS)
    assert r.status_code == 200, r.text
    ins = conn.sql_containing("INSERT INTO kb_entries")
    assert len(ins) == 1
    params = ins[0][1]
    assert params["tid"] == TID
    assert params["q"] == "Q?" and params["a"] == "A."
    assert len(params["id"]) == 32, "uuid4().hex, same id shape as before"


def test_add_rejects_an_empty_answer(client, monkeypatch):
    conn = use(monkeypatch, _add_conn())
    r = client.post("/platform/knowledge", json={"answer": "   "}, headers=HEADERS)
    assert r.status_code == 400
    assert conn.sql_containing("INSERT INTO kb_entries") == []


def test_add_enforces_the_entry_cap(client, monkeypatch):
    conn = use(monkeypatch, _add_conn(count=kb._MAX_ENTRIES))
    r = client.post("/platform/knowledge", json={"answer": "A."}, headers=HEADERS)
    assert r.status_code == 400
    assert conn.sql_containing("INSERT INTO kb_entries") == []


def test_blank_question_is_stored_as_null(client, monkeypatch):
    conn = use(monkeypatch, _add_conn())
    client.post("/platform/knowledge", json={"question": "  ", "answer": "A."},
                headers=HEADERS)
    assert conn.sql_containing("INSERT INTO kb_entries")[0][1]["q"] is None


@pytest.mark.parametrize("lang", ["en", "es", "auto", "EN", " es "])
def test_add_accepts_valid_languages_case_and_whitespace_insensitive(client, monkeypatch, lang):
    conn = use(monkeypatch, _add_conn())
    r = client.post("/platform/knowledge", json={"answer": "A.", "language": lang},
                    headers=HEADERS)
    assert r.status_code == 200, r.text
    assert conn.sql_containing("INSERT INTO kb_entries")[0][1]["lang"] == lang.strip().lower()


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_add_treats_blank_language_as_unspecified(client, monkeypatch, blank):
    conn = use(monkeypatch, _add_conn())
    body = {"answer": "A."}
    if blank is not None:
        body["language"] = blank
    client.post("/platform/knowledge", json=body, headers=HEADERS)
    assert conn.sql_containing("INSERT INTO kb_entries")[0][1]["lang"] is None


def test_add_rejects_an_invalid_language(client, monkeypatch):
    conn = use(monkeypatch, _add_conn())
    r = client.post("/platform/knowledge", json={"answer": "A.", "language": "fr"},
                    headers=HEADERS)
    assert r.status_code == 400
    assert "language" in r.json()["detail"]
    assert conn.sql_containing("INSERT INTO kb_entries") == []


def test_add_response_includes_the_saved_language(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[
        ("count(*)", [(0,)]),
        ("INSERT INTO kb_entries", [faq_row(language="en")]),
    ]))
    r = client.post("/platform/knowledge", json={"answer": "A.", "language": "en"},
                    headers=HEADERS)
    assert r.json()["entry"]["language"] == "en"


# ── edit (new) ────────────────────────────────────────────────────────────────


def test_edit_updates_in_place(client, monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("UPDATE kb_entries", [faq_row(answer="new")])]))
    r = client.put(f"/platform/knowledge/{EID}", json={"question": "Q?", "answer": "new"},
                   headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["answer"] == "new"
    sql, params = conn.sql_containing("UPDATE kb_entries")[0]
    assert "updated_at = now()" in sql
    assert params["id"] == EID and params["tid"] == TID


def test_edit_is_scoped_by_tenant(client, monkeypatch):
    """A valid id belonging to another tenant must not be editable. Scoping is
    the isolation guarantee — not the fact that ids are unguessable uuids."""
    conn = use(monkeypatch, FakeConn(rules=[("UPDATE kb_entries", [])]))
    r = client.put(f"/platform/knowledge/{EID}", json={"answer": "x"}, headers=HEADERS)
    assert r.status_code == 404
    assert "tenant_id = :tid" in conn.sql_containing("UPDATE kb_entries")[0][0]


def test_edit_cannot_touch_a_pdf_row(client, monkeypatch):
    """PDF text comes from the uploaded file; editing it here would make the
    entry disagree with the original archived in R2."""
    conn = use(monkeypatch, FakeConn(rules=[("UPDATE kb_entries", [])]))
    client.put(f"/platform/knowledge/{EID}", json={"answer": "x"}, headers=HEADERS)
    assert "kind = 'faq'" in conn.sql_containing("UPDATE kb_entries")[0][0]


def test_edit_rejects_a_malformed_id(client, monkeypatch):
    conn = use(monkeypatch, FakeConn())
    r = client.put("/platform/knowledge/not-an-id", json={"answer": "x"}, headers=HEADERS)
    assert r.status_code == 400
    assert conn.sql_containing("UPDATE kb_entries") == []


def test_edit_and_add_share_one_validator(client, monkeypatch):
    """So limits can't drift between the two paths."""
    conn = use(monkeypatch, FakeConn())
    r = client.put(f"/platform/knowledge/{EID}",
                   json={"answer": "x" * (kb._MAX_ANSWER_LEN + 1)}, headers=HEADERS)
    assert r.status_code == 400
    assert conn.sql_containing("UPDATE kb_entries") == []


def test_edit_updates_the_language(client, monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("UPDATE kb_entries", [faq_row(language="es")])]))
    r = client.put(f"/platform/knowledge/{EID}", json={"answer": "x", "language": "es"},
                   headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["language"] == "es"
    assert conn.sql_containing("UPDATE kb_entries")[0][1]["lang"] == "es"


def test_edit_rejects_an_invalid_language(client, monkeypatch):
    conn = use(monkeypatch, FakeConn())
    r = client.put(f"/platform/knowledge/{EID}", json={"answer": "x", "language": "fr"},
                   headers=HEADERS)
    assert r.status_code == 400
    assert conn.sql_containing("UPDATE kb_entries") == []


def test_edit_clears_language_when_sent_blank(client, monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[("UPDATE kb_entries", [faq_row(language=None)])]))
    client.put(f"/platform/knowledge/{EID}", json={"answer": "x", "language": ""},
              headers=HEADERS)
    assert conn.sql_containing("UPDATE kb_entries")[0][1]["lang"] is None


# ── delete ────────────────────────────────────────────────────────────────────


def test_delete_removes_the_row(client, monkeypatch):
    conn = use(monkeypatch, FakeConn(rules=[
        ("DELETE FROM kb_entries", [{"kind": "faq", "source": None}])
    ]))
    r = client.delete(f"/platform/knowledge/{EID}", headers=HEADERS)
    assert r.status_code == 200
    sql, params = conn.sql_containing("DELETE FROM kb_entries")[0]
    assert "tenant_id = :tid" in sql
    assert params["tid"] == TID


def test_delete_unknown_is_404(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("DELETE FROM kb_entries", [])]))
    r = client.delete(f"/platform/knowledge/{EID}", headers=HEADERS)
    assert r.status_code == 404


def test_deleting_a_pdf_removes_the_r2_original(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[
        ("DELETE FROM kb_entries", [{"kind": "pdf", "source": "knowledge-pdfs/x.pdf"}])
    ]))
    deleted = {}
    monkeypatch.setitem(
        __import__("sys").modules, "platform_api.recordings",
        type("M", (), {
            "r2_configured": staticmethod(lambda: True),
            "_bucket": staticmethod(lambda: "bkt"),
            "_get_client": staticmethod(lambda: type("C", (), {
                "delete_object": staticmethod(
                    lambda **kw: deleted.update(kw)
                )
            })()),
        })(),
    )
    r = client.delete(f"/platform/knowledge/{EID}", headers=HEADERS)
    assert r.status_code == 200
    assert deleted.get("Key") == "knowledge-pdfs/x.pdf"


# ── retrieval corpus (tools.py) ───────────────────────────────────────────────


def test_db_read_failure_raises_rather_than_returning_empty(monkeypatch):
    """The single most important behavior here. If a failed read looked like
    an empty KB, the agent would answer as though the tenant had written
    nothing — confidently and wrongly."""
    class Boom:
        def connect(self):
            raise RuntimeError("connection reset")

    monkeypatch.setattr("platform_db.get_engine", lambda: Boom())
    with pytest.raises(T._KBUnavailable):
        T._kb_db_entries("otro-nivel")


def test_no_database_url_is_a_legitimate_empty(monkeypatch):
    """Local dev / tests: git docs only is the complete answer, not a degraded
    one, so this must NOT raise."""
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    assert T._kb_db_entries("otro-nivel") == []


def test_default_tenant_never_queries_the_db(monkeypatch):
    called = []
    monkeypatch.setattr("platform_db.get_engine", lambda: called.append(1))
    assert T._kb_db_entries("default") == []
    assert called == []


def test_db_entries_render_like_the_git_faq_docs(monkeypatch):
    conn = FakeConn(rules=[("FROM kb_entries", [("id1", "Q?", "A.")])])
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))
    assert T._kb_db_entries("otro-nivel") == [("id1", "**Q: Q?**\nA: A.\n")]


def test_db_entry_without_a_question_is_a_plain_note(monkeypatch):
    conn = FakeConn(rules=[("FROM kb_entries", [("id1", None, "Just a note.")])])
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))
    assert T._kb_db_entries("otro-nivel") == [("id1", "Just a note.\n")]


# ── index hash ────────────────────────────────────────────────────────────────


def test_hash_changes_when_a_db_entry_is_added():
    """Without this the index would never rebuild after a dashboard edit — a
    process holding a persisted index would keep serving the old corpus."""
    src = T._kb_dir("otro-nivel")
    assert T._kb_content_hash(src, []) != T._kb_content_hash(src, [("a", "body")])


def test_hash_changes_when_a_db_entry_is_edited():
    """Edits can leave length unchanged, so the hash must cover the text."""
    src = T._kb_dir("otro-nivel")
    a = T._kb_content_hash(src, [("a", "aaaa")])
    b = T._kb_content_hash(src, [("a", "bbbb")])
    assert a != b


def test_hash_is_stable_for_identical_input():
    src = T._kb_dir("otro-nivel")
    entries = [("a", "one"), ("b", "two")]
    assert T._kb_content_hash(src, entries) == T._kb_content_hash(src, entries)


def test_hash_distinguishes_entry_ids():
    src = T._kb_dir("otro-nivel")
    assert T._kb_content_hash(src, [("a", "x")]) != T._kb_content_hash(src, [("b", "x")])
