# platform_api/knowledge.py — Knowledge Base manager (PLATFORM_BLUEPRINT.md
# Phase 2, "Knowledge base manager"). MVP: list/add/delete a tenant's own
# FAQ/text snippets and uploaded PDFs, plus a read-only "ask a test question" box.
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
#   - PDFs: langchain_community.document_loaders.PyPDFLoader (backed by
#     pypdf) — both already project dependencies (requirements.txt), used
#     nowhere else yet but installed for exactly this kind of document
#     loading. No new package added for this feature.
#   - platform_api.recordings' R2 helpers (_get_client/_bucket/r2_configured)
#     — the same Cloudflare R2 archive already used for call recordings,
#     reused here to keep the original PDF file, best-effort (fail-soft: a
#     KB entry is still created from the extracted text even if R2 is
#     unconfigured or the archive call fails).
#
# Dashboard-managed entries live under a dedicated tenants/<id>/kb/dashboard/
# subfolder, one small .md file per entry (filename = entry id), kept
# separate from the hand-authored onboarding docs (01_about.md, 02_services.md,
# ...) so a tenant can never accidentally delete those through this API — this
# endpoint only ever touches files it created itself. PDF-derived entries live
# one level deeper, tenants/<id>/kb/dashboard/pdf/ — still covered by the
# index's recursive glob, but naturally excluded from the plain-text/FAQ list
# (kb/dashboard/*.md is non-recursive) and from other_docs_count (still a
# descendant of kb/dashboard/).

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from platform_api.security import require_tenant, verify_platform_secret

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_ANSWER_LEN = 4000
_MAX_QUESTION_LEN = 300
_MAX_ENTRIES = 200
_ENTRY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PARSE_QA_RE = re.compile(r"^\*\*Q: (.+?)\*\*\s*\nA: ([\s\S]*)$")


# 4MB, not the 10-20MB one might expect: uploads route through the same
# Next.js proxy pattern as every other /platform/* write
# (platformProxy.ts — the browser never talks to Railway directly), and
# Vercel's Node.js serverless functions hard-cap the request body at ~4.5MB
# platform-wide (not configurable). A larger cap here would silently 413 at
# Vercel's edge before ever reaching this code. Going bigger later means a
# presigned direct-to-R2 upload path instead of this proxy — a real
# follow-up, not a config tweak.
_MAX_PDF_BYTES = 4 * 1024 * 1024
_MAX_PDF_TEXT_CHARS = 200_000  # ~40-50 pages of text; plenty for a KB doc
_MAX_PDFS = 20  # separate, lower cap than _MAX_ENTRIES — PDFs are heavier to embed


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


def _pdf_dir(tenant_id: str) -> Path:
    return _dashboard_dir(tenant_id) / "pdf"


def _pdf_md_path(tenant_id: str, entry_id: str) -> Path:
    return _pdf_dir(tenant_id) / f"{entry_id}.md"


def _pdf_meta_path(tenant_id: str, entry_id: str) -> Path:
    return _pdf_dir(tenant_id) / f"{entry_id}.json"


def _pdf_r2_key(tenant_id: str, entry_id: str) -> str:
    return f"knowledge-pdfs/{tenant_id}/{entry_id}.pdf"


def _parse_pdf_entry(md_path: Path) -> dict:
    meta: dict = {}
    meta_path = md_path.with_suffix(".json")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    created_at = datetime.fromtimestamp(md_path.stat().st_mtime, tz=timezone.utc).isoformat()
    return {
        "id": md_path.stem,
        "filename": meta.get("filename") or f"{md_path.stem}.pdf",
        "size_bytes": meta.get("size_bytes"),
        "pages": meta.get("pages"),
        "truncated": bool(meta.get("truncated")),
        "has_original": bool(meta.get("r2_key")),
        "created_at": created_at,
    }


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

    pdfs = []
    pdf_dir = _pdf_dir(tenant_id)
    if pdf_dir.is_dir():
        files = sorted(pdf_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files:
            try:
                pdfs.append(_parse_pdf_entry(p))
            except Exception as e:
                log.warning("PDF entry %s unreadable, skipping: %s", p, e)

    # Onboarding-authored docs (01_about.md, ...) aren't editable here — just
    # surfaced as a count so the tenant knows Esmi also draws on those.
    # dash_dir appears in a PDF entry's .parents too (kb/dashboard/pdf/x.md),
    # so PDFs are correctly excluded from this count already.
    other_docs_count = 0
    if kb_dir.is_dir():
        other_docs_count = sum(
            1 for p in kb_dir.rglob("*.md") if dash_dir not in p.parents
        )

    return {
        "tenant_id": tenant_id,
        "entries": entries,
        "pdfs": pdfs,
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


@router.post("/platform/knowledge/pdf")
def platform_upload_pdf(request: Request, file: UploadFile = File(...)) -> dict:
    """Upload a PDF: extract its text into a new dashboard-managed KB entry
    (searchable immediately, same as a manual entry), and best-effort archive
    the original file to R2 so it isn't lost — purely for reference, R2 is
    never read by search_knowledge_base.

    Sync `def` on purpose (matches every other /platform/* handler) — reads
    the upload via file.file (a SpooledTemporaryFile), not the async
    UploadFile.read() API, since FastAPI already runs sync handlers in a
    threadpool.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    filename = file.filename or "upload.pdf"
    if not filename.lower().endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(content) > _MAX_PDF_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"PDF must be at most {_MAX_PDF_BYTES // (1024 * 1024)} MB",
        )

    pdf_dir = _pdf_dir(tenant_id)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    if len(list(pdf_dir.glob("*.md"))) >= _MAX_PDFS:
        raise HTTPException(
            status_code=400,
            detail=f"This tenant already has {_MAX_PDFS} uploaded PDFs — the self-serve "
            "limit. Delete one before adding another.",
        )

    # PyPDFLoader (pypdf-backed) needs a real file path, not raw bytes.
    from langchain_community.document_loaders import PyPDFLoader

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        pages = PyPDFLoader(tmp_path).load()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't read this PDF: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    text = "\n\n".join(p.page_content.strip() for p in pages if p.page_content.strip())
    if not text.strip():
        raise HTTPException(
            status_code=400,
            detail="Couldn't extract any text from this PDF — it may be a scanned "
            "image with no selectable text.",
        )
    truncated = len(text) > _MAX_PDF_TEXT_CHARS
    if truncated:
        text = text[:_MAX_PDF_TEXT_CHARS]

    entry_id = uuid.uuid4().hex
    md_path = _pdf_md_path(tenant_id, entry_id)
    md_path.write_text(text, encoding="utf-8")

    r2_key = None
    try:
        from platform_api.recordings import _bucket, _get_client, r2_configured

        if r2_configured():
            _get_client().put_object(
                Bucket=_bucket(),
                Key=_pdf_r2_key(tenant_id, entry_id),
                Body=content,
                ContentType="application/pdf",
            )
            r2_key = _pdf_r2_key(tenant_id, entry_id)
    except Exception as e:
        log.warning(
            "PDF %s: R2 archive failed (%s: %s) — extracted text was still saved.",
            entry_id, type(e).__name__, e,
        )

    _pdf_meta_path(tenant_id, entry_id).write_text(
        json.dumps(
            {
                "filename": filename,
                "size_bytes": len(content),
                "pages": len(pages),
                "truncated": truncated,
                "r2_key": r2_key,
            }
        ),
        encoding="utf-8",
    )

    from tools import invalidate_kb_index

    invalidate_kb_index(tenant_id)
    log.info(
        "Tenant '%s': PDF %s uploaded (%s, %d pages, %d chars%s).",
        tenant_id, entry_id, filename, len(pages), len(text), " truncated" if truncated else "",
    )

    return {"tenant_id": tenant_id, "entry": _parse_pdf_entry(md_path)}


@router.delete("/platform/knowledge/{entry_id}")
def platform_delete_knowledge(entry_id: str, request: Request) -> dict:
    """Delete one dashboard-managed entry — text/FAQ or PDF-derived, ids never
    collide between the two — then invalidate the KB cache. A PDF entry also
    best-effort deletes its R2-archived original; a failure there still
    removes the local KB entry (R2 is reference-only, never load-bearing)."""
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    if not _ENTRY_ID_RE.match(entry_id):
        raise HTTPException(status_code=400, detail="Invalid entry id")

    text_path = _entry_path(tenant_id, entry_id)
    pdf_path = _pdf_md_path(tenant_id, entry_id)

    if text_path.exists():
        text_path.unlink()
    elif pdf_path.exists():
        meta_path = _pdf_meta_path(tenant_id, entry_id)
        r2_key = None
        if meta_path.exists():
            try:
                r2_key = json.loads(meta_path.read_text(encoding="utf-8")).get("r2_key")
            except Exception:
                pass
            meta_path.unlink(missing_ok=True)
        pdf_path.unlink()
        if r2_key:
            try:
                from platform_api.recordings import _bucket, _get_client, r2_configured

                if r2_configured():
                    _get_client().delete_object(Bucket=_bucket(), Key=r2_key)
            except Exception as e:
                log.warning(
                    "PDF %s: R2 delete failed (%s: %s) — local entry removed anyway.",
                    entry_id, type(e).__name__, e,
                )
    else:
        raise HTTPException(status_code=404, detail="Entry not found")

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
