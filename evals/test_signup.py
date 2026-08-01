"""Self-serve signup + provisioning pipeline (Phase 4 ticket 4.1, stage 2).

No live Postgres in this environment, so the DB is a recording fake: it captures
every statement + bound params and returns canned rows. That verifies handler
control flow, transaction sequencing, parameter binding, auth, validation and
the response contract — but NOT Postgres grammar. The SQL itself still has to
be exercised against a real database once (see the stage-2 notes).

Run: PYTHONUTF8=1 pytest evals/test_signup.py -v
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.signup as signup
from platform_api import provisioning as prov
from platform_api.signup import (
    RESERVED_SLUGS,
    allocate_slug,
    seed_config,
    slugify,
)

SECRET = "test-platform-secret"
USER = "user_2abcXYZ"
HEADERS = {"X-Platform-Secret": SECRET, "X-Platform-User": USER}


# ── recording fake DB ─────────────────────────────────────────────────────────


class FakeResult:
    def __init__(self, rows):
        self._rows = rows or []

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        row = self._rows[0]
        return row[0] if isinstance(row, tuple) else row

    def one(self):
        """Mirrors SQLAlchemy: raises when there is no row, so a handler that
        assumes RETURNING produced something fails in tests the same way it
        would in production."""
        if not self._rows:
            raise AssertionError("one() on an empty result")
        return self._rows[0]

    def mappings(self):
        return self

    # mappings().first()/.all() reuse the methods above; rows are dicts there.


class FakeConn:
    """Matches canned results to statements by SQL substring, in order."""

    def __init__(self, rules=None):
        self.executed = []
        self.rules = list(rules or [])

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.executed.append((sql, params or {}))
        for i, (needle, rows) in enumerate(self.rules):
            if needle in sql:
                self.rules.pop(i)
                return FakeResult(rows)
        return FakeResult([])

    def sql_containing(self, needle):
        return [(s, p) for s, p in self.executed if needle in s]


class FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def _ctx(self):
        engine = self

        class _Ctx:
            def __enter__(self):
                return engine.conn

            def __exit__(self, *a):
                return False

        return _Ctx()

    def begin(self):
        return self._ctx()

    def connect(self):
        return self._ctx()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(signup.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_throttle():
    signup._signup_hits.clear()
    yield
    signup._signup_hits.clear()


def use_engine(monkeypatch, conn):
    monkeypatch.setattr(signup, "_engine_or_503", lambda: FakeEngine(conn))
    return conn


def valid_body(**over):
    body = {
        "company_name": "Bella Vista Barbers",
        "contact_email": "owner@bellavista.example",
        "contact_name": "Ana Ruiz",
        "contact_phone": "+1 416 555 0110",
        "business_tz": "America/Toronto",
        "requested_plan": "pro",
    }
    body.update(over)
    return body


# ── slug allocation ───────────────────────────────────────────────────────────


def test_slugify_shapes():
    assert slugify("Bella Vista Barbers") == "bella-vista-barbers"
    assert slugify("  Joe's  Pizza & Pasta!! ") == "joe-s-pizza-pasta"
    assert slugify("Café Münster") == "caf-m-nster"
    assert slugify("!!!") == ""
    assert len(slugify("x" * 300)) <= 48


def test_slugify_output_is_a_legal_tenant_id():
    from tenants import _TENANT_ID_RE

    for name in ("Bella Vista Barbers", "A", "3M Corp", "Ann & Sons -- Ltd."):
        s = slugify(name)
        assert _TENANT_ID_RE.fullmatch(s), f"{name!r} -> {s!r}"


def test_allocate_slug_free():
    assert allocate_slug(FakeConn(), "bella-vista-barbers") == "bella-vista-barbers"


def test_allocate_slug_suffixes_on_db_collision():
    class Taken(FakeConn):
        def execute(self, stmt, params=None):
            super().execute(stmt, params)
            taken = {"acme-dental", "acme-dental-2"}
            return FakeResult([(1,)] if params and params.get("tid") in taken else [])

    assert allocate_slug(Taken(), "acme-dental") == "acme-dental-3"


def test_reserved_slugs_are_never_allocated():
    for reserved in ("default", "admin", "orchelix-ai-consulting"):
        assert reserved in RESERVED_SLUGS
        assert allocate_slug(FakeConn(), reserved) != reserved


def test_existing_on_disk_tenant_cannot_be_claimed():
    """A self-serve signup must never be able to take a live tenant's slug,
    even though that tenant may have no row in the tenants table."""
    assert allocate_slug(FakeConn(), "otro-nivel") == "otro-nivel-2"
    assert allocate_slug(FakeConn(), "acme") == "acme-2"


def test_undrivable_name_is_a_400():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        allocate_slug(FakeConn(), "")
    assert e.value.status_code == 400


# ── seed config ───────────────────────────────────────────────────────────────


def test_seed_config_does_not_inherit_orchelix_pricing():
    """Regression guard for the leak this ticket found: an omitted/falsy
    pricing key made a brand-new tenant quote Orchelix's own SaaS packages."""
    from tenants import _config_from_file

    body = signup.SignupRequest(**valid_body())
    cfg = _config_from_file("bella-vista-barbers", seed_config(body, "bella-vista-barbers"))

    assert cfg.pricing == []
    blob = json.dumps(cfg.pricing) + cfg.pricing_note
    assert "Esmi" not in blob
    assert "8,500" not in blob and "8500" not in blob


def test_seed_config_carries_signup_fields():
    from tenants import _config_from_file

    body = signup.SignupRequest(**valid_body())
    cfg = _config_from_file("x", seed_config(body, "x"))

    assert cfg.company_name == "Bella Vista Barbers"
    assert cfg.business_tz == "America/Toronto"
    assert cfg.email_booking_to == "owner@bellavista.example"
    assert cfg.email_escalation_to == "owner@bellavista.example"
    assert cfg.business_hours == (9, 17)


def test_seed_config_keeps_orchelix_sender():
    """emails.from is deliberately absent — it's tied to SendGrid domain
    verification, the same field config.py refuses to let tenants edit."""
    assert "from" not in seed_config(signup.SignupRequest(**valid_body()), "x")["emails"]


def test_seed_pricing_note_has_a_spanish_variant():
    cfg = seed_config(signup.SignupRequest(**valid_body()), "x")
    assert cfg["pricing_note"] and cfg["pricing_note_es"]
    assert cfg["pricing_note"] != cfg["pricing_note_es"]


# ── auth ──────────────────────────────────────────────────────────────────────


def test_signup_requires_platform_secret(client):
    r = client.post("/platform/signup", json=valid_body(), headers={"X-Platform-User": USER})
    assert r.status_code == 401


def test_signup_requires_platform_user(client, monkeypatch):
    use_engine(monkeypatch, FakeConn())
    r = client.post("/platform/signup", json=valid_body(),
                    headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400
    assert "X-Platform-User" in r.json()["detail"]


def test_signup_503_without_db(client, monkeypatch):
    monkeypatch.setattr("platform_db.get_engine", lambda: None)
    r = client.post("/platform/signup", json=valid_body(), headers=HEADERS)
    assert r.status_code == 503


# ── validation ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "over,field",
    [
        ({"company_name": ""}, "company_name"),
        ({"company_name": "x" * 201}, "company_name"),
        ({"contact_email": "not-an-email"}, "contact_email"),
        ({"contact_email": ""}, "contact_email"),
        ({"business_tz": "Mars/Olympus"}, "business_tz"),
        ({"requested_plan": "platinum"}, "requested_plan"),
    ],
)
def test_signup_rejects_bad_input(client, monkeypatch, over, field):
    use_engine(monkeypatch, FakeConn())
    r = client.post("/platform/signup", json=valid_body(**over), headers=HEADERS)
    assert r.status_code == 400
    assert field in r.json()["detail"]


def test_signup_rejects_malformed_explicit_slug(client, monkeypatch):
    use_engine(monkeypatch, FakeConn())
    r = client.post("/platform/signup", json=valid_body(tenant_id="Not A Slug!"),
                    headers=HEADERS)
    assert r.status_code == 400
    assert "tenant_id" in r.json()["detail"]


def test_bad_input_is_rejected_before_any_write(client, monkeypatch):
    conn = use_engine(monkeypatch, FakeConn())
    client.post("/platform/signup", json=valid_body(business_tz="Mars/Olympus"),
                headers=HEADERS)
    assert conn.sql_containing("INSERT INTO tenants") == []


# ── the happy path ────────────────────────────────────────────────────────────


def _happy_conn():
    return FakeConn(rules=[("INSERT INTO provisioning_jobs", [("job-uuid-1",)])])


def test_signup_creates_tenant_job_and_config(client, monkeypatch):
    conn = use_engine(monkeypatch, _happy_conn())
    r = client.post("/platform/signup", json=valid_body(), headers=HEADERS)
    assert r.status_code == 201, r.text

    data = r.json()
    assert data["tenant_id"] == "bella-vista-barbers"
    assert data["onboarding_status"] == "submitted"
    assert data["next"]["slug"] == "bella-vista-barbers"

    tenant_ins = conn.sql_containing("INSERT INTO tenants")
    assert len(tenant_ins) == 1
    params = tenant_ins[0][1]
    assert params["onboarding_status"] == "submitted"
    assert params["contact_email"] == "owner@bellavista.example"

    cfg_ins = conn.sql_containing("INSERT INTO tenant_configs")
    assert len(cfg_ins) == 1
    sql, cfg_params = cfg_ins[0]
    assert "VALUES (:tid, 1," in sql, "seed must be published version 1"
    assert "published" in sql and "true" in sql
    assert json.loads(cfg_params["config"])["pricing"] == []
    assert cfg_params["by"] == f"signup:{USER}"


def test_signup_starts_on_managed_and_records_requested_plan(client, monkeypatch):
    """The approved decision: plan stays 'managed' (unlimited, no soft-limit
    emails) until an admin assigns the real one; the ask is stored separately."""
    conn = use_engine(monkeypatch, _happy_conn())
    r = client.post("/platform/signup", json=valid_body(requested_plan="pro"),
                    headers=HEADERS)
    assert r.status_code == 201

    params = conn.sql_containing("INSERT INTO tenants")[0][1]
    assert params["plan"] == "managed"
    assert params["requested_plan"] == "pro"
    assert params["status"] == "trial", "must not inherit the column default 'live'"
    assert r.json()["plan"] == "managed"
    assert r.json()["requested_plan"] == "pro"


def test_signup_creates_all_seven_steps_with_right_kinds(client, monkeypatch):
    conn = use_engine(monkeypatch, _happy_conn())
    client.post("/platform/signup", json=valid_body(), headers=HEADERS)

    inserted = {
        p["step"]: p["status"]
        for _, p in conn.sql_containing("INSERT INTO provisioning_steps")
    }
    assert set(inserted) == set(prov.STEPS)
    for step in prov.AUTOMATED_STEPS:
        assert inserted[step] == "pending", step
    for step in prov.MANUAL_STEPS:
        assert inserted[step] == "manual", step


def test_signup_marks_the_two_backend_steps_done(client, monkeypatch):
    conn = use_engine(monkeypatch, _happy_conn())
    client.post("/platform/signup", json=valid_body(), headers=HEADERS)

    updates = {
        p["step"]: p["status"]
        for _, p in conn.sql_containing("UPDATE provisioning_steps")
    }
    assert updates["tenant_row"] == "done"
    assert updates["config_seed"] == "done"
    assert "clerk_org" not in updates, "clerk_org is call 2's job, not call 1's"


def test_signup_clears_the_tenant_cache(client, monkeypatch):
    """A prior slug-check caches 'no such tenant' for 60s; without the clear
    the new tenant's own dashboard 400s until it expires."""
    cleared = []
    monkeypatch.setattr(signup, "clear_tenant_cache", lambda tid: cleared.append(tid))
    use_engine(monkeypatch, _happy_conn())
    client.post("/platform/signup", json=valid_body(), headers=HEADERS)
    assert cleared == ["bella-vista-barbers"]


# ── abuse controls ────────────────────────────────────────────────────────────


def test_throttle_blocks_the_fourth_attempt(client, monkeypatch):
    monkeypatch.setattr(signup, "_engine_or_503", lambda: FakeEngine(_happy_conn()))
    codes = [
        client.post("/platform/signup", json=valid_body(), headers=HEADERS).status_code
        for _ in range(4)
    ]
    assert codes[:3] == [201, 201, 201]
    assert codes[3] == 429


def test_throttle_is_per_user(client, monkeypatch):
    monkeypatch.setattr(signup, "_engine_or_503", lambda: FakeEngine(_happy_conn()))
    for _ in range(3):
        client.post("/platform/signup", json=valid_body(), headers=HEADERS)
    other = {"X-Platform-Secret": SECRET, "X-Platform-User": "user_someone_else"}
    assert client.post("/platform/signup", json=valid_body(), headers=other).status_code == 201


def test_one_pending_signup_per_user(client, monkeypatch):
    conn = FakeConn(rules=[("FROM tenants t JOIN provisioning_jobs", [("earlier-co",)])])
    use_engine(monkeypatch, conn)
    r = client.post("/platform/signup", json=valid_body(), headers=HEADERS)
    assert r.status_code == 409
    assert "earlier-co" in r.json()["detail"]
    assert conn.sql_containing("INSERT INTO tenants") == []


def test_explicit_slug_collision_is_409_with_a_suggestion(client, monkeypatch):
    use_engine(monkeypatch, FakeConn())
    r = client.post("/platform/signup", json=valid_body(tenant_id="otro-nivel"),
                    headers=HEADERS)
    assert r.status_code == 409
    assert "otro-nivel-2" in r.json()["detail"]


# ── clerk-org (call 2) ────────────────────────────────────────────────────────


def _clerk_conn(existing_org=None, onboarding="submitted", steps=None):
    steps = steps or [
        {"step": s, "status": "done" if s in ("tenant_row", "config_seed", "clerk_org")
         else "manual", "detail": None, "error": None, "started_at": None,
         "finished_at": None, "updated_by": None}
        for s in prov.STEPS
    ]
    return FakeConn(rules=[
        ("SELECT clerk_org_id, onboarding_status", [(existing_org, onboarding)]),
        ("FROM provisioning_jobs WHERE tenant_id", [{
            "id": "job-uuid-1", "status": "running", "created_by": USER, "error": None,
            "created_at": None, "updated_at": None, "completed_at": None,
        }]),
        ("SELECT step, status, detail, error", steps),
        ("SELECT step, status FROM provisioning_steps",
         [(s["step"], s["status"]) for s in steps]),
        ("SELECT step, status, detail, error", steps),
    ])


def test_clerk_org_links_and_advances_to_review(client, monkeypatch):
    conn = use_engine(monkeypatch, _clerk_conn())
    r = client.post("/platform/signup/bella-vista-barbers/clerk-org",
                    json={"clerk_org_id": "org_abc123"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["clerk_org_id"] == "org_abc123"

    linked = conn.sql_containing("UPDATE tenants SET clerk_org_id")
    assert len(linked) == 1 and linked[0][1]["org"] == "org_abc123"
    assert conn.sql_containing("onboarding_status = 'review'")


def test_clerk_org_rejects_bad_id_format(client, monkeypatch):
    use_engine(monkeypatch, _clerk_conn())
    r = client.post("/platform/signup/x/clerk-org",
                    json={"clerk_org_id": "abc123"}, headers=HEADERS)
    assert r.status_code == 400
    assert "org_" in r.json()["detail"]


def test_clerk_org_requires_id_or_error(client, monkeypatch):
    use_engine(monkeypatch, _clerk_conn())
    r = client.post("/platform/signup/x/clerk-org", json={}, headers=HEADERS)
    assert r.status_code == 400


def test_clerk_org_is_idempotent_for_the_same_org(client, monkeypatch):
    """A retried Next.js request must not fail the flow."""
    conn = use_engine(monkeypatch, _clerk_conn(existing_org="org_abc123"))
    r = client.post("/platform/signup/x/clerk-org",
                    json={"clerk_org_id": "org_abc123"}, headers=HEADERS)
    assert r.status_code == 200
    assert conn.sql_containing("UPDATE tenants SET clerk_org_id") == []


def test_clerk_org_conflict_on_a_different_org(client, monkeypatch):
    use_engine(monkeypatch, _clerk_conn(existing_org="org_original"))
    r = client.post("/platform/signup/x/clerk-org",
                    json={"clerk_org_id": "org_different"}, headers=HEADERS)
    assert r.status_code == 409


def test_clerk_org_404_for_unknown_tenant(client, monkeypatch):
    use_engine(monkeypatch, FakeConn(rules=[("SELECT clerk_org_id, onboarding_status", [])]))
    r = client.post("/platform/signup/ghost/clerk-org",
                    json={"clerk_org_id": "org_abc"}, headers=HEADERS)
    assert r.status_code == 404


def test_clerk_org_failure_report_marks_the_step_failed(client, monkeypatch):
    conn = use_engine(monkeypatch, _clerk_conn())
    r = client.post("/platform/signup/x/clerk-org",
                    json={"error": "Clerk API 422: slug taken"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["clerk_org_id"] is None
    failed = [p for _, p in conn.sql_containing("UPDATE provisioning_steps")
              if p["step"] == "clerk_org"]
    assert failed and failed[0]["status"] == "failed"


def test_clerk_org_does_not_drag_back_an_active_tenant(client, monkeypatch):
    conn = use_engine(monkeypatch, _clerk_conn(onboarding="active"))
    client.post("/platform/signup/x/clerk-org",
                json={"clerk_org_id": "org_abc123"}, headers=HEADERS)
    assert conn.sql_containing("onboarding_status = 'review'") == []


# ── provisioning job-status rules ─────────────────────────────────────────────


def _status_conn(step_statuses):
    rows = [(s, step_statuses.get(s, "pending")) for s in prov.STEPS]
    return FakeConn(rules=[("SELECT step, status FROM provisioning_steps", rows)])


def test_job_running_while_automated_work_outstanding():
    conn = _status_conn({"tenant_row": "done", "config_seed": "done"})
    assert prov.recompute_job_status(conn, "j") == prov.JOB_RUNNING


def test_job_needs_review_when_only_manual_steps_remain():
    conn = _status_conn(
        {"tenant_row": "done", "config_seed": "done", "clerk_org": "done",
         "vapi_assistant": "manual", "phone_number": "manual",
         "calendar": "manual", "kb_seed": "manual"}
    )
    assert prov.recompute_job_status(conn, "j") == prov.JOB_NEEDS_REVIEW


def test_job_complete_when_everything_resolved():
    conn = _status_conn({s: "done" for s in prov.STEPS})
    assert prov.recompute_job_status(conn, "j") == prov.JOB_COMPLETE


def test_skipped_counts_as_resolved():
    statuses = {s: "done" for s in prov.STEPS}
    statuses["phone_number"] = "skipped"
    assert prov.recompute_job_status(_status_conn(statuses), "j") == prov.JOB_COMPLETE


def test_any_failure_wins():
    statuses = {s: "done" for s in prov.STEPS}
    statuses["clerk_org"] = "failed"
    assert prov.recompute_job_status(_status_conn(statuses), "j") == prov.JOB_FAILED


def test_unresolved_steps_blocks_on_manual_work():
    steps = [{"step": s, "status": "manual"} for s in prov.MANUAL_STEPS]
    steps += [{"step": s, "status": "done"} for s in prov.AUTOMATED_STEPS]
    assert set(prov.unresolved_steps(steps)) == prov.MANUAL_STEPS


def test_set_step_rejects_unknown_names():
    with pytest.raises(ValueError):
        prov.set_step(FakeConn(), "j", "not_a_step", "done")
    with pytest.raises(ValueError):
        prov.set_step(FakeConn(), "j", prov.STEP_CALENDAR, "not_a_status")


def test_manual_steps_ship_with_an_explanatory_note():
    conn = FakeConn(rules=[("INSERT INTO provisioning_jobs", [("job-1",)])])
    prov.create_job(conn, "some-tenant", created_by=USER)
    for _, p in conn.sql_containing("INSERT INTO provisioning_steps"):
        if p["step"] in prov.MANUAL_STEPS:
            assert p["detail"] and "note" in json.loads(p["detail"]), p["step"]
