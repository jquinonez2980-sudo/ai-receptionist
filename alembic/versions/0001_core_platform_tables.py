"""Core platform tables: tenants, tenant_configs, calls, chat_sessions.

Phase 0 of PLATFORM_BLUEPRINT.md (Ticket 1). Additive only — the agent runtime
does not require any of these tables to exist; tenants.load_tenant() falls back
to tenants/<id>/config.json when the tenant_configs lookup fails or is empty.

Revision ID: 0001
Revises:
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Text(), primary_key=True),  # slug, e.g. 'otro-nivel'
        sa.Column("clerk_org_id", sa.Text(), nullable=True, unique=True),
        # trial | live | past_due | suspended | archived
        sa.Column("status", sa.Text(), nullable=False, server_default="live"),
        # starter | pro | scale | managed
        sa.Column("plan", sa.Text(), nullable=False, server_default="managed"),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("business_tz", sa.Text(), nullable=True),
        sa.Column("locale_default", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # Append-only config versions. load_tenant() reads the highest published
    # version; the config jsonb is byte-compatible with tenants/<id>/config.json
    # (same shape tenants._config_from_file parses).
    op.create_table(
        "tenant_configs",
        sa.Column(
            "tenant_id",
            sa.Text(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column(
            "published", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "version"),
    )
    op.create_index(
        "ix_tenant_configs_lookup",
        "tenant_configs",
        ["tenant_id", "published", "version"],
    )

    op.create_table(
        "calls",
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
        sa.Column("vapi_call_id", sa.Text(), nullable=True, unique=True),
        # Plain text for now — a phone_numbers table arrives in a later phase.
        sa.Column("vapi_phone_number_id", sa.Text(), nullable=True),
        sa.Column("caller_e164", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        # booked | info | escalated | voicemail | abandoned (derived)
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("transcript", postgresql.JSONB(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("recording_key", sa.Text(), nullable=True),  # object-storage key
        sa.Column("cost_vapi", sa.Numeric(10, 4), nullable=True),
        sa.Column("cost_llm", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_calls_tenant_started", "calls", ["tenant_id", "started_at"])

    op.create_table(
        "chat_sessions",
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
        # LangGraph thread id (already tenant-namespaced by tenants.namespaced_thread)
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default="web"),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.UniqueConstraint("tenant_id", "thread_id", name="uq_chat_sessions_thread"),
    )
    op.create_index(
        "ix_chat_sessions_tenant_last", "chat_sessions", ["tenant_id", "last_at"]
    )


def downgrade() -> None:
    op.drop_table("chat_sessions")
    op.drop_table("calls")
    op.drop_table("tenant_configs")
    op.drop_table("tenants")
