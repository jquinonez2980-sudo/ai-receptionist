# platform_api/plans.py — Phase 3 ticket 3.2: plan tiers + SOFT usage limits.
#
# Keys match what's actually sold today (tools.py _PRICING / scripts/stripe_setup.py
# TIERS), not a separate vocabulary. `managed` is the default/catch-all for custom or
# negotiated deals and every tenant that hasn't been assigned a real plan yet —
# unlimited, never shows a limit or warning. Soft only: nothing here blocks a call.

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

DEFAULT_PLAN_KEY = "managed"

# Minutes at which a plan's status flips from "ok" to "approaching" / "over".
_APPROACHING_PCT = 80
_OVER_PCT = 100


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    included_minutes: Optional[int]  # None = unlimited, no soft limit shown


PLANS: dict[str, Plan] = {
    "local": Plan("local", "Esmi Local", 300),
    "pro": Plan("pro", "Esmi Pro", 750),
    "enterprise": Plan("enterprise", "Esmi Enterprise", 2000),
    "managed": Plan("managed", "Managed (custom)", None),
}


def get_plan(plan_key: Optional[str]) -> Plan:
    """Unknown, blank, or unmigrated `tenants.plan` values fall back to
    `managed` (unlimited) rather than erroring or inventing a limit nobody
    configured — a bad DB value must never surface a false "over limit"."""
    return PLANS.get((plan_key or "").strip().lower(), PLANS[DEFAULT_PLAN_KEY])


def usage_status(minutes_used: float, plan: Plan) -> dict:
    """Soft-limit status for the dashboard: ok | approaching | over.

    `included_minutes` is None (managed/unlimited) → no percent, no status,
    nothing enforced — this ticket is display-only, no hard blocking.
    """
    if plan.included_minutes is None:
        return {"included_minutes": None, "percent_used": None, "status": None}
    pct = round((minutes_used / plan.included_minutes) * 100) if plan.included_minutes else 0
    if pct >= _OVER_PCT:
        status = "over"
    elif pct >= _APPROACHING_PCT:
        status = "approaching"
    else:
        status = "ok"
    return {
        "included_minutes": plan.included_minutes,
        "percent_used": pct,
        "status": status,
    }
