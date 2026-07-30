"""Add usage_notifications — dedup table for soft usage-limit warning emails.

Phase 3 ticket 3.4. One row per tenant/billing-period/threshold that has
already been emailed; the PRIMARY KEY plus INSERT ... ON CONFLICT DO NOTHING
(platform_api/usage_alerts.py) is what guarantees at most one email per
tenant per threshold per month, safe even under concurrent webhook calls.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_notifications",
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        # 'approaching' | 'over'
        sa.Column("threshold", sa.Text(), nullable=False),
        sa.Column(
            "sent_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "period_start", "threshold"),
    )


def downgrade() -> None:
    op.drop_table("usage_notifications")
