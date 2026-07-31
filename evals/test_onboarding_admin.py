"""Admin onboarding queue + approve/reject (Phase 4 ticket 4.1, stage 3).

Same recording-fake DB approach as evals/test_signup.py — verifies auth,
guards, write sequencing and the response contract, NOT Postgres grammar.

The rule these exist to protect: approval is the ONLY path that sets
onboarding_status='active', and it must be impossible to reach it with an
unresolved provisioning step.

Run: PYTHONUTF8=1 pytest evals/test_onboarding_admin.py -v
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.onboarding as onb
from evals.test_signup import FakeConn, FakeEngine
from platform_api import provisioning as prov

ADMIN_SECRET = "test-admin-secret"
CLIENT_SECRET = "test-platform-secret"
STAFF = "user_staff_1"
HEADERS = {"X-Platform-Admin-Secret": ADMIN_SECRET, "X-Platform-User": STAFF}
TID = "bella-vista-barbers"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("PLATFORM_API_SECRET", CLIENT_SECRET)
    app = FastAPI()
    app.include_router(onb.router)
    return TestClient(app)


def tenant_row(**over):
    row = {
        "id": TID,
        "company_name": "Bella Vista Barbers",
        "business_tz": "America/Toronto",
        "status": "trial",
        "plan": "managed",
        "onboarding_status": "review",
        "clerk_org_id": "org_abc123",
        "contact_name": "Ana Ruiz",
        "contact_email": "owner@bellavista.example",
        "contact_phone": "+14165550110",
        "requested_plan": "pro",
        "submitted_at": None,
        "approved_at": None,
        "approved_by": None,
        "rejected_reason": None,
        "activated_at": None,
        "created_at": None,
    }
    row.update(over)
    return row


def steps(all_resolved=True, **over):
    out = []
    for s in prov.STEPS:
        default = "done" if (all_resolved or s in prov.AUTOMATED_STEPS) else "manual"
        out.append(
            {
                "step": s, "status": over.get(s, default), "detail": None, "error": None,
                "started_at": None, "finished_at": None, "updated_by": None,
            }
        )
    return out


def make_conn(row=None, step_rows=None):
    """Every query the handlers issue, in matching order."""
    row = row if row is not None else tenant_row()
    step_rows = step_rows if step_rows is not None else steps()
    job = {
        "id": "job-uuid-1", "status": "needs_review", "created_by": "user_1",
        "error": None, "created_at": None, "updated_at": None, "completed_at": None,
    }
    rules = []
    for _ in range(6):  # handlers re-read to build the response
        rules += [
            ("FROM tenants WHERE id", [row]),
            ("FROM provisioning_jobs WHERE tenant_id", [job]),
            ("SELECT step, status, detail, error", step_rows),
            ("SELECT step, status FROM provisioning_steps",
             [(s["step"], s["status"]) for s in step_rows]),
        ]
    return FakeConn(rules=rules)


def use(monkeypatch, conn):
    monkeypatch.setattr(onb, "_engine_or_503", lambda: FakeEngine(conn))
    return conn


# ── auth ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/platform/admin/onboarding", None),
        ("get", f"/platform/admin/onboarding/{TID}", None),
        ("patch", f"/platform/admin/onboarding/{TID}/steps/calendar", {"status": "done"}),
        ("post", f"/platform/admin/onboarding/{TID}/approve", {"plan": "pro"}),
        ("post", f"/platform/admin/onboarding/{TID}/reject", {"reason": "spam"}),
    ],
)
def test_every_route_requires_the_admin_secret(client, monkeypatch, method, path, body):
    use(monkeypatch, make_conn())
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 401


def test_client_secret_cannot_reach_admin_routes(client, monkeypatch):
    """The whole point of a separate admin secret: a leaked client-dashboard
    secret must not be able to approve a tenant."""
    use(monkeypatch, make_conn())
    r = client.post(
        f"/platform/admin/onboarding/{TID}/approve",
        json={"plan": "pro"},
        headers={"X-Platform-Admin-Secret": CLIENT_SECRET},
    )
    assert r.status_code == 401


def test_503_without_db(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    assert client.get("/platform/admin/onboarding", headers=HEADERS).status_code == 503


# ── queue ─────────────────────────────────────────────────────────────────────


def test_list_filters_to_pending_by_default(client, monkeypatch):
    conn = use(monkeypatch, make_conn())
    r = client.get("/platform/admin/onboarding", headers=HEADERS)
    assert r.status_code == 200
    sql, params = conn.executed[0]
    assert "onboarding_status = ANY(:statuses)" in sql
    assert set(params["statuses"]) == set(onb.PENDING_ONBOARDING_STATUSES)
    assert "active" not in params["statuses"]


def test_list_include_all_drops_the_filter(client, monkeypatch):
    conn = use(monkeypatch, make_conn())
    client.get("/platform/admin/onboarding?include=all", headers=HEADERS)
    assert "onboarding_status = ANY" not in conn.executed[0][0]


def test_detail_reports_progress_and_can_approve(client, monkeypatch):
    use(monkeypatch, make_conn())
    body = client.get(f"/platform/admin/onboarding/{TID}", headers=HEADERS).json()
    assert body["tenant_id"] == TID
    assert body["steps_total"] == len(prov.STEPS)
    assert body["steps_resolved"] == len(prov.STEPS)
    assert body["unresolved_steps"] == []
    assert body["can_approve"] is True
    assert body["requested_plan"] == "pro"
    assert body["plan"] == "managed", "still managed until approval"


def test_can_approve_false_while_manual_work_outstanding(client, monkeypatch):
    use(monkeypatch, make_conn(step_rows=steps(all_resolved=False)))
    body = client.get(f"/platform/admin/onboarding/{TID}", headers=HEADERS).json()
    assert body["can_approve"] is False
    assert set(body["unresolved_steps"]) == prov.MANUAL_STEPS


def test_unknown_tenant_is_404(client, monkeypatch):
    use(monkeypatch, FakeConn(rules=[("FROM tenants WHERE id", [])]))
    r = client.get("/platform/admin/onboarding/ghost-co", headers=HEADERS)
    assert r.status_code == 404


def test_invalid_tenant_id_404s_instead_of_becoming_default(client, monkeypatch):
    """normalize_tenant_id() silently maps junk to 'default'; for an admin
    route that would show Orchelix's own record under someone else's name."""
    use(monkeypatch, make_conn())
    r = client.get("/platform/admin/onboarding/Not%20A%20Slug", headers=HEADERS)
    assert r.status_code == 404


# ── manual step completion ────────────────────────────────────────────────────


def test_marking_a_manual_step_done_records_detail(client, monkeypatch):
    conn = use(monkeypatch, make_conn(step_rows=steps(all_resolved=False)))
    r = client.patch(
        f"/platform/admin/onboarding/{TID}/steps/vapi_assistant",
        json={"status": "done", "detail": {"assistant_id": "asst_9f2c"}},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    upd = [p for _, p in conn.sql_containing("UPDATE provisioning_steps")]
    assert upd and upd[0]["step"] == "vapi_assistant"
    assert upd[0]["status"] == "done"
    assert "asst_9f2c" in upd[0]["detail"]
    assert upd[0]["by"] == STAFF


def test_step_detail_is_merged_not_replaced(client, monkeypatch):
    """The manual note must survive an admin recording an id over it."""
    use(monkeypatch, make_conn())
    conn = use(monkeypatch, make_conn())
    client.patch(
        f"/platform/admin/onboarding/{TID}/steps/calendar",
        json={"status": "done", "detail": {"calendar_id": "abc@group"}},
        headers=HEADERS,
    )
    sql = conn.sql_containing("UPDATE provisioning_steps")[0][0]
    assert "||" in sql, "detail must be a jsonb merge"


def test_unknown_step_is_404(client, monkeypatch):
    use(monkeypatch, make_conn())
    r = client.patch(
        f"/platform/admin/onboarding/{TID}/steps/not_a_step",
        json={"status": "done"}, headers=HEADERS,
    )
    assert r.status_code == 404


def test_bad_step_status_is_400(client, monkeypatch):
    use(monkeypatch, make_conn())
    r = client.patch(
        f"/platform/admin/onboarding/{TID}/steps/calendar",
        json={"status": "finished"}, headers=HEADERS,
    )
    assert r.status_code == 400


def test_oversized_step_detail_is_rejected(client, monkeypatch):
    use(monkeypatch, make_conn())
    r = client.patch(
        f"/platform/admin/onboarding/{TID}/steps/calendar",
        json={"status": "done", "detail": {"calendar_id": "x" * 600}},
        headers=HEADERS,
    )
    assert r.status_code == 400


# ── approve ───────────────────────────────────────────────────────────────────


def test_approve_activates_and_assigns_the_plan(client, monkeypatch):
    conn = use(monkeypatch, make_conn())
    r = client.post(
        f"/platform/admin/onboarding/{TID}/approve",
        json={"plan": "pro", "status": "live"}, headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    upd = conn.sql_containing("UPDATE tenants SET")
    assert len(upd) == 1
    sql, params = upd[0]
    assert "onboarding_status = 'active'" in sql
    assert "approved_at = now()" in sql
    assert "approved_by = :by" in sql
    # COALESCE, so re-activating a previously-live tenant keeps the original
    # go-live timestamp instead of silently rewriting history.
    assert "activated_at = COALESCE(activated_at, now())" in sql
    assert "rejected_reason = NULL" in sql
    assert params["plan"] == "pro"
    assert params["status"] == "live"
    assert params["by"] == STAFF


def test_approve_writes_the_shared_audit_row(client, monkeypatch):
    conn = use(monkeypatch, make_conn())
    client.post(f"/platform/admin/onboarding/{TID}/approve",
                json={"plan": "pro"}, headers=HEADERS)
    audit = conn.sql_containing("INSERT INTO tenant_plan_changes")
    assert len(audit) == 1
    p = audit[0][1]
    assert p["old_plan"] == "managed" and p["new_plan"] == "pro"
    assert p["old_status"] == "trial" and p["new_status"] == "live"
    assert p["by"] == f"approve:{STAFF}"


def test_approve_defaults_status_to_live(client, monkeypatch):
    conn = use(monkeypatch, make_conn())
    client.post(f"/platform/admin/onboarding/{TID}/approve",
                json={"plan": "local"}, headers=HEADERS)
    assert conn.sql_containing("UPDATE tenants SET")[0][1]["status"] == "live"


def test_approve_is_blocked_by_unresolved_steps(client, monkeypatch):
    """The gate. Manual VAPI/number/calendar/KB work cannot be skipped."""
    conn = use(monkeypatch, make_conn(step_rows=steps(all_resolved=False)))
    r = client.post(f"/platform/admin/onboarding/{TID}/approve",
                    json={"plan": "pro"}, headers=HEADERS)
    assert r.status_code == 409
    assert "unresolved" in r.json()["detail"].lower()
    assert conn.sql_containing("UPDATE tenants SET") == []
    assert conn.sql_containing("INSERT INTO tenant_plan_changes") == []


def test_approve_rejects_an_unknown_plan(client, monkeypatch):
    conn = use(monkeypatch, make_conn())
    r = client.post(f"/platform/admin/onboarding/{TID}/approve",
                    json={"plan": "platinum"}, headers=HEADERS)
    assert r.status_code == 400
    assert conn.sql_containing("UPDATE tenants SET") == []


def test_approve_rejects_an_unknown_status(client, monkeypatch):
    use(monkeypatch, make_conn())
    r = client.post(f"/platform/admin/onboarding/{TID}/approve",
                    json={"plan": "pro", "status": "vip"}, headers=HEADERS)
    assert r.status_code == 400


def test_approving_twice_is_a_409(client, monkeypatch):
    conn = use(monkeypatch, make_conn(row=tenant_row(onboarding_status="active")))
    r = client.post(f"/platform/admin/onboarding/{TID}/approve",
                    json={"plan": "pro"}, headers=HEADERS)
    assert r.status_code == 409
    assert conn.sql_containing("UPDATE tenants SET") == []


def test_approve_clears_the_tenant_cache_immediately(client, monkeypatch):
    """tenant_is_active() caches for 60s — an admin who approves then places a
    test call must reach the tenant, not fall back to 'default'."""
    cleared = []
    monkeypatch.setattr(onb, "clear_tenant_cache", lambda tid: cleared.append(tid))
    use(monkeypatch, make_conn())
    client.post(f"/platform/admin/onboarding/{TID}/approve",
                json={"plan": "pro"}, headers=HEADERS)
    assert cleared == [TID]


def test_approve_after_rejection_is_allowed(client, monkeypatch):
    """A mis-click on Reject has to be recoverable."""
    conn = use(monkeypatch, make_conn(row=tenant_row(onboarding_status="rejected")))
    r = client.post(f"/platform/admin/onboarding/{TID}/approve",
                    json={"plan": "pro"}, headers=HEADERS)
    assert r.status_code == 200
    assert "rejected_reason = NULL" in conn.sql_containing("UPDATE tenants SET")[0][0]


# ── reject ────────────────────────────────────────────────────────────────────


def test_reject_records_the_reason_and_keeps_the_row(client, monkeypatch):
    conn = use(monkeypatch, make_conn())
    r = client.post(f"/platform/admin/onboarding/{TID}/reject",
                    json={"reason": "Duplicate of an existing client"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    sql, params = conn.sql_containing("UPDATE tenants SET")[0]
    assert "onboarding_status = 'rejected'" in sql
    assert params["reason"] == "Duplicate of an existing client"
    assert conn.sql_containing("DELETE") == [], "the row and slug must be kept"


def test_reject_leaves_billing_status_alone(client, monkeypatch):
    """`status` is the billing lifecycle — a declined signup never billed, so
    overloading 'archived' here would make the billing timeline lie."""
    conn = use(monkeypatch, make_conn())
    client.post(f"/platform/admin/onboarding/{TID}/reject",
                json={"reason": "no"}, headers=HEADERS)
    assert "status =" not in conn.sql_containing("UPDATE tenants SET")[0][0].replace(
        "onboarding_status =", ""
    )


def test_reject_requires_a_reason(client, monkeypatch):
    conn = use(monkeypatch, make_conn())
    for body in ({"reason": ""}, {"reason": "   "}, {"reason": "x" * 1001}):
        r = client.post(f"/platform/admin/onboarding/{TID}/reject",
                        json=body, headers=HEADERS)
        assert r.status_code == 400
    assert conn.sql_containing("UPDATE tenants SET") == []


def test_cannot_reject_an_active_tenant(client, monkeypatch):
    conn = use(monkeypatch, make_conn(row=tenant_row(onboarding_status="active")))
    r = client.post(f"/platform/admin/onboarding/{TID}/reject",
                    json={"reason": "changed my mind"}, headers=HEADERS)
    assert r.status_code == 409
    assert conn.sql_containing("UPDATE tenants SET") == []
