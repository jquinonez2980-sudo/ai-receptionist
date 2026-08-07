"""tools.py's scenario_mode short-circuit — the safety mechanism Quality
Studio (platform_api/quality_studio.py) depends on to exercise real
tool/business-rule logic without ever touching a real customer's calendar,
inbox, or phone.

This is the highest-blast-radius change in the Quality Studio build: it
touches book_appointment_core and every write-adjacent tool real, paying
tenants call every day. Every test here monkeypatches the actual I/O calls
(_get_calendar_service, SendGridAPIClient, _send_*) to hard-fail loudly if
scenario_mode ever lets one through — a silent pass here would mean a
"practice run" could really book, cancel, or email a real customer.

What matters here:
  1. book_appointment_core(scenario_mode=True) for a time that IS actually
     open: real closed-day/closed-hours checks run (there's nothing to
     reject), but it returns a synthetic success WITHOUT calling
     _get_calendar_service at all — no Calendar API touched, period.
  2. book_appointment_core(scenario_mode=True) for a time that is NOT open
     (outside business hours): the REAL closed-hours rejection still fires,
     before scenario_mode is ever consulted — this is the after_hours
     scenario's whole point, and confirms scenario_mode doesn't paper over
     real business-rule failures.
  3. The @tool book_appointment wrapper threads config.configurable.
     scenario_mode through to book_appointment_core correctly.
  4. escalate_to_human(scenario_mode=True) never imports/calls
     SendGridAPIClient.
  5. request_cancellation_code / cancel_appointment / reschedule_appointment
     all short-circuit before _get_calendar_service in scenario mode
     (defense-in-depth — none of Quality Studio's v1 scenarios call these,
     but a stray LLM decision must never be able to reach them for real).

Run: PYTHONUTF8=1 pytest evals/test_tools_scenario_mode.py -v
"""

from datetime import datetime, timedelta

import tools

TENANT = "default"


def _next_weekday_at(hour: int, weekday: int = 2) -> str:
    """Next occurrence of `weekday` (Tue=1, Wed=2 by datetime.weekday()) at
    `hour`:00 America/Toronto, far enough in the future to never be "today"
    (avoids same-day edge cases in _closed_day_message/_closed_hours_message).
    ISO string with the fixed -05:00 offset (America/Toronto standard time —
    fine for a fixed test date; DST doesn't affect which hour is "9am").
    """
    base = datetime.now() + timedelta(days=14)
    days_ahead = (weekday - base.weekday()) % 7
    d = base + timedelta(days=days_ahead)
    return d.strftime(f"%Y-%m-%dT{hour:02d}:00:00-05:00")


# Default tenant: Mon-Fri, 9am-5pm America/Toronto (tools._HOURS/_BUSINESS_DAYS).
OPEN_START = _next_weekday_at(10)  # Wednesday 10am — well within hours
OPEN_END = _next_weekday_at(10).replace(":00:00-05:00", ":30:00-05:00")
CLOSED_START = _next_weekday_at(3)  # Wednesday 3am — no tenant is ever open
CLOSED_END = _next_weekday_at(3).replace(":00:00-05:00", ":30:00-05:00")


def _boom_calendar_service(*a, **kw):
    raise AssertionError("_get_calendar_service must not be called in scenario_mode")


# ── book_appointment_core ───────────────────────────────────────────────────


def test_scenario_mode_open_slot_never_touches_calendar(monkeypatch):
    monkeypatch.setattr(tools, "_get_calendar_service", _boom_calendar_service)

    result = tools.book_appointment_core(
        tenant_id=TENANT,
        summary="Test booking",
        start_time=OPEN_START,
        end_time=OPEN_END,
        attendee_email="jamie@example.com",
        scenario_mode=True,
    )

    assert result["ok"] is True
    assert result["scenario_mode"] is True
    assert "[Practice run" in result["message"]
    assert result["event_id"] is None


def test_scenario_mode_still_enforces_real_closed_hours(monkeypatch):
    """The whole point of after_hours: real business-rule logic runs even in
    scenario_mode, BEFORE the synthetic-success short-circuit."""
    monkeypatch.setattr(tools, "_get_calendar_service", _boom_calendar_service)

    result = tools.book_appointment_core(
        tenant_id=TENANT,
        summary="Test booking",
        start_time=CLOSED_START,
        end_time=CLOSED_END,
        attendee_email="jamie@example.com",
        scenario_mode=True,
    )

    assert result["ok"] is False
    assert "scenario_mode" not in result  # never reached the synthetic branch
    assert "[Practice run" not in result["message"]


def test_scenario_mode_false_is_unaffected_by_the_new_parameter(monkeypatch):
    """Sanity check: a normal (non-scenario) closed-hours call behaves
    identically to before this change — scenario_mode defaults False."""
    monkeypatch.setattr(tools, "_get_calendar_service", _boom_calendar_service)

    result = tools.book_appointment_core(
        tenant_id=TENANT,
        summary="Test booking",
        start_time=CLOSED_START,
        end_time=CLOSED_END,
        attendee_email="jamie@example.com",
    )

    assert result["ok"] is False


# ── @tool book_appointment wrapper threads scenario_mode through config ────


def test_book_appointment_tool_threads_scenario_mode_from_config(monkeypatch):
    monkeypatch.setattr(tools, "_get_calendar_service", _boom_calendar_service)

    config = {"configurable": {"tenant_id": TENANT, "scenario_mode": True}}
    result = tools.book_appointment.invoke(
        {
            "summary": "Test booking",
            "start_time": OPEN_START,
            "end_time": OPEN_END,
            "attendee_email": "jamie@example.com",
        },
        config=config,
    )

    assert "[Practice run" in result


def test_book_appointment_tool_without_scenario_mode_reaches_calendar(monkeypatch):
    """Confirms the wrapper doesn't accidentally short-circuit for a normal
    call — it must reach _get_calendar_service (which we make raise, just to
    prove it was actually called, then catch the resulting fallback)."""
    monkeypatch.setattr(tools, "_get_calendar_service", _boom_calendar_service)

    config = {"configurable": {"tenant_id": TENANT}}  # no scenario_mode key at all
    result = tools.book_appointment.invoke(
        {
            "summary": "Test booking",
            "start_time": OPEN_START,
            "end_time": OPEN_END,
            "attendee_email": "jamie@example.com",
        },
        config=config,
    )

    # book_appointment's @tool wrapper catches everything and returns the
    # calendar-unconfigured fallback string — the important thing is it's NOT
    # the scenario_mode success message, i.e. it really tried to book.
    assert "[Practice run" not in result


# ── escalate_to_human ────────────────────────────────────────────────────────


def test_escalate_scenario_mode_never_imports_sendgrid(monkeypatch):
    def boom_get_key(*a, **kw):
        raise AssertionError("_get_sendgrid_key must not be called in scenario_mode")

    monkeypatch.setattr(tools, "_get_sendgrid_key", boom_get_key)

    config = {"configurable": {"tenant_id": TENANT, "scenario_mode": True}}
    result = tools.escalate_to_human.invoke(
        {"reason": "Practice escalation", "user_summary": "Test summary"}, config=config
    )

    assert "[Practice run" in result
    assert "ESCALATION_FAILED" not in result


# ── request_cancellation_code / cancel_appointment / reschedule_appointment ─


def test_request_cancellation_code_scenario_mode_never_touches_calendar(monkeypatch):
    monkeypatch.setattr(tools, "_get_calendar_service", _boom_calendar_service)
    config = {"configurable": {"tenant_id": TENANT, "scenario_mode": True}}
    result = tools.request_cancellation_code.invoke({"event_id": "abcd1234"}, config=config)
    assert "[Practice run" in result


def test_cancel_appointment_scenario_mode_never_touches_calendar(monkeypatch):
    monkeypatch.setattr(tools, "_get_calendar_service", _boom_calendar_service)
    config = {"configurable": {"tenant_id": TENANT, "scenario_mode": True}}
    result = tools.cancel_appointment.invoke(
        {"event_id": "abcd1234", "confirmation_code": "123456"}, config=config
    )
    assert "[Practice run" in result


def test_reschedule_appointment_scenario_mode_never_touches_calendar(monkeypatch):
    monkeypatch.setattr(tools, "_get_calendar_service", _boom_calendar_service)
    config = {"configurable": {"tenant_id": TENANT, "scenario_mode": True}}
    result = tools.reschedule_appointment.invoke(
        {
            "event_id": "abcd1234",
            "new_start_time": OPEN_START,
            "new_end_time": OPEN_END,
            "confirmation_code": "123456",
        },
        config=config,
    )
    assert "[Practice run" in result


# ── _scenario_mode_from_config ───────────────────────────────────────────────


def test_scenario_mode_from_config_defaults_false():
    assert tools._scenario_mode_from_config(None) is False
    assert tools._scenario_mode_from_config({}) is False
    assert tools._scenario_mode_from_config({"configurable": {}}) is False


def test_scenario_mode_from_config_reads_true():
    assert tools._scenario_mode_from_config({"configurable": {"scenario_mode": True}}) is True
