# platform_api/knowledge.py — Knowledge Base manager (PLATFORM_BLUEPRINT.md
# Phase 2, "Knowledge base manager"). MVP: list/add/delete a tenant's own
# FAQ/text snippets, plus a read-only "ask a test question" box.
#
# Reuses the existing KB infrastructure end to end rather than building a
# parallel one:
#   - tools._kb_dir(tenant_id) — same source folder search_knowledge_base
#     already reads (tenants/<id>/kb/ on disk).
#   - The same DirectoryLoader(glob="**/*.md") + FAISS index in tools.py picks
#     up new files automatically — recursive, so a subdirectory works with no
#     changes there.
#   - tools.invalidate_kb_index(tenant_id) — new, see tools.py: the FAISS
#     cache was build-once-per-process with no invalidation hook, so a
#     dashboard edit would otherwise sit unused until the next deploy.
#   - tools.search_knowledge_base.func(...) — the exact production retrieval
#     tool, called directly for the "test" endpoint so it can never drift
#     from what the live agent actually sees.
#
# Dashboard-managed entries live under a dedicated tenants/<id>/kb/dashboard/
# subfolder, one small .md file per entry (filename = entry id), kept
# separate from the hand-authored onboarding docs (01_about.md, 02_services.md,
# ...) so a tenant can never accidentally delete those through this API — this
# endpoint only ever touches files it created itself.

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from platform_api.security import require_tenant, verify_platform_secret

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_ANSWER_LEN = 4000
_MAX_QUESTION_LEN = 300
_MAX_ENTRIES = 200
_ENTRY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PARSE_QA_RE = re.compile(r"^\*\*Q: (.+?)\*\*\s*\nA: ([\s\S]*)$")


def _dashboard_dir(tenant_id: str) -> Path:
    from tools import _kb_dir

    return _kb_dir(tenant_id) / "dashboard"


def _entry_path(tenant_id: str, entry_id: str) -> Path:
    return _dashboard_dir(tenant_id) / f"{entry_id}.md"


def _render_entry(question: Optional[str], answer: str) -> str:
    """Same '**Q: ... **\\nA: ...' shape the hand-authored FAQ docs already
    use (see tenants/*/kb/*_faq.md) — new entries read naturally alongside
    the existing ones, and this format round-trips cleanly in _parse_entry."""
    if question:
        return f"**Q: {question}**\nA: {answer}\n"
    return f"{answer}\n"


def _parse_entry(path: Path) -> dict:
    text = path.read_text(encoding="utf-8").strip()
    m = _PARSE_QA_RE.match(text)
    question = m.group(1) if m else None
    answer = m.group(2) if m else text
    created_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return {
        "id": path.stem,
        "question": question,
        "answer": answer,
        "created_at": created_at,
    }


def _reject_default(tenant_id: str) -> None:
    if tenant_id == "default":
        raise HTTPException(
            status_code=400,
            detail="Orchelix's own knowledge base isn't managed through this endpoint.",
        )


class KnowledgeEntryCreate(BaseModel):
    question: Optional[str] = None
    answer: str


class KnowledgeTestQuery(BaseModel):
    query: str


@router.get("/platform/knowledge")
def platform_list_knowledge(request: Request) -> dict:
    """List the tenant's dashboard-managed KB entries, newest first.

    Sync `def` on purpose (FastAPI threadpool) — blocking filesystem reads.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    from tools import _kb_dir

    kb_dir = _kb_dir(tenant_id)
    dash_dir = _dashboard_dir(tenant_id)

    entries = []
    if dash_dir.is_dir():
        files = sorted(dash_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files:
            try:
                entries.append(_parse_entry(p))
            except Exception as e:
                log.warning("Knowledge entry %s unreadable, skipping: %s", p, e)

    # Onboarding-authored docs (01_about.md, ...) aren't editable here — just
    # surfaced as a count so the tenant knows Esmi also draws on those.
    other_docs_count = 0
    if kb_dir.is_dir():
        other_docs_count = sum(
            1 for p in kb_dir.rglob("*.md") if dash_dir not in p.parents
        )

    return {
        "tenant_id": tenant_id,
        "entries": entries,
        "other_docs_count": other_docs_count,
    }


@router.post("/platform/knowledge")
def platform_add_knowledge(body: KnowledgeEntryCreate, request: Request) -> dict:
    """Add one FAQ/text entry, written as its own .md file, then invalidate
    the in-process KB cache so it's searchable immediately (no redeploy)."""
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    answer = body.answer.strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer is required")
    if len(answer) > _MAX_ANSWER_LEN:
        raise HTTPException(
            status_code=400, detail=f"answer must be at most {_MAX_ANSWER_LEN} characters"
        )
    question = (body.question or "").strip() or None
    if question and len(question) > _MAX_QUESTION_LEN:
        raise HTTPException(
            status_code=400, detail=f"question must be at most {_MAX_QUESTION_LEN} characters"
        )

    dash_dir = _dashboard_dir(tenant_id)
    dash_dir.mkdir(parents=True, exist_ok=True)
    existing = list(dash_dir.glob("*.md"))
    if len(existing) >= _MAX_ENTRIES:
        raise HTTPException(
            status_code=400,
            detail=f"This tenant already has {_MAX_ENTRIES} knowledge entries — "
            "the self-serve limit. Delete one before adding another.",
        )

    entry_id = uuid.uuid4().hex
    path = _entry_path(tenant_id, entry_id)
    path.write_text(_render_entry(question, answer), encoding="utf-8")

    from tools import invalidate_kb_index

    invalidate_kb_index(tenant_id)
    log.info("Tenant '%s': knowledge entry %s added.", tenant_id, entry_id)

    return {"tenant_id": tenant_id, "entry": _parse_entry(path)}


@router.delete("/platform/knowledge/{entry_id}")
def platform_delete_knowledge(entry_id: str, request: Request) -> dict:
    """Delete one dashboard-managed entry, then invalidate the KB cache."""
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    if not _ENTRY_ID_RE.match(entry_id):
        raise HTTPException(status_code=400, detail="Invalid entry id")

    path = _entry_path(tenant_id, entry_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Entry not found")

    path.unlink()

    from tools import invalidate_kb_index

    invalidate_kb_index(tenant_id)
    log.info("Tenant '%s': knowledge entry %s deleted.", tenant_id, entry_id)

    return {"tenant_id": tenant_id, "deleted": entry_id}


@router.post("/platform/knowledge/test")
def platform_test_knowledge(body: KnowledgeTestQuery, request: Request) -> dict:
    """Run the tenant's own question through the EXACT production
    search_knowledge_base tool — never a reimplementation — so this always
    reflects what Esmi would actually retrieve, never a cousin of it."""
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")

    from tools import search_knowledge_base

    result = search_knowledge_base.func(query, config={"configurable": {"tenant_id": tenant_id}})

    return {"tenant_id": tenant_id, "query": query, "result": result}
