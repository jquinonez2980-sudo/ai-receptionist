"""GET /platform/calls (list) and GET /platform/calls/{id} (detail) —
docs/ESMI_DASHBOARD_UX.md Section 5.2.

DB is a recording fake (FakeConn/FakeEngine, imported from
evals.test_signup — same pattern evals/test_chats_detail.py already
established for a sibling detail endpoint): no live Postgres in this
environment, so these tests verify handler control flow, SQL shape, bound
params, auth, and the response contract — not Postgres grammar itself.
R2 signed-URL behavior is tested against a fake boto3-shaped client
(platform_api.recordings._get_client/_bucket monkeypatched directly), not
mocked away entirely, since "never a long-lived public bucket URL" is
exactly the property worth verifying here.

What matters here:
  1. Auth: same verify_platform_secret/require_tenant every other route uses,
     on both list and detail.
  2. Tenant isolation: the WHERE clause scopes both list and detail by
     tenant_id, not just id — detail 404s a cross-tenant call id exactly
     like an unknown one (same fail-closed convention as platform_api/
     chats.py's GET /platform/chats/{id}).
  3. Malformed call_id -> 404 for detail (never a 400/500 leaking that the
     id shape was wrong vs. genuinely not found).
  4. List filters (outcome, from_date/to_date, language, has_recording) all
     reach the WHERE clause with the right bound params/SQL fragments.
  5. Signed URL: an R2 object key gets presigned (short-lived, bucket stays
     private); a legacy http(s) URL passes through unchanged; a missing key
     returns recording_url: null rather than erroring.
  6. list and detail return byte-identical field shapes for the same row —
     both go through the same _row_to_call helper, so they can't drift.
  7. No DB configured -> 503 for both.
  8. call_log._detect_language: the cheap heuristic that populates the new
     language column at ingest time.

Run: PYTHONUTF8=1 pytest evals/test_calls.py -v
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.call_log as call_log
import platform_api.calls as calls
import platform_api.recordings as recordings
from evals.test_signup import FakeConn, FakeEngine

SECRET = "test-platform-secret"
TID = "otro-nivel"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": TID}
CALL_ID = "d684ef4f-9200-4f4c-aceb-462a89df7e96"


def use(monkeypatch, conn):
    monkeypatch.setattr("platform_db.get_engine", lambda: FakeEngine(conn))
    return conn


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(calls.router)
    return TestClient(app)


def call_row(**over):
    row = {
        "id": uuid.UUID(CALL_ID),
        "vapi_call_id": "vapi-call-123",
        "caller_e164": "+16475551234",
        "started_at": datetime(2026, 8, 4, 13, 50, 20, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 8, 4, 13, 52, 0, tzinfo=timezone.utc),
        "duration_sec": 100,
        "outcome": "booked",
        "language": "en",
        "summary": "Caller booked a haircut for Thursday.",
        "transcript": {"text": "hi", "messages": []},
        "recording_key": None,
        "cost_vapi": None,
        "cost_llm": None,
        "created_at": datetime(2026, 8, 4, 13, 52, 5, tzinfo=timezone.utc),
    }
    row.update(over)
    return row


def conn_for_list(rows, total=None):
    return FakeConn(
        rules=[
            ("SELECT count(*) FROM calls", [total if total is not None else len(rows)]),
            ("ORDER BY started_at DESC", rows),
        ]
    )


class FakeS3:
    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://r2.example/{Params['Key']}?ttl={ExpiresIn}"


@pytest.fixture(autouse=True)
def _r2(monkeypatch):
    monkeypatch.setattr(recordings, "_get_client", lambda: FakeS3())
    monkeypatch.setattr(recordings, "_bucket", lambda: "bkt")


# ── auth ──────────────────────────────────────────────────────────────────


def test_list_requires_platform_secret(client, monkeypatch):
    use(monkeypatch, conn_for_list([]))
    r = client.get("/platform/calls", headers={"X-Tenant-Id": TID})
    assert r.status_code == 401


def test_list_requires_tenant(client, monkeypatch):
    use(monkeypatch, conn_for_list([]))
    r = client.get("/platform/calls", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


def test_detail_requires_platform_secret(client, monkeypatch):
    use(monkeypatch, FakeConn())
    r = client.get(f"/platform/calls/{CALL_ID}", headers={"X-Tenant-Id": TID})
    assert r.status_code == 401


def test_detail_requires_tenant(client, monkeypatch):
    use(monkeypatch, FakeConn())
    r = client.get(f"/platform/calls/{CALL_ID}", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


# ── DB unavailable ───────────────────────────────────────────────────────


def test_list_no_db_is_503(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.get("/platform/calls", headers=HEADERS)
    assert r.status_code == 503


def test_detail_no_db_is_503(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.get(f"/platform/calls/{CALL_ID}", headers=HEADERS)
    assert r.status_code == 503


# ── validation ────────────────────────────────────────────────────────────


def test_bad_outcome_is_400(client, monkeypatch):
    use(monkeypatch, conn_for_list([]))
    r = client.get("/platform/calls?outcome=not-a-real-outcome", headers=HEADERS)
    assert r.status_code == 400


def test_bad_from_date_is_400(client, monkeypatch):
    use(monkeypatch, conn_for_list([]))
    r = client.get("/platform/calls?from_date=not-a-date", headers=HEADERS)
    assert r.status_code == 400


def test_malformed_call_id_is_404_not_400(client, monkeypatch):
    """A shape error reads identically to "not found" — never leaks that the
    id format itself was the problem."""
    use(monkeypatch, FakeConn())
    r = client.get("/platform/calls/not-a-uuid", headers=HEADERS)
    assert r.status_code == 404


# ── tenant isolation ──────────────────────────────────────────────────────


def test_unknown_call_id_is_404(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("WHERE id = :id AND tenant_id = :tenant_id", [])]))
    r = client.get(f"/platform/calls/{CALL_ID}", headers=HEADERS)
    assert r.status_code == 404


def test_cross_tenant_call_id_404s_identically_to_unknown(client, monkeypatch):
    """A real row belonging to a DIFFERENT tenant must 404 exactly like an
    unknown id — the WHERE clause scopes by tenant_id, not just id, so the
    fake DB (which doesn't itself enforce the WHERE) simulates the mismatch
    by returning no row, same as the real DB would for another tenant's call."""
    conn = use(monkeypatch, FakeConn(rules=[("WHERE id = :id AND tenant_id = :tenant_id", [])]))
    r = client.get(f"/platform/calls/{CALL_ID}", headers=HEADERS)
    assert r.status_code == 404
    sql, params = conn.sql_containing("FROM calls")[0]
    assert "tenant_id = :tenant_id" in sql
    assert params["tenant_id"] == TID


def test_list_scopes_by_tenant_id(client, monkeypatch):
    conn = use(monkeypatch, conn_for_list([]))
    client.get("/platform/calls", headers=HEADERS)
    sql, params = conn.sql_containing("SELECT count(*)")[0]
    assert "tenant_id = :tenant_id" in sql
    assert params["tenant_id"] == TID


# ── filters reach the WHERE clause ──────────────────────────────────────────


def test_outcome_filter_reaches_query(client, monkeypatch):
    conn = use(monkeypatch, conn_for_list([]))
    client.get("/platform/calls?outcome=booked", headers=HEADERS)
    sql, params = conn.sql_containing("ORDER BY started_at DESC")[0]
    assert "outcome = :outcome" in sql
    assert params["outcome"] == "booked"


def test_date_range_filter_reaches_query(client, monkeypatch):
    conn = use(monkeypatch, conn_for_list([]))
    client.get("/platform/calls?from_date=2026-08-01&to_date=2026-08-05", headers=HEADERS)
    sql, params = conn.sql_containing("ORDER BY started_at DESC")[0]
    assert "started_at >= :d_from" in sql
    assert "started_at < :d_to_excl" in sql
    assert params["d_from"].isoformat() == "2026-08-01"
    # to_date is inclusive on the calendar day -> exclusive bound is the next day
    assert params["d_to_excl"].isoformat() == "2026-08-06"


def test_language_filter_reaches_query(client, monkeypatch):
    conn = use(monkeypatch, conn_for_list([]))
    client.get("/platform/calls?language=es", headers=HEADERS)
    sql, params = conn.sql_containing("ORDER BY started_at DESC")[0]
    assert "language = :language" in sql
    assert params["language"] == "es"


@pytest.mark.parametrize("value,expected_sql", [("true", "recording_key IS NOT NULL"), ("false", "recording_key IS NULL")])
def test_has_recording_filter_reaches_query(client, monkeypatch, value, expected_sql):
    conn = use(monkeypatch, conn_for_list([]))
    client.get(f"/platform/calls?has_recording={value}", headers=HEADERS)
    sql, _params = conn.sql_containing("ORDER BY started_at DESC")[0]
    assert expected_sql in sql


def test_no_has_recording_filter_omits_the_clause(client, monkeypatch):
    conn = use(monkeypatch, conn_for_list([]))
    client.get("/platform/calls", headers=HEADERS)
    sql, _params = conn.sql_containing("ORDER BY started_at DESC")[0]
    assert "recording_key IS" not in sql


# ── response shape / signed URLs ────────────────────────────────────────────


def test_list_response_shape(client, monkeypatch):
    use(monkeypatch, conn_for_list([call_row()]))
    r = client.get("/platform/calls", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TID
    assert body["total"] == 1
    c = body["calls"][0]
    assert c["id"] == CALL_ID
    assert c["caller"] == "+16475551234"
    assert c["duration_sec"] == 100
    assert c["outcome"] == "booked"
    assert c["language"] == "en"
    assert c["summary"] == "Caller booked a haircut for Thursday."
    assert c["recording_url"] is None  # no recording_key on this row


def test_detail_response_shape_matches_list_shape(client, monkeypatch):
    """list and detail must never drift — same row through the same
    shaping function."""
    use(monkeypatch, conn_for_list([call_row(recording_key="recordings/otro-nivel/abc.wav")]))
    list_body = client.get("/platform/calls", headers=HEADERS).json()

    use(monkeypatch, FakeConn(rules=[(
        "WHERE id = :id AND tenant_id = :tenant_id",
        [call_row(recording_key="recordings/otro-nivel/abc.wav")],
    )]))
    detail_body = client.get(f"/platform/calls/{CALL_ID}", headers=HEADERS).json()

    assert list_body["calls"][0] == detail_body["call"]


def test_r2_key_is_presigned_short_lived(client, monkeypatch):
    use(monkeypatch, conn_for_list([call_row(recording_key="recordings/otro-nivel/abc.wav")]))
    r = client.get("/platform/calls", headers=HEADERS)
    url = r.json()["calls"][0]["recording_url"]
    assert url == "https://r2.example/recordings/otro-nivel/abc.wav?ttl=3600"


def test_legacy_http_recording_url_passes_through_unchanged(client, monkeypatch):
    legacy_url = "https://storage.vapi.ai/some-legacy-recording.wav"
    use(monkeypatch, conn_for_list([call_row(recording_key=legacy_url)]))
    r = client.get("/platform/calls", headers=HEADERS)
    assert r.json()["calls"][0]["recording_url"] == legacy_url


def test_missing_recording_key_is_null_not_an_error(client, monkeypatch):
    use(monkeypatch, conn_for_list([call_row(recording_key=None)]))
    r = client.get("/platform/calls", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["calls"][0]["recording_url"] is None


def test_presign_failure_omits_recording_not_500(client, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("R2 unreachable")

    monkeypatch.setattr(recordings, "_get_client", lambda: type("C", (), {"generate_presigned_url": boom})())
    use(monkeypatch, conn_for_list([call_row(recording_key="recordings/otro-nivel/abc.wav")]))
    r = client.get("/platform/calls", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["calls"][0]["recording_url"] is None


# ── call_log._detect_language ────────────────────────────────────────────────


def test_detect_language_none_for_empty_transcript():
    assert call_log._detect_language("") is None
    assert call_log._detect_language("   ") is None


def test_detect_language_spanish_accents():
    assert call_log._detect_language("¿Cuáles son sus horarios de atención?") == "es"


def test_detect_language_spanish_words_without_accents():
    assert call_log._detect_language("hola quiero saber si tienen cita disponible") == "es"


def test_detect_language_defaults_english():
    assert call_log._detect_language("Hi, I'd like to book an appointment for Thursday.") == "en"
