# platform_api/chat_log.py — web chat turn -> chat_sessions table.
#
# Mirrors platform_api/call_log.py's shape (upsert keyed on a stable id,
# fail-soft on DB errors) but far lighter: chat_sessions is metadata only,
# never a transcript. The LangGraph Postgres checkpointer stays the source of
# truth for actual messages (see api.py's _stream_chat).

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

CHAT_OUTCOMES = ("booked", "escalated")

_ESCALATION_TOOLS = {"escalate_to_human", "transfercall", "transfer_call"}

# One call to record_chat_turn() covers one full turn (the user's message +
# the assistant's reply), so message_count advances by 2 per call rather than
# tracking each side separately.
_MESSAGES_PER_TURN = 2


def derive_outcome(tools_called: Optional[set]) -> Optional[str]:
    """Light outcome signal from the tools invoked during one turn.

    booked takes precedence over escalated (a completed booking is the
    stronger signal even if the same turn also touched escalation). None
    when neither fired — outcome then stays whatever an earlier turn set.
    """
    if not tools_called:
        return None
    if "book_appointment" in tools_called:
        return "booked"
    if tools_called & _ESCALATION_TOOLS:
        return "escalated"
    return None


def record_chat_turn(
    tenant_id: str, thread_id: str, tools_called: Optional[set] = None
) -> None:
    """Upsert one chat_sessions row for (tenant_id, thread_id), keyed on the
    unique (tenant_id, thread_id) constraint from alembic 0001.

    thread_id is expected already tenant-namespaced (tenants.namespaced_thread)
    to match the checkpointer's thread — same id space, so a dashboard row
    lines up with the actual conversation if transcript lookup is added later.

    Fail-soft: swallows every exception and logs. Never raises into the chat
    path — a DB hiccup must not break /chat.
    """
    try:
        from sqlalchemy import text

        from platform_db import get_engine

        engine = get_engine()
        if engine is None:
            log.debug("chat_sessions: DATABASE_URL not set — turn not logged.")
            return

        outcome = derive_outcome(tools_called)

        with engine.begin() as conn:
            # The FK target must exist even for tenants created after the
            # importer ran (see call_log.upsert_call).
            conn.execute(
                text("INSERT INTO tenants (id) VALUES (:tid) ON CONFLICT (id) DO NOTHING"),
                {"tid": tenant_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO chat_sessions (
                        tenant_id, thread_id, channel, started_at, last_at,
                        message_count, outcome
                    ) VALUES (
                        :tenant_id, :thread_id, 'web', now(), now(),
                        :messages_per_turn, :outcome
                    )
                    ON CONFLICT (tenant_id, thread_id) DO UPDATE SET
                        last_at = now(),
                        message_count = chat_sessions.message_count + :messages_per_turn,
                        outcome = COALESCE(EXCLUDED.outcome, chat_sessions.outcome)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "thread_id": thread_id,
                    "messages_per_turn": _MESSAGES_PER_TURN,
                    "outcome": outcome,
                },
            )
    except Exception:
        log.exception(
            "chat_sessions upsert failed for tenant=%s thread=%s — turn not logged.",
            tenant_id, thread_id,
        )
