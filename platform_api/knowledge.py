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
# STORAGE (changed in alembic 0007 — this fixed a data-loss bug):
# Dashboard-managed entries live in the kb_entries Postgres table, NOT on
# disk. They previously lived under tenants/<id>/kb/dashboard/*.md, which is
# inside the container's writable layer; the ai-receptionist service has no
# Railway volume, so every deploy silently deleted them while the manager
# carried on looking like it worked. Verified before migrating: all 11
# non-default tenants had 0 entries and 0 PDFs, so nothing needed rescuing.
#
# The hand-authored onboarding docs (01_about.md, 02_services.md, ...) stay on
# disk under tenants/<id>/kb/. They are git-tracked, ship with the image, and
# were never at risk. This API never touches them — it only ever reads/writes
# its own kb_entries rows, so a tenant cannot delete them through the
# dashboard. They are surfaced here purely as other_docs_count.
#
# Retrieval reads BOTH: tools._build_kb_index() embeds the git-tracked files
# union the tenant's kb_entries rows, and folds the DB content into the index
# hash so an edit rebuilds correctly.

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
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
# docs/ESMI_DASHBOARD_UX.md Section 5.3 — EN / ES / Auto selector. Same
# validate-at-the-API-layer approach as calls.language (platform_api/
# call_log.py): no DB check constraint on kb_entries.language.
_VALID_LANGUAGES = {"en", "es", "auto"}


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


# ── storage: kb_entries (Postgres) — see the STORAGE note in the header ────


def _db():
    """Engine, or 503. Fail closed — with the DB down we cannot tell an empty
    knowledge base from an unreadable one, and answering "you have no entries"
    would invite a tenant to re-add everything they already have."""
    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")
    return engine


def _entry_out(row) -> dict:
    """FAQ row -> the same shape the filesystem version returned."""
    return {
        "id": row["id"],
        "question": row["question"],
        "answer": row["answer"],
        # None for every entry created before migration 0010, and for any
        # entry a tenant never set a language on — "unspecified", not "en".
        # .get() (not row["language"]) so a caller's row dict/mapping that
        # predates this field degrades to None instead of KeyError.
        "language": row.get("language"),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _pdf_out(row) -> dict:
    """PDF row -> the same shape the filesystem version returned. Display-only
    fields come from `meta`; `source` holds the R2 key when the original was
    archived."""
    meta = row["meta"] or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return {
        "id": row["id"],
        "filename": meta.get("filename") or f"{row['id']}.pdf",
        "size_bytes": meta.get("size_bytes"),
        "pages": meta.get("pages"),
        "truncated": bool(meta.get("truncated")),
        "has_original": bool(row["source"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _count_kind(conn, tenant_id: str, kind: str) -> int:
    from sqlalchemy import text

    return conn.execute(
        text("SELECT count(*) FROM kb_entries WHERE tenant_id = :tid AND kind = :kind"),
        {"tid": tenant_id, "kind": kind},
    ).scalar_one()


def _dashboard_dir(tenant_id: str) -> Path:
    """Retained ONLY so other_docs_count can exclude the legacy directory if a
    stale one is still present in an image. Nothing writes here any more."""
    from tools import _kb_dir

    return _kb_dir(tenant_id) / "dashboard"


# NOTE: the "**Q: ...**\nA: ..." rendering that used to live here now happens
# in tools._kb_db_entries(), at the point the corpus is assembled for
# embedding — the same shape the hand-authored *_faq.md files use, so a
# dashboard entry reads identically to a git-tracked one once retrieved.


def _reject_default(tenant_id: str) -> None:
    if tenant_id == "default":
        raise HTTPException(
            status_code=400,
            detail="Orchelix's own knowledge base isn't managed through this endpoint.",
        )


def _validate_entry_body(question: Optional[str], answer: str) -> tuple[Optional[str], str]:
    """Shared by add and edit so the two can't drift on limits."""
    answer = (answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer is required")
    if len(answer) > _MAX_ANSWER_LEN:
        raise HTTPException(
            status_code=400, detail=f"answer must be at most {_MAX_ANSWER_LEN} characters"
        )
    q = (question or "").strip() or None
    if q and len(q) > _MAX_QUESTION_LEN:
        raise HTTPException(
            status_code=400, detail=f"question must be at most {_MAX_QUESTION_LEN} characters"
        )
    return q, answer


def _validate_language(language: Optional[str]) -> Optional[str]:
    """Empty/None -> unspecified (None). Anything else must be one of
    _VALID_LANGUAGES — reject junk rather than silently storing it, same
    contract as every other enum-shaped field in platform_api/config.py."""
    lang = (language or "").strip().lower()
    if not lang:
        return None
    if lang not in _VALID_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"language must be one of: {', '.join(sorted(_VALID_LANGUAGES))}",
        )
    return lang


def _publish(tenant_id: str) -> None:
    """Make a write visible to retrieval.

    Drops this process's FAISS cache; the next search_knowledge_base call
    recomputes the corpus hash (which now covers kb_entries — see
    tools._kb_content_hash) and re-embeds. Deliberately best-effort: the row is
    already committed, and a failure here means the edit goes live at the next
    rebuild rather than immediately. Never fail a successful write over it.
    """
    try:
        from tools import invalidate_kb_index

        invalidate_kb_index(tenant_id)
    except Exception as e:
        log.warning(
            "Tenant '%s': KB cache invalidation failed (%s: %s) — the entry is "
            "saved and will be picked up on the next index rebuild.",
            tenant_id, type(e).__name__, e,
        )


def _pdf_r2_key(tenant_id: str, entry_id: str) -> str:
    """Unchanged — R2 keys for already-archived originals must stay stable."""
    return f"knowledge-pdfs/{tenant_id}/{entry_id}.pdf"


class KnowledgeEntryCreate(BaseModel):
    question: Optional[str] = None
    answer: str
    language: Optional[str] = None


class KnowledgeTestQuery(BaseModel):
    query: str


@router.get("/platform/knowledge")
def platform_list_knowledge(request: Request) -> dict:
    """List the tenant's dashboard-managed KB entries, newest first.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy query.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    from sqlalchemy import text

    from tools import _kb_dir

    engine = _db()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, kind, question, answer, language, source, meta, created_at "
                "FROM kb_entries WHERE tenant_id = :tid "
                "ORDER BY created_at DESC"
            ),
            {"tid": tenant_id},
        ).mappings().all()

    entries = [_entry_out(r) for r in rows if r["kind"] == "faq"]
    pdfs = [_pdf_out(r) for r in rows if r["kind"] == "pdf"]

    # Onboarding-authored docs (01_about.md, ...) aren't editable here — just
    # surfaced as a count so the tenant knows Esmi also draws on those. These
    # are git-tracked and ship with the image, so they are the one part of the
    # KB that was never at risk from the ephemeral-disk bug.
    kb_dir = _kb_dir(tenant_id)
    dash_dir = _dashboard_dir(tenant_id)
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
    """Add one FAQ/text entry, then invalidate the in-process KB cache so it's
    searchable immediately (no redeploy)."""
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    question, answer = _validate_entry_body(body.question, body.answer)
    language = _validate_language(body.language)

    from sqlalchemy import text

    engine = _db()
    entry_id = uuid.uuid4().hex
    with engine.begin() as conn:
        if _count_kind(conn, tenant_id, "faq") >= _MAX_ENTRIES:
            raise HTTPException(
                status_code=400,
                detail=f"This tenant already has {_MAX_ENTRIES} knowledge entries — "
                "the self-serve limit. Delete one before adding another.",
            )
        row = conn.execute(
            text(
                "INSERT INTO kb_entries (id, tenant_id, kind, question, answer, language) "
                "VALUES (:id, :tid, 'faq', :q, :a, :lang) "
                "RETURNING id, question, answer, language, created_at"
            ),
            {"id": entry_id, "tid": tenant_id, "q": question, "a": answer, "lang": language},
        ).mappings().one()
        entry = _entry_out(row)

    _publish(tenant_id)
    log.info("Tenant '%s': knowledge entry %s added.", tenant_id, entry_id)

    return {"tenant_id": tenant_id, "entry": entry}


@router.put("/platform/knowledge/{entry_id}")
def platform_update_knowledge(
    entry_id: str, body: KnowledgeEntryCreate, request: Request
) -> dict:
    """Edit one FAQ/text entry in place.

    Scoped by tenant_id as well as id: the id alone is a uuid and unguessable,
    but isolation must not rest on that. PDF rows are not editable — their text
    comes from the uploaded file, so changing it here would make the entry
    disagree with the original archived in R2.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    if not _ENTRY_ID_RE.match(entry_id):
        raise HTTPException(status_code=400, detail="Invalid entry id")

    question, answer = _validate_entry_body(body.question, body.answer)
    language = _validate_language(body.language)

    from sqlalchemy import text

    engine = _db()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE kb_entries SET question = :q, answer = :a, language = :lang, "
                "updated_at = now() "
                "WHERE id = :id AND tenant_id = :tid AND kind = 'faq' "
                "RETURNING id, question, answer, language, created_at"
            ),
            {"id": entry_id, "tid": tenant_id, "q": question, "a": answer, "lang": language},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Entry not found")
        entry = _entry_out(row)

    _publish(tenant_id)
    log.info("Tenant '%s': knowledge entry %s edited.", tenant_id, entry_id)

    return {"tenant_id": tenant_id, "entry": entry}


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

    engine = _db()
    with engine.connect() as conn:
        if _count_kind(conn, tenant_id, "pdf") >= _MAX_PDFS:
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

    # The extracted text is the thing retrieval actually reads, so it goes in
    # Postgres alongside FAQ entries. `source` is the R2 key for the original
    # (null when the archive failed or R2 is unconfigured — fail-soft, the
    # entry is still useful); `meta` carries the display-only fields.
    from sqlalchemy import text as _sql

    with engine.begin() as conn:
        row = conn.execute(
            _sql(
                "INSERT INTO kb_entries (id, tenant_id, kind, answer, source, meta) "
                "VALUES (:id, :tid, 'pdf', :a, :src, CAST(:meta AS jsonb)) "
                "RETURNING id, source, meta, created_at"
            ),
            {
                "id": entry_id,
                "tid": tenant_id,
                "a": text,
                "src": r2_key,
                "meta": json.dumps(
                    {
                        "filename": filename,
                        "size_bytes": len(content),
                        "pages": len(pages),
                        "truncated": truncated,
                    }
                ),
            },
        ).mappings().one()
        entry = _pdf_out(row)

    _publish(tenant_id)
    log.info(
        "Tenant '%s': PDF %s uploaded (%s, %d pages, %d chars%s).",
        tenant_id, entry_id, filename, len(pages), len(text), " truncated" if truncated else "",
    )

    return {"tenant_id": tenant_id, "entry": entry}


@router.delete("/platform/knowledge/{entry_id}")
def platform_delete_knowledge(entry_id: str, request: Request) -> dict:
    """Delete one dashboard-managed entry — FAQ or PDF-derived — then
    invalidate the KB cache. A PDF entry also best-effort deletes its
    R2-archived original; a failure there still removes the row (R2 is
    reference-only and never read by retrieval)."""
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    _reject_default(tenant_id)

    if not _ENTRY_ID_RE.match(entry_id):
        raise HTTPException(status_code=400, detail="Invalid entry id")

    from sqlalchemy import text

    engine = _db()
    with engine.begin() as conn:
        # DELETE ... RETURNING: one statement, and scoping by tenant_id means a
        # valid id from another tenant deletes nothing and 404s.
        row = conn.execute(
            text(
                "DELETE FROM kb_entries WHERE id = :id AND tenant_id = :tid "
                "RETURNING kind, source"
            ),
            {"id": entry_id, "tid": tenant_id},
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Entry not found")

    if row["kind"] == "pdf" and row["source"]:
        try:
            from platform_api.recordings import _bucket, _get_client, r2_configured

            if r2_configured():
                _get_client().delete_object(Bucket=_bucket(), Key=row["source"])
        except Exception as e:
            log.warning(
                "PDF %s: R2 delete failed (%s: %s) — entry removed anyway.",
                entry_id, type(e).__name__, e,
            )

    _publish(tenant_id)
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
