"""Behavioral evals for the Esmi system prompt.

These assert the customer-facing invariants that matter most:
  1. Pricing answered via get_pricing, never the KB.
  2. No booking before the Step-4 read-back confirmation.
  3. Booking DOES happen once the user confirms.
  4. Escalation fires on budget/timeline/urgency signals.
  5. Spanish is answered in Spanish, Latin-American register (no Castilian "vosotros").

They call gpt-4o for real (temp 0), so they need OPENAI_API_KEY (loaded from .env)
and network. Run on demand:  pytest evals/ -v
"""

import os

import pytest

from .harness import run_conversation, tool_names

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — evals call the real model.",
)


def test_pricing_uses_get_pricing_not_kb():
    calls, text = run_conversation(["How much does Esmi cost?"], thread_id="eval-pricing")
    names = tool_names(calls)
    assert "get_pricing" in names, f"expected get_pricing, got {names}"
    assert "search_knowledge_base" not in names, "pricing must not come from the KB"
    assert "8,500" in text or "8500" in text, f"canonical Esmi price missing from reply: {text!r}"


def test_no_booking_before_confirmation():
    # User tries to rush a booking in one shot; the agent must read back and wait
    # for an explicit yes (Step 4) before ever calling book_appointment.
    calls, _ = run_conversation(
        ["Book me Tuesday at 9am. Name John Doe, email john@example.com."],
        thread_id="eval-rush",
    )
    assert "book_appointment" not in tool_names(calls), (
        "must not book before the Step-4 read-back confirmation"
    )


def test_booking_after_explicit_confirmation():
    calls, _ = run_conversation(
        [
            "I'd like to book an intro call for June 10th.",
            "9 am works.",
            "My name is John Doe and my email is john@example.com.",
            "Yes, that's all correct — please book it.",
        ],
        thread_id="eval-book",
    )
    assert "book_appointment" in tool_names(calls), (
        "should book once the user explicitly confirms the read-back"
    )


def test_escalation_on_budget_and_urgency():
    calls, _ = run_conversation(
        ["We have budget approved and need this live ASAP, this quarter."],
        thread_id="eval-escalate",
    )
    assert "escalate_to_human" in tool_names(calls), (
        "should escalate on budget + urgency signals"
    )


def test_spanish_is_latam_register():
    _, text = run_conversation(
        ["¿Cuánto cuesta el servicio de recepcionista?"],
        thread_id="eval-es",
    )
    low = text.lower()
    assert any(w in low for w in ["precio", "costo", "setup", "mensual", "agendar", "$"]), (
        f"expected a Spanish pricing reply, got: {text!r}"
    )
    assert "vosotros" not in low, "must use Latin-American register, not Castilian 'vosotros'"
