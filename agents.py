# agents.py — Phase 1 / Phase 3
#
# Phase 1 was correctness-only: the prompt and ReAct shape are unchanged from
# v0 so behaviour is comparable. Phase 2 will split this into the
# supervisor + specialists architecture (Greeter / Qualifier / Informer /
# Booker / Closer) per the architecture review.
#
# Phase 3: the system prompt now lives in prompts/esmi_system.md — versioned,
# reviewable, and exercised by the eval harness (evals/) — instead of an inline
# string. Behaviour is identical; {today} is substituted at request time.

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

llm = ChatOpenAI(model="gpt-4o", temperature=0)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "esmi_system.md"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

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
    """Fresh dynamic-prompt middleware that injects today's date per request.

    Using middleware (not a baked system_prompt) keeps {today} correct on a
    long-running process — the date is resolved at request time, not at boot.
    .replace (not .format) so literal braces in the prompt can't break templating.
    """

    @dynamic_prompt
    def _esmi_system_prompt(request) -> str:
        today = date.today().isoformat()
        return _SYSTEM_PROMPT.replace("{today}", today)

    return _esmi_system_prompt


receptionist_agent = create_agent(
    llm,
    tools=ESMI_TOOLS,
    middleware=[make_prompt_middleware()],
)

print("✅ Esmi receptionist agent loaded.")
