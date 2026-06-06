# graph.py — Phase 1 + Phase 4
#
# USE_MULTI_AGENT=0 (default): single receptionist node (Phase 1).
# USE_MULTI_AGENT=1: three-specialist graph with rule-based routing (Phase 4).
#
# The external API (graph.astream_events, thread_id, AgentState) is identical
# in both modes — api.py and the streaming path are unaffected by the flag.

from __future__ import annotations

import logging
import os
from typing import Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from state import AgentState
from agents import receptionist_agent, make_informer, make_booker, make_closer
from observability import init_observability

log = logging.getLogger(__name__)
init_observability()

USE_MULTI_AGENT = os.getenv("USE_MULTI_AGENT", "0") == "1"

# ── Checkpointer factory ──────────────────────────────────────────────────────

def _build_postgres_saver(db_url: str) -> BaseCheckpointSaver:
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=db_url,
        max_size=int(os.getenv("DB_POOL_SIZE", "10")),
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=True,
    )
    saver = PostgresSaver(pool)
    saver.setup()
    return saver


def get_checkpointer() -> BaseCheckpointSaver:
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
            "Failed to initialize PostgresSaver (%s). Falling back to MemorySaver.",
            e,
        )
        return MemorySaver()


# ── Phase 1: single-node graph ───────────────────────────────────────────────

def _build_single_agent_graph(checkpointer):
    workflow = StateGraph(AgentState)
    workflow.add_node("receptionist", receptionist_agent)
    workflow.set_entry_point("receptionist")
    workflow.add_edge("receptionist", END)
    return workflow.compile(checkpointer=checkpointer)


# ── Phase 4: three-specialist graph ──────────────────────────────────────────

# Keywords that indicate the user wants to book / manage an appointment.
_BOOKING_KW = frozenset({
    "book", "schedule", "appointment", "slot", "availability",
    "reschedule", "cancel", "move my", "change my", "find my booking",
    "intro call", "demo call", "when are you", "next week", "next monday",
    "next tuesday", "next wednesday", "next thursday", "next friday",
})

# Keywords that indicate urgency / hot-lead / human escalation needed.
_URGENCY_KW = frozenset({
    "asap", "urgent", "this quarter", "q3", "q4", "budget approved",
    "budget is approved", "ready to start", "need this now", "immediately",
    "speak with", "talk to a person", "talk to someone", "frustrated",
    "a human",
})


def _route(state: AgentState) -> str:
    """Rule-based router — zero latency, no LLM hop.

    Priority: urgency (closer) > booking intent (booker) > default (informer).
    Booking-in-progress sticky: if appointment_details is populated the
    conversation is mid-booking; keep it in the booker's domain.
    """
    msgs = state.get("messages") or []
    # Find the most recent human message.
    human_text = ""
    for m in reversed(msgs):
        if getattr(m, "type", None) == "human":
            human_text = (m.content or "").lower()
            break

    # Urgency always wins.
    if any(kw in human_text for kw in _URGENCY_KW):
        return "closer"

    # Mid-booking: appointment_details was set by a previous booker run.
    if state.get("appointment_details"):
        return "booker"

    # Explicit booking intent.
    if any(kw in human_text for kw in _BOOKING_KW):
        return "booker"

    # Default: answer questions.
    return "informer"


def _build_multi_agent_graph(checkpointer):
    informer = make_informer()
    booker   = make_booker()
    closer   = make_closer()

    workflow = StateGraph(AgentState)
    workflow.add_node("informer", informer)
    workflow.add_node("booker",   booker)
    workflow.add_node("closer",   closer)

    # Route every incoming turn to the right specialist.
    workflow.set_conditional_entry_point(
        _route,
        {"informer": "informer", "booker": "booker", "closer": "closer"},
    )

    # Each specialist produces one reply then ends the turn.
    for node in ("informer", "booker", "closer"):
        workflow.add_edge(node, END)

    return workflow.compile(checkpointer=checkpointer)


# ── Public graph instance ─────────────────────────────────────────────────────

def build_graph(checkpointer: Optional[BaseCheckpointSaver] = None):
    cp = checkpointer or get_checkpointer()
    if USE_MULTI_AGENT:
        log.info("Graph mode: Phase 4 multi-agent (USE_MULTI_AGENT=1).")
        g = _build_multi_agent_graph(cp)
        print("✅ Multi-agent graph built (informer / booker / closer).")
    else:
        log.info("Graph mode: Phase 1 single-agent (USE_MULTI_AGENT=0).")
        g = _build_single_agent_graph(cp)
        print("✅ Single-agent graph built.")
    return g


graph = build_graph()
