# leads.py — persistent record of qualified leads (finding 7.1: lead_score and
# qualified were computed by graph.py's specialist nodes but never went
# anywhere — no CRM row, no dashboard, no digest email. This gives them one:
# a `leads` table in the SAME Railway Postgres already backing the LangGraph
# checkpointer, no new infrastructure).
#
# Writes are synchronous + best-effort, called from graph.py's node wrappers
# (which are sync functions run by LangGraph's executor) — a DB hiccup here
# must never break the conversation, so every failure is caught and logged,
# never raised. Reads (the /leads endpoint in api.py) are async, matching
# FastAPI's async request handlers.

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    thread_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    lead_score INTEGER,
    qualified BOOLEAN NOT NULL DEFAULT FALSE,
    contact TEXT,
    summary TEXT,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_UPSERT_SQL = """
INSERT INTO leads (thread_id, tenant_id, lead_score, qualified, contact, summary, last_updated)
VALUES (%(thread_id)s, %(tenant_id)s, %(lead_score)s, %(qualified)s, %(contact)s, %(summary)s, %(last_updated)s)
ON CONFLICT (thread_id) DO UPDATE SET
    tenant_id = EXCLUDED.tenant_id,
    lead_score = EXCLUDED.lead_score,
    qualified = EXCLUDED.qualified,
    contact = COALESCE(EXCLUDED.contact, leads.contact),
    summary = COALESCE(EXCLUDED.summary, leads.summary),
    last_updated = EXCLUDED.last_updated;
"""

_LIST_SQL = """
SELECT thread_id, tenant_id, lead_score, qualified, contact, summary, last_updated
FROM leads
ORDER BY qualified DESC, lead_score DESC NULLS LAST, last_updated DESC
LIMIT %s;
"""

_table_ensured = False


def _ensure_table_sync(conn) -> None:
    global _table_ensured
    if _table_ensured:
        return
    with conn.cursor() as cur:
        cur.execute(_TABLE_SQL)
    _table_ensured = True


def record_lead(
    thread_id: str,
    tenant_id: str,
    lead_score: Optional[int],
    qualified: bool,
    contact: Optional[str] = None,
    summary: Optional[str] = None,
) -> None:
    """Upsert a lead snapshot. Best-effort and synchronous — called directly
    from graph.py's (sync) node wrappers. Silently no-ops without DATABASE_URL
    (local dev); never raises on any other failure, just logs a warning.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return
    try:
        import psycopg

        with psycopg.connect(db_url, autocommit=True) as conn:
            _ensure_table_sync(conn)
            with conn.cursor() as cur:
                cur.execute(
                    _UPSERT_SQL,
                    {
                        "thread_id": thread_id,
                        "tenant_id": tenant_id,
                        "lead_score": lead_score,
                        "qualified": qualified,
                        "contact": contact,
                        "summary": summary,
                        "last_updated": datetime.now(timezone.utc),
                    },
                )
    except Exception:
        log.warning("record_lead failed for thread_id=%s", thread_id, exc_info=True)


async def list_leads(limit: int = 50) -> list[dict]:
    """Async read for the /leads diagnostic endpoint (api.py)."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return []
    import psycopg
    from psycopg.rows import dict_row

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_TABLE_SQL)
            await cur.execute(_LIST_SQL, (limit,))
            rows = await cur.fetchall()

    return [
        {**row, "last_updated": row["last_updated"].isoformat() if row.get("last_updated") else None}
        for row in rows
    ]
