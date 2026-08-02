"""Self-serve onboarding, real Railway DB, full path: signup -> clerk-org ->
admin resolves the 4 manual steps -> approve -> tenant_is_active() true.

Every other onboarding test (test_signup.py, test_onboarding_admin.py,
test_onboarding_gate.py, test_signup_status.py) uses a recording fake DB —
they verify auth, guards, and write sequencing, but never actually exercised
Postgres. This is that one real run: self-cleaning, skipped unless
DATABASE_URL is set.

The backend never calls Clerk's API itself (that's the Next.js route's job —
see app/api/platform/signup/clerk-org-create/route.ts); POST .../clerk-org
just records whatever id string is reported. That means this test can drive
the ENTIRE backend path with a fabricated-but-well-formed org id, with no
Clerk credentials needed.

Run: railway run pytest evals/test_onboarding_e2e.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "sk-test-unit")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — no live Postgres to test against",
)


@pytest.fixture
def client():
    import platform_api.onboarding as onboarding
    import platform_api.signup as signup

    app = FastAPI()
    app.include_router(signup.router)
    app.include_router(onboarding.router)
    return TestClient(app)


def test_signup_through_approve_activates_the_tenant(client):
    from sqlalchemy import text

    from platform_db import get_engine
    from platform_api import provisioning as prov
    from tenants import clear_tenant_cache, tenant_is_active

    engine = get_engine()
    assert engine is not None, "DATABASE_URL set but get_engine() returned None"

    platform_secret = os.environ["PLATFORM_API_SECRET"]
    admin_secret = os.environ["PLATFORM_ADMIN_SECRET"]

    run_id = uuid.uuid4().hex[:10]
    tenant_id = f"e2e-onboarding-{run_id}"
    clerk_user = f"user_e2e_test_{run_id}"
    company_name = f"E2E Test Business {run_id} (safe to delete)"

    signup_headers = {"X-Platform-Secret": platform_secret, "X-Platform-User": clerk_user}
    admin_headers = {"X-Platform-Admin-Secret": admin_secret, "X-Platform-User": "e2e-test-admin"}

    try:
        # ── 1. POST /platform/signup — tenant_row + config_seed, automated ──
        r = client.post(
            "/platform/signup",
            headers=signup_headers,
            json={
                "company_name": company_name,
                "contact_email": "onboarding-e2e-test@example.com",
                "contact_name": "E2E Test Contact",
                "contact_phone": "+15555550100",
                "business_tz": "America/Toronto",
                "requested_plan": "local",
                "tenant_id": tenant_id,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["tenant_id"] == tenant_id
        assert body["onboarding_status"] == "submitted"
        assert body["plan"] == "managed"  # never the requested plan, until approval
        step_by_name = {s["step"]: s["status"] for s in body["steps"]}
        assert step_by_name[prov.STEP_TENANT_ROW] == prov.STEP_DONE
        assert step_by_name[prov.STEP_CONFIG_SEED] == prov.STEP_DONE
        assert step_by_name[prov.STEP_CLERK_ORG] == prov.STEP_PENDING

        # A second signup by the same user must 409 (one in-flight signup rule).
        r_dup = client.post(
            "/platform/signup",
            headers=signup_headers,
            json={
                "company_name": "Should be rejected",
                "contact_email": "onboarding-e2e-test@example.com",
                "business_tz": "America/Toronto",
            },
        )
        assert r_dup.status_code == 409

        # GET /platform/signup/mine resumes to this tenant, needing the org.
        mine = client.get("/platform/signup/mine", headers=signup_headers).json()
        assert mine["tenant"]["tenant_id"] == tenant_id
        assert mine["needs_clerk_org"] is True

        # ── 2. POST .../clerk-org — records the org, flips review ───────────
        fake_org_id = f"org_e2etest{run_id}"
        r = client.post(
            f"/platform/signup/{tenant_id}/clerk-org",
            headers=signup_headers,
            json={"clerk_org_id": fake_org_id},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["clerk_org_id"] == fake_org_id
        assert body["onboarding_status"] == "review"
        assert body["job_status"] == prov.JOB_NEEDS_REVIEW
        assert set(body["unresolved_steps"]) == set(prov.MANUAL_STEPS)

        # Re-posting the SAME org id is idempotent, not a 409.
        r_again = client.post(
            f"/platform/signup/{tenant_id}/clerk-org",
            headers=signup_headers,
            json={"clerk_org_id": fake_org_id},
        )
        assert r_again.status_code == 200

        # ── 3. Admin queue sees it, approve is blocked (manual steps open) ──
        detail = client.get(
            f"/platform/admin/onboarding/{tenant_id}", headers=admin_headers
        ).json()
        assert detail["onboarding_status"] == "review"
        assert detail["can_approve"] is False
        assert set(detail["unresolved_steps"]) == set(prov.MANUAL_STEPS)

        r_blocked = client.post(
            f"/platform/admin/onboarding/{tenant_id}/approve",
            headers=admin_headers,
            json={"plan": "local"},
        )
        assert r_blocked.status_code == 409

        # ── 4. Resolve the 4 manual steps ───────────────────────────────────
        for step in prov.MANUAL_STEPS:
            r = client.patch(
                f"/platform/admin/onboarding/{tenant_id}/steps/{step}",
                headers=admin_headers,
                json={"status": "done", "detail": {"note": f"e2e-test:{step}"}},
            )
            assert r.status_code == 200, r.text

        detail = client.get(
            f"/platform/admin/onboarding/{tenant_id}", headers=admin_headers
        ).json()
        assert detail["can_approve"] is True
        assert detail["unresolved_steps"] == []

        # ── 5. Approve — the only path to onboarding_status=active ─────────
        r = client.post(
            f"/platform/admin/onboarding/{tenant_id}/approve",
            headers=admin_headers,
            json={"plan": "local", "status": "trial"},
        )
        assert r.status_code == 200, r.text
        approved = r.json()
        assert approved["onboarding_status"] == "active"
        assert approved["plan"] == "local"
        assert approved["account_status"] == "trial"

        # A second approve must 409 (already active).
        r_twice = client.post(
            f"/platform/admin/onboarding/{tenant_id}/approve",
            headers=admin_headers,
            json={"plan": "local"},
        )
        assert r_twice.status_code == 409

        # ── 6. The actual traffic gate must now say yes ─────────────────────
        clear_tenant_cache(tenant_id)
        assert tenant_is_active(tenant_id) is True

    finally:
        # Self-cleaning: cascades to tenant_configs, provisioning_jobs,
        # provisioning_steps, tenant_plan_changes (all ON DELETE CASCADE).
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        clear_tenant_cache(tenant_id)


def test_reject_keeps_the_row_and_slug(client):
    from sqlalchemy import text

    from platform_db import get_engine
    from tenants import clear_tenant_cache, tenant_is_active

    engine = get_engine()
    assert engine is not None

    platform_secret = os.environ["PLATFORM_API_SECRET"]
    admin_secret = os.environ["PLATFORM_ADMIN_SECRET"]
    run_id = uuid.uuid4().hex[:10]
    tenant_id = f"e2e-reject-{run_id}"
    clerk_user = f"user_e2e_reject_{run_id}"

    signup_headers = {"X-Platform-Secret": platform_secret, "X-Platform-User": clerk_user}
    admin_headers = {"X-Platform-Admin-Secret": admin_secret, "X-Platform-User": "e2e-test-admin"}

    try:
        r = client.post(
            "/platform/signup",
            headers=signup_headers,
            json={
                "company_name": f"E2E Reject Test {run_id}",
                "contact_email": "onboarding-e2e-test@example.com",
                "business_tz": "America/Toronto",
                "tenant_id": tenant_id,
            },
        )
        assert r.status_code == 201, r.text

        r = client.post(
            f"/platform/admin/onboarding/{tenant_id}/reject",
            headers=admin_headers,
            json={"reason": "e2e test rejection"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["onboarding_status"] == "rejected"
        assert body["rejected_reason"] == "e2e test rejection"

        # The slug stays claimed and the tenant never serves traffic.
        clear_tenant_cache(tenant_id)
        assert tenant_is_active(tenant_id) is False

        # A fresh signup by the SAME user, after rejection, is allowed again
        # (PENDING_SIGNUP_STATUSES excludes 'rejected').
        r2 = client.post(
            "/platform/signup",
            headers=signup_headers,
            json={
                "company_name": f"E2E Reject Retry {run_id}",
                "contact_email": "onboarding-e2e-test@example.com",
                "business_tz": "America/Toronto",
            },
        )
        assert r2.status_code == 201, r2.text
        second_tenant_id = r2.json()["tenant_id"]

        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tenants WHERE id = :tid"), {"tid": second_tenant_id}
            )
        clear_tenant_cache(second_tenant_id)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        clear_tenant_cache(tenant_id)
