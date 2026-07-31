# platform_api/provisioning.py — provisioning job/step data layer (Phase 4
# ticket 4.1). No HTTP routes here: signup.py drives the pipeline forward and
# (stage 3) onboarding.py renders and completes it. Both go through this
# module so the step vocabulary and the job-status rules live in one place.
#
# Every function takes an open SQLAlchemy connection rather than fetching its
# own, so a caller can do "insert tenant + create job + mark two steps done"
# in a single transaction — a half-provisioned tenant with no job row would be
# invisible to the admin queue, which is the one failure mode worth designing
# out.
#
# What "manual" means: the vapi_assistant / phone_number / calendar / kb_seed
# steps are real work that just isn't automated in v1 (see the stage-2 notes —
# VAPI needs an API key and a template, number purchase costs money, calendar
# creation is its own ticket, and the KB lives on Railway's ephemeral disk).
# They are seeded as `manual` so they show up in the admin checklist as
# outstanding human tasks, and approval is blocked until each is resolved.
# That turns the new-client runbook into tracked state instead of tribal
# knowledge, and leaves the pipeline shape ready for real automation later
# with no schema change.

from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── step vocabulary (mirrors alembic 0006's comment; order is display order) ──

STEP_TENANT_ROW = "tenant_row"
STEP_CLERK_ORG = "clerk_org"
STEP_CONFIG_SEED = "config_seed"
STEP_VAPI_ASSISTANT = "vapi_assistant"
STEP_PHONE_NUMBER = "phone_number"
STEP_CALENDAR = "calendar"
STEP_KB_SEED = "kb_seed"

STEPS: tuple[str, ...] = (
    STEP_TENANT_ROW,
    STEP_CLERK_ORG,
    STEP_CONFIG_SEED,
    STEP_VAPI_ASSISTANT,
    STEP_PHONE_NUMBER,
    STEP_CALENDAR,
    STEP_KB_SEED,
)

# Automated in v1 — these run inside the signup request.
AUTOMATED_STEPS: frozenset[str] = frozenset(
    {STEP_TENANT_ROW, STEP_CLERK_ORG, STEP_CONFIG_SEED}
)
# Tracked human tasks in v1.
MANUAL_STEPS: frozenset[str] = frozenset(STEPS) - AUTOMATED_STEPS

STEP_LABELS: dict[str, str] = {
    STEP_TENANT_ROW: "Tenant record",
    STEP_CLERK_ORG: "Clerk organization",
    STEP_CONFIG_SEED: "Config seed",
    STEP_VAPI_ASSISTANT: "VAPI assistant",
    STEP_PHONE_NUMBER: "Phone number",
    STEP_CALENDAR: "Google Calendar",
    STEP_KB_SEED: "Knowledge base seed",
}

# Why each manual step is manual — surfaced in the admin checklist so whoever
# is working the queue knows what is expected of them.
STEP_MANUAL_NOTES: dict[str, str] = {
    STEP_VAPI_ASSISTANT: (
        "Create the assistant in the VAPI dashboard, then paste its id here. "
        "It also has to go into the tenant config's vapi.assistant_ids."
    ),
    STEP_PHONE_NUMBER: (
        "Buy a local number in the client's area code and attach it to the "
        "assistant. Deliberately not automated — it costs money."
    ),
    STEP_CALENDAR: (
        "Create the Orchelix-managed Google Calendar and put its id in the "
        "tenant config (calendar_id, or the location's calendar_id)."
    ),
    STEP_KB_SEED: (
        "Add the client's starting knowledge docs via the dashboard Knowledge "
        "page. Not automated: the KB lives on Railway's ephemeral disk, so a "
        "backend-written seed would vanish on the next deploy."
    ),
}

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_SKIPPED = "skipped"
STEP_FAILED = "failed"
STEP_MANUAL = "manual"
STEP_STATUSES = (
    STEP_PENDING,
    STEP_RUNNING,
    STEP_DONE,
    STEP_SKIPPED,
    STEP_FAILED,
    STEP_MANUAL,
)
# Statuses that count as "nothing left to do on this step".
RESOLVED_STEP_STATUSES = frozenset({STEP_DONE, STEP_SKIPPED})

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_NEEDS_REVIEW = "needs_review"
JOB_COMPLETE = "complete"
JOB_FAILED = "failed"
JOB_STATUSES = (JOB_PENDING, JOB_RUNNING, JOB_NEEDS_REVIEW, JOB_COMPLETE, JOB_FAILED)


# ── job / step writes ─────────────────────────────────────────────────────────


def create_job(conn, tenant_id: str, created_by: Optional[str]) -> str:
    """Create a provisioning job plus one row per step. Returns the job id.

    Automated steps start `pending`; manual steps start `manual` with their
    explanatory note already in `detail` so the admin checklist is
    self-describing without the UI hardcoding the same strings.
    """
    from sqlalchemy import text

    job_id = conn.execute(
        text(
            "INSERT INTO provisioning_jobs (tenant_id, status, created_by) "
            "VALUES (:tid, :status, :by) RETURNING id"
        ),
        {"tid": tenant_id, "status": JOB_RUNNING, "by": created_by},
    ).scalar_one()

    for step in STEPS:
        manual = step in MANUAL_STEPS
        detail = {"note": STEP_MANUAL_NOTES[step]} if manual and step in STEP_MANUAL_NOTES else None
        conn.execute(
            text(
                "INSERT INTO provisioning_steps (job_id, step, status, detail) "
                "VALUES (:job, :step, :status, :detail)"
            ),
            {
                "job": job_id,
                "step": step,
                "status": STEP_MANUAL if manual else STEP_PENDING,
                "detail": json.dumps(detail) if detail is not None else None,
            },
        )
    return str(job_id)


def set_step(
    conn,
    job_id: str,
    step: str,
    status: str,
    detail: Optional[dict] = None,
    error: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> None:
    """Update one step's status/detail/error.

    `detail` MERGES onto whatever is already stored (jsonb ||) rather than
    replacing it, so recording a VAPI assistant id doesn't wipe the manual note
    explaining what the step is. finished_at is stamped when the step reaches a
    terminal status and cleared if it goes back to running.
    """
    if step not in STEPS:
        raise ValueError(f"Unknown provisioning step '{step}'")
    if status not in STEP_STATUSES:
        raise ValueError(f"Unknown step status '{status}'")

    from sqlalchemy import text

    terminal = status in (STEP_DONE, STEP_SKIPPED, STEP_FAILED)
    conn.execute(
        text(
            """
            UPDATE provisioning_steps SET
                status      = :status,
                detail      = COALESCE(detail, '{}'::jsonb) || COALESCE(CAST(:detail AS jsonb), '{}'::jsonb),
                error       = :error,
                started_at  = COALESCE(started_at, now()),
                finished_at = CASE WHEN :terminal THEN now() ELSE NULL END,
                updated_by  = COALESCE(:by, updated_by)
            WHERE job_id = :job AND step = :step
            """
        ),
        {
            "status": status,
            "detail": json.dumps(detail) if detail is not None else None,
            "error": error,
            "terminal": terminal,
            "by": updated_by,
            "job": job_id,
            "step": step,
        },
    )


def recompute_job_status(conn, job_id: str) -> str:
    """Derive and persist the job's status from its steps. Returns the new value.

    Rules, in precedence order:
      any step failed                        -> failed
      every step resolved (done/skipped)     -> complete
      every AUTOMATED step resolved          -> needs_review  (humans' turn)
      otherwise                              -> running
    """
    from sqlalchemy import text

    rows = conn.execute(
        text("SELECT step, status FROM provisioning_steps WHERE job_id = :job"),
        {"job": job_id},
    ).all()
    by_step = {r[0]: r[1] for r in rows}

    if any(s == STEP_FAILED for s in by_step.values()):
        status = JOB_FAILED
    elif all(by_step.get(s) in RESOLVED_STEP_STATUSES for s in STEPS):
        status = JOB_COMPLETE
    elif all(by_step.get(s) in RESOLVED_STEP_STATUSES for s in AUTOMATED_STEPS):
        status = JOB_NEEDS_REVIEW
    else:
        status = JOB_RUNNING

    conn.execute(
        text(
            "UPDATE provisioning_jobs SET status = :status, updated_at = now(), "
            "completed_at = CASE WHEN :done THEN now() ELSE completed_at END "
            "WHERE id = :job"
        ),
        {"status": status, "done": status == JOB_COMPLETE, "job": job_id},
    )
    return status


# ── reads ─────────────────────────────────────────────────────────────────────


def latest_job(conn, tenant_id: str) -> Optional[dict]:
    """Most recent provisioning job for a tenant with its steps, or None."""
    from sqlalchemy import text

    job = conn.execute(
        text(
            "SELECT id, status, created_by, error, created_at, updated_at, completed_at "
            "FROM provisioning_jobs WHERE tenant_id = :tid "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"tid": tenant_id},
    ).mappings().first()
    if job is None:
        return None
    return {
        "job_id": str(job["id"]),
        "status": job["status"],
        "created_by": job["created_by"],
        "error": job["error"],
        "created_at": _iso(job["created_at"]),
        "updated_at": _iso(job["updated_at"]),
        "completed_at": _iso(job["completed_at"]),
        "steps": job_steps(conn, str(job["id"])),
    }


def job_steps(conn, job_id: str) -> list[dict]:
    """All steps for a job in STEPS order (not insertion or alphabetical)."""
    from sqlalchemy import text

    rows = conn.execute(
        text(
            "SELECT step, status, detail, error, started_at, finished_at, updated_by "
            "FROM provisioning_steps WHERE job_id = :job"
        ),
        {"job": job_id},
    ).mappings().all()
    by_step = {r["step"]: r for r in rows}

    out: list[dict] = []
    for step in STEPS:
        r = by_step.get(step)
        if r is None:
            continue
        detail = r["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        out.append(
            {
                "step": step,
                "label": STEP_LABELS[step],
                "automated": step in AUTOMATED_STEPS,
                "status": r["status"],
                "detail": detail or {},
                "error": r["error"],
                "started_at": _iso(r["started_at"]),
                "finished_at": _iso(r["finished_at"]),
                "updated_by": r["updated_by"],
            }
        )
    return out


def unresolved_steps(steps: list[dict]) -> list[str]:
    """Step keys still blocking approval — anything not done/skipped."""
    return [s["step"] for s in steps if s["status"] not in RESOLVED_STEP_STATUSES]


def _iso(v):
    return v.isoformat() if v else None
