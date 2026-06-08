"""Unit tests for the Phase 4 rule-based router (_route in graph.py).

These require no model calls and run in milliseconds. They lock in the
routing logic before any multi-agent inference tests are added in Phase C.

The router must be deterministic and correct for the common cases — the
behavioral evals (test_evals.py) validate what each specialist does after
being routed to.
"""

import pytest
from langchain_core.messages import HumanMessage, AIMessage

# Import the routing function directly — no graph build needed.
from graph import _route


def _state(human_msg: str, appointment_details=None, messages_extra=None):
    """Build a minimal AgentState with a single human message."""
    msgs = [HumanMessage(content=human_msg)]
    if messages_extra:
        msgs = messages_extra + msgs
    return {
        "messages": msgs,
        "lead_score": None,
        "qualified": None,
        "appointment_details": appointment_details,
        "next": None,
    }


# ── Informer routing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "How much does Esmi cost?",
    "What services do you offer?",
    "Tell me about the Revenue Operations Agents.",
    "What's included in the setup fee?",
    "Do you work with dental offices?",
    "How long does deployment take?",
    "Hi, what does Orchelix do?",
])
def test_info_questions_route_to_informer(msg):
    assert _route(_state(msg)) == "informer", (
        f"'{msg}' should route to informer, not {_route(_state(msg))}"
    )


# ── Booker routing ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "I'd like to book an intro call.",
    "Can I schedule a demo?",
    "Do you have availability next week?",
    "I need to reschedule my appointment.",
    "Cancel my booking please.",
    "What slots do you have on Tuesday?",
    "I want to book a call for next Thursday.",
    "Find my booking.",
])
def test_booking_intent_routes_to_booker(msg):
    assert _route(_state(msg)) == "booker", (
        f"'{msg}' should route to booker, not {_route(_state(msg))}"
    )


def test_appointment_in_progress_stays_in_booker():
    """If appointment_details is set, mid-booking messages stay in booker."""
    state = _state("9am works for me.", appointment_details={"summary": "Intro Call"})
    assert _route(state) == "booker"


def _sticky_state(human_msg: str, next_node: str):
    """State where a prior specialist set state['next']."""
    return {
        "messages": [HumanMessage(content=human_msg)],
        "lead_score": None, "qualified": None,
        "appointment_details": None, "next": next_node,
    }


@pytest.mark.parametrize("msg", [
    "june 11 at 10am",        # the exact message that broke booking in prod
    "10am",
    "yes",
    "that works",
    "john@example.com",
    "tomorrow afternoon",
    "the first one",
])
def test_mid_booking_replies_stick_to_booker(msg):
    """Keyword-free mid-flow replies must stay in booker when next='booker'.

    Regression guard: 'june 11 at 10am' has no booking keyword, so without
    sticky routing it fell through to the informer (no calendar tools) and
    Esmi replied 'I can't book appointments directly'.
    """
    assert _route(_sticky_state(msg, "booker")) == "booker", (
        f"'{msg}' mid-booking should stick to booker, not {_route(_sticky_state(msg, 'booker'))}"
    )


def test_urgency_still_escapes_sticky_booker():
    """Urgency must override booker stickiness — a hot signal always reaches closer."""
    assert _route(_sticky_state("actually we need this ASAP", "booker")) == "closer"


def test_next_none_does_not_force_booker():
    """A cleared next (None) routes by keywords normally (default informer)."""
    assert _route(_sticky_state("what do you offer?", None)) == "informer"


# ── Closer routing ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "We have budget approved and need this ASAP.",
    "We're ready to start this quarter.",
    "I need this urgently, our Q4 deadline is tight.",
    "I'd like to speak with someone.",
    "Can I talk to a person?",
    "I'm frustrated, this isn't working.",
    "Budget is approved, let's move immediately.",
])
def test_urgency_routes_to_closer(msg):
    assert _route(_state(msg)) == "closer", (
        f"'{msg}' should route to closer, not {_route(_state(msg))}"
    )


def test_urgency_overrides_booking_intent():
    """Urgency beats booking — hot lead goes to closer even if they mention a slot."""
    msg = "We need this ASAP, can we book something this week?"
    assert _route(_state(msg)) == "closer"


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_messages_defaults_to_informer():
    assert _route({"messages": [], "lead_score": None, "qualified": None,
                   "appointment_details": None, "next": None}) == "informer"


def test_no_messages_key_defaults_to_informer():
    assert _route({}) == "informer"


def test_last_human_message_is_used_not_ai():
    """Router reads the most recent HUMAN message, ignoring AI replies."""
    msgs = [
        HumanMessage(content="I'd like to book a call."),  # booking intent
        AIMessage(content="What day works best?"),         # AI reply — must be ignored
    ]
    # The last AI message has no booking/urgency keywords — but router should
    # look at the last HUMAN message, which does have booking intent.
    state = {
        "messages": msgs,
        "lead_score": None, "qualified": None,
        "appointment_details": None, "next": None,
    }
    assert _route(state) == "booker"
