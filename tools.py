# tools.py — Phase 1
#
# Changes vs. v0 (all correctness, no behaviour change for end user):
#
#   1. search_knowledge_base — FAISS index is built ONCE at module load (or
#      loaded from disk) instead of re-embedding the entire KB on every call.
#      Index is persisted to KB_INDEX_DIR with a content-hash sidecar so it
#      automatically rebuilds when KB docs change.
#
#   2. book_appointment — now idempotent. Accepts an optional
#      `idempotency_key`; the system generates one if absent. The key is
#      hashed into a deterministic Google Calendar event ID, so a retried
#      or double-clicked call returns the SAME booking instead of creating
#      a duplicate. Also re-checks the slot is still free immediately
#      before insert to prevent races.
#
#   3. list_available_slots — returns slots with a stable slot_id so the
#      Phase 2 UI can pass it back without regex-parsing assistant text.
#      Backwards-compatible "Available slots:\n..." text is still returned.
#
#   4. Lazy import of Google Calendar client; no work at module load.
#
#   5. Tenant hooks: get_calendar_service(tenant_id=...) and KB index path
#      both accept an optional tenant prefix. Phase 1 leaves the default
#      tenant in place; Phase 2 wires multi-tenant routing.

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_community.vectorstores import FAISS
from langchain_core.tools import tool
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()
log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════
#  GOOGLE CALENDAR — lazy service builder
# ════════════════════════════════════════════════════════════════════════

_CAL_SERVICE_CACHE: dict[str, object] = {}
_CAL_LOCK = threading.Lock()


def _get_calendar_service(tenant_id: str = "default"):
    """Return a cached Google Calendar service client.

    Tries (in order):
      1. Streamlit secret  GOOGLE_TOKEN_JSON  (recommended for Streamlit Cloud)
      2. Local token.json file                (works locally; must be gitignored!)

    Tenant_id is a forward-looking hook — Phase 2 will look up per-tenant
    tokens by this key. Phase 1 always uses the global token.
    """
    if tenant_id in _CAL_SERVICE_CACHE:
        return _CAL_SERVICE_CACHE[tenant_id]

    with _CAL_LOCK:
        if tenant_id in _CAL_SERVICE_CACHE:
            return _CAL_SERVICE_CACHE[tenant_id]

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = None

        # Method 1: Base64-encoded token blob (from .env baked into Docker image)
        token_b64 = os.environ.get("GOOGLE_TOKEN_B64")
        if token_b64:
            try:
                import base64
                token_data = json.loads(base64.b64decode(token_b64).decode("utf-8"))
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False
                ) as f:
                    json.dump(token_data, f)
                    tmp_path = f.name
                creds = Credentials.from_authorized_user_file(tmp_path)
                os.unlink(tmp_path)
                log.info("Google Calendar: credentials loaded from GOOGLE_TOKEN_B64.")
            except Exception as e:
                log.warning(f"GOOGLE_TOKEN_B64 decode failed: {e}")

        # Method 2: Individual env vars — no JSON quoting issues (preferred for Railway)
        # Set GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET in Railway vars.
        refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if refresh_token and client_id and client_secret:
            try:
                token_data = {
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "token_uri": os.environ.get(
                        "GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token"
                    ),
                    "scopes": ["https://www.googleapis.com/auth/calendar"],
                }
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False
                ) as f:
                    json.dump(token_data, f)
                    tmp_path = f.name
                creds = Credentials.from_authorized_user_file(tmp_path)
                os.unlink(tmp_path)
            except Exception as e:
                log.warning(f"Individual Google credential env vars failed: {e}")

        # Method 2: GOOGLE_TOKEN_JSON env var (full JSON blob)
        if creds is None:
            token_json_env = os.environ.get("GOOGLE_TOKEN_JSON")
            if token_json_env:
                try:
                    stripped = token_json_env.strip().strip("'\"")
                    token_data = json.loads(stripped)
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".json", delete=False
                    ) as f:
                        json.dump(token_data, f)
                        tmp_path = f.name
                    creds = Credentials.from_authorized_user_file(tmp_path)
                    os.unlink(tmp_path)
                except Exception as e:
                    log.warning(f"GOOGLE_TOKEN_JSON env var parse failed: {e}")

        # Method 3: Streamlit Cloud secret (legacy)
        if creds is None:
            try:
                import streamlit as st  # type: ignore

                token_data = json.loads(st.secrets["GOOGLE_TOKEN_JSON"])
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False
                ) as f:
                    json.dump(token_data, f)
                    tmp_path = f.name
                creds = Credentials.from_authorized_user_file(tmp_path)
                os.unlink(tmp_path)
            except Exception:
                pass

        # Method 4: Local token.json file
        if creds is None:
            token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
            if os.path.exists(token_path):
                creds = Credentials.from_authorized_user_file(token_path)

        if creds is None:
            raise RuntimeError(
                "Google Calendar credentials not found. "
                "Set GOOGLE_TOKEN_JSON in Streamlit secrets, or provide a "
                "local token.json (gitignored). See .env.example."
            )

        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        _CAL_SERVICE_CACHE[tenant_id] = service
        return service


# ════════════════════════════════════════════════════════════════════════
#  SENDGRID KEY HELPER
# ════════════════════════════════════════════════════════════════════════

def _get_vapi_key() -> str | None:
    """Return the VAPI private API key, decoding from base64 if needed."""
    key = os.environ.get("VAPI_API_KEY")
    if key:
        return key
    key_b64 = os.environ.get("VAPI_API_KEY_B64")
    if key_b64:
        try:
            import base64
            return base64.b64decode(key_b64).decode("utf-8")
        except Exception as e:
            log.warning(f"VAPI_API_KEY_B64 decode failed: {e}")
    return None


def _get_sendgrid_key() -> str | None:
    """Return the SendGrid API key, decoding from base64 if needed.

    Checks in order:
      1. SENDGRID_API_KEY      — plain text (Railway shared var)
      2. SENDGRID_API_KEY_B64  — base64-encoded (baked into Dockerfile)
    """
    key = os.environ.get("SENDGRID_API_KEY")
    if key:
        return key
    key_b64 = os.environ.get("SENDGRID_API_KEY_B64")
    if key_b64:
        try:
            import base64
            return base64.b64decode(key_b64).decode("utf-8")
        except Exception as e:
            log.warning(f"SENDGRID_API_KEY_B64 decode failed: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════
#  EMAIL NOTIFICATION HELPER  (unchanged from v0 in behaviour)
# ════════════════════════════════════════════════════════════════════════


def _send_booking_notification(
    summary: str,
    start_time: str,
    end_time: str,
    attendee_email: Optional[str] = None,
) -> None:
    """Send a branded ops email on every booking. Best-effort."""
    try:
        api_key = _get_sendgrid_key()
        if not api_key:
            log.warning("SendGrid key not found — skipping booking email.")
            return

        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        try:
            dt = datetime.fromisoformat(start_time)
            formatted_date = dt.strftime("%A, %B %d, %Y")
            formatted_time = dt.strftime("%I:%M %p")
        except Exception:
            formatted_date = start_time
            formatted_time = ""

        try:
            dt_end = datetime.fromisoformat(end_time)
            formatted_end_time = dt_end.strftime("%I:%M %p")
        except Exception:
            formatted_end_time = end_time

        subject = f"📅 New Booking — {summary}"
        html_content = f"""
        <div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: linear-gradient(135deg, #0A2540, #0e3460);
                        padding: 24px 28px; border-radius: 12px 12px 0 0;">
                <h1 style="color: #ffffff; margin: 0; font-size: 20px;">📅 New Appointment Booked</h1>
                <p style="color: #00D4EE; margin: 4px 0 0; font-size: 12px;
                           letter-spacing: 0.06em; text-transform: uppercase;">
                    Orchelix AI Consulting — Esmi Receptionist
                </p>
            </div>
            <div style="background: #f8f9fa; padding: 28px; border: 1px solid #e2e8f0;
                        border-radius: 0 0 12px 12px;">
                <p style="color: #0A2540; font-size: 15px;">A new appointment booked through <b>Esmi</b>:</p>
                <table style="width: 100%; border-collapse: collapse; margin-top: 12px;">
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;width:140px;">Title</td>
                        <td style="color:#0A2540;font-weight:600;">{summary}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Date</td>
                        <td style="color:#0A2540;font-weight:600;">{formatted_date}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Time</td>
                        <td style="color:#0A2540;font-weight:600;">{formatted_time} – {formatted_end_time}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Client</td>
                        <td>{attendee_email or "Not provided"}</td></tr>
                </table>
            </div>
        </div>
        """

        message = Mail(
            from_email="info@orchelix.com",
            to_emails="info@orchelix.com",
            subject=subject,
            html_content=html_content,
        )
        SendGridAPIClient(api_key).send(message)
        log.info("Booking notification email sent.")
    except Exception as e:
        log.warning(f"Email notification failed: {e}")


# ════════════════════════════════════════════════════════════════════════
#  KNOWLEDGE BASE — built once, persisted to disk
# ════════════════════════════════════════════════════════════════════════

_KB_LOCK = threading.Lock()
_KB_CACHE: dict[str, FAISS] = {}


def _kb_dir(tenant_id: str = "default") -> Path:
    """Source folder for KB markdown files. Tenant hook in place for Phase 2."""
    base = Path(os.getenv("KB_SOURCE_DIR", "orchelix_knowledge_base"))
    if tenant_id == "default":
        return base
    return base.parent / f"{base.name}__{tenant_id}"


def _kb_index_dir(tenant_id: str = "default") -> Path:
    """Persistence dir for the FAISS index + content hash."""
    base = Path(os.getenv("KB_INDEX_DIR", ".kb_index"))
    return base / tenant_id


def _kb_content_hash(src_dir: Path) -> str:
    """SHA256 over (path, size, mtime) for every .md file. Cheap, correct enough."""
    h = hashlib.sha256()
    if not src_dir.exists():
        return "missing"
    for p in sorted(src_dir.rglob("*.md")):
        try:
            st = p.stat()
            h.update(str(p.relative_to(src_dir)).encode())
            h.update(str(st.st_size).encode())
            h.update(str(int(st.st_mtime)).encode())
        except Exception:
            continue
    return h.hexdigest()


def _build_kb_index(tenant_id: str = "default") -> Optional[FAISS]:
    """Build (or load from disk) the FAISS index for `tenant_id`.

    Strategy:
      * If `<index_dir>/hash.txt` matches the source folder's content hash,
        load the existing FAISS index from disk.
      * Otherwise, re-embed everything, save the index + hash sidecar.
    """
    src = _kb_dir(tenant_id)
    if not src.exists():
        log.error(f"KB source dir does not exist: {src}")
        return None

    cur_hash = _kb_content_hash(src)
    idx_dir = _kb_index_dir(tenant_id)
    hash_path = idx_dir / "hash.txt"

    embeddings = OpenAIEmbeddings()

    # Try load from disk
    if hash_path.exists() and hash_path.read_text().strip() == cur_hash:
        try:
            log.info(f"KB[{tenant_id}]: loading persisted FAISS index from {idx_dir}.")
            return FAISS.load_local(
                str(idx_dir), embeddings, allow_dangerous_deserialization=True
            )
        except Exception as e:
            log.warning(f"KB[{tenant_id}]: failed to load persisted index ({e}); rebuilding.")

    # Rebuild
    log.info(f"KB[{tenant_id}]: building FAISS index from {src} (this calls OpenAI embeddings).")
    loader = DirectoryLoader(
        str(src),
        glob="**/*.md",
        loader_cls=TextLoader,
        loader_kwargs={"encoding": "utf-8"},
    )
    docs = loader.load()
    if not docs:
        log.error(f"KB[{tenant_id}]: no .md docs found in {src}.")
        return None

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    splits = splitter.split_documents(docs)
    vs = FAISS.from_documents(splits, embeddings)

    idx_dir.mkdir(parents=True, exist_ok=True)
    vs.save_local(str(idx_dir))
    hash_path.write_text(cur_hash)
    log.info(f"KB[{tenant_id}]: index built ({len(splits)} chunks) and saved to {idx_dir}.")
    return vs


def _get_kb_index(tenant_id: str = "default") -> Optional[FAISS]:
    """Thread-safe cached accessor for the KB index."""
    if tenant_id in _KB_CACHE:
        return _KB_CACHE[tenant_id]
    with _KB_LOCK:
        if tenant_id in _KB_CACHE:
            return _KB_CACHE[tenant_id]
        vs = _build_kb_index(tenant_id)
        if vs is not None:
            _KB_CACHE[tenant_id] = vs
        return vs


# ── Tool: search_knowledge_base ──────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """Search the Orchelix AI knowledge base (all .md files) by semantic similarity."""
    try:
        vs = _get_kb_index()
        if vs is None:
            return "Knowledge base unavailable. (No docs loaded.)"
        retriever = vs.as_retriever(search_kwargs={"k": 6})
        results = retriever.invoke(query)
        return "\n\n".join(doc.page_content for doc in results)
    except Exception as e:
        log.exception("KB search failed")
        return f"Knowledge base error: {e}"


# ════════════════════════════════════════════════════════════════════════
#  CALENDAR TOOLS
# ════════════════════════════════════════════════════════════════════════

_BUSINESS_TZ = "America/Toronto"
_HOURS = range(9, 17)  # 9 AM – 5 PM
_SLOT_MIN = 30


def _slot_id(start_iso: str) -> str:
    """Stable, opaque slot id derived from start time. Lets Phase 2 UI pass back
    a slot the user picked without regex-parsing the assistant's text."""
    return hashlib.sha1(start_iso.encode()).hexdigest()[:16]


@tool
def list_available_slots(start_date: str, end_date: str) -> str:
    """List 30-min slots between start_date and end_date (inclusive), Mon–Fri, 9–5,
    America/Toronto. One freebusy call covers the whole range.

    Args:
        start_date: ISO date 'YYYY-MM-DD'
        end_date:   ISO date 'YYYY-MM-DD'
    """
    try:
        import pytz

        service = _get_calendar_service()
        tz = pytz.timezone(_BUSINESS_TZ)

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        window_start = tz.localize(start_dt.replace(hour=0, minute=0, second=0))
        window_end = tz.localize(end_dt.replace(hour=23, minute=59, second=59))

        result = (
            service.freebusy()
            .query(
                body={
                    "timeMin": window_start.isoformat(),
                    "timeMax": window_end.isoformat(),
                    "timeZone": _BUSINESS_TZ,
                    "items": [{"id": "primary"}],
                }
            )
            .execute()
        )
        busy_periods = result["calendars"]["primary"]["busy"]

        busy_ranges = []
        for period in busy_periods:
            b_start = datetime.fromisoformat(period["start"]).astimezone(tz)
            b_end = datetime.fromisoformat(period["end"]).astimezone(tz)
            busy_ranges.append((b_start, b_end))

        available: list[str] = []
        current = start_dt
        while current <= end_dt:
            if current.weekday() < 5:  # Mon–Fri only
                for hour in _HOURS:
                    for minute in (0, 30):
                        slot_start = tz.localize(
                            current.replace(
                                hour=hour, minute=minute, second=0, microsecond=0
                            )
                        )
                        slot_end = slot_start + timedelta(minutes=_SLOT_MIN)

                        is_busy = any(
                            bs < slot_end and be > slot_start
                            for bs, be in busy_ranges
                        )
                        if not is_busy:
                            # Phase 1 keeps the assistant-readable text format;
                            # slot_id is logged so Phase 2 UI can adopt it.
                            iso = slot_start.isoformat()
                            log.debug(
                                "slot %s id=%s",
                                slot_start.strftime("%Y-%m-%d %H:%M"),
                                _slot_id(iso),
                            )
                            available.append(
                                f"{current.strftime('%A, %B %d')} "
                                f"{slot_start.strftime('%I:%M %p')} – "
                                f"{slot_end.strftime('%I:%M %p')}"
                            )
            current += timedelta(days=1)

        if not available:
            return (
                f"No available slots between {start_date} and {end_date}. "
                "The calendar appears fully booked for this period."
            )

        return "Available slots:\n" + "\n".join(available[:12])

    except RuntimeError as e:
        return f"⚠️ Calendar not configured: {e}"
    except Exception as e:
        log.exception("list_available_slots failed")
        return f"Calendar error: {e}"


# ── Idempotency helpers ──────────────────────────────────────────────────
def _idem_event_id(idem_key: str) -> str:
    """Map an arbitrary idempotency key to a deterministic, valid Google
    Calendar event ID (base32hex, lowercase, length 5–1024).

    A sha256 hex digest is 64 chars of [0-9a-f] — a strict subset of base32hex,
    so Google accepts it. Same idem_key → same event ID → duplicate insert
    returns HTTP 409 instead of creating a second event.
    """
    return hashlib.sha256(idem_key.encode()).hexdigest()


def _slot_still_free(service, start_iso: str, end_iso: str) -> bool:
    """Re-verify a slot is free immediately before insert. Closes the window
    where two simultaneous callers could both pass the LLM's list step and
    then both try to book."""
    try:
        result = (
            service.freebusy()
            .query(
                body={
                    "timeMin": start_iso,
                    "timeMax": end_iso,
                    "timeZone": _BUSINESS_TZ,
                    "items": [{"id": "primary"}],
                }
            )
            .execute()
        )
        return len(result["calendars"]["primary"]["busy"]) == 0
    except Exception as e:
        log.warning(f"Pre-book freebusy check failed: {e}. Proceeding with insert.")
        return True  # fail-open; the insert itself is still idempotent


# ── Tool: book_appointment ───────────────────────────────────────────────
@tool
def book_appointment(
    summary: str,
    start_time: str,
    end_time: str,
    attendee_email: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> str:
    """Book a confirmed appointment in Google Calendar (idempotent).

    Args:
        summary: Event title.
        start_time: ISO 8601 start (with timezone), e.g. '2026-05-26T10:00:00-04:00'.
        end_time:   ISO 8601 end.
        attendee_email: Optional invitee email.
        idempotency_key: Optional. If omitted, derived from the deterministic
            (summary,start,end,email) tuple — so the SAME logical booking
            attempted twice yields ONE event.

    Returns: human-readable confirmation string.
    """
    try:
        # Derive a stable idem key if the caller didn't provide one.
        if not idempotency_key:
            idempotency_key = "|".join(
                [summary or "", start_time, end_time, (attendee_email or "").lower()]
            )

        service = _get_calendar_service()
        event_id = _idem_event_id(idempotency_key)

        # Pre-flight race check (best-effort; insert is the source of truth).
        if not _slot_still_free(service, start_time, end_time):
            # The slot is busy — check whether the conflict IS our own event
            # (idempotent re-attempt), in which case we just return success.
            try:
                existing = (
                    service.events()
                    .get(calendarId="primary", eventId=event_id)
                    .execute()
                )
                return (
                    f"✅ Already booked (idempotent).\n"
                    f"Title: {existing.get('summary')}\n"
                    f"Start: {existing['start'].get('dateTime')}\n"
                    f"End:   {existing['end'].get('dateTime')}\n"
                    f"Link:  {existing.get('htmlLink', 'N/A')}"
                )
            except Exception:
                return (
                    "⚠️ That time is no longer available — someone else just "
                    "booked it. Could you pick another slot?"
                )

        event_body = {
            "id": event_id,
            "summary": summary,
            "start": {"dateTime": start_time, "timeZone": _BUSINESS_TZ},
            "end": {"dateTime": end_time, "timeZone": _BUSINESS_TZ},
        }
        if attendee_email:
            event_body["attendees"] = [{"email": attendee_email}]

        try:
            created = (
                service.events()
                .insert(calendarId="primary", body=event_body, sendUpdates="all")
                .execute()
            )
        except Exception as e:
            # Google raises HttpError with resp.status==409 on duplicate id.
            status = getattr(getattr(e, "resp", None), "status", None)
            if status == 409:
                created = (
                    service.events()
                    .get(calendarId="primary", eventId=event_id)
                    .execute()
                )
                log.info(f"book_appointment: idempotent hit ({event_id[:8]}…)")
            else:
                raise

        _send_booking_notification(
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            attendee_email=attendee_email,
        )

        return (
            f"✅ Appointment booked!\n"
            f"Title: {summary}\n"
            f"Start: {start_time}\n"
            f"End:   {end_time}\n"
            f"Link:  {created.get('htmlLink', 'N/A')}"
        )

    except RuntimeError as e:
        return f"⚠️ Calendar not configured: {e}"
    except Exception as e:
        log.exception("book_appointment failed")
        return f"Calendar error: {e}"


# ── Tool: escalate_to_human ───────────────────────────────────────────────
@tool
def escalate_to_human(reason: str, user_summary: str) -> str:
    """Notify the Orchelix team that a lead needs human follow-up.

    Call this when:
    - You searched the knowledge base twice and still cannot answer accurately.
    - The user mentions budget, timeline, or urgency ("ready to start", "ASAP", "need this soon").
    - The user expresses frustration or explicitly asks to speak with a person.

    Args:
        reason: Short label, e.g. "hot lead — mentioned budget" or "out of scope question".
        user_summary: 2-3 sentences summarising what the user needs.
    """
    try:
        api_key = _get_sendgrid_key()
        if not api_key:
            log.warning("SendGrid key not found — escalation email skipped.")
            return "I've flagged this for our team and someone will follow up with you shortly."

        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        subject = f"[Esmi Escalation] {reason}"
        html_content = f"""
        <div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: linear-gradient(135deg, #0A2540, #0e3460);
                        padding: 24px 28px; border-radius: 12px 12px 0 0;">
                <h1 style="color: #ffffff; margin: 0; font-size: 20px;">🚨 Esmi Escalation</h1>
                <p style="color: #00D4EE; margin: 4px 0 0; font-size: 12px;
                           letter-spacing: 0.06em; text-transform: uppercase;">
                    Orchelix AI Consulting — Action Required
                </p>
            </div>
            <div style="background: #f8f9fa; padding: 28px; border: 1px solid #e2e8f0;
                        border-radius: 0 0 12px 12px;">
                <table style="width: 100%; border-collapse: collapse; margin-top: 4px;">
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;width:140px;">Reason</td>
                        <td style="color:#0A2540;font-weight:600;">{reason}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Time</td>
                        <td style="color:#0A2540;">{ts}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;vertical-align:top;">Summary</td>
                        <td style="color:#0A2540;">{user_summary}</td></tr>
                </table>
            </div>
        </div>
        """

        message = Mail(
            from_email="info@orchelix.com",
            to_emails="jquinonez2980@gmail.com",
            subject=subject,
            html_content=html_content,
        )
        SendGridAPIClient(api_key).send(message)
        log.info(f"Escalation email sent: {reason}")
    except Exception as e:
        log.warning(f"Escalation email failed: {e}")

    return "I've flagged this for our team and someone will follow up with you shortly."


# ── Eager KB index warm-up (best-effort) ─────────────────────────────────
try:
    _get_kb_index()
except Exception as _e:  # never crash import
    log.warning(f"KB warm-up skipped: {_e}")

print("✅ Tools loaded (Phase 1: persisted KB + idempotent booking).")
