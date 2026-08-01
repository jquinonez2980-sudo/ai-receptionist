"""GET /platform/signup/mine + GET /platform/tenant-status (Phase 4, stage A).

Recording-fake DB, same approach as evals/test_signup.py — verifies auth,
shape, and the two invariants that matter:

  1. /signup/mine and the signup write guard must agree on "already applied".
     If they drift, the wizard offers a fresh start that then 409s on submit.
  2. /tenant-status's can_serve_traffic must come from tenants.tenant_is_active()
     — the same function that gates real voice/chat/booking traffic — not from
     a second reading of onboarding_status.

Run: PYTHONUTF8=1 pytest evals/test_signup_status.py -v
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.signup as signup
import platform_api.tenant_status as ts
from evals.test_signup import FakeConn, FakeEngine
from platform_api import provisioning as prov

SECRET = "test-platform-secret"
USER = "user_2abcXYZ"
HEADERS = {"X-Platform-Secret": SECRET, "X-Platform-User": USER}
TID = "bella-vista-barbers"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(signup.router)
    app.include_router(ts.router)
    return TestClient(app)


def tenant_row(**over):
    row = {
        "id": TID,
        "company_name": "Bella Vista Barbers",
        "business_tz": "America/Toronto",
        "onboarding_status": "review",
        "requested_plan": "pro",
        "clerk_org_id": "org_abc123",
        "contact_name": "Ana Ruiz",
        "contact_email": "owner@bellavista.example",
        "contact_phone": "+14165550110",
        "submitted_at": None,
        "rejected_reason": None,
    }
    row.update(over)
    return row


def steps(clerk_status="done"):
    return [
        {
            "step": s,
            "status": clerk_status if s == prov.STEP_CLERK_ORG
            else ("done" if s in prov.AUTOMATED_STEPS else "manual"),
            "detail": {"note": "internal operator note — must not leak"},
            "error": "raw upstream error — must not leak",
            "started_at": None, "finished_at": None, "updated_by": "staff",
        }
        for s in prov.STEPS
    ]


def mine_conn(pending=TID, row=None, step_rows=None):
    job = {
        "id": "job-uuid-1", "status": "needs_review", "created_by": USER,
        "error": None, "created_at": None, "updated_at": None, "completed_at": None,
    }
    rules = [("JOIN provisioning_jobs j", [(pending,)] if pending else [])]
    if pending:
        rules += [
            ("FROM tenants WHERE id", [row if row is not None else tenant_row()]),
            ("FROM provisioning_jobs WHERE tenant_id", [job]),
            ("SELECT step, status, detail, error",
             step_rows if step_rows is not None else steps()),
        ]
    return FakeConn(rules=rules)


def use_signup(monkeypatch, conn):
    monkeypatch.setattr(signup, "_engine_or_503", lambda: FakeEngine(conn))
    return conn


# ── /platform/signup/mine ─────────────────────────────────────────────────────


def test_mine_requires_platform_secret(client, monkeypatch):
    use_signup(monkeypatch, mine_conn())
    r = client.get("/platform/signup/mine", headers={"X-Platform-User": USER})
    assert r.status_code == 401


def test_mine_requires_platform_user(client, monkeypatch):
    use_signup(monkeypatch, mine_conn())
    r = client.get("/platform/signup/mine", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


def test_mine_503_without_db(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    assert client.get("/platform/signup/mine", headers=HEADERS).status_code == 503


def test_mine_empty_for_a_new_user(client, monkeypatch):
    use_signup(monkeypatch, mine_conn(pending=None))
    body = client.get("/platform/signup/mine", headers=HEADERS).json()
    assert body == {"tenant": None, "can_start_new": True, "needs_clerk_org": False}


def test_mine_returns_the_pending_application(client, monkeypatch):
    use_signup(monkeypatch, mine_conn())
    body = client.get("/platform/signup/mine", headers=HEADERS).json()
    assert body["tenant"]["tenant_id"] == TID
    assert body["tenant"]["onboarding_status"] == "review"
    assert body["tenant"]["requested_plan"] == "pro"
    assert body["can_start_new"] is False
    assert body["steps_total"] == len(prov.STEPS)


def test_mine_scopes_the_lookup_to_the_calling_user(client, monkeypatch):
    conn = use_signup(monkeypatch, mine_conn())
    client.get("/platform/signup/mine", headers=HEADERS)
    sql, params = conn.executed[0]
    assert "j.created_by = :uid" in sql
    assert params["uid"] == USER


def test_mine_never_leaks_internal_step_notes_or_errors(client, monkeypatch):
    """The admin shape carries operator notes and raw upstream errors. This is
    a customer-facing route — neither may appear anywhere in the payload."""
    use_signup(monkeypatch, mine_conn())
    r = client.get("/platform/signup/mine", headers=HEADERS)
    raw, body = r.text, r.json()

    assert "must not leak" not in raw
    assert "internal operator note" not in raw
    assert "raw upstream error" not in raw
    # No per-step structures at all — only the coarse counts.
    assert "steps" not in body and "job" not in body
    assert set(body) == {
        "tenant", "job_status", "steps_total", "steps_resolved",
        "can_start_new", "needs_clerk_org",
    }


def test_mine_reports_progress_without_the_step_list(client, monkeypatch):
    use_signup(monkeypatch, mine_conn(step_rows=steps(clerk_status="manual")))
    body = client.get("/platform/signup/mine", headers=HEADERS).json()
    assert body["steps_total"] == len(prov.STEPS)
    assert body["steps_resolved"] == len(prov.AUTOMATED_STEPS) - 1
    assert "steps" not in body


# ── the resume signal ─────────────────────────────────────────────────────────


def test_needs_clerk_org_false_when_the_org_exists(client, monkeypatch):
    use_signup(monkeypatch, mine_conn())
    assert client.get("/platform/signup/mine", headers=HEADERS).json()[
        "needs_clerk_org"
    ] is False


def test_needs_clerk_org_true_after_a_failed_org_creation(client, monkeypatch):
    """The exact recovery case: signup succeeded, Clerk org creation didn't."""
    use_signup(
        monkeypatch,
        mine_conn(row=tenant_row(clerk_org_id=None), step_rows=steps(clerk_status="failed")),
    )
    body = client.get("/platform/signup/mine", headers=HEADERS).json()
    assert body["needs_clerk_org"] is True
    assert body["can_start_new"] is False, "must not offer a fresh start — it would 409"


def test_needs_clerk_org_true_when_step_never_ran(client, monkeypatch):
    use_signup(
        monkeypatch,
        mine_conn(row=tenant_row(clerk_org_id=None), step_rows=steps(clerk_status="pending")),
    )
    assert client.get("/platform/signup/mine", headers=HEADERS).json()[
        "needs_clerk_org"
    ] is True


def test_mine_and_the_write_guard_use_one_helper(monkeypatch):
    """Invariant 1, asserted structurally: both paths call
    pending_signup_tenant_id, so 'already applied' cannot mean two things."""
    calls = []
    monkeypatch.setattr(
        signup, "pending_signup_tenant_id",
        lambda conn, uid: (calls.append(uid), TID)[1],
    )
    with pytest.raises(Exception) as e:
        signup._assert_no_pending_signup(FakeConn(), USER)
    assert "409" in str(e.value) or "already have a business" in str(e.value)
    assert calls == [USER]


def test_pending_statuses_exclude_terminal_states():
    assert "active" not in signup.PENDING_SIGNUP_STATUSES
    assert "rejected" not in signup.PENDING_SIGNUP_STATUSES


# ── /platform/tenant-status ───────────────────────────────────────────────────


def status_headers(tid=TID):
    return {"X-Platform-Secret": SECRET, "X-Tenant-Id": tid}


def test_tenant_status_requires_secret(client):
    r = client.get("/platform/tenant-status", headers={"X-Tenant-Id": "acme"})
    assert r.status_code == 401


def test_tenant_status_requires_tenant_header(client):
    r = client.get("/platform/tenant-status", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


def test_tenant_status_live_tenant(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    body = client.get("/platform/tenant-status", headers=status_headers("acme")).json()
    assert body["tenant_id"] == "acme"
    assert body["onboarding_status"] == "active"
    assert body["can_serve_traffic"] is True


def test_tenant_status_pending_tenant_cannot_serve(client, monkeypatch):
    monkeypatch.setattr(ts, "tenant_onboarding_status", lambda tid: "review")
    monkeypatch.setattr(ts, "tenant_is_active", lambda tid: False)
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    monkeypatch.setattr("tenants.tenant_exists", lambda tid: True)
    body = client.get("/platform/tenant-status", headers=status_headers("acme")).json()
    assert body["onboarding_status"] == "review"
    assert body["can_serve_traffic"] is False


def test_can_serve_traffic_comes_from_the_runtime_gate(client, monkeypatch):
    """Invariant 2. If someone re-derives this from onboarding_status instead
    of calling tenant_is_active(), the banner starts lying about the phone."""
    called = []
    monkeypatch.setattr(
        ts, "tenant_is_active", lambda tid: (called.append(tid), False)[1]
    )
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    client.get("/platform/tenant-status", headers=status_headers("acme"))
    assert called == ["acme"]


def test_tenant_status_survives_a_plan_lookup_failure(client, monkeypatch):
    """The banner only needs the cached values; a DB blip must not 500 the
    dashboard shell."""
    class Boom:
        def connect(self):
            raise RuntimeError("connection reset")

    monkeypatch.setattr("platform_db.get_engine", lambda: Boom())
    r = client.get("/platform/tenant-status", headers=status_headers("acme"))
    assert r.status_code == 200
    body = r.json()
    assert body["can_serve_traffic"] is True
    assert body["account_status"] is None and body["plan"] is None


def test_tenant_status_unknown_tenant_is_400(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.get("/platform/tenant-status", headers=status_headers("ghost-co"))
    assert r.status_code == 400
