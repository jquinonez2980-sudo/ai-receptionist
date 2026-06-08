"""Phase 4 multi-agent integration tests.

These run the FULL Phase 4 graph topology — real _route() + focused specialist
prompts + stub tools. They validate that routing + specialist behavior work
together end-to-end, not in isolation.

Structural enforcement note:
  informer can ONLY call search_knowledge_base / get_pricing
  booker   can ONLY call calendar tools
  closer   can ONLY call escalate_to_human
This is guaranteed by the tool lists in build_multi_agent_test_graph() —
a specialist literally cannot call a tool it was not given. Tests assert
the positive (right tool called) not the negative (wrong tool not called),
because the negative is already structurally impossible.

KB-miss limitation:
  In the Phase 4 graph, routing happens on USER INPUT — before tools run.
  If informer's KB search returns nothing, informer cannot re-route to closer
  mid-turn (no tool available). This is a known Phase 4 trade-off; the
  KB-miss escalation invariant is Phase 1 only (test_evals.py covers it).

These tests call gpt-4o for real — need OPENAI_API_KEY. Rate-limit guard
via conftest.py autouse fixture (5s before each test).
Run: PYTHONUTF8=1 pytest evals/test_multi_agent.py -v
"""

import os

import pytest

from .harness import run_multi_agent_conversation, tool_names

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — multi-agent tests call the real model.",
)


def test_ma_pricing_routes_to_informer_and_calls_get_pricing():
    """Through the full graph: pricing question → informer → get_pricing.

    The informer node has no booking tools — structural proof it can't book.
    """
    calls, text = run_multi_agent_conversation(
        ["How much does Esmi cost?"],
        thread_id="eval-ma-pricing",
    )
    names = tool_names(calls)
    assert "get_pricing" in names, f"pricing must call get_pricing: {names}"
    assert "$8,500" in text or "8,500" in text, (
        f"canonical Esmi price must appear in reply: {text[:300]!r}"
    )


def test_ma_booking_intent_routes_to_booker_and_checks_calendar():
    """Through the full graph: booking intent → booker → list_available_slots.

    The booker node has no info tools — structural proof it can't KB-search.
    """
    calls, _ = run_multi_agent_conversation(
        [
            "I'd like to book an intro call.",
            "Next Tuesday works.",
        ],
        thread_id="eval-ma-booking",
    )
    names = tool_names(calls)
    assert "list_available_slots" in names, (
        f"booking intent + day must check calendar: {names}"
    )


def test_ma_keyword_free_date_reply_reaches_calendar():
    """Regression: the exact prod flow that broke booking.

    Turn 1 "Book an appointment" routes to booker (keyword 'book').
    Turn 2 "june 11 at 10am" has NO booking keyword — without sticky routing
    it fell through to the informer (no calendar tools) and Esmi replied
    "I can't book appointments directly". With next='booker' stickiness it
    stays in booker and checks the calendar.
    """
    calls, _ = run_multi_agent_conversation(
        [
            "Book an appointment",
            "june 11 at 10am",
        ],
        thread_id="eval-ma-sticky-date",
    )
    names = tool_names(calls)
    assert "list_available_slots" in names, (
        f"keyword-free date reply must stick to booker and check calendar: {names}"
    )


def test_ma_urgency_routes_to_closer_and_escalates():
    """Through the full graph: urgency signal → closer → escalate_to_human."""
    calls, _ = run_multi_agent_conversation(
        ["We have budget approved and need this ASAP, this quarter."],
        thread_id="eval-ma-urgency",
    )
    names = tool_names(calls)
    assert "escalate_to_human" in names, (
        f"budget+urgency must escalate to human via closer: {names}"
    )


def test_ma_pricing_then_booking_crosses_specialists():
    """Multi-turn: pricing Q (informer) then booking intent (booker).

    Validates that routing adapts correctly turn-by-turn and state (messages)
    carries context between specialists so the booker knows who they are.
    """
    calls, _ = run_multi_agent_conversation(
        [
            "How much does Esmi cost?",          # turn 1 → informer
            "Great. I'd like to book a call.",   # turn 2 → booker
            "Next Tuesday.",                     # turn 3 → booker (gives day)
        ],
        thread_id="eval-ma-cross",
    )
    names = tool_names(calls)
    assert "get_pricing" in names, (
        f"first turn must have called get_pricing: {names}"
    )
    assert "list_available_slots" in names, (
        f"third turn must have called list_available_slots: {names}"
    )


def test_ma_no_booking_before_step4_confirmation():
    """Booker specialist must not book until the user explicitly confirms (Step 4).

    This is the most critical customer-protection invariant — same as Phase 1
    test_no_booking_before_confirmation but running through the Phase 4 graph.
    """
    calls, _ = run_multi_agent_conversation(
        ["Book me Tuesday at 9am. Name John Doe, email john@example.com."],
        thread_id="eval-ma-no-early-book",
    )
    assert "book_appointment" not in tool_names(calls), (
        "booker must not call book_appointment before the Step-4 read-back"
    )
