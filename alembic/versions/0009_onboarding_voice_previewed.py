"""Add tenants.onboarding_voice_previewed_at — onboarding voice gate
(docs/ESMI_DASHBOARD_UX.md Section 7 Step 3, P2).

Nullable, set exactly once (first successful POST /platform/voice/preview
for the tenant) by platform_api/voice_preview.py — never written by the
frontend directly, so a failed/errored preview can never set it. NULL means
"hasn't previewed yet"; existing tenants that completed onboarding before
this gate existed are unaffected since the gate only applies to the new
dashboard onboarding step, not to tenant_is_active().

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-07
"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants", sa.Column("onboarding_voice_previewed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tenants", "onboarding_voice_previewed_at")
