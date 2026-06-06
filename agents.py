# agents.py — Phase 1 + Phase 4
#
# Phase 1: single receptionist_agent with all 8 tools.
# Phase 4: three specialist agents (informer, booker, closer), each with a
#          focused prompt and only the tools it needs.  graph.py selects
#          which architecture to use via the USE_MULTI_AGENT env var.

from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt

from tools import (
    book_appointment,
    cancel_appointment,
    escalate_to_human,
    find_booking,
    get_pricing,
    list_available_slots,
    reschedule_appointment,
    search_knowledge_base,
)

load_dotenv()

_PROMPTS = Path(__file__).parent / "prompts"

# ── Prompt helpers ────────────────────────────────────────────────────────────

def _load_prompt(filename: str) -> str:
    return (_PROMPTS / filename).read_text(encoding="utf-8")


def _make_middleware(prompt_text: str):
    """Dynamic-prompt middleware that resolves {today} per request."""
    @dynamic_prompt
    def _prompt(request) -> str:
        return prompt_text.replace("{today}", date.today().isoformat())
    return _prompt


# ── Phase 1: single receptionist agent ───────────────────────────────────────

llm = ChatOpenAI(model="gpt-4o", temperature=0)

ESMI_TOOLS = [
    search_knowledge_base,
    get_pricing,
    list_available_slots,
    book_appointment,
    find_booking,
    reschedule_appointment,
    cancel_appointment,
    escalate_to_human,
]

def make_prompt_middleware():
    """Phase 1 middleware — used by harness.py and the Phase 1 graph."""
    return _make_middleware(_load_prompt("esmi_system.md"))


receptionist_agent = create_agent(
    llm,
    tools=ESMI_TOOLS,
    middleware=[make_prompt_middleware()],
)


# ── Phase 4: specialist agent factories ──────────────────────────────────────
# Each factory returns a fresh compiled agent. Called once at graph build time.

def make_informer(model=None):
    """Answers questions about services, pricing, and FAQs.
    Tools: search_knowledge_base, get_pricing only.
    """
    return create_agent(
        model or llm,
        tools=[search_knowledge_base, get_pricing],
        middleware=[_make_middleware(_load_prompt("informer.md"))],
    )


def make_booker(model=None):
    """Manages all calendar operations — book, find, reschedule, cancel.
    Tools: list_available_slots, book_appointment, find_booking,
           reschedule_appointment, cancel_appointment.
    """
    return create_agent(
        model or llm,
        tools=[
            list_available_slots,
            book_appointment,
            find_booking,
            reschedule_appointment,
            cancel_appointment,
        ],
        middleware=[_make_middleware(_load_prompt("booker.md"))],
    )


def make_closer(model=None):
    """Handles hot leads, KB misses, and human hand-offs.
    Tools: escalate_to_human only.
    """
    return create_agent(
        model or llm,
        tools=[escalate_to_human],
        middleware=[_make_middleware(_load_prompt("closer.md"))],
    )


print("✅ Esmi agents loaded.")
