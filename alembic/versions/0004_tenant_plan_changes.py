"""Add tenant_plan_changes — insert-only audit trail for admin plan/status edits.

Phase 3 ticket 3.5 (admin plan assignment). Mirrors how tenant_configs already
tracks created_by/created_at for config edits — same idea, applied to
tenants.plan / tenants.status changes made via PATCH /platform/admin/tenants/
{tenant_id}/plan.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_plan_changes",
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
        sa.Column("old_plan", sa.Text(), nullable=True),
        sa.Column("new_plan", sa.Text(), nullable=False),
        sa.Column("old_status", sa.Text(), nullable=True),
        sa.Column("new_status", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_tenant_plan_changes_tenant", "tenant_plan_changes", ["tenant_id", "changed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_plan_changes_tenant", table_name="tenant_plan_changes")
    op.drop_table("tenant_plan_changes")
