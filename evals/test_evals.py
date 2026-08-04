"""Behavioral evals for the Esmi system prompt.

These assert the customer-facing invariants that matter most:
  1. What Esmi ITSELF costs is stated clearly (Starter/Growth/Scale + the
     pricing URL), via the tenant_pricing_pitch prompt override — not a
     get_pricing tool call, which is reserved for a CLIENT tenant's OWN
     service prices (see agents/tools if you're extending this to a client
     tenant; the harness doesn't currently support selecting a non-default
     tenant, and every non-default tenant has no pricing pitch set anyway).
  2. No booking before the Step-4 read-back confirmation.
  3. Booking DOES happen once the user confirms.
  4. Escalation fires on budget/timeline/urgency signals.
  5. Spanish is answered in Spanish, Latin-American register (no Castilian "vosotros").

They call gpt-4o for real (temp 0), so they need OPENAI_API_KEY (loaded from .env)
and network. Run on demand:  pytest evals/ -v
"""

import os
from datetime import date, timedelta

import pytest

from .harness import run_conversation, tool_names


def _future_weekday_phrase(days_ahead: int = 3) -> str:
    """A near-future weekday like 'Wednesday, June 17' so the booking eval never
    goes stale as the calendar advances (a hardcoded date becomes the past)."""
    d = date.today() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%A, %B %d").replace(" 0", " ")

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — evals call the real model.",
)


def test_esmi_own_pricing_is_stated_clearly():
    """2026-08 change: asking "how much does Esmi cost" now gets the real
    Starter/Growth/Scale numbers up front (agents.py's esmi_pricing_pitch
    prompt injection — tenants.py's TenantConfig.esmi_pricing_pitch, set only
    for 'default', which is what this harness always exercises), not the old
    "deflect + capture contact info" canned line, and not a get_pricing tool
    call (that tool is reserved for a CLIENT tenant's own service prices).
    The old $8,500 Enterprise-tier figure is stale — the live orchelix.com/
    pricing page moved to Starter $299 / Growth $599 / Scale $999 — so it
    must never be quoted either."""
    calls, text = run_conversation(["How much does Esmi cost?"], thread_id="eval-pricing")
    names = tool_names(calls)
    assert "get_pricing" not in names, (
        f"Esmi's own pricing pitch is prompt-injected text, not a tool call: {names}"
    )
    assert "8,500" not in text and "8500" not in text, (
        f"stale canonical price must never be quoted: {text!r}"
    )
    low = text.lower()
    assert any(w in low for w in ("starter", "growth", "scale", "299", "599", "999")), (
        f"expected the real plan names/numbers, got: {text!r}"
    )


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
            f"I'd like to book an intro call for {_future_weekday_phrase()}.",
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


def test_reschedule_flow_finds_then_reschedules():
    calls, _ = run_conversation(
        [
            "I need to move my existing appointment to a different time.",
            "It's booked under john@example.com.",
            f"Please move it to {_future_weekday_phrase(5)}.",
            "10 am works for me.",
            "Yes, that's correct — please move it.",
            "The code is 123456.",
        ],
        thread_id="eval-resched",
    )
    names = tool_names(calls)
    assert "find_booking" in names, f"should look up the booking first: {names}"
    assert "request_cancellation_code" in names, (
        f"must send a confirmation code before rescheduling (finding 10.1): {names}"
    )
    assert "reschedule_appointment" in names, f"should reschedule after confirmation: {names}"
    assert (
        names.index("find_booking")
        < names.index("request_cancellation_code")
        < names.index("reschedule_appointment")
    ), f"must find the booking, send a code, then reschedule, in that order: {names}"


def test_cancel_flow_confirms_before_cancelling():
    calls, _ = run_conversation(
        [
            "I want to cancel my appointment.",
            "It's under john@example.com.",
            "Yes, cancel it.",
            "The code is 123456.",
        ],
        thread_id="eval-cancel",
    )
    names = tool_names(calls)
    assert "find_booking" in names, f"should look up the booking first: {names}"
    assert "request_cancellation_code" in names, (
        f"must send a confirmation code before cancelling (finding 10.1): {names}"
    )
    assert "cancel_appointment" in names, f"should cancel after confirmation: {names}"
    assert (
        names.index("find_booking")
        < names.index("request_cancellation_code")
        < names.index("cancel_appointment")
    ), f"must find the booking, send a code, then cancel, in that order: {names}"


def test_kb_failure_escalates_not_fabricates():
    # KB returns nothing — the agent must escalate, not invent an answer.
    calls, _ = run_conversation(
        ["Do you integrate with my custom in-house ERP system from 1998?"],
        thread_id="eval-kbfail",
        kb_empty=True,
    )
    names = tool_names(calls)
    assert "search_knowledge_base" in names, f"should try the KB first: {names}"
    assert "escalate_to_human" in names, (
        f"should escalate to a human when the KB can't answer (no fabrication): {names}"
    )


# ── Phase 4 routing invariants ────────────────────────────────────────────────
# These establish behavioral pre-conditions that the multi-agent graph must
# preserve. They pass against the current single-agent (Phase 1) and will
# remain the regression net once the supervisor + specialists are introduced.

def test_pricing_intent_stays_in_informer_domain():
    """A question about what ESMI ITSELF costs must never trigger booking
    tools (or get_pricing, or escalate_to_human before contact info is even
    given — see test_esmi_own_pricing_is_stated_clearly) — it stays a plain
    conversational answer (the prompt-injected pricing pitch) within the
    informer's domain.

    Invariant maps to: Supervisor → Informer (not Booker or Closer).
    """
    calls, _ = run_conversation(
        ["What does Esmi cost?"],
        thread_id="eval-route-info",
    )
    names = tool_names(calls)
    for tool_name in ("get_pricing", "book_appointment", "list_available_slots",
                      "find_booking", "escalate_to_human"):
        assert tool_name not in names, (
            f"a first-turn 'what does Esmi cost' question must not trigger {tool_name} "
            f"(no contact info has been given yet): {names}"
        )


def test_booking_intent_calls_calendar_not_escalation():
    """Booking flow: once the user provides a day, list_available_slots is called.

    Phase 1 (single-agent): the booking flow follows Step 1 — agent asks for a
    preferred day before calling the calendar. So we give a specific day to
    trigger the tool call, then assert the calendar is checked (not escalated).

    Phase 4 note: this maps to Supervisor → Booker. The Booker node inherits
    the same 5-step flow, so the assertion stays identical after the migration.
    """
    calls, _ = run_conversation(
        [
            "I'd like to book an intro call.",
            "Next Tuesday works for me.",  # give a day so Step 2 fires
        ],
        thread_id="eval-route-book",
    )
    names = tool_names(calls)
    assert "list_available_slots" in names, (
        f"booking flow must check calendar once a day is given: {names}"
    )
    assert "escalate_to_human" not in names, (
        f"a simple booking request must not escalate to a human: {names}"
    )


def test_lead_capture_offers_intro_call_after_services_question():
    """The LEAD CAPTURE rule ("after answering any question about pricing,
    services, or how {company} works, offer a quick intro call") applies to
    general services questions. It does NOT apply to "what does Esmi cost" —
    that question gets its own distinct hot-lead-capture CTA ("can I get your
    name and contact info") instead, per the PRICING — ESMI ITSELF rule, so
    this test uses a plain services question to avoid conflating the two."""
    _, text = run_conversation(["What services does Orchelix offer?"], thread_id="eval-leadcap")
    low = text.lower()
    assert any(w in low for w in ["intro call", "book", "calendar", "schedule", "quick call"]), (
        f"a services answer should offer to book an intro call: {text!r}"
    )
