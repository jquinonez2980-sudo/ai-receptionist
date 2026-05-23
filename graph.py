# graph.py — Phase 1
#
# Changes vs. v0:
#   1. Checkpointer is now a factory. DATABASE_URL → PostgresSaver (production).
#      No DATABASE_URL → MemorySaver with a loud warning (dev only).
#   2. Idempotent setup(): PostgresSaver.setup() is safe to call repeatedly.
#   3. Observability is initialized once on import.
#
# Graph topology is unchanged — Phase 2 will replace the single ReAct node
# with the supervisor + specialists pattern from the architecture review.

from __future__ import annotations

import logging
import os
from typing import Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from state import AgentState
from agents import receptionist_agent
from observability import init_observability

log = logging.getLogger(__name__)
init_observability()


# ── Checkpointer factory ──────────────────────────────────────────────────
def _build_postgres_saver(db_url: str) -> BaseCheckpointSaver:
    """Construct a pooled PostgresSaver. Tables are created on first run."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=db_url,
        max_size=int(os.getenv("DB_POOL_SIZE", "10")),
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=True,
    )
    saver = PostgresSaver(pool)
    saver.setup()  # idempotent; creates checkpoint tables if missing
    return saver


def get_checkpointer() -> BaseCheckpointSaver:
    """Return the configured checkpointer for this process.

    Production: PostgresSaver. Dev fallback: MemorySaver (lossy on restart).
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        log.warning(
            "DATABASE_URL is not set — falling back to MemorySaver. "
            "Conversations WILL be lost on restart. Set DATABASE_URL for prod."
        )
        return MemorySaver()
    try:
        saver = _build_postgres_saver(db_url)
        log.info("Checkpointer: PostgresSaver (connected).")
        return saver
    except Exception as e:
        log.error(
            "Failed to initialize PostgresSaver (%s). Falling back to MemorySaver. "
            "Fix DATABASE_URL before going to production.",
            e,
        )
        return MemorySaver()


# ── Graph build ───────────────────────────────────────────────────────────
def build_graph(checkpointer: Optional[BaseCheckpointSaver] = None):
    """Build the Esmi graph. Inject a checkpointer for tests."""
    workflow = StateGraph(AgentState)
    workflow.add_node("receptionist", receptionist_agent)
    workflow.set_entry_point("receptionist")
    workflow.add_edge("receptionist", END)
    return workflow.compile(checkpointer=checkpointer or get_checkpointer())


graph = build_graph()
log.info("Esmi graph built.")
print("✅ Simple graph built successfully!")
