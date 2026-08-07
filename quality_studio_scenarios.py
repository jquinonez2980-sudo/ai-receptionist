# quality_studio_scenarios.py — fixed practice-call scripts for Quality
# Studio (docs/ESMI_DASHBOARD_UX.md Section 3.6).
#
# A scenario is a FIXED list of caller lines, sent in order to the tenant's
# real agent (same graph.py graph /chat uses). This is a scripted caller,
# not an adaptive one — turns do not branch on what the agent actually
# replies. That is a deliberate v1 simplification, not an oversight: a
# caller that reacts to the agent's real wording would need its own LLM
# playing the "caller" role (a second simulated agent), which is exactly
# what docs/ESMI_DASHBOARD_UX.md Section 3.6 warns against building
# ("otherwise a scenario can pass in Quality Studio while the live agent
# behaves differently"). A fixed script still exercises the real agent's
# real tool-calling and KB/business-rule behavior — it just can't adapt
# mid-conversation. See platform_api/quality_studio.py for how a run is
# scored (per-scenario `evaluate`).
#
# Phase 2 (angry/urgent, existing-client-reschedule) reuses the exact same
# mechanism — no new plumbing, just two more scripts + evaluate() cases
# (platform_api/quality_studio.py) + a scenario_mode branch on find_booking
# (tools.py) so the reschedule script has a deterministic booking to act on
# without depending on real calendar state.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    description: str
    language: str  # "en" | "es" — informational, shown in the UI
    turns: tuple[str, ...]  # scripted caller lines, sent in order


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="new_lead_books",
        label="New lead books appointment",
        description="A first-time caller asks about availability and books a slot.",
        language="en",
        turns=(
            "Hi, I'd like to book an appointment.",
            "Do you have anything tomorrow afternoon?",
            "The first option works — my name is Jamie Rivera, "
            "and you can reach me at jamie.rivera@example.com.",
            "Yes, that's correct, please go ahead and book it.",
        ),
    ),
    Scenario(
        id="faq_only",
        label="FAQ only",
        description="A caller asks about pricing and hours without booking anything.",
        language="en",
        turns=(
            "Hi, quick question before I book anything — what are your hours, "
            "and how much do your services typically cost?",
        ),
    ),
    Scenario(
        id="spanish_caller",
        label="Spanish caller",
        description="A full conversation in Spanish, checking language detection.",
        language="es",
        turns=(
            "Hola, quisiera saber si tienen citas disponibles esta semana.",
            "¿Cuáles son sus horarios de atención?",
        ),
    ),
    Scenario(
        id="after_hours",
        label="After hours",
        description=(
            "A caller asks for a time no business is ever open at — tests the "
            "real closed-hours logic rather than the wall clock."
        ),
        language="en",
        turns=("Hi, can I come in at 3 in the morning tomorrow?",),
    ),
    Scenario(
        id="angry_urgent",
        label="Angry / urgent caller",
        description="A frustrated caller demands to speak with a person right away.",
        language="en",
        turns=(
            "This is the third time I've called about this and nobody has fixed "
            "it. I am extremely frustrated.",
            "I don't want to explain it again — just get me a real person right "
            "now, please.",
        ),
    ),
    Scenario(
        id="existing_client_reschedule",
        label="Existing client reschedule",
        description="A returning caller needs to move their appointment to a new time.",
        language="en",
        turns=(
            "Hi, I already have an appointment booked and I need to move it to "
            "a different day.",
            "My email is jamie.rivera@example.com — could you look it up?",
            "Great, got it — the code is 482913. Can you move it to Friday at "
            "2pm instead?",
        ),
    ),
)

_BY_ID = {s.id: s for s in SCENARIOS}


def get_scenario(scenario_id: str) -> Scenario | None:
    return _BY_ID.get((scenario_id or "").strip().lower())
