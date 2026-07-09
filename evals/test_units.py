"""Pure-logic unit tests — no network, no model, no OPENAI_API_KEY needed.

Run with:  pytest evals/test_units.py -v
"""
from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock, patch

# Ensure tools can import without real API keys
os.environ.setdefault("OPENAI_API_KEY", "sk-test-unit")


# ── _event_matches_contact ────────────────────────────────────────────────────

def test_event_matches_phone_with_dashes():
    from tools import _event_matches_contact
    event = {"description": "Caller contact: 416-555-1234"}
    assert _event_matches_contact(event, "416-555-1234")


def test_event_matches_partial_phone_substring():
    """Digit substring of the stored number also matches."""
    from tools import _event_matches_contact
    event = {"description": "Caller contact: +1 416 555 1234"}
    assert _event_matches_contact(event, "416-555")


def test_event_does_not_match_different_phone():
    from tools import _event_matches_contact
    event = {"description": "Caller contact: 416-555-1234"}
    assert not _event_matches_contact(event, "905-888-0000")


def test_event_matches_email_attendee():
    from tools import _event_matches_contact
    event = {"attendees": [{"email": "user@example.com"}]}
    assert _event_matches_contact(event, "user@example.com")


def test_event_does_not_match_wrong_email():
    from tools import _event_matches_contact
    event = {"attendees": [{"email": "user@example.com"}]}
    assert not _event_matches_contact(event, "other@example.com")


def test_event_no_digits_contact_does_not_match():
    """A plain name (no digits) never matches a voice event."""
    from tools import _event_matches_contact
    event = {"description": "Caller contact: 416-555-1234"}
    assert not _event_matches_contact(event, "John Doe")


# ── _idem_event_id ────────────────────────────────────────────────────────────

def test_idem_event_id_same_inputs_same_key():
    from tools import _idem_event_id
    key = "Intro Call|2026-06-20T10:00:00-04:00|2026-06-20T10:30:00-04:00|user@example.com"
    assert _idem_event_id(key) == _idem_event_id(key)


def test_idem_event_id_different_slot_different_key():
    from tools import _idem_event_id
    key_a = "Intro Call|2026-06-20T10:00:00-04:00|2026-06-20T10:30:00-04:00|user@example.com"
    key_b = "Intro Call|2026-06-21T10:00:00-04:00|2026-06-21T10:30:00-04:00|user@example.com"
    assert _idem_event_id(key_a) != _idem_event_id(key_b)


def test_idem_event_id_is_hex_string():
    from tools import _idem_event_id
    result = _idem_event_id("some-idempotency-key")
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_idem_event_id_different_email_different_key():
    from tools import _idem_event_id
    key_a = "Intro Call|2026-06-20T10:00:00-04:00|2026-06-20T10:30:00-04:00|alice@example.com"
    key_b = "Intro Call|2026-06-20T10:00:00-04:00|2026-06-20T10:30:00-04:00|bob@example.com"
    assert _idem_event_id(key_a) != _idem_event_id(key_b)


# ── _resolve_event_id — short ids (finding 4.4) ───────────────────────────────

def test_resolve_event_id_passes_through_full_id_without_lookup():
    """A full (or long enough) id is returned as-is -- no calendar API call."""
    from tools import _resolve_event_id
    full_id = "0d0cc7672878c17d543dfd8f799eb54a349105323e44f59a0541802b3e4b1cc9"
    service = MagicMock()
    assert _resolve_event_id(service, "primary", full_id) == full_id
    service.events.assert_not_called()


def test_resolve_event_id_resolves_short_prefix_to_full_id():
    from tools import _resolve_event_id
    full_id = "0d0cc7672878c17d543dfd8f799eb54a349105323e44f59a0541802b3e4b1cc9"
    other_id = "ffffffff2878c17d543dfd8f799eb54a349105323e44f59a0541802b3e4b1cc9"
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [{"id": full_id}, {"id": other_id}]
    }
    assert _resolve_event_id(service, "primary", full_id[:8]) == full_id


def test_resolve_event_id_returns_none_for_no_match():
    from tools import _resolve_event_id
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {"items": []}
    assert _resolve_event_id(service, "primary", "deadbeef") is None


def test_resolve_event_id_returns_none_for_empty_input():
    from tools import _resolve_event_id
    service = MagicMock()
    assert _resolve_event_id(service, "primary", "") is None
    assert _resolve_event_id(service, "primary", None) is None


def test_find_booking_exposes_short_id_not_full_hex():
    """Regression test for finding 4.4: a caller (especially over voice)
    shouldn't have to transcribe a 64-char hex id."""
    from tools import find_booking, _idem_event_id

    full_id = _idem_event_id("Intro Call|2026-07-14T10:00:00-04:00|2026-07-14T10:30:00-04:00|jane@x.com")
    event = {
        "id": full_id,
        "start": {"dateTime": "2026-07-14T10:00:00-04:00"},
        "summary": "Intro Call",
        "attendees": [{"email": "jane@x.com"}],
    }
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {"items": [event]}

    with patch("tools._get_calendar_service", return_value=service), \
         patch("tools.load_tenant") as mock_load_tenant:
        # Minimal tenant stand-in: single calendar, no multi-location map.
        tenant = MagicMock()
        tenant.calendar_id = "primary"
        tenant.business_tz = "America/New_York"
        tenant.locations = {}
        tenant.all_calendar_ids.return_value = [("default", "primary")]
        mock_load_tenant.return_value = tenant
        result = find_booking.invoke({"contact": "jane@x.com"})

    assert full_id not in result, "find_booking must not expose the full 64-char event id"
    assert full_id[:8] in result


# ── _parse_time_slots / _clean_response / _strip_slots_from_text ─────────────

_SAMPLE_RESPONSE = (
    "I have the following slots available for **Monday, June 23**:\n"
    "- 9:00 AM – 9:30 AM\n"
    "- 10:00 AM – 10:30 AM\n"
    "- 2:00 PM – 2:30 PM\n"
    "Which of these works best for you?"
)


def test_parse_time_slots_extracts_slots():
    from _text_utils import _parse_time_slots
    _, slots = _parse_time_slots(_SAMPLE_RESPONSE)
    assert len(slots) == 3
    assert "9:00 AM – 9:30 AM" in slots
    assert "10:00 AM – 10:30 AM" in slots
    assert "2:00 PM – 2:30 PM" in slots


def test_parse_time_slots_extracts_date_label():
    from _text_utils import _parse_time_slots
    date_label, _ = _parse_time_slots(_SAMPLE_RESPONSE)
    assert date_label is not None
    assert "June 23" in date_label


def test_parse_time_slots_no_slots_returns_empty():
    from _text_utils import _parse_time_slots
    date_label, slots = _parse_time_slots("Hello, how can I help you today?")
    assert slots == []
    assert date_label is None


def test_clean_response_strips_markdown():
    from _text_utils import _clean_response
    text = "**Bold** and _italic_ and `code` and ### Header"
    cleaned = _clean_response(text)
    assert "**" not in cleaned
    assert "`" not in cleaned
    assert "###" not in cleaned
    assert "Bold" in cleaned
    assert "italic" in cleaned
    assert "code" in cleaned


def test_strip_slots_from_text_removes_bullet_slot_lines():
    from _text_utils import _strip_slots_from_text
    text = (
        "Available times:\n"
        "- 9:00 AM – 9:30 AM\n"
        "- 10:00 AM – 10:30 AM\n"
        "Which of these works best for you?"
    )
    stripped = _strip_slots_from_text(text)
    assert "9:00 AM" not in stripped
    assert "Available times:" in stripped


def test_round_trip_parse_then_strip():
    """_strip_slots_from_text removes exactly the lines _parse_time_slots found."""
    from _text_utils import _parse_time_slots, _strip_slots_from_text
    _, slots = _parse_time_slots(_SAMPLE_RESPONSE)
    stripped = _strip_slots_from_text(_SAMPLE_RESPONSE)
    for slot in slots:
        assert slot not in stripped


# ── _enhance_slots_for_voice — year-boundary ─────────────────────────────────

def test_enhance_slots_for_voice_same_year():
    """Slot month == current month → same year in ISO output."""
    from _text_utils import _enhance_slots_for_voice
    slots_text = "Tuesday, June 10 10:00 AM – 10:30 AM"
    result = _enhance_slots_for_voice(slots_text, _today=date(2026, 6, 1))
    assert "2026" in result


def test_enhance_slots_for_voice_next_year_for_january_slot_in_december():
    """January slot when today is December → next year in ISO output."""
    from _text_utils import _enhance_slots_for_voice
    slots_text = "Tuesday, January 06 10:00 AM – 10:30 AM"
    result = _enhance_slots_for_voice(slots_text, _today=date(2026, 12, 15))
    assert "2027" in result
    # Confirm it's NOT using 2026 for the ISO timestamps
    assert "2027-01-06" in result or "2027" in result.split("start_iso=")[-1]


def test_enhance_slots_for_voice_later_month_same_year():
    """August slot when today is June → same year."""
    from _text_utils import _enhance_slots_for_voice
    slots_text = "Friday, August 14 9:00 AM – 9:30 AM"
    result = _enhance_slots_for_voice(slots_text, _today=date(2026, 6, 1))
    assert "2026" in result
    assert "2027" not in result


# ── HTML escaping in escalation email ────────────────────────────────────────

def test_escalation_email_html_escapes_xss():
    """LLM-generated <script> in reason/summary must be escaped before SendGrid."""
    captured = {}

    class MockMail:
        def __init__(self, from_email, to_emails, subject, html_content):
            captured["html_content"] = html_content
            captured["subject"] = subject

    mock_sg_client = MagicMock()
    mock_sg_client.return_value.send.return_value = MagicMock(status_code=202)

    with (
        patch("tools._get_sendgrid_key", return_value="SG.fake-key"),
        patch("sendgrid.SendGridAPIClient", mock_sg_client),
        patch("sendgrid.helpers.mail.Mail", MockMail),
    ):
        from tools import escalate_to_human
        escalate_to_human.invoke({
            "reason": "<script>alert(1)</script>",
            "user_summary": '<img src=x onerror=alert(2)>',
        })

    assert captured, "Mail was never instantiated — check the mock setup"
    html = captured["html_content"]
    # Raw unescaped open tags must not appear in the rendered HTML
    assert "<script>" not in html, "Unescaped <script> found in email body"
    assert "<img " not in html, "Unescaped <img> found in email body"
    # Escaped versions must appear
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html
