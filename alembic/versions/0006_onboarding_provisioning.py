"""Onboarding lifecycle + provisioning pipeline (Phase 4, ticket 4.1).

Additive only. Three changes:

1. tenants gains an `onboarding_status` column plus the signup contact fields.
   This is DELIBERATELY separate from the existing `status` column: `status`
   (trial | live | past_due | suspended | archived) is the BILLING lifecycle
   already read by platform_api/admin.py, usage.py and the billing page.
   Overloading it with "not approved yet" would conflate that with "past due"
   and change the meaning of rows those readers already handle. Existing rows
   default to 'active' so every current tenant and every current query is
   byte-identical after this migration.

2. provisioning_jobs — one row per onboarding run for a tenant.

3. provisioning_steps — one row per step per job. A separate table rather than
   a JSONB blob on the job because the step is the retry unit and the admin
   console renders per-step state (PLATFORM_BLUEPRINT.md section 5: "each step
   ... retryable, visible in super-admin").

Note for deploys: migrations are run manually on Railway (the Dockerfile CMD
only starts uvicorn), so the new backend code goes live BEFORE this runs.
tenants.py's status lookup treats an UndefinedColumn error exactly like a DB
outage — falls back to the filesystem registry — so that window is safe.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


# draft -> submitted -> provisioning -> review -> active, plus terminal
# 'rejected'. Only 'active' may serve production traffic (see
# tenants.tenant_is_active). Mirrored in tenants.ONBOARDING_STATUSES —
# keep the two in sync.
_ONBOARDING_STATUSES = (
    "draft",
    "submitted",
    "provisioning",
    "review",
    "active",
    "rejected",
)

# pending -> running -> needs_review -> complete, plus terminal 'failed'.
_JOB_STATUSES = ("pending", "running", "needs_review", "complete", "failed")

# 'manual' means "a human task, tracked but not automated" — the v1 state for
# the vapi_assistant / phone_number / calendar / kb_seed steps.
_STEP_STATUSES = ("pending", "running", "done", "skipped", "failed", "manual")


def upgrade() -> None:
    # ── 1. tenants: onboarding lifecycle + signup fields ─────────────────────
    op.add_column(
        "tenants",
        sa.Column(
            "onboarding_status",
            sa.Text(),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_check_constraint(
        "ck_tenants_onboarding_status",
        "tenants",
        sa.column("onboarding_status").in_(_ONBOARDING_STATUSES),
    )
    op.add_column("tenants", sa.Column("contact_name", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("contact_email", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("contact_phone", sa.Text(), nullable=True))
    # What the signup form asked for. Never what they get — `plan` stays
    # 'managed' until an admin confirms a real plan at approval time.
    op.add_column("tenants", sa.Column("requested_plan", sa.Text(), nullable=True))
    op.add_column(
        "tenants", sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "tenants", sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("tenants", sa.Column("approved_by", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("rejected_reason", sa.Text(), nullable=True))
    # Partial index: the admin onboarding queue only ever scans non-active
    # rows, which will stay a tiny fraction of the table.
    op.create_index(
        "ix_tenants_onboarding_pending",
        "tenants",
        ["onboarding_status", "submitted_at"],
        postgresql_where=sa.text("onboarding_status <> 'active'"),
    )

    # ── 2. provisioning_jobs ─────────────────────────────────────────────────
    op.create_table(
        "provisioning_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            sa.column("status").in_(_JOB_STATUSES), name="ck_provisioning_jobs_status"
        ),
    )
    op.create_index(
        "ix_provisioning_jobs_tenant", "provisioning_jobs", ["tenant_id", "created_at"]
    )

    # ── 3. provisioning_steps ────────────────────────────────────────────────
    op.create_table(
        "provisioning_steps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provisioning_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # tenant_row | clerk_org | config_seed | vapi_assistant | phone_number
        # | calendar | kb_seed (tenants/platform_api provisioning.STEPS)
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        # Result identifiers produced by the step: clerk org id, VAPI assistant
        # id, purchased number, calendar id. Free-form on purpose — each step
        # records different things and this is display/audit data, not a key.
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.UniqueConstraint("job_id", "step", name="uq_provisioning_steps_job_step"),
        sa.CheckConstraint(
            sa.column("status").in_(_STEP_STATUSES), name="ck_provisioning_steps_status"
        ),
    )
    op.create_index("ix_provisioning_steps_job", "provisioning_steps", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_provisioning_steps_job", table_name="provisioning_steps")
    op.drop_table("provisioning_steps")
    op.drop_index("ix_provisioning_jobs_tenant", table_name="provisioning_jobs")
    op.drop_table("provisioning_jobs")
    op.drop_index("ix_tenants_onboarding_pending", table_name="tenants")
    op.drop_constraint("ck_tenants_onboarding_status", "tenants", type_="check")
    for col in (
        "rejected_reason",
        "approved_by",
        "approved_at",
        "submitted_at",
        "requested_plan",
        "contact_phone",
        "contact_email",
        "contact_name",
        "onboarding_status",
    ):
        op.drop_column("tenants", col)
