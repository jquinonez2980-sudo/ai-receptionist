"""Add kb_entries.language — FAQ language field
(docs/ESMI_DASHBOARD_UX.md Section 5.3).

Nullable, backfilled to NULL for every existing row (unspecified — no badge
in the dashboard) — no data migration needed. platform_api/knowledge.py
validates new values against {"en", "es", "auto"} at the API layer, same as
calls.language (migration 0008): no DB check constraint, so a value this
table has never seen can't fail a deploy mid-migration.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-07
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kb_entries", sa.Column("language", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("kb_entries", "language")
