# agents.py — Phase 1 + Phase 4
#
# Phase 1: single receptionist_agent with all 8 tools.
# Phase 4: three specialist agents (informer, booker, closer), each with a
#          focused prompt and only the tools it needs.  graph.py selects
#          which architecture to use via the USE_MULTI_AGENT env var.

from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt
from langchain_openai import ChatOpenAI

from tenants import load_tenant
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
_TENANTS_DIR = Path(__file__).parent / "tenants"

# ── Prompt helpers ────────────────────────────────────────────────────────────

def _load_prompt(filename: str) -> str:
    return (_PROMPTS / filename).read_text(encoding="utf-8")


def _load_tenant_prompt(prompt_name: str, tenant_id: str) -> str:
    """Resolve a prompt for a tenant.

    Uses tenants/<id>/prompts/<name> if that override file exists, else the
    shared base prompt in prompts/<name>; then fills {company} from the tenant
    config. The default tenant fills {company} → "Orchelix AI Consulting", so
    its prompts are byte-identical to before.
    """
    override = _TENANTS_DIR / tenant_id / "prompts" / prompt_name
    if tenant_id != "default" and override.exists():
        text = override.read_text(encoding="utf-8")
    else:
        text = _load_prompt(prompt_name)
    return text.replace("{company}", load_tenant(tenant_id).company_name)


def _make_middleware(prompt_name: str):
    """Dynamic-prompt middleware: resolves the tenant's prompt at request time.

    tenant_id comes from runtime.context (seeded by api.py via context=...).
    {company} is filled from the tenant config; {today} from the clock. Using
    .replace (not .format) keeps literal braces in prompts safe.
    """
    @dynamic_prompt
    def _prompt(request) -> str:
        ctx = getattr(getattr(request, "runtime", None), "context", None) or {}
        tenant_id = (ctx.get("tenant_id") if isinstance(ctx, dict) else "default") or "default"
        text = _load_tenant_prompt(prompt_name, tenant_id)
        return text.replace("{today}", date.today().isoformat())
    return _prompt


# ── Phase 1: single receptionist agent ───────────────────────────────────────

# Lazy init: ChatOpenAI validates the API key at construction time, which
# crashes any import of this module in environments without OPENAI_API_KEY
# (e.g. CI unit tests, linters). Wrapping in try/except lets the module
# load cleanly; actual agent calls will fail at invocation time as expected.
try:
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    _llm_ready = True
except Exception:
    llm = None
    _llm_ready = False

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
    return _make_middleware("esmi_system.md")


receptionist_agent = (
    create_agent(llm, tools=ESMI_TOOLS, middleware=[make_prompt_middleware()])
    if _llm_ready else None
)


# ── Phase 4: specialist agent factories ──────────────────────────────────────
# Each factory returns a fresh compiled agent. Called once at graph build time.

def make_informer(model=None):
    """Answers questions about services, pricing, and FAQs.
    Tools: search_knowledge_base, get_pricing, escalate_to_human.

    escalate_to_human lets the informer hand off when the KB can't answer a
    question — closing the Phase 4 gap where off-script questions got weak,
    fabricated answers instead of a human follow-up.
    """
    return create_agent(
        model or llm,
        tools=[search_knowledge_base, get_pricing, escalate_to_human],
        middleware=[_make_middleware("informer.md")],
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
        middleware=[_make_middleware("booker.md")],
    )


def make_closer(model=None):
    """Handles hot leads, KB misses, and human hand-offs.
    Tools: escalate_to_human only.
    """
    return create_agent(
        model or llm,
        tools=[escalate_to_human],
        middleware=[_make_middleware("closer.md")],
    )


print("✅ Esmi agents loaded.")
