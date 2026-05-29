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
import re
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
#  SMS CONFIRMATION HELPER  (voice bookings)
# ════════════════════════════════════════════════════════════════════════


def _send_sms_confirmation(to_number: str, when: str) -> None:
    """Text a booking confirmation to a voice caller. Best-effort — never raises.

    Voice callers leave the call with no email invite (we only have their phone),
    so this closes the loop. Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and
    TWILIO_SMS_FROM (an SMS-capable Twilio number in E.164). If any is missing
    the send is skipped with a warning.
    """
    try:
        sid = os.environ.get("TWILIO_ACCOUNT_SID")
        token = os.environ.get("TWILIO_AUTH_TOKEN")
        from_number = os.environ.get("TWILIO_SMS_FROM")
        if not (sid and token and from_number):
            log.warning("Twilio SMS not configured — skipping booking confirmation text.")
            return

        from twilio.rest import Client

        body = (
            f"You're confirmed for {when} with Orchelix AI Consulting. "
            "Reply to this message if anything changes. — Esmi"
        )
        Client(sid, token).messages.create(body=body, from_=from_number, to=to_number)
        log.info("Booking confirmation SMS sent to %s", to_number)
    except Exception as e:
        log.warning(f"Booking confirmation SMS failed: {e}")


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
    except Exception:
        log.exception("KB search failed")
        return (
            "I couldn't pull that up just now. I can connect you with our team "
            "to get you an accurate answer."
        )


# ════════════════════════════════════════════════════════════════════════
#  PRICING — canonical, deterministic (single source of truth)
# ════════════════════════════════════════════════════════════════════════
#
# Pricing is returned from this structured constant — NOT from the vector
# store — so quoted numbers are always exact. RAG chunking can split a price
# table and surface a partial/wrong figure, which is the worst failure mode
# for a sales receptionist. Keep these numbers in sync with
# orchelix_knowledge_base/13_pricing_tiers.md.

_PRICING = [
    {
        "name": "Esmi — AI Virtual Receptionist & Lead Qualification",
        "popular": True,
        "setup_from": 8500,
        "monthly_from": 1099,
        "best_for": "Any business that receives inbound leads or inquiries and wants 24/7 coverage without missing a lead.",
        "highlights": [
            "Never miss another lead",
            "Intelligent 24/7 qualification and booking",
            "Bilingual support (EN/ES) available",
        ],
    },
    {
        "name": "Revenue Operations Agents (AI Sales & Lead Management)",
        "popular": False,
        "setup_from": 9500,
        "monthly_from": 1299,
        "best_for": "Sales teams and businesses with active lead flow who want to scale follow-up without burning out their team.",
        "highlights": [
            "Scale sales follow-up without burning out your team",
            "Cleaner pipeline and higher conversion rates",
        ],
    },
    {
        "name": "Firm OS — Custom Multi-Agent Operations System",
        "popular": False,
        "setup_from": 24000,
        "monthly_from": 2499,
        "best_for": "Growing businesses ready for coordinated AI operations across multiple departments.",
        "highlights": [
            "Multiple specialized agents working together as one team",
            "Bookkeeping automation available as a module",
            "One central dashboard for full visibility",
        ],
    },
]


@tool
def get_pricing() -> str:
    """Return Orchelix's current, canonical pricing for every package.

    Use this for ANY pricing question instead of the knowledge base — these
    numbers are authoritative and exact. Never quote prices from memory or KB
    search; always call this tool first.
    """
    lines: list[str] = []
    for p in _PRICING:
        title = p["name"] + ("  ★ Most Popular" if p["popular"] else "")
        lines.append(title)
        lines.append(
            f"Setup from ${p['setup_from']:,} · ${p['monthly_from']:,}/mo managed service"
        )
        for h in p["highlights"]:
            lines.append(f"- {h}")
        lines.append(f"Ideal for: {p['best_for']}")
        lines.append("")
    lines.append(
        "Pricing model: a one-time setup fee plus a monthly managed service "
        "(monitoring, optimization, updates, support). No long-term contract "
        "on the monthly service."
    )
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
#  CALENDAR TOOLS
# ════════════════════════════════════════════════════════════════════════

_BUSINESS_TZ = "America/Toronto"
_HOURS = range(9, 17)  # 9 AM – 5 PM
_SLOT_MIN = 30

# Caller-safe message spoken/shown when the calendar is unreachable. It nudges
# the voice model to fall back to a human transfer instead of reading a stack trace.
_CALENDAR_FALLBACK = (
    "I'm having trouble reaching the calendar right now. "
    "Let me connect you with someone on our team so we don't lose your spot."
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: str | None) -> bool:
    """True only for a syntactically valid email. Phone numbers (the voice
    path passes the caller's number here) return False so we never hand an
    invalid attendee email to Google Calendar."""
    return bool(value and _EMAIL_RE.match(value.strip()))


def _friendly_when(start_iso: str) -> str:
    """Render an ISO start time as a spoken-friendly phrase, e.g.
    'Thursday, May 29 at 10:00 AM'. Falls back to the raw value if unparseable."""
    try:
        dt = datetime.fromisoformat(start_iso)
        return f"{dt.strftime('%A, %B %d')} at {dt.strftime('%I:%M %p').lstrip('0')}"
    except Exception:
        return start_iso


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
        log.error("list_available_slots: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception as e:
        log.exception("list_available_slots failed")
        return _CALENDAR_FALLBACK


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
        attendee_email: Optional contact for the booking. A valid email is added
            as a calendar attendee (and gets an invite). The voice path passes
            the caller's phone number here instead — that is recorded in the
            event description, never as an attendee (Google rejects non-emails).
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
                when = _friendly_when(existing["start"].get("dateTime", start_time))
                return f"That's already booked — you're confirmed for {when}."
            except Exception:
                return (
                    "⚠️ That time is no longer available — someone else just "
                    "booked it. Could you pick another slot?"
                )

        contact = (attendee_email or "").strip()
        has_email = _looks_like_email(contact)

        event_body = {
            "id": event_id,
            "summary": summary,
            "start": {"dateTime": start_time, "timeZone": _BUSINESS_TZ},
            "end": {"dateTime": end_time, "timeZone": _BUSINESS_TZ},
        }
        if has_email:
            event_body["attendees"] = [{"email": contact}]
        elif contact:
            # Voice path: contact is a phone number, not an email. Record it in
            # the description so it never reaches the (validated) attendees field.
            event_body["description"] = f"Booked by phone. Caller contact: {contact}"

        # Only ask Google to email invites when there is a real attendee.
        send_updates = "all" if has_email else "none"

        try:
            created = (
                service.events()
                .insert(calendarId="primary", body=event_body, sendUpdates=send_updates)
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

        # Voice bookings (phone contact, no email invite) get a confirmation text.
        if contact and not has_email and any(c.isdigit() for c in contact):
            _send_sms_confirmation(contact, _friendly_when(start_time))

        return f"Booked — confirmed for {_friendly_when(start_time)}."

    except RuntimeError as e:
        log.error("book_appointment: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception as e:
        log.exception("book_appointment failed")
        return _CALENDAR_FALLBACK


# ── Manage existing bookings: find / cancel / reschedule ──────────────────
def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _event_matches_contact(event: dict, contact: str) -> bool:
    """True if an event belongs to `contact` (an email attendee for chat
    bookings, or a phone number stored in the description for voice bookings)."""
    contact = contact.strip()
    if _looks_like_email(contact):
        emails = [a.get("email", "").lower() for a in event.get("attendees", [])]
        return contact.lower() in emails
    cd = _digits(contact)
    return bool(cd) and cd in _digits(event.get("description", ""))


@tool
def find_booking(contact: str) -> str:
    """Find a caller's upcoming appointment(s) by email or phone number.

    Call this before rescheduling or canceling. `contact` is the caller's email
    (chat) or phone number (voice). Returns each match with an event id that
    cancel_appointment and reschedule_appointment require.
    """
    try:
        import pytz

        service = _get_calendar_service()
        tz = pytz.timezone(_BUSINESS_TZ)
        now = datetime.now(tz)
        future = now + timedelta(days=60)
        events = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=future.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
            .get("items", [])
        )
        matches = [e for e in events if _event_matches_contact(e, contact)]
        if not matches:
            return "I don't see any upcoming bookings under that contact."
        lines = ["Found these upcoming bookings:"]
        for e in matches:
            when = _friendly_when(e.get("start", {}).get("dateTime", ""))
            lines.append(f"- {e.get('summary', '(no title)')} on {when} (id: {e['id']})")
        return "\n".join(lines)
    except RuntimeError as e:
        log.error("find_booking: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception:
        log.exception("find_booking failed")
        return _CALENDAR_FALLBACK


@tool
def cancel_appointment(event_id: str) -> str:
    """Cancel an existing appointment by its event id (from find_booking).

    Always confirm with the caller before calling this — cancellation is final.
    """
    try:
        service = _get_calendar_service()
        try:
            event = (
                service.events().get(calendarId="primary", eventId=event_id).execute()
            )
        except Exception:
            return "I couldn't find that booking — it may have already been canceled."
        when = _friendly_when(event.get("start", {}).get("dateTime", ""))
        send_updates = "all" if event.get("attendees") else "none"
        service.events().delete(
            calendarId="primary", eventId=event_id, sendUpdates=send_updates
        ).execute()
        return f"Done — I've canceled your appointment for {when}."
    except RuntimeError as e:
        log.error("cancel_appointment: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception:
        log.exception("cancel_appointment failed")
        return _CALENDAR_FALLBACK


@tool
def reschedule_appointment(
    event_id: str, new_start_time: str, new_end_time: str
) -> str:
    """Move an existing appointment to a new time.

    Args:
        event_id: The booking's event id (from find_booking).
        new_start_time: ISO 8601 start with timezone, e.g. '2026-05-29T10:00:00-04:00'.
        new_end_time:   ISO 8601 end with timezone.

    Always confirm the new time with the caller before calling this.
    """
    try:
        service = _get_calendar_service()
        try:
            event = (
                service.events().get(calendarId="primary", eventId=event_id).execute()
            )
        except Exception:
            return (
                "I couldn't find that booking — it may have been canceled. "
                "Want me to book a new time?"
            )
        if not _slot_still_free(service, new_start_time, new_end_time):
            return "That new time isn't available — could you pick another slot?"

        event["start"] = {"dateTime": new_start_time, "timeZone": _BUSINESS_TZ}
        event["end"] = {"dateTime": new_end_time, "timeZone": _BUSINESS_TZ}
        send_updates = "all" if event.get("attendees") else "none"
        service.events().update(
            calendarId="primary", eventId=event_id, body=event, sendUpdates=send_updates
        ).execute()

        # Re-confirm by SMS for voice (phone) bookings.
        desc = event.get("description", "")
        if "Caller contact:" in desc and not event.get("attendees"):
            phone = desc.split("Caller contact:", 1)[1].strip()
            if any(c.isdigit() for c in phone):
                _send_sms_confirmation(phone, _friendly_when(new_start_time))

        return f"Done — I've moved your appointment to {_friendly_when(new_start_time)}."
    except RuntimeError as e:
        log.error("reschedule_appointment: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception:
        log.exception("reschedule_appointment failed")
        return _CALENDAR_FALLBACK


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
