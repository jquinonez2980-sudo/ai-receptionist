"""Stub tools that mirror the real tool names/signatures/descriptions but record
calls instead of touching Google Calendar, SendGrid, or Twilio.

Descriptions are kept close to the real tools (tools.py) so routing fidelity is
preserved — what we're testing is whether the system prompt + model pick the
right tool, never the tool's external side effects.
"""

from typing import Optional

from langchain_core.tools import tool

# Real pricing text so pricing assertions are meaningful (no external calls).
from tools import get_pricing as _real_get_pricing

# Recorder: list of (tool_name, kwargs) in call order, across a conversation.
CALLS: list[tuple[str, dict]] = []


def reset() -> None:
    CALLS.clear()


@tool
def search_knowledge_base(query: str) -> str:
    """For questions about services, FAQs, packages, branding, or company info (NOT prices).
    Quote the knowledge base — do not paraphrase from memory."""
    CALLS.append(("search_knowledge_base", {"query": query}))
    return (
        "Orchelix builds custom AI receptionist and revenue-operations agents, "
        "deployed in ~2-3 weeks. [stub KB result]"
    )


@tool
def get_pricing() -> str:
    """For ANY pricing question (cost, setup fee, monthly fee, 'how much'). Returns
    exact, authoritative numbers. Always use this for prices — never the knowledge base."""
    CALLS.append(("get_pricing", {}))
    return _real_get_pricing.invoke({})


@tool
def list_available_slots(start_date: str, end_date: str) -> str:
    """List available appointment slots for an ISO date range (YYYY-MM-DD). Call ONLY
    after the user gives a preferred day."""
    CALLS.append(("list_available_slots", {"start_date": start_date, "end_date": end_date}))
    return (
        "Available slots:\n"
        "- Wednesday, June 10 9:00 AM – 9:30 AM "
        "(start_iso 2026-06-10T09:00:00-04:00, end_iso 2026-06-10T09:30:00-04:00)\n"
        "- Wednesday, June 10 10:00 AM – 10:30 AM "
        "(start_iso 2026-06-10T10:00:00-04:00, end_iso 2026-06-10T10:30:00-04:00)"
    )


@tool
def book_appointment(
    summary: str,
    start_time: str,
    end_time: str,
    attendee_email: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> str:
    """Book a confirmed appointment in Google Calendar. Call ONLY when you have a
    confirmed slot + name + email AND the user explicitly confirmed the Step-4 read-back."""
    CALLS.append(
        (
            "book_appointment",
            {
                "summary": summary,
                "start_time": start_time,
                "end_time": end_time,
                "attendee_email": attendee_email,
            },
        )
    )
    return "Booked! A confirmation has been sent."


@tool
def find_booking(contact: str) -> str:
    """Find a caller's upcoming bookings by email or phone. Returns event ids."""
    CALLS.append(("find_booking", {"contact": contact}))
    return "Found 1 booking: event_id=evt_test_123 — Intro Call, Wednesday June 10 at 9:00 AM."


@tool
def reschedule_appointment(event_id: str, new_start_time: str, new_end_time: str) -> str:
    """Move an existing booking (event id from find_booking) to a new confirmed time."""
    CALLS.append(
        (
            "reschedule_appointment",
            {"event_id": event_id, "new_start_time": new_start_time, "new_end_time": new_end_time},
        )
    )
    return "Rescheduled."


@tool
def cancel_appointment(event_id: str) -> str:
    """Cancel an existing booking (event id from find_booking) after confirming which one."""
    CALLS.append(("cancel_appointment", {"event_id": event_id}))
    return "Cancelled."


@tool
def escalate_to_human(reason: str, user_summary: str) -> str:
    """Notify the Orchelix team that a lead needs human follow-up — on budget/timeline/
    urgency signals, KB failures, frustration, or a request for a person."""
    CALLS.append(("escalate_to_human", {"reason": reason, "user_summary": user_summary}))
    return "I've flagged this for our team and someone will follow up with you shortly."


ALL_STUBS = [
    search_knowledge_base,
    get_pricing,
    list_available_slots,
    book_appointment,
    find_booking,
    reschedule_appointment,
    cancel_appointment,
    escalate_to_human,
]
