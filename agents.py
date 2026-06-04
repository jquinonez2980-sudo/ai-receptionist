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
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

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


def _esmi_prompt(state: dict) -> list[BaseMessage]:
    today = date.today().isoformat()
    # .replace (not .format) so literal braces a future prompt edit might add
    # (e.g. a JSON example) can never break templating.
    content = _SYSTEM_PROMPT.replace("{today}", today)
    return [SystemMessage(content=content)] + state["messages"]


receptionist_agent = create_react_agent(
    llm,
    tools=[
        search_knowledge_base,
        get_pricing,
        list_available_slots,
        book_appointment,
        find_booking,
        reschedule_appointment,
        cancel_appointment,
        escalate_to_human,
    ],
    prompt=_esmi_prompt,
)

print("✅ Esmi receptionist agent loaded.")
