"""Unit tests for the router in graph.py.

Three tiers:
  _route_rules(state) — deterministic, zero-latency, no model. Returns a node
    name on a high-confidence match (urgency, explicit booking keyword), or
    None when no rule fires. Deliberately does NOT decide sticky continuation
    (see finding 5.2) or the old appointment_details rule (removed — finding
    1.1). These tests lock in that tier and run in milliseconds.
  _llm_route_sticky(text) — used only while state["next"] == "booker". Biased
    toward "booker" so context-free mid-flow replies ("10am", "yes") don't get
    misrouted; only escapes for clearly unrelated messages. Fail-safe tests are
    model-free (mocked); the classification-quality tests are model-gated.
  _route(state) — rules first, then sticky (LLM-biased) or the general LLM
    classifier on no match. The LLM-fallback tests are model-gated (need
    OPENAI_API_KEY).
"""

import os
from unittest.mock import patch

import pytest
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage

load_dotenv()  # for the model-gated tests below

from graph import _llm_route_sticky, _route, _route_rules  # noqa: E402


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


@pytest.mark.parametrize("msg", [
    "When are you open?",
    "I'm away next week, tell me about your services.",
    "How do I cancel the monthly service?",
    "Are you open next Monday?",
])
def test_previously_ambiguous_phrases_no_longer_hijack_booker(msg):
    """Regression test for finding 5.1: these used to keyword-match the booker
    rule via "when are you"/"next week"/weekday names/bare "cancel" even
    though they're FAQ or pricing questions, not booking intent. They should
    now fall through to the LLM router instead."""
    assert _route_rules(_state(msg)) is None, (
        f"'{msg}' should no longer match a deterministic booker rule, got {_route_rules(_state(msg))}"
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
def test_sticky_state_has_no_deterministic_rule(msg):
    """Finding 5.2: sticky continuation is no longer decided by _route_rules —
    _route runs _llm_route_sticky for it instead (see the model-gated
    equivalents of this test below, and test_llm_route_sticky_* for the
    classifier's fail-safe/escape behavior)."""
    assert _route_rules(_state(msg, next="booker")) is None


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


@pytest.mark.parametrize("msg", [
    "Necesitamos esto lo antes posible, el presupuesto ya está aprobado.",
    "Estamos listos para empezar este trimestre.",
    "Quiero hablar con una persona real, por favor.",
    "Esto es urgente, necesito una respuesta ahora.",
    "Estoy frustrado, esto no está funcionando.",
])
def test_spanish_urgency_matches_closer_rule(msg):
    """Regression test for finding 7.2: urgency keywords were English-only
    despite the product's bilingual (LATAM Spanish) support claim."""
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


# ── Sticky-router fail-safe (model-free, mocked) ─────────────────────────────

def test_llm_route_sticky_defaults_to_booker_on_error():
    """_llm_route_sticky must fail toward 'booker' (not 'informer'), preserving
    today's sticky safety net whenever the classifier is unavailable."""
    with patch("graph._get_router_llm", side_effect=RuntimeError("boom")):
        assert _llm_route_sticky("whatever the user said") == "booker"


def test_llm_route_sticky_defaults_to_booker_on_empty_input():
    assert _llm_route_sticky("") == "booker"
    assert _llm_route_sticky(None) == "booker"


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


@pytestmark_model
@pytest.mark.parametrize("msg", [
    "june 11 at 10am",        # the message that broke booking in prod
    "10am",
    "yes",
    "that works",
    "john@example.com",
    "tomorrow afternoon",
    "the first one",
])
def test_sticky_llm_router_keeps_ambiguous_replies_in_booker(msg):
    """The real classifier, given the sticky-biased prompt, must still keep
    context-free date/time/confirmation fragments in the booker — this is the
    regression case _llm_route_sticky's bias exists to protect."""
    assert _route(_state(msg, next="booker")) == "booker"


@pytestmark_model
def test_sticky_llm_router_escapes_for_unrelated_question():
    """Finding 5.2: a clearly unrelated question mid-booking must now escape
    to the informer instead of being trapped (the booker has no pricing tool).

    Message must NOT contain deterministic booking keywords (e.g. "intro call",
    "book", "available") or sticky continue keywords ("how about", "location") —
    those correctly stay on booker. This only asserts the LLM sticky escape path.
    """
    assert _route(_state("wait, how much is the setup fee?", next="booker")) == "informer"
