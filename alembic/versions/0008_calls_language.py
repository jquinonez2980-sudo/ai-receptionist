"""Add calls.language — Calls list/detail language filter
(docs/ESMI_DASHBOARD_UX.md Section 5.2).

Nullable, backfilled to NULL for every existing row (shown as "Unknown" in
the dashboard, excluded only when the has_recording/language filters are
explicitly used) — no data migration needed, no VAPI re-ingest required.
platform_api/call_log.py starts populating it for new calls going forward
via a cheap heuristic on the transcript text (same approach
quality_studio.py's _looks_spanish already uses), not a VAPI payload field
— VAPI's end-of-call-report doesn't carry a reliable detected-language
field across versions.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-08
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("language", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "language")
