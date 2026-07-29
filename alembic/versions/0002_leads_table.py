"""Adopt the existing `leads` table into alembic + add dashboard status.

`leads` was NOT created by a migration — leads.py (finding 7.1) lazily
CREATE TABLE IF NOT EXISTS's it the first time record_lead() runs from a
qualified multi-agent conversation (graph.py's booker/closer nodes). It is
live in production today (thread_id PK, tenant_id, lead_score, qualified,
contact, summary, last_updated) but only ever covers WEB CHAT — voice bypasses
the graph entirely (api.py's /voice/tools calls tools directly), so a phone
escalation is never recorded here.

This migration:
  1. CREATE TABLE IF NOT EXISTS with leads.py's exact schema — a no-op in
     prod (table already exists byte-for-byte), but makes a fresh environment
     that has never run record_lead() end up in the same state.
  2. Adds `status` (new|contacted|won|lost) — the one real gap: nothing
     tracks dashboard follow-up state today. Additive-only: leads.py's
     INSERT/UPSERT use an explicit column list that never mentions `status`,
     so this cannot change its behavior — new rows just take the default,
     and re-upserts (a conversation continuing after someone marks a lead
     contacted) never touch it back to 'new'.
  3. An index for the tenant-scoped dashboard query pattern.

platform_api/leads.py additionally treats escalated `calls` rows with no
matching `leads` row as synthetic/derived leads (voice's uncaptured half) —
promoted into a real row (thread_id = 'voice:<call id>') on first status
change. That logic lives entirely in the read/write API, not here.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            thread_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            lead_score INTEGER,
            qualified BOOLEAN NOT NULL DEFAULT FALSE,
            contact TEXT,
            summary TEXT,
            last_updated TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_leads_tenant_last_updated "
        "ON leads (tenant_id, last_updated DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_leads_tenant_last_updated")
    op.drop_column("leads", "status")
