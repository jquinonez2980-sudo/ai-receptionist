"""voice_id / speed / language_pref become self-serve editable (Voice Studio
backend prerequisite — docs/ESMI_DASHBOARD_UX.md Section 12.1).

Exercises platform_api.config's pure merge/serialize helpers directly — no DB,
no HTTP, no VAPI call. That last part matters: as of this change there is NO
sync from a saved value to the tenant's live VAPI assistant, so these tests
only cover "the dashboard can read and write its own record of the choice" —
not "changing this changes what a caller hears." See tenants.py's voice_id
field comment.

What matters here, same three shapes as test_config_timezone.py:

  1. An omitted field must leave the stored value alone.
  2. An out-of-range speed or unknown language_pref must 400, not corrupt state.
  3. The allow-list did not accidentally widen further than these three fields.

Run: PYTHONUTF8=1 pytest evals/test_config_voice.py -v
"""

import pytest
from fastapi import HTTPException

from platform_api.config import (
    _LANGUAGE_PREFS,
    _VOICE_SPEED_MAX,
    _VOICE_SPEED_MIN,
    ConfigUpdate,
    _apply_update,
    _safe_config_out,
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
        "voice_id": "",
        "speed": 1.0,
        "language_pref": "auto",
        # Wiring the tenant must never be able to touch through this endpoint.
        "calendar_id": "shop@group.calendar.google.com",
        "vapi": {"assistant_ids": ["asst_real"], "phone_number_ids": ["pn_real"]},
        "slot_minutes": 30,
    }
    cfg.update(over)
    return cfg


# ── GET exposes them ─────────────────────────────────────────────────────────


def test_get_returns_voice_fields():
    out = _safe_config_out(
        _config_from_file("bv", raw_config(voice_id="sofia", speed=1.1, language_pref="es"))
    )
    assert out["voice_id"] == "sofia"
    assert out["speed"] == 1.1
    assert out["language_pref"] == "es"


def test_get_returns_defaults_when_unset():
    out = _safe_config_out(_config_from_file("bv", raw_config()))
    assert out["voice_id"] == ""
    assert out["speed"] == 1.0
    assert out["language_pref"] == "auto"


# ── PUT accepts valid values ─────────────────────────────────────────────────


def test_valid_voice_id_is_applied():
    merged = _apply_update(raw_config(), ConfigUpdate(voice_id="Sofia"))
    # stored lowercase — dashboard ids are case-insensitive by convention
    assert merged["voice_id"] == "sofia"
    assert _config_from_file("bv", merged).voice_id == "sofia"


def test_valid_speed_is_applied():
    merged = _apply_update(raw_config(), ConfigUpdate(speed=0.9))
    assert merged["speed"] == 0.9
    assert _config_from_file("bv", merged).speed == 0.9


@pytest.mark.parametrize("pref", sorted(_LANGUAGE_PREFS))
def test_valid_language_pref_is_applied(pref):
    merged = _apply_update(raw_config(), ConfigUpdate(language_pref=pref))
    assert merged["language_pref"] == pref


def test_language_pref_is_normalized_to_lowercase():
    merged = _apply_update(raw_config(), ConfigUpdate(language_pref="ES"))
    assert merged["language_pref"] == "es"


# ── PUT rejects invalid values without corrupting state ─────────────────────


@pytest.mark.parametrize("bad_speed", [0.5, 0.84, 1.16, 2.0, -1.0])
def test_out_of_range_speed_is_a_400(bad_speed):
    with pytest.raises(HTTPException) as e:
        _apply_update(raw_config(), ConfigUpdate(speed=bad_speed))
    assert e.value.status_code == 400
    assert "speed" in e.value.detail


def test_boundary_speeds_are_accepted():
    """0.85 and 1.15 are inclusive bounds, not exclusive."""
    assert _apply_update(raw_config(), ConfigUpdate(speed=_VOICE_SPEED_MIN))["speed"] == (
        _VOICE_SPEED_MIN
    )
    assert _apply_update(raw_config(), ConfigUpdate(speed=_VOICE_SPEED_MAX))["speed"] == (
        _VOICE_SPEED_MAX
    )


@pytest.mark.parametrize("bad_pref", ["english", "spanish", "fr", ""])
def test_invalid_language_pref_is_a_400(bad_pref):
    with pytest.raises(HTTPException) as e:
        _apply_update(raw_config(), ConfigUpdate(language_pref=bad_pref))
    assert e.value.status_code == 400
    assert "language_pref" in e.value.detail


def test_language_pref_whitespace_is_stripped_before_validating():
    """" EN " is valid after strip+lower — matches every other string field's
    strip-then-validate order (e.g. business_tz, transfer_phone)."""
    merged = _apply_update(raw_config(), ConfigUpdate(language_pref=" EN "))
    assert merged["language_pref"] == "en"


def test_invalid_speed_leaves_the_config_untouched():
    raw = raw_config(speed=1.0)
    with pytest.raises(HTTPException):
        _apply_update(raw, ConfigUpdate(speed=5.0))
    assert raw["speed"] == 1.0, "input must not be mutated"


def test_overlong_voice_id_is_a_400():
    with pytest.raises(HTTPException) as e:
        _apply_update(raw_config(), ConfigUpdate(voice_id="x" * 65))
    assert e.value.status_code == 400


# ── the destructive-if-wrong case ────────────────────────────────────────────


def test_omitting_voice_fields_preserves_them():
    """An unrelated edit must not reset voice/speed/language to defaults."""
    merged = _apply_update(
        raw_config(voice_id="camila", speed=1.1, language_pref="es"),
        ConfigUpdate(company_name="New Name"),
    )
    assert merged["voice_id"] == "camila"
    assert merged["speed"] == 1.1
    assert merged["language_pref"] == "es"


# ── the allow-list did not widen further ─────────────────────────────────────


def test_wiring_fields_survive_a_voice_edit():
    merged = _apply_update(
        raw_config(), ConfigUpdate(voice_id="ava", speed=1.05, language_pref="en")
    )
    assert merged["calendar_id"] == "shop@group.calendar.google.com"
    assert merged["vapi"]["assistant_ids"] == ["asst_real"]
    assert merged["vapi"]["phone_number_ids"] == ["pn_real"]
    assert merged["slot_minutes"] == 30
