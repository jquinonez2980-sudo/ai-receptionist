"""Unit tests for the router in graph.py.

Two tiers:
  _route_rules(state) — deterministic, zero-latency, no model. Returns a node
    name on a high-confidence match, or None when no rule fires. These tests
    lock in that tier and run in milliseconds.
  _route(state) — rules first, then a cheap LLM classifier on no match (Phase 2).
    The LLM-fallback tests are model-gated (need OPENAI_API_KEY).
"""

import os

import pytest
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage

load_dotenv()  # for the model-gated tests below

from graph import _route, _route_rules  # noqa: E402


def _state(human_msg: str, appointment_details=None, messages_extra=None, next=None):
    """Build a minimal AgentState with a single human message."""
    msgs = [HumanMessage(content=human_msg)]
    if messages_extra:
        msgs = messages_extra + msgs
    return {
        "messages": msgs,
        "lead_score": None,
        "qualified": None,
        "appointment_details": appointment_details,
        "next": next,
    }


# ── Deterministic tier: info questions have NO rule (→ None, fall to LLM/default) ─

@pytest.mark.parametrize("msg", [
    "How much does Esmi cost?",
    "What services do you offer?",
    "Tell me about the Revenue Operations Agents.",
    "What's included in the setup fee?",
    "Do you work with dental offices?",
    "How long does deployment take?",
    "Hi, what does Orchelix do?",
])
def test_info_questions_have_no_deterministic_rule(msg):
    assert _route_rules(_state(msg)) is None, (
        f"'{msg}' should not match a deterministic rule (handled by LLM/default)"
    )


# ── Deterministic tier: booking ──────────────────────────────────────────────

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
def test_booking_intent_matches_booker_rule(msg):
    assert _route_rules(_state(msg)) == "booker", (
        f"'{msg}' should match the booker rule, not {_route_rules(_state(msg))}"
    )


def test_completed_booking_does_not_permanently_hijack_routing():
    """Regression test for the permanent-hijack bug: appointment_details is
    never cleared once a booking succeeds, so a stale "appointment_details is
    set" rule used to force every later message (even unrelated questions) to
    the booker forever. Mid-booking continuation is the sticky `next` field's
    job (see test_mid_booking_replies_stick_to_booker), not appointment_details."""
    state = _state("What services do you offer?", appointment_details={"summary": "Intro Call"}, next=None)
    assert _route_rules(state) is None


@pytest.mark.parametrize("msg", [
    "june 11 at 10am",        # the message that broke booking in prod
    "10am",
    "yes",
    "that works",
    "john@example.com",
    "tomorrow afternoon",
    "the first one",
])
def test_mid_booking_replies_stick_to_booker(msg):
    """Keyword-free mid-flow replies must stay in booker when next='booker'."""
    assert _route_rules(_state(msg, next="booker")) == "booker"


# ── Deterministic tier: urgency ──────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "We have budget approved and need this ASAP.",
    "We're ready to start this quarter.",
    "I need this urgently, our Q4 deadline is tight.",
    "I'd like to speak with someone.",
    "Can I talk to a person?",
    "I'm frustrated, this isn't working.",
    "Budget is approved, let's move immediately.",
])
def test_urgency_matches_closer_rule(msg):
    assert _route_rules(_state(msg)) == "closer", (
        f"'{msg}' should match the closer rule, not {_route_rules(_state(msg))}"
    )


def test_urgency_overrides_booking_intent():
    msg = "We need this ASAP, can we book something this week?"
    assert _route_rules(_state(msg)) == "closer"


def test_urgency_escapes_sticky_booker():
    assert _route_rules(_state("actually we need this ASAP", next="booker")) == "closer"


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_empty_messages_has_no_rule():
    assert _route_rules({"messages": [], "lead_score": None, "qualified": None,
                         "appointment_details": None, "next": None}) is None


def test_no_messages_key_has_no_rule():
    assert _route_rules({}) is None


def test_last_human_message_is_used_not_ai():
    msgs = [
        HumanMessage(content="I'd like to book a call."),
        AIMessage(content="What day works best?"),
    ]
    state = {"messages": msgs, "lead_score": None, "qualified": None,
             "appointment_details": None, "next": None}
    assert _route_rules(state) == "booker"


# ── Phase 2: LLM fallback (model-gated) ──────────────────────────────────────

pytestmark_model = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — LLM router tests call the real model.",
)


@pytestmark_model
def test_llm_router_catches_keyword_free_booking():
    """No booking keyword, no sticky state — the LLM must still route to booker."""
    assert _route(_state("can you get me on your calendar sometime?")) == "booker"


@pytestmark_model
def test_llm_router_catches_keyword_free_escalation():
    """No urgency keyword — the LLM must still route frustration to closer."""
    assert _route(_state("I have been trying to reach a real person for days")) == "closer"


@pytestmark_model
def test_llm_router_defaults_plain_question_to_informer():
    assert _route(_state("what do you actually do?")) == "informer"
