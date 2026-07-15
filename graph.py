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
        # DATABASE_URL being SET but unreachable is a config/outage error, not the
        # "no database configured" case above — silently falling back to MemorySaver
        # here would keep the service serving requests while quietly losing every
        # conversation on the next restart, with only a log line to notice by.
        # Fail hard instead so a broken DB is visible immediately (deploy fails),
        # not discovered later as "why did all our threads lose their history".
        log.error(
            "DATABASE_URL is set but AsyncPostgresSaver init failed (%s). "
            "Refusing to silently fall back to MemorySaver.",
            e,
        )
        raise


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
# Deliberately excludes ambiguous phrases that collide with non-booking intent:
# "when are you" (catches "when are you open?" — an hours/FAQ question), "next
# week"/weekday names (catches "I'm away next week, tell me about your
# services"), and bare "cancel" (catches "how do I cancel the monthly
# service?" — a pricing/terms question). The LLM router (below) handles those
# correctly; only unambiguous booking verbs/phrases belong in this fast tier.
_BOOKING_KW = frozenset({
    "book", "schedule", "appointment", "slot", "availability",
    "available",  # "whats available this thursday" / "times available"
    "reschedule", "cancel my appointment", "cancel my booking",
    "move my", "change my", "find my booking",
    "intro call", "demo call",
})

# Mid-booking location/day switches must stay on booker (has calendar tools).
# Without this, "how about keele" can be LLM-routed to informer, which only has
# KB search and invents "couldn't find slots".
_BOOKING_CONTINUE_KW = frozenset({
    "weston", "keele", "location", "available", "instead",
    "other day", "different day", "same day", "how about", "what about",
    "walk-in", "walk in",
})

# Keywords that indicate urgency / hot-lead / human escalation needed.
# Includes Spanish equivalents (finding 7.2) — the product explicitly sells
# LATAM-Spanish bilingual support, so a Spanish-speaking hot lead needs the
# same zero-latency rule-routing an English one gets, not a bet entirely on
# the LLM router.
_URGENCY_KW = frozenset({
    "asap", "urgent", "this quarter", "q3", "q4", "budget approved",
    "budget is approved", "ready to start", "need this now", "immediately",
    "speak with", "talk to a person", "talk to someone", "frustrated",
    "a human",
    # Spanish
    "urgente", "lo antes posible", "cuanto antes", "este trimestre",
    "presupuesto aprobado", "listos para empezar", "listo para empezar",
    "necesito esto ahora", "lo necesito ahora", "inmediatamente",
    "hablar con una persona", "hablar con alguien", "frustrado", "frustrada",
    "un humano", "una persona real",
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
        # timeout/max_retries: a hung or slow classifier call would otherwise add
        # unbounded latency to every keyword-miss turn before any user-visible
        # token streams. Fail fast and fall back (both _llm_route and
        # _llm_route_sticky catch the resulting exception).
        _router_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, timeout=3, max_retries=0)
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


# Sticky-booker router: used only while state["next"] == "booker" (mid-booking
# flow). Unlike _ROUTER_SYSTEM, this is deliberately BIASED toward "booker" —
# the sticky mechanism exists because keyword-free mid-flow replies like "10am"
# or "june 11 at 10am" (see test_mid_booking_replies_stick_to_booker — "the
# message that broke booking in prod") have no context clues on their own, and
# a generic classifier could easily misread them as unrelated. Only a message
# that's CLEARLY unrelated to the in-progress booking should escape.
_STICKY_ROUTER_SYSTEM = (
    "You are the router for an AI receptionist. The user is CURRENTLY in the middle "
    "of booking, rescheduling, or canceling an appointment. Classify their latest "
    "message into exactly one of:\n"
    "- booker: continues the booking flow in any way — a date, time, name, phone, "
    "email, confirmation ('yes'/'that works'), a LOCATION change (Weston, Keele, "
    "'other shop', 'how about keele'), a service change, or anything that could "
    "plausibly be part of scheduling. Default to this whenever there's any doubt.\n"
    "- informer: a CLEARLY unrelated question — pure pricing/services FAQ with no "
    "scheduling intent — that has nothing to do with the booking in progress.\n"
    "- closer: a hot lead signal — budget, timeline, urgency, frustration, or "
    "asking to speak with a human.\n"
    "Reply with ONLY one word: booker, informer, or closer."
)


def _llm_route_sticky(human_text: str) -> str:
    """Classify a message while the sticky booker flow is active. Fail-safe:
    returns 'booker' (not 'informer') on any error or empty input, preserving
    today's sticky behavior whenever the classifier can't be trusted."""
    text = (human_text or "").strip()
    if not text:
        return "booker"
    try:
        resp = _get_router_llm().invoke(
            [("system", _STICKY_ROUTER_SYSTEM), ("user", text)],
            config={"tags": [_ROUTER_STREAM_TAG]},
        )
        ans = (resp.content or "").strip().lower()
        for node in ("closer", "informer", "booker"):
            if node in ans:
                log.info("Sticky-booker LLM router classified %r → %s", text[:60], node)
                return node
    except Exception:
        log.warning("Sticky-booker LLM router failed; defaulting to booker.", exc_info=True)
    return "booker"


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
      2. explicit booking keyword → booker
      3. no match → None

    NOTE: there is deliberately no "appointment_details is set → booker" rule.
    appointment_details is never cleared once a booking succeeds (see
    _make_booker_node), so that rule used to permanently hijack every later
    message in the thread to the booker, even "what services do you offer?"
    turns after the booking was done. A completed booking should fall through
    to the LLM router like any other message.

    Sticky mid-booking continuation (state["next"] == "booker") is deliberately
    NOT handled here — see _route, which runs the LLM-classified _llm_route_sticky
    for that case instead of forcing "booker" unconditionally (finding 5.2: a
    stale "how much does the intro call cost?" used to stay trapped in the
    booker, which has no pricing tool).
    """
    human_text = _last_human_text(state)

    if any(kw in human_text for kw in _URGENCY_KW):
        return "closer"
    if any(kw in human_text for kw in _BOOKING_KW):
        return "booker"
    return None


def _route(state: AgentState) -> str:
    """Conditional-edge router. Deterministic rules first; then sticky
    continuation (LLM-classified, biased toward booker); then the general LLM
    classifier (if enabled) instead of a blind informer default."""
    decided = _route_rules(state)
    if decided is not None:
        return decided
    if state.get("next") == "booker":
        human = _last_human_text(state)
        # Location/day switches mid-booking need calendar tools — never informer.
        if any(kw in human for kw in _BOOKING_CONTINUE_KW):
            return "booker"
        if _LLM_ROUTER_ENABLED:
            return _llm_route_sticky(human)
        return "booker"  # no classifier available — preserve the old sticky behavior
    if _LLM_ROUTER_ENABLED:
        return _llm_route(_last_human_text(state))
    return "informer"


# ── Context compression ───────────────────────────────────────────────────────

_SUMMARIZE_AFTER_TURNS = 15  # number of human turns before compressing history


def _compress_node(state: AgentState) -> dict:
    """Summarise old messages once the conversation exceeds _SUMMARIZE_AFTER_TURNS.

    Runs before routing on every turn — a no-op until the threshold is hit.
    Removes messages older than the last 6 (3 full turns) and folds them into
    `conversation_summary` (a dedicated state field, NOT a message-list entry —
    see finding 1.3 below) so the specialists always see compact context
    without losing key lead/booking details.
    """
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

    history = "\n".join(
        f"{getattr(m, 'type', 'msg')}: {(m.content or '')[:300]}"
        for m in to_compress
    )
    prompt = (
        "Summarise this AI receptionist conversation for handoff context. "
        "Preserve: the user's name and email, any booked appointments "
        "(date/time/event id), pricing discussed, budget/urgency signals, "
        "open questions. Be concise.\n\n"
    )
    prior_summary = state.get("conversation_summary")
    if prior_summary:
        # Compression can run more than once in a long conversation — fold the
        # existing summary in rather than letting a second pass silently drop it.
        prompt += f"Summary of even earlier turns (preserve this, don't drop it):\n{prior_summary}\n\n"
    prompt += "New turns to summarise:\n" + history

    # gpt-4o-mini is plenty for plain summarization — this isn't a task that
    # needs the flagship model, unlike the specialist agents themselves (12.3).
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    resp = llm.invoke([{"role": "user", "content": prompt}])

    log.info("Compressed %d messages into a summary.", len(to_compress))

    # Ordering fix (finding 1.3): the add_messages reducer always appends any
    # brand-new message id to the END of the list — there's no way to make a
    # freshly-added message land BEFORE the messages it summarizes, so storing
    # the summary as a message meant the model saw [recent turns] -> [earlier
    # summary], reverse chronological, with a trailing SystemMessage that could
    # even read as an instruction override. Storing it in `conversation_summary`
    # instead (injected into the system prompt by agents._make_middleware,
    # which always precedes the message history) fixes the ordering outright.
    removals = [RemoveMessage(id=m.id) for m in to_compress]
    return {"messages": removals, "conversation_summary": resp.content}


# ── State-writing specialist wrappers ─────────────────────────────────────────

def _with_lead_score(config: RunnableConfig, lead_score: int) -> dict:
    """Merge the running lead_score into configurable so escalate_to_human
    (tools.py) can read it via its `config` param and include it in the
    escalation email — lead_score lives in AgentState, not the original
    request config, so it has to be threaded through explicitly like this."""
    base = dict(config or {})
    base["configurable"] = {**(base.get("configurable") or {}), "lead_score": lead_score}
    return base


def _maybe_record_lead(config: RunnableConfig, result: dict, summary: Optional[str] = None) -> None:
    """Finding 7.1: lead_score/qualified were computed but never went anywhere.
    Best-effort snapshot into the `leads` table (leads.py) once a thread
    becomes a qualified lead (booked, or escalated) — a no-op otherwise, and
    silently swallows any DB failure (leads.record_lead never raises), since
    this must never break the actual conversation.
    """
    if not result.get("qualified"):
        return
    from tenants import normalize_tenant_id

    from leads import record_lead

    configurable = (config or {}).get("configurable") or {}
    thread_id = str(configurable.get("thread_id") or "unknown")
    tenant_id = normalize_tenant_id(configurable.get("tenant_id"))
    appt = result.get("appointment_details") or {}
    record_lead(
        thread_id=thread_id,
        tenant_id=tenant_id,
        lead_score=result.get("lead_score"),
        qualified=True,
        contact=appt.get("attendee_email"),
        summary=summary,
    )


def _make_informer_node(informer):
    """Wrap the informer agent to also update lead_score.

    Clears `next` so a prior booker stickiness is released — informer is the
    safe default, so follow-up questions naturally fall back here.
    """
    def node(state: AgentState, config: RunnableConfig) -> dict:
        # Forward config (with the running lead_score merged in, so escalate_to_human
        # can put it in the email — see _with_lead_score) so the sub-agent's tools
        # (RunnableConfig injection) and prompt (runtime.context) both see the
        # per-request tenant_id.
        lead_score_in = state.get("lead_score") or 0
        result = informer.invoke(state, config=_with_lead_score(config, lead_score_in))
        msgs = result.get("messages", [])
        last = msgs[-1].content if msgs else ""
        # Pricing discussed = warmer lead (20 pts); any informer reply = mild intent (5 pts).
        # get_pricing formats amounts with thousands separators (e.g. "$8,500"), so the
        # signal strings must match that format or this bump can never fire.
        from tenants import load_tenant, normalize_tenant_id
        tenant_id = normalize_tenant_id(((config or {}).get("configurable") or {}).get("tenant_id"))
        _tenant_cfg = load_tenant(tenant_id)
        pricing_signals = [f"{v:,}" for p in _tenant_cfg.pricing for v in (p.get("setup_from"), p.get("monthly_from")) if v]
        bump = 20 if any(p in last for p in pricing_signals) else 5
        return {
            "messages": msgs,
            "lead_score": min(100, lead_score_in + bump),
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

        result = {
            "messages": msgs,
            "appointment_details": appt,
            "lead_score": min(100, score),
            "qualified": qualified,
            "next": next_node,
        }
        _maybe_record_lead(config, result, summary=appt.get("summary") if appt else None)
        return result
    return node


def _make_closer_node(closer):
    """Wrap the closer agent to mark lead qualified on escalation.

    Clears `next` — after a hand-off, follow-ups fall back to the informer.
    """
    def node(state: AgentState, config: RunnableConfig) -> dict:
        score = state.get("lead_score") or 0
        qualified = state.get("qualified") or False
        agent_result = closer.invoke(state, config=_with_lead_score(config, score))
        msgs = agent_result.get("messages", [])

        summary_text = None
        for m in msgs:
            tool_calls = getattr(m, "tool_calls", None) or []
            for tc in tool_calls:
                if tc.get("name") == "escalate_to_human":
                    score = max(score, 80)
                    qualified = True
                    summary_text = tc.get("args", {}).get("user_summary") or summary_text

        result = {
            "messages": msgs,
            "lead_score": min(100, score),
            "qualified": qualified,
            "next": None,
        }
        _maybe_record_lead(config, result, summary=summary_text)
        return result
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
