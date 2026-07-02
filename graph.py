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

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agents import make_booker, make_closer, make_informer, receptionist_agent
from observability import init_observability
from state import AgentState

log = logging.getLogger(__name__)
init_observability()

USE_MULTI_AGENT = os.getenv("USE_MULTI_AGENT", "0") == "1"

# ── Checkpointer factory ──────────────────────────────────────────────────────
# api.py uses graph.astream_events() which is fully async. The checkpointer
# must therefore be AsyncPostgresSaver (not the sync PostgresSaver), or it
# raises NotImplementedError on every await checkpointer.aget_tuple() call.
# We initialise the async pool + saver via asyncio.run() at import time,
# before uvicorn creates its event loop — safe at module scope.

async def _build_async_checkpointer() -> BaseCheckpointSaver:
    """Build the async-compatible checkpointer.

    Must be called from inside a running event loop (e.g. FastAPI lifespan).
    AsyncPostgresSaver is required because api.py uses astream_events() which
    is fully async — the sync PostgresSaver raises NotImplementedError on every
    await checkpointer.aget_tuple() call.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        log.warning(
            "DATABASE_URL is not set — falling back to MemorySaver. "
            "Conversations WILL be lost on restart. Set DATABASE_URL for prod."
        )
        return MemorySaver()
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=db_url,
            max_size=int(os.getenv("DB_POOL_SIZE", "10")),
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # idempotent — creates checkpoint tables if missing
        log.info("Checkpointer: AsyncPostgresSaver (connected).")
        return saver
    except Exception as e:
        log.error(
            "Failed to initialize AsyncPostgresSaver (%s). Falling back to MemorySaver.",
            e,
        )
        return MemorySaver()


def get_checkpointer() -> BaseCheckpointSaver:
    """Sync checkpointer for tests and local dev (always MemorySaver).
    Production uses build_graph_async() which calls _build_async_checkpointer().
    """
    log.warning(
        "get_checkpointer() returns MemorySaver — for production use "
        "build_graph_async() from the FastAPI lifespan instead."
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


# Phase 2: when the deterministic rules don't match, classify with a cheap LLM
# instead of blindly defaulting to informer. Catches keyword-free booking
# ("can you get me on your calendar") and escalation ("I've waited for days")
# that the keyword lists miss. Web-chat only — the voice path routes via VAPI.
_LLM_ROUTER_ENABLED = os.getenv("USE_LLM_ROUTER", "1") == "1"
_ROUTER_NODES = ("informer", "booker", "closer")
# Tag on the router classifier's LLM call. api.py drops on_chat_model_stream
# events carrying this tag so the one-word classification never reaches the user.
_ROUTER_STREAM_TAG = "esmi-router-internal"
_ROUTER_SYSTEM = (
    "You are the router for an AI receptionist. Classify the user's latest message "
    "into exactly one of:\n"
    "- booker: wants to book, reschedule, or cancel an appointment, OR is giving a "
    "date/time/availability in an ongoing booking.\n"
    "- closer: a hot lead mentioning budget, timeline, or urgency; expresses "
    "frustration; or asks to speak with a human.\n"
    "- informer: anything else — questions about services, pricing, the company, or "
    "general conversation.\n"
    "Reply with ONLY one word: informer, booker, or closer."
)
_router_llm = None


def _get_router_llm():
    global _router_llm
    if _router_llm is None:
        from langchain_openai import ChatOpenAI
        _router_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    return _router_llm


def _llm_route(human_text: str) -> str:
    """Classify a keyword-miss message. Fail-safe: returns 'informer' on any error."""
    text = (human_text or "").strip()
    if not text:
        return "informer"
    try:
        # Tag the call so the SSE layer (api.py) can filter the classifier's
        # one-word output out of the user-facing token stream.
        resp = _get_router_llm().invoke(
            [("system", _ROUTER_SYSTEM), ("user", text)],
            config={"tags": [_ROUTER_STREAM_TAG]},
        )
        ans = (resp.content or "").strip().lower()
        for node in ("booker", "closer", "informer"):  # booker/closer before informer
            if node in ans:
                log.info("LLM router classified %r → %s", text[:60], node)
                return node
    except Exception:
        log.warning("LLM router failed; defaulting to informer.", exc_info=True)
    return "informer"


def _last_human_text(state: AgentState) -> str:
    for m in reversed(state.get("messages") or []):
        if getattr(m, "type", None) == "human":
            return (m.content or "").lower()
    return ""


def _route_rules(state: AgentState) -> str | None:
    """Deterministic, zero-latency routing tier. Returns a node name when a rule
    fires with high confidence, or None when no rule matches (caller decides the
    fallback). Kept pure + side-effect-free so the routing unit tests need no model.

    Priority:
      1. urgency signals → closer (always, even mid-booking)
      2. sticky: state["next"] == "booker" → booker (keyword-free mid-flow replies)
      3. explicit booking keyword → booker
      4. no match → None

    NOTE: there is deliberately no "appointment_details is set → booker" rule.
    appointment_details is never cleared once a booking succeeds (see
    _make_booker_node), so that rule used to permanently hijack every later
    message in the thread to the booker, even "what services do you offer?"
    turns after the booking was done. The sticky `next` mechanism already
    covers legitimate mid-booking continuation; a completed booking should
    fall through to the LLM router like any other message.
    """
    human_text = _last_human_text(state)

    if any(kw in human_text for kw in _URGENCY_KW):
        return "closer"
    if state.get("next") == "booker":
        return "booker"
    if any(kw in human_text for kw in _BOOKING_KW):
        return "booker"
    return None


def _route(state: AgentState) -> str:
    """Conditional-edge router. Deterministic rules first; on no match, the LLM
    classifier (if enabled) instead of a blind informer default."""
    decided = _route_rules(state)
    if decided is not None:
        return decided
    if _LLM_ROUTER_ENABLED:
        return _llm_route(_last_human_text(state))
    return "informer"


# ── Context compression ───────────────────────────────────────────────────────

_SUMMARIZE_AFTER_TURNS = 15  # number of human turns before compressing history


def _compress_node(state: AgentState) -> dict:
    """Summarise old messages once the conversation exceeds _SUMMARIZE_AFTER_TURNS.

    Runs before routing on every turn — a no-op until the threshold is hit.
    Removes messages older than the last 6 (3 full turns) and replaces them
    with a single SystemMessage summary so the specialists always see compact
    context without losing key lead/booking details.
    """
    from langchain_core.messages import SystemMessage
    from langchain_openai import ChatOpenAI
    from langgraph.graph.message import RemoveMessage

    msgs = state.get("messages") or []
    human_count = sum(1 for m in msgs if getattr(m, "type", None) == "human")
    if human_count < _SUMMARIZE_AFTER_TURNS:
        return {}

    # Keep last 6 messages (roughly 3 full turns) as live context.
    to_compress = msgs[:-6]
    if not to_compress:
        return {}

    # Skip messages that are already summary placeholders.
    to_summarize = [m for m in to_compress if not (
        isinstance(m, SystemMessage) and
        str(getattr(m, "content", "")).startswith("[Earlier conversation]:")
    )]
    if not to_summarize:
        return {}

    history = "\n".join(
        f"{getattr(m, 'type', 'msg')}: {(m.content or '')[:300]}"
        for m in to_summarize
    )
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    resp = llm.invoke([{
        "role": "user",
        "content": (
            "Summarise this AI receptionist conversation for handoff context. "
            "Preserve: the user's name and email, any booked appointments "
            "(date/time/event id), pricing discussed, budget/urgency signals, "
            "open questions. Be concise.\n\n" + history
        ),
    }])

    removals = [RemoveMessage(id=m.id) for m in to_compress]
    summary = SystemMessage(
        content=f"[Earlier conversation]: {resp.content}"
    )
    log.info("Compressed %d messages into a summary.", len(to_compress))
    return {"messages": removals + [summary]}


# ── State-writing specialist wrappers ─────────────────────────────────────────

def _make_informer_node(informer):
    """Wrap the informer agent to also update lead_score.

    Clears `next` so a prior booker stickiness is released — informer is the
    safe default, so follow-up questions naturally fall back here.
    """
    def node(state: AgentState, config: RunnableConfig) -> dict:
        # Forward config so the sub-agent's tools (RunnableConfig injection) and
        # prompt (runtime.context) both see the per-request tenant_id.
        result = informer.invoke(state, config=config)
        msgs = result.get("messages", [])
        last = msgs[-1].content if msgs else ""
        # Pricing discussed = warmer lead (20 pts); any informer reply = mild intent (5 pts).
        from tenants import load_tenant
        tenant_id = ((config or {}).get("configurable") or {}).get("tenant_id") or "default"
        _tenant_cfg = load_tenant(tenant_id)
        pricing_signals = [str(v) for p in _tenant_cfg.pricing for v in (p.get("setup_from"), p.get("monthly_from")) if v]
        bump = 20 if any(p in last for p in pricing_signals) else 5
        return {
            "messages": msgs,
            "lead_score": min(100, (state.get("lead_score") or 0) + bump),
            "next": None,
        }
    return node


def _booking_call_succeeded(content) -> bool:
    """True only for book_appointment's actual success strings.

    book_appointment (tools.py) returns "Booked — confirmed for ..." on a fresh
    insert, or "That's already booked — you're confirmed for ..." on an
    idempotent re-attempt. Every failure path (closed day, outside business
    hours, slot taken, calendar unreachable) returns a different, non-matching
    string, so this never mis-classifies a rejection as a success.
    """
    text = content if isinstance(content, str) else str(content)
    return text.startswith("Booked") or "you're confirmed" in text


def _make_booker_node(booker):
    """Wrap the booker agent: populate appointment_details on success AND keep
    the conversation sticky while a booking is mid-flow.

    The booking flow spans multiple turns where the user's replies ("june 11 at
    10am", "yes", "john@example.com") contain no booking keywords. Without
    stickiness the router would send those to the informer (which has no calendar
    tools). Setting next="booker" while mid-flow keeps the conversation here until
    the booking completes; once book_appointment fires we release (next=None).
    """
    def node(state: AgentState, config: RunnableConfig) -> dict:
        result = booker.invoke(state, config=config)
        msgs = result.get("messages", [])

        appt = state.get("appointment_details")
        score = state.get("lead_score") or 0
        qualified = state.get("qualified") or False
        booked = False

        # Scan only the new messages added this turn (not the full history) to
        # avoid re-firing booked=True on tool calls from earlier turns.
        prior_count = len(state.get("messages") or [])
        new_msgs = msgs[prior_count:] if len(msgs) > prior_count else msgs

        # Map tool_call_id -> args from this turn's AIMessage tool calls, so a
        # successful ToolMessage result can be traced back to what was booked.
        call_args_by_id: dict[str, dict] = {}
        for m in new_msgs:
            for tc in (getattr(m, "tool_calls", None) or []):
                if tc.get("name") == "book_appointment":
                    call_args_by_id[tc.get("id")] = tc.get("args", {})

        # Booking "success" is judged from the ToolMessage RESULT, not the fact
        # that book_appointment was called — a rejected/failed booking (slot
        # taken, outside hours, calendar down) must not mark the lead qualified
        # or hijack routing to the booker.
        for m in new_msgs:
            if isinstance(m, ToolMessage) and getattr(m, "name", None) == "book_appointment":
                if _booking_call_succeeded(m.content):
                    appt = call_args_by_id.get(getattr(m, "tool_call_id", None), appt)
                    score = 90
                    qualified = True
                    booked = True

        # Booking done → release stickiness. Still mid-flow → stay in booker.
        next_node = None if booked else "booker"

        return {
            "messages": msgs,
            "appointment_details": appt,
            "lead_score": min(100, score),
            "qualified": qualified,
            "next": next_node,
        }
    return node


def _make_closer_node(closer):
    """Wrap the closer agent to mark lead qualified on escalation.

    Clears `next` — after a hand-off, follow-ups fall back to the informer.
    """
    def node(state: AgentState, config: RunnableConfig) -> dict:
        result = closer.invoke(state, config=config)
        msgs = result.get("messages", [])

        score = state.get("lead_score") or 0
        qualified = state.get("qualified") or False

        for m in msgs:
            tool_calls = getattr(m, "tool_calls", None) or []
            for tc in tool_calls:
                if tc.get("name") == "escalate_to_human":
                    score = max(score, 80)
                    qualified = True

        return {
            "messages": msgs,
            "lead_score": min(100, score),
            "qualified": qualified,
            "next": None,
        }
    return node


def _build_multi_agent_graph(checkpointer):
    informer = make_informer()
    booker   = make_booker()
    closer   = make_closer()

    workflow = StateGraph(AgentState)

    # Pre-routing: compress long conversations (no-op until threshold).
    workflow.add_node("compress", _compress_node)

    # Specialist wrapper nodes — run the agent + write state fields.
    workflow.add_node("informer", _make_informer_node(informer))
    workflow.add_node("booker",   _make_booker_node(booker))
    workflow.add_node("closer",   _make_closer_node(closer))

    # compress → route → specialist → END
    workflow.set_entry_point("compress")
    workflow.add_conditional_edges(
        "compress",
        _route,
        {"informer": "informer", "booker": "booker", "closer": "closer"},
    )
    for node in ("informer", "booker", "closer"):
        workflow.add_edge(node, END)

    return workflow.compile(checkpointer=checkpointer)


# ── Public graph instance ─────────────────────────────────────────────────────

def build_graph(checkpointer: Optional[BaseCheckpointSaver] = None):
    """Sync builder — used by tests and the module-level dev instance.
    Uses MemorySaver unless a checkpointer is explicitly injected.
    """
    cp = checkpointer or MemorySaver()
    if USE_MULTI_AGENT:
        g = _build_multi_agent_graph(cp)
        print("✅ Multi-agent graph built (informer / booker / closer) (checkpointer:", type(cp).__name__ + ")")
    else:
        g = _build_single_agent_graph(cp)
        print("✅ Single-agent graph built (checkpointer:", type(cp).__name__ + ")")
    return g


async def build_graph_async():
    """Async builder for production — call from FastAPI lifespan.
    Uses AsyncPostgresSaver when DATABASE_URL is set; MemorySaver otherwise.
    """
    cp = await _build_async_checkpointer()
    return build_graph(checkpointer=cp)


# Module-level instance: MemorySaver (tests + local dev).
# api.py replaces this at startup via build_graph_async().
# Wrapped in try/except so the module can be imported without OPENAI_API_KEY
# (e.g. CI unit tests, linters). Production always has the key set via Railway.
try:
    graph = build_graph()
except Exception:
    graph = None
