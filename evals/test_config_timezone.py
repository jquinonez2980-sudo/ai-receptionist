"""business_tz becomes self-serve editable (Settings page).

Exercises platform_api.config's pure merge/serialize helpers directly — no DB,
no HTTP. What matters here is not that the field round-trips (it obviously
does) but the three things that would be quietly destructive if wrong:

  1. An omitted business_tz must leave the stored value alone. PUT merges onto
     the raw config, and every save sends the whole allow-list — a bug here
     would rewrite the timezone on every unrelated edit.
  2. A bad zone must 400 at the field, not blow up later inside pytz on the
     tenant's next booking.
  3. Widening the allow-list must not have widened it any further. calendar_id,
     vapi ids and the SendGrid sender stay unreachable from a tenant PUT.

Run: PYTHONUTF8=1 pytest evals/test_config_timezone.py -v
"""

import pytest
from fastapi import HTTPException

from platform_api.config import (
    _DIFF_LABELS,
    ConfigUpdate,
    _apply_update,
    _safe_config_out,
    _summarize_change,
)
from tenants import _config_from_file


def raw_config(**over):
    """A tenant config in the on-disk / tenant_configs JSON shape."""
    cfg = {
        "company_name": "Bella Vista Barbers",
        "business_tz": "America/Toronto",
        "business_hours": [9, 17],
        "business_days": [0, 1, 2, 3, 4],
        "emails": {"booking_to": "owner@bv.example", "escalation_to": "owner@bv.example"},
        "greeting": "",
        "transfer_phone": "+14165550110",
        "services": {},
        "pricing": [],
        # Wiring the tenant must never be able to touch through this endpoint.
        "calendar_id": "shop@group.calendar.google.com",
        "vapi": {"assistant_ids": ["asst_real"], "phone_number_ids": ["pn_real"]},
        "slot_minutes": 30,
    }
    cfg.update(over)
    return cfg


# ── GET exposes it ───────────────────────────────────────────────────────────


def test_get_returns_business_tz():
    """The form has nothing to render without this — it wasn't returned before."""
    out = _safe_config_out(_config_from_file("bv", raw_config()))
    assert out["business_tz"] == "America/Toronto"


# ── PUT accepts it ───────────────────────────────────────────────────────────


def test_valid_timezone_is_applied():
    merged = _apply_update(raw_config(), ConfigUpdate(business_tz="America/Los_Angeles"))
    assert merged["business_tz"] == "America/Los_Angeles"
    # and survives the parse the endpoint runs before writing
    assert _config_from_file("bv", merged).business_tz == "America/Los_Angeles"


def test_timezone_is_stripped():
    merged = _apply_update(raw_config(), ConfigUpdate(business_tz="  Europe/Madrid  "))
    assert merged["business_tz"] == "Europe/Madrid"


@pytest.mark.parametrize(
    "bad", ["Mars/Olympus", "not a zone", "EST5EDT/nope", "America/Toront", ""]
)
def test_invalid_timezone_is_a_400(bad):
    with pytest.raises(HTTPException) as e:
        _apply_update(raw_config(), ConfigUpdate(business_tz=bad))
    assert e.value.status_code == 400
    assert "business_tz" in e.value.detail
    assert "IANA" in e.value.detail


def test_invalid_timezone_leaves_the_config_untouched():
    raw = raw_config()
    with pytest.raises(HTTPException):
        _apply_update(raw, ConfigUpdate(business_tz="Mars/Olympus"))
    assert raw["business_tz"] == "America/Toronto", "input must not be mutated"


# ── the destructive-if-wrong case ────────────────────────────────────────────


def test_omitting_business_tz_preserves_it():
    """An unrelated edit must not rewrite the timezone."""
    merged = _apply_update(raw_config(), ConfigUpdate(company_name="New Name"))
    assert merged["business_tz"] == "America/Toronto"
    assert merged["company_name"] == "New Name"


def test_business_hours_are_not_rewritten_by_a_tz_change():
    """Clock times stay put — reinterpreting them is exactly the consequence
    the UI confirmation warns about, and it must happen by re-reading, never by
    silently shifting the stored numbers."""
    merged = _apply_update(raw_config(), ConfigUpdate(business_tz="Asia/Tokyo"))
    assert merged["business_hours"] == [9, 17]


# ── the allow-list did not widen ─────────────────────────────────────────────


def test_wiring_fields_survive_a_timezone_edit():
    merged = _apply_update(raw_config(), ConfigUpdate(business_tz="Europe/London"))
    assert merged["calendar_id"] == "shop@group.calendar.google.com"
    assert merged["vapi"]["assistant_ids"] == ["asst_real"]
    assert merged["vapi"]["phone_number_ids"] == ["pn_real"]
    assert merged["slot_minutes"] == 30


def test_sender_email_is_still_unreachable():
    """emails.from is tied to SendGrid domain verification — a tenant PUT can
    only set booking_to / escalation_to."""
    assert "from" not in ConfigUpdate.model_fields
    merged = _apply_update(
        raw_config(emails={"from": "info@orchelix.com", "booking_to": "a@b.c"}),
        ConfigUpdate(business_tz="Europe/London"),
    )
    assert merged["emails"]["from"] == "info@orchelix.com"


def test_config_update_allow_list_is_exactly_what_we_expect():
    """Pins the editable surface so a future field addition is deliberate."""
    assert set(ConfigUpdate.model_fields) == {
        "company_name",
        "business_tz",
        "greeting",
        "transfer_phone",
        "business_hours",
        "business_days",
        "locations",
        "services",
        "emails",
        "expected_version",
    }


# ── version history ──────────────────────────────────────────────────────────


def test_timezone_change_is_labelled_in_version_history():
    assert _DIFF_LABELS["business_tz"] == "timezone"
    summary = _summarize_change(
        raw_config(), raw_config(business_tz="Europe/Madrid")
    )
    assert summary == "timezone changed"


def test_unrelated_edit_does_not_report_a_timezone_change():
    summary = _summarize_change(raw_config(), raw_config(company_name="Other"))
    assert "timezone" not in summary
