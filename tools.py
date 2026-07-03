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
import hmac
import html
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_community.vectorstores import FAISS
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from tenants import load_tenant, normalize_tenant_id, tenant_secret

load_dotenv()
log = logging.getLogger(__name__)


def _tenant_from_config(config: RunnableConfig | None) -> str:
    """Extract tenant_id from an injected RunnableConfig (default when absent).

    LangGraph injects `config` into any @tool that declares a `config:
    RunnableConfig` param, and `configurable` is inherited from the top-level
    astream_events config through every subgraph + the ToolNode. The voice path
    passes config explicitly on `.invoke(..., config={"configurable":{...}})`.
    """
    raw = ((config or {}).get("configurable") or {}).get("tenant_id") or "default"
    return normalize_tenant_id(raw)


# ════════════════════════════════════════════════════════════════════════
#  GOOGLE CALENDAR — lazy service builder
# ════════════════════════════════════════════════════════════════════════

_CAL_SERVICE_CACHE: dict[str, object] = {}
_CAL_LOCK = threading.Lock()


def resolve_google_credentials(tenant_id: str = "default"):
    """Resolve Google Calendar OAuth credentials for a tenant.

    For "default": tries GOOGLE_TOKEN_B64 → individual env vars →
      GOOGLE_TOKEN_JSON → local token.json.
    For other tenants: tries per-tenant GOOGLE_TOKEN_B64 via tenant_secret,
      then per-tenant individual vars. Raises RuntimeError if no credentials
      are found — callers catch this and return _CALENDAR_FALLBACK.
    """
    from google.oauth2.credentials import Credentials

    def _creds_from_token_data(data: dict):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name
        try:
            return Credentials.from_authorized_user_file(tmp_path)
        finally:
            os.unlink(tmp_path)

    if tenant_id != "default":
        # Per-tenant only — never fall back to the default/Orchelix token.
        token_b64 = tenant_secret(tenant_id, "GOOGLE_TOKEN_B64")
        if token_b64:
            try:
                import base64
                data = json.loads(base64.b64decode(token_b64).decode("utf-8"))
                creds = _creds_from_token_data(data)
                log.info("Google Calendar: credentials loaded from per-tenant GOOGLE_TOKEN_B64 (%s).", tenant_id)
                return creds
            except Exception as e:
                log.warning("Per-tenant GOOGLE_TOKEN_B64 decode failed for %s: %s", tenant_id, e)

        refresh_token = tenant_secret(tenant_id, "GOOGLE_REFRESH_TOKEN")
        client_id = tenant_secret(tenant_id, "GOOGLE_CLIENT_ID")
        client_secret = tenant_secret(tenant_id, "GOOGLE_CLIENT_SECRET")
        if refresh_token and client_id and client_secret:
            try:
                data = {
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "token_uri": tenant_secret(tenant_id, "GOOGLE_TOKEN_URI") or "https://oauth2.googleapis.com/token",
                    "scopes": ["https://www.googleapis.com/auth/calendar"],
                }
                return _creds_from_token_data(data)
            except Exception as e:
                log.warning("Per-tenant individual Google creds failed for %s: %s", tenant_id, e)

        raise RuntimeError(f"Calendar not configured for tenant {tenant_id}")

    # Default tenant — full resolution chain.
    creds = None

    token_b64 = os.environ.get("GOOGLE_TOKEN_B64")
    if token_b64:
        try:
            import base64
            data = json.loads(base64.b64decode(token_b64).decode("utf-8"))
            creds = _creds_from_token_data(data)
            log.info("Google Calendar: credentials loaded from GOOGLE_TOKEN_B64.")
        except Exception as e:
            log.warning("GOOGLE_TOKEN_B64 decode failed: %s", e)

    if creds is None:
        refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if refresh_token and client_id and client_secret:
            try:
                data = {
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "token_uri": os.environ.get("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
                    "scopes": ["https://www.googleapis.com/auth/calendar"],
                }
                creds = _creds_from_token_data(data)
            except Exception as e:
                log.warning("Individual Google credential env vars failed: %s", e)

    if creds is None:
        token_json_env = os.environ.get("GOOGLE_TOKEN_JSON")
        if token_json_env:
            try:
                data = json.loads(token_json_env.strip().strip("'\""))
                creds = _creds_from_token_data(data)
            except Exception as e:
                log.warning("GOOGLE_TOKEN_JSON env var parse failed: %s", e)

    if creds is None:
        try:
            import streamlit as st  # type: ignore
            data = json.loads(st.secrets["GOOGLE_TOKEN_JSON"])
            creds = _creds_from_token_data(data)
        except Exception:
            pass

    if creds is None:
        token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path)

    if creds is None:
        raise RuntimeError(
            "Google Calendar credentials not found. "
            "Set GOOGLE_TOKEN_B64, GOOGLE_REFRESH_TOKEN, or GOOGLE_TOKEN_JSON "
            "in Railway env vars, or provide a local token.json (gitignored)."
        )

    return creds


def _get_calendar_service(tenant_id: str = "default"):
    """Return a cached Google Calendar service client for a tenant.

    Raises RuntimeError for non-default tenants with no credentials configured.
    Every tool catches RuntimeError and returns _CALENDAR_FALLBACK.
    """
    if tenant_id in _CAL_SERVICE_CACHE:
        return _CAL_SERVICE_CACHE[tenant_id]

    with _CAL_LOCK:
        if tenant_id in _CAL_SERVICE_CACHE:
            return _CAL_SERVICE_CACHE[tenant_id]

        from googleapiclient.discovery import build

        creds = resolve_google_credentials(tenant_id)
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


def _get_sendgrid_key(tenant_id: str = "default") -> str | None:
    """Return the SendGrid API key for a tenant, decoding from base64 if needed.

    Resolves per-tenant via tenant_secret (TENANT_<ID>_SENDGRID_API_KEY for
    non-default tenants; the global SENDGRID_API_KEY for default). Falls back to
    the _B64 variant the same way.
    """
    key = tenant_secret(tenant_id, "SENDGRID_API_KEY")
    if key:
        return key
    key_b64 = tenant_secret(tenant_id, "SENDGRID_API_KEY_B64")
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


def _send_sms_confirmation(to_number: str, when: str, tenant_id: str = "default") -> None:
    """Text a booking confirmation to a voice caller. Best-effort — never raises.

    Voice callers leave the call with no email invite (we only have their phone),
    so this closes the loop. Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and
    TWILIO_SMS_FROM (an SMS-capable Twilio number in E.164), resolved per-tenant.
    If any is missing the send is skipped with a warning.
    """
    try:
        sid = tenant_secret(tenant_id, "TWILIO_ACCOUNT_SID")
        token = tenant_secret(tenant_id, "TWILIO_AUTH_TOKEN")
        from_number = tenant_secret(tenant_id, "TWILIO_SMS_FROM")
        if not (sid and token and from_number):
            log.warning("Twilio SMS not configured — skipping booking confirmation text.")
            return

        from twilio.rest import Client

        cfg = load_tenant(tenant_id)
        body = (
            f"You're confirmed for {when} with {cfg.sms_signature}. "
            "Reply to this message if anything changes. — Esmi"
        )
        Client(sid, token).messages.create(body=body, from_=from_number, to=to_number)
        log.info("Booking confirmation SMS sent to %s", to_number)
    except Exception as e:
        log.warning(f"Booking confirmation SMS failed: {e}")


# ════════════════════════════════════════════════════════════════════════
#  CANCEL/RESCHEDULE CONFIRMATION CODE  (finding 10.1: anyone who knows a
#  contact's email/phone could cancel their booking with no verification)
# ════════════════════════════════════════════════════════════════════════
#
# Unlike the booking-notification helpers above (best-effort, never block the
# flow on failure), these MUST signal failure to the caller — if we can't
# deliver a code, cancel_appointment/reschedule_appointment must refuse to
# proceed rather than silently falling back to the old no-verification behavior.


def _extract_contact(event: dict) -> tuple[Optional[str], bool]:
    """Return (contact, is_email) for a booking's attendee email (chat) or
    phone stored in the description (voice), or (None, False) if neither."""
    attendees = event.get("attendees") or []
    if attendees:
        email = attendees[0].get("email")
        if email:
            return email, True
    desc = event.get("description", "")
    if "Caller contact:" in desc:
        phone = desc.split("Caller contact:", 1)[1].strip()
        if phone and any(c.isdigit() for c in phone):
            return phone, False
    return None, False


def _mask_contact(contact: str, is_email: bool) -> str:
    """Partially obscure a contact for the caller-facing confirmation message."""
    if is_email:
        local, _, domain = contact.partition("@")
        masked_local = (local[:2] if len(local) > 2 else local[:1]) + "***"
        return f"{masked_local}@{domain}" if domain else f"{masked_local}"
    digits = _digits(contact)
    return f"***-{digits[-4:]}" if len(digits) >= 4 else "***"


def _send_confirmation_code_sms(to_number: str, code: str, tenant_id: str = "default") -> bool:
    """Text a cancel/reschedule confirmation code. Returns True only if actually sent."""
    try:
        sid = tenant_secret(tenant_id, "TWILIO_ACCOUNT_SID")
        token = tenant_secret(tenant_id, "TWILIO_AUTH_TOKEN")
        from_number = tenant_secret(tenant_id, "TWILIO_SMS_FROM")
        if not (sid and token and from_number):
            log.warning("Twilio SMS not configured — cannot send confirmation code.")
            return False
        from twilio.rest import Client

        cfg = load_tenant(tenant_id)
        body = f"Your {cfg.sms_signature} confirmation code is {code}. It expires in 10 minutes."
        Client(sid, token).messages.create(body=body, from_=from_number, to=to_number)
        log.info("Confirmation code SMS sent to %s", to_number)
        return True
    except Exception as e:
        log.warning(f"Confirmation code SMS failed: {e}")
        return False


def _send_confirmation_code_email(to_email: str, code: str, tenant_id: str = "default") -> bool:
    """Email a cancel/reschedule confirmation code. Returns True only if actually sent."""
    try:
        api_key = _get_sendgrid_key(tenant_id)
        if not api_key:
            log.warning("SendGrid key not found — cannot send confirmation code.")
            return False
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        cfg = load_tenant(tenant_id)
        message = Mail(
            from_email=cfg.email_from,
            to_emails=to_email,
            subject=f"Your {cfg.company_name} confirmation code",
            html_content=(
                f"<p>Your confirmation code is <b>{html.escape(code)}</b>. "
                "It expires in 10 minutes.</p>"
            ),
        )
        SendGridAPIClient(api_key).send(message)
        log.info("Confirmation code email sent to %s", to_email)
        return True
    except Exception as e:
        log.warning(f"Confirmation code email failed: {e}")
        return False


def _verify_and_consume_code(service, cal_id: str, event: dict, code: str) -> tuple[bool, str]:
    """Check a caller-supplied code against the one request_cancellation_code
    stored on the event's extendedProperties.private. Increments a persisted
    attempt counter on mismatch (capped) so guessing is bounded; clears the
    code on success so it can't be replayed for a second action.

    Mutates `event["extendedProperties"]` in place (in addition to patching the
    server) so a caller that later does a full event .update() — reschedule_appointment
    does, cancel_appointment doesn't since it deletes the event — submits the
    already-consistent state instead of silently reverting it to the pre-patch copy.

    Returns (ok, message-to-return-to-the-caller-if-not-ok).
    """
    props = dict(event.get("extendedProperties") or {})
    private = dict(props.get("private") or {})
    stored_code = private.get("cancel_code")
    expires = private.get("cancel_code_expires")
    attempts = int(private.get("cancel_attempts", "0") or "0")

    if not stored_code:
        return False, (
            "I need to send a confirmation code first — call request_cancellation_code, "
            "then ask the caller to read it back before trying again."
        )
    if attempts >= 5:
        return False, "Too many incorrect codes for this booking — I can send a fresh one if you'd like."
    try:
        expired = datetime.fromisoformat(expires) < datetime.now(timezone.utc)
    except Exception:
        expired = True
    if expired:
        return False, "That code has expired — I can send a new one."

    if not hmac.compare_digest(str(code).strip(), str(stored_code)):
        private["cancel_attempts"] = str(attempts + 1)
        props["private"] = private
        event["extendedProperties"] = props
        remaining = max(0, 5 - (attempts + 1))
        try:
            _retry_calendar(
                lambda: service.events()
                .patch(calendarId=cal_id, eventId=event["id"], body={"extendedProperties": props})
                .execute()
            )
        except Exception:
            log.warning("Failed to persist incremented cancel_attempts for %s", event.get("id"))
        return False, f"That code doesn't match — {remaining} attempt(s) left. Could you double-check it?"

    # Success — clear the code so it can't be reused for a later cancel/reschedule.
    for k in ("cancel_code", "cancel_code_expires", "cancel_attempts"):
        private.pop(k, None)
    props["private"] = private
    event["extendedProperties"] = props
    try:
        _retry_calendar(
            lambda: service.events()
            .patch(calendarId=cal_id, eventId=event["id"], body={"extendedProperties": props})
            .execute()
        )
    except Exception:
        log.warning("Failed to clear confirmation code for %s after use", event.get("id"))
    return True, ""


# ════════════════════════════════════════════════════════════════════════
#  EMAIL NOTIFICATION HELPER  (unchanged from v0 in behaviour)
# ════════════════════════════════════════════════════════════════════════


def _send_booking_notification(
    summary: str,
    start_time: str,
    end_time: str,
    attendee_email: Optional[str] = None,
    tenant_id: str = "default",
) -> None:
    """Send a branded ops email on every booking. Best-effort."""
    try:
        api_key = _get_sendgrid_key(tenant_id)
        if not api_key:
            log.warning("SendGrid key not found — skipping booking email.")
            return
        cfg = load_tenant(tenant_id)

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

        subject = f"📅 New Booking — {html.escape(summary)}"
        html_content = f"""
        <div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: linear-gradient(135deg, #0A2540, #0e3460);
                        padding: 24px 28px; border-radius: 12px 12px 0 0;">
                <h1 style="color: #ffffff; margin: 0; font-size: 20px;">📅 New Appointment Booked</h1>
                <p style="color: #00D4EE; margin: 4px 0 0; font-size: 12px;
                           letter-spacing: 0.06em; text-transform: uppercase;">
                    {html.escape(cfg.company_name)} — Esmi Receptionist
                </p>
            </div>
            <div style="background: #f8f9fa; padding: 28px; border: 1px solid #e2e8f0;
                        border-radius: 0 0 12px 12px;">
                <p style="color: #0A2540; font-size: 15px;">A new appointment booked through <b>Esmi</b>:</p>
                <table style="width: 100%; border-collapse: collapse; margin-top: 12px;">
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;width:140px;">Title</td>
                        <td style="color:#0A2540;font-weight:600;">{html.escape(summary)}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Date</td>
                        <td style="color:#0A2540;font-weight:600;">{html.escape(formatted_date)}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Time</td>
                        <td style="color:#0A2540;font-weight:600;">{html.escape(formatted_time)} – {html.escape(formatted_end_time)}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Client</td>
                        <td>{html.escape(attendee_email) if attendee_email else "Not provided"}</td></tr>
                </table>
            </div>
        </div>
        """

        message = Mail(
            from_email=cfg.email_from,
            to_emails=cfg.email_booking_to,
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
    """Source folder for KB markdown files.

    default → the legacy KB_SOURCE_DIR (orchelix_knowledge_base/).
    other   → tenants/<id>/kb/  (co-located with the tenant's config.json).

    tenant_id is re-validated here (not just at the API boundary) since this
    path feeds directly into filesystem reads/writes — defense in depth against
    a traversal id reaching this function via any other code path.
    """
    tenant_id = normalize_tenant_id(tenant_id)
    if tenant_id == "default":
        return Path(os.getenv("KB_SOURCE_DIR", "orchelix_knowledge_base"))
    return Path(__file__).parent / "tenants" / tenant_id / "kb"


def _kb_index_dir(tenant_id: str = "default") -> Path:
    """Persistence dir for the FAISS index + content hash."""
    base = Path(os.getenv("KB_INDEX_DIR", ".kb_index"))
    return base / normalize_tenant_id(tenant_id)


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
def search_knowledge_base(query: str, config: RunnableConfig = None) -> str:
    """Search the knowledge base (all .md files) by semantic similarity."""
    try:
        vs = _get_kb_index(_tenant_from_config(config))
        if vs is None:
            return "Knowledge base unavailable. (No docs loaded.)"
        retriever = vs.as_retriever(search_kwargs={"k": 6})
        results = retriever.invoke(query)
        if not results:
            return (
                "NO_RESULTS: the knowledge base has no relevant information for "
                "this question. Do not guess — escalate to a human."
            )
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
def get_pricing(config: RunnableConfig = None) -> str:
    """Return the current, canonical pricing for every package.

    Use this for ANY pricing question instead of the knowledge base — these
    numbers are authoritative and exact. Never quote prices from memory or KB
    search; always call this tool first.
    """
    tenant = load_tenant(_tenant_from_config(config))
    pricing = tenant.pricing
    lines: list[str] = []
    for p in pricing:
        title = p["name"] + ("  ★ Most Popular" if p["popular"] else "")
        lines.append(title)
        # Use price_label override if provided, otherwise fall back to SaaS format
        if p.get("price_label"):
            lines.append(p["price_label"])
        else:
            lines.append(
                f"Setup from ${p['setup_from']:,} · ${p['monthly_from']:,}/mo managed service"
            )
        for h in p["highlights"]:
            lines.append(f"- {h}")
        lines.append(f"Ideal for: {p['best_for']}")
        lines.append("")
    # Use tenant-level pricing note override if set, otherwise default SaaS footer
    footer = tenant.pricing_note or (
        "Pricing model: a one-time setup fee plus a monthly managed service "
        "(monitoring, optimization, updates, support). No long-term contract "
        "on the monthly service."
    )
    lines.append(footer)
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
#  CALENDAR TOOLS
# ════════════════════════════════════════════════════════════════════════

_BUSINESS_TZ = "America/Toronto"
_HOURS = range(9, 17)  # 9 AM – 5 PM
_SLOT_MIN = 30
_BUSINESS_DAYS = (0, 1, 2, 3, 4)  # Mon–Fri (datetime.weekday(): Monday=0 ... Sunday=6)

# Caller-safe message spoken/shown when the calendar is unreachable. It nudges
# the voice model to fall back to a human transfer instead of reading a stack trace.
_CALENDAR_FALLBACK = (
    "I'm having trouble reaching the calendar right now. "
    "Let me connect you with someone on our team so we don't lose your spot."
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _retry_calendar(fn, *args, max_attempts: int = 3, **kwargs):
    """Retry a Google Calendar API call with exponential back-off.

    Returns the function result on success. On exhaustion raises the last
    exception so the caller can map it to _CALENDAR_FALLBACK.

    Only retries transient errors (network, 5xx). Skips retry on 4xx since
    those indicate a bad request that won't improve on retry.
    """
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            # Don't retry 4xx (bad request / conflict / not-found)
            if status is not None and 400 <= int(status) < 500:
                raise
            last = e
            if attempt < max_attempts - 1:
                wait = 1.0 * (2 ** attempt)
                log.warning(
                    "Calendar API transient error (%s), retrying in %.1fs (attempt %d/%d)",
                    type(e).__name__, wait, attempt + 1, max_attempts,
                )
                time.sleep(wait)
    raise last


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


def _closed_day_message(start_iso: str, business_days: tuple[int, ...]) -> Optional[str]:
    """Return a caller-facing message if start_iso falls on a day the tenant is
    closed, else None. Defense in depth: book_appointment and
    reschedule_appointment can be called directly (voice path, retries) without
    ever going through list_available_slots, so the day-of-week filter must be
    enforced here too, not just at the listing step."""
    try:
        dt = datetime.fromisoformat(start_iso)
    except Exception:
        return None  # unparseable — let the existing calendar-side validation handle it
    if dt.weekday() in business_days:
        return None
    return (
        f"We're closed on {dt.strftime('%A')}s — could you pick a different day?"
    )


def _fmt_hour(hour: int) -> str:
    """24h int → spoken-friendly 12h label, e.g. 9 -> '9 AM', 17 -> '5 PM'."""
    return datetime(2000, 1, 1, hour % 24).strftime("%I %p").lstrip("0")


def _closed_hours_message(
    start_iso: str, end_iso: str, business_hours: tuple[int, int], business_tz: str
) -> Optional[str]:
    """Return a caller-facing message if start_iso/end_iso fall outside the
    tenant's business hours, else None. Same defense-in-depth rationale as
    _closed_day_message: book_appointment/reschedule_appointment can be called
    directly (voice path, a mis-resolved 'tomorrow at 8' → 8pm, retries)
    without ever going through list_available_slots, so the hours window must
    be enforced here too."""
    try:
        import pytz

        tz = pytz.timezone(business_tz)
        start_dt = datetime.fromisoformat(start_iso).astimezone(tz)
        end_dt = datetime.fromisoformat(end_iso).astimezone(tz)
    except Exception:
        return None  # unparseable — let existing calendar-side validation handle it

    open_hour, close_hour = business_hours
    start_h = start_dt.hour + start_dt.minute / 60
    end_h = end_dt.hour + end_dt.minute / 60
    if start_h < open_hour or end_h > close_hour:
        return (
            f"That's outside our business hours ({_fmt_hour(open_hour)}–"
            f"{_fmt_hour(close_hour)}) — could you pick a time in that window?"
        )
    return None


@tool
def list_available_slots(start_date: str, end_date: str, config: RunnableConfig = None) -> str:
    """List bookable slots between start_date and end_date (inclusive), on the
    tenant's configured business days, during business hours in the business
    timezone. One freebusy call covers the whole range.

    Args:
        start_date: ISO date 'YYYY-MM-DD'
        end_date:   ISO date 'YYYY-MM-DD'
    """
    try:
        import pytz

        tenant_id = _tenant_from_config(config)
        cfg = load_tenant(tenant_id)
        service = _get_calendar_service(tenant_id)
        tz = pytz.timezone(cfg.business_tz)

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        window_start = tz.localize(start_dt.replace(hour=0, minute=0, second=0))
        window_end = tz.localize(end_dt.replace(hour=23, minute=59, second=59))

        cal_id = cfg.calendar_id
        result = (
            service.freebusy()
            .query(
                body={
                    "timeMin": window_start.isoformat(),
                    "timeMax": window_end.isoformat(),
                    "timeZone": cfg.business_tz,
                    "items": [{"id": cal_id}],
                }
            )
            .execute()
        )
        busy_periods = result["calendars"][cal_id]["busy"]

        busy_ranges = []
        for period in busy_periods:
            b_start = datetime.fromisoformat(period["start"]).astimezone(tz)
            b_end = datetime.fromisoformat(period["end"]).astimezone(tz)
            busy_ranges.append((b_start, b_end))

        # Floor: never offer a slot that has already started (plus a short lead
        # time), so "anything today?" at 3pm doesn't surface a 9am slot.
        earliest = datetime.now(tz) + timedelta(minutes=15)

        available: list[str] = []
        current = start_dt
        while current <= end_dt:
            if current.weekday() in cfg.business_days:  # tenant's configured days
                for hour in cfg.hours_range:
                    for minute in (0, 30):
                        slot_start = tz.localize(
                            current.replace(
                                hour=hour, minute=minute, second=0, microsecond=0
                            )
                        )
                        if slot_start < earliest:
                            continue
                        slot_end = slot_start + timedelta(minutes=cfg.slot_minutes)

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
    except Exception:
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


def _slot_still_free(
    service, start_iso: str, end_iso: str,
    business_tz: str = _BUSINESS_TZ, calendar_id: str = "primary",
) -> bool:
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
                    "timeZone": business_tz,
                    "items": [{"id": calendar_id}],
                }
            )
            .execute()
        )
        return len(result["calendars"][calendar_id]["busy"]) == 0
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
    config: RunnableConfig = None,
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

        tenant_id = _tenant_from_config(config)
        cfg = load_tenant(tenant_id)

        closed_msg = _closed_day_message(start_time, cfg.business_days)
        if closed_msg:
            return closed_msg
        hours_msg = _closed_hours_message(start_time, end_time, cfg.business_hours, cfg.business_tz)
        if hours_msg:
            return hours_msg

        service = _get_calendar_service(tenant_id)
        event_id = _idem_event_id(idempotency_key)

        cal_id = cfg.calendar_id
        # Pre-flight race check (best-effort; insert is the source of truth).
        if not _slot_still_free(service, start_time, end_time, cfg.business_tz, cal_id):
            # The slot is busy — check whether the conflict IS our own event
            # (idempotent re-attempt), in which case we just return success.
            try:
                existing = (
                    service.events()
                    .get(calendarId=cal_id, eventId=event_id)
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
        # VAPI passes unresolved template literals (e.g. "{{call.customer.number}}")
        # when caller number is unavailable. Normalise before any downstream use.
        if contact.startswith("{{"):
            contact = "Phone not captured"
            attendee_email = "Phone not captured"
        has_email = _looks_like_email(contact)

        event_body = {
            "id": event_id,
            "summary": summary,
            "start": {"dateTime": start_time, "timeZone": cfg.business_tz},
            "end": {"dateTime": end_time, "timeZone": cfg.business_tz},
        }
        if has_email:
            event_body["attendees"] = [{"email": contact}]
        elif contact:
            # Voice path: contact is a phone number, not an email. Record it in
            # the description so it never reaches the (validated) attendees field.
            event_body["description"] = f"Booked by phone. Caller contact: {contact}"

        # Only ask Google to email invites when there is a real attendee.
        send_updates = "all" if has_email else "none"

        def _insert():
            return (
                service.events()
                .insert(calendarId=cal_id, body=event_body, sendUpdates=send_updates)
                .execute()
            )

        try:
            _retry_calendar(_insert)
        except Exception as e:
            # Google raises HttpError with resp.status==409 on duplicate id.
            status = getattr(getattr(e, "resp", None), "status", None)
            if status == 409:
                service.events().get(calendarId=cal_id, eventId=event_id).execute()
                log.info(f"book_appointment: idempotent hit ({event_id[:8]}…)")
            else:
                raise

        _send_booking_notification(
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            attendee_email=attendee_email,
            tenant_id=tenant_id,
        )

        # Voice bookings (phone contact, no email invite) get a confirmation text.
        if contact and not has_email and any(c.isdigit() for c in contact):
            _send_sms_confirmation(contact, _friendly_when(start_time), tenant_id)

        return f"Booked — confirmed for {_friendly_when(start_time)}."

    except RuntimeError as e:
        log.error("book_appointment: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception:
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


# Short-id prefix length exposed to the model/caller instead of the full
# 64-char hex event id (finding 4.4). SHA256 hex has ~4 billion possible
# 8-char prefixes — collision risk within one small business's calendar is
# negligible, and _resolve_event_id below treats an ambiguous/no match the
# same as a not-found booking rather than guessing.
_SHORT_ID_LEN = 8


def _resolve_event_id(service, cal_id: str, given_id: str) -> Optional[str]:
    """Resolve a short id (what find_booking now exposes) back to the full
    Google Calendar event id. A value already long enough to plausibly be a
    full id is returned as-is — only short values trigger the lookup, so this
    is a no-op for any caller still passing a full id.

    Looks across a wide window (-7 days to +90 days) since a caller might be
    canceling/rescheduling something just booked or well in the future.
    Returns None (not the input) when nothing or more than one event matches
    the prefix — the caller falls through to the normal "couldn't find that
    booking" message rather than acting on an ambiguous match.
    """
    given_id = (given_id or "").strip()
    if len(given_id) > _SHORT_ID_LEN:
        return given_id
    if not given_id:
        return None
    try:
        now = datetime.now(timezone.utc)
        events = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=(now - timedelta(days=7)).isoformat(),
                timeMax=(now + timedelta(days=90)).isoformat(),
                singleEvents=True,
                maxResults=250,
            )
            .execute()
            .get("items", [])
        )
        matches = [e["id"] for e in events if e.get("id", "").startswith(given_id)]
        return matches[0] if len(matches) == 1 else None
    except Exception:
        log.warning("_resolve_event_id lookup failed for %r", given_id, exc_info=True)
        return None


@tool
def find_booking(contact: str, config: RunnableConfig = None) -> str:
    """Find a caller's upcoming appointment(s) by email or phone number.

    Call this before rescheduling or canceling. `contact` is the caller's email
    (chat) or phone number (voice). Returns each match with a short id that
    request_cancellation_code, cancel_appointment, and reschedule_appointment
    accept in place of the full booking id.
    """
    try:
        import pytz

        tenant_id = _tenant_from_config(config)
        cfg = load_tenant(tenant_id)
        service = _get_calendar_service(tenant_id)
        tz = pytz.timezone(cfg.business_tz)
        now = datetime.now(tz)
        future = now + timedelta(days=60)
        events = (
            service.events()
            .list(
                calendarId=cfg.calendar_id,
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
            short_id = e.get("id", "")[:_SHORT_ID_LEN]
            lines.append(f"- {e.get('summary', '(no title)')} on {when} (id: {short_id})")
        return "\n".join(lines)
    except RuntimeError as e:
        log.error("find_booking: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception:
        log.exception("find_booking failed")
        return _CALENDAR_FALLBACK


@tool
def request_cancellation_code(event_id: str, config: RunnableConfig = None) -> str:
    """Send a confirmation code before canceling or rescheduling a booking.

    Call this FIRST — after find_booking, before cancel_appointment or
    reschedule_appointment. Sends a 6-digit code to the contact on file (email
    or phone, whichever the booking has) and expires in 10 minutes. This stops
    anyone who merely knows a contact's email/phone from canceling or moving
    their booking — only someone with access to that contact's inbox/phone can
    read the code back.

    Tell the caller a code was sent (to the masked contact in the result) and
    ask them to read it back, then pass it as confirmation_code to
    cancel_appointment / reschedule_appointment. If this returns a message
    starting with "CONFIRMATION_CODE_FAILED", do NOT cancel or reschedule
    without it — offer to connect the caller with a human instead.
    """
    try:
        tenant_id = _tenant_from_config(config)
        cfg = load_tenant(tenant_id)
        service = _get_calendar_service(tenant_id)
        cal_id = cfg.calendar_id
        event_id = _resolve_event_id(service, cal_id, event_id) or event_id
        try:
            event = service.events().get(calendarId=cal_id, eventId=event_id).execute()
        except Exception:
            return "I couldn't find that booking — it may have already been canceled."

        contact, is_email = _extract_contact(event)
        if not contact:
            return (
                "CONFIRMATION_CODE_FAILED: no contact on file for this booking — "
                "cannot verify identity. Offer to connect the caller with a human."
            )

        code = f"{secrets.randbelow(900000) + 100000}"
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        props = dict(event.get("extendedProperties") or {})
        private = dict(props.get("private") or {})
        private.update({"cancel_code": code, "cancel_code_expires": expires, "cancel_attempts": "0"})
        props["private"] = private
        _retry_calendar(
            lambda: service.events()
            .patch(calendarId=cal_id, eventId=event_id, body={"extendedProperties": props})
            .execute()
        )

        sent = (
            _send_confirmation_code_email(contact, code, tenant_id)
            if is_email
            else _send_confirmation_code_sms(contact, code, tenant_id)
        )
        if not sent:
            return (
                "CONFIRMATION_CODE_FAILED: couldn't deliver a code to the contact on "
                "file. Do not cancel or reschedule without one — offer to connect the "
                "caller with a human instead."
            )

        channel = "email" if is_email else "phone number"
        return (
            f"I've sent a confirmation code to the {channel} on file "
            f"({_mask_contact(contact, is_email)}). Ask them to read it back before "
            "you cancel or reschedule."
        )
    except RuntimeError as e:
        log.error("request_cancellation_code: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception:
        log.exception("request_cancellation_code failed")
        return _CALENDAR_FALLBACK


@tool
def cancel_appointment(event_id: str, confirmation_code: str, config: RunnableConfig = None) -> str:
    """Cancel an existing appointment by its event id (from find_booking).

    Requires confirmation_code from request_cancellation_code, read back by the
    caller — call request_cancellation_code first and never skip straight to
    canceling on a contact's say-so alone.
    """
    try:
        tenant_id = _tenant_from_config(config)
        cfg = load_tenant(tenant_id)
        service = _get_calendar_service(tenant_id)
        cal_id = cfg.calendar_id
        event_id = _resolve_event_id(service, cal_id, event_id) or event_id
        try:
            event = (
                service.events().get(calendarId=cal_id, eventId=event_id).execute()
            )
        except Exception:
            return "I couldn't find that booking — it may have already been canceled."

        ok, msg = _verify_and_consume_code(service, cal_id, event, confirmation_code)
        if not ok:
            return msg

        when = _friendly_when(event.get("start", {}).get("dateTime", ""))
        send_updates = "all" if event.get("attendees") else "none"
        _retry_calendar(
            lambda: service.events()
            .delete(calendarId=cal_id, eventId=event_id, sendUpdates=send_updates)
            .execute()
        )
        return f"Done — I've canceled your appointment for {when}."
    except RuntimeError as e:
        log.error("cancel_appointment: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception:
        log.exception("cancel_appointment failed")
        return _CALENDAR_FALLBACK


@tool
def reschedule_appointment(
    event_id: str,
    new_start_time: str,
    new_end_time: str,
    confirmation_code: str,
    config: RunnableConfig = None,
) -> str:
    """Move an existing appointment to a new time.

    Args:
        event_id: The booking's event id (from find_booking).
        new_start_time: ISO 8601 start with timezone, e.g. '2026-05-29T10:00:00-04:00'.
        new_end_time:   ISO 8601 end with timezone.
        confirmation_code: From request_cancellation_code, read back by the caller.

    Always confirm the new time with the caller before calling this. Call
    request_cancellation_code first — never skip straight to rescheduling on a
    contact's say-so alone.
    """
    try:
        tenant_id = _tenant_from_config(config)
        cfg = load_tenant(tenant_id)

        closed_msg = _closed_day_message(new_start_time, cfg.business_days)
        if closed_msg:
            return closed_msg
        hours_msg = _closed_hours_message(new_start_time, new_end_time, cfg.business_hours, cfg.business_tz)
        if hours_msg:
            return hours_msg

        service = _get_calendar_service(tenant_id)
        cal_id = cfg.calendar_id
        event_id = _resolve_event_id(service, cal_id, event_id) or event_id
        try:
            event = (
                service.events().get(calendarId=cal_id, eventId=event_id).execute()
            )
        except Exception:
            return (
                "I couldn't find that booking — it may have been canceled. "
                "Want me to book a new time?"
            )

        ok, msg = _verify_and_consume_code(service, cal_id, event, confirmation_code)
        if not ok:
            return msg

        if not _slot_still_free(service, new_start_time, new_end_time, cfg.business_tz, cal_id):
            return "That new time isn't available — could you pick another slot?"

        event["start"] = {"dateTime": new_start_time, "timeZone": cfg.business_tz}
        event["end"] = {"dateTime": new_end_time, "timeZone": cfg.business_tz}
        send_updates = "all" if event.get("attendees") else "none"
        _retry_calendar(
            lambda: service.events()
            .update(calendarId=cal_id, eventId=event_id, body=event, sendUpdates=send_updates)
            .execute()
        )

        # Re-confirm by SMS for voice (phone) bookings.
        desc = event.get("description", "")
        if "Caller contact:" in desc and not event.get("attendees"):
            phone = desc.split("Caller contact:", 1)[1].strip()
            if any(c.isdigit() for c in phone):
                _send_sms_confirmation(phone, _friendly_when(new_start_time), tenant_id)

        return f"Done — I've moved your appointment to {_friendly_when(new_start_time)}."
    except RuntimeError as e:
        log.error("reschedule_appointment: calendar not configured: %s", e)
        return _CALENDAR_FALLBACK
    except Exception:
        log.exception("reschedule_appointment failed")
        return _CALENDAR_FALLBACK


# ── Tool: escalate_to_human ───────────────────────────────────────────────
@tool
def escalate_to_human(reason: str, user_summary: str, config: RunnableConfig = None) -> str:
    """Notify the team that a lead needs human follow-up.

    Call this when:
    - You searched the knowledge base twice and still cannot answer accurately.
    - The user mentions budget, timeline, or urgency ("ready to start", "ASAP", "need this soon").
    - The user expresses frustration or explicitly asks to speak with a person.

    Args:
        reason: Short label, e.g. "hot lead — mentioned budget" or "out of scope question".
            For a visitor who wants Esmi for their own business, use
            "New Esmi Lead: [name] — [business type]" (omit "— [business type]" if
            unknown) — this exact format becomes the email subject verbatim.
        user_summary: 2-3 sentences summarising what the user needs.

    Returns a normal confirmation string on success. On failure the result starts
    with "ESCALATION_FAILED:" — if you see that prefix, the team was NOT notified:
    apologize, do not tell the user someone will follow up, and offer a direct
    contact instead.
    """
    try:
        tenant_id = _tenant_from_config(config)
        cfg = load_tenant(tenant_id)
        api_key = _get_sendgrid_key(tenant_id)
        if not api_key:
            log.error("Escalation email NOT sent — no SendGrid key configured for tenant '%s'.", tenant_id)
            return (
                "ESCALATION_FAILED: the team was NOT notified (no email configured). "
                "Apologize to the user, do not promise a follow-up, and offer a direct "
                "contact (phone/email) instead."
            )

        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        # Hot-lead-for-Esmi-itself escalations carry their own pre-formatted
        # subject (reason == "New Esmi Lead: ..."); other escalations keep the
        # "[Esmi Escalation]" prefix.
        if reason.strip().lower().startswith("new esmi lead"):
            subject = html.escape(reason)
        else:
            subject = f"[Esmi Escalation] {html.escape(reason)}"

        # thread_id/tenant_id let a human look the conversation up (checkpointer
        # table or Railway logs); lead_score is only present in multi-agent mode
        # (injected via graph._with_lead_score) — single-agent mode doesn't track
        # it, so the row is omitted rather than showing a misleading 0.
        configurable = (config or {}).get("configurable") or {}
        thread_id = configurable.get("thread_id")
        lead_score = configurable.get("lead_score")
        extra_rows = ""
        if lead_score is not None:
            extra_rows += (
                '<tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;'
                'padding:10px 0;">Lead Score</td>'
                f'<td style="color:#0A2540;">{html.escape(str(lead_score))}/100</td></tr>'
            )
        if thread_id:
            extra_rows += (
                '<tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;'
                'padding:10px 0;">Conversation</td>'
                f'<td style="color:#0A2540;">{html.escape(tenant_id)} / {html.escape(str(thread_id))}</td></tr>'
            )

        html_content = f"""
        <div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: linear-gradient(135deg, #0A2540, #0e3460);
                        padding: 24px 28px; border-radius: 12px 12px 0 0;">
                <h1 style="color: #ffffff; margin: 0; font-size: 20px;">🚨 Esmi Escalation</h1>
                <p style="color: #00D4EE; margin: 4px 0 0; font-size: 12px;
                           letter-spacing: 0.06em; text-transform: uppercase;">
                    {html.escape(cfg.company_name)} — Action Required
                </p>
            </div>
            <div style="background: #f8f9fa; padding: 28px; border: 1px solid #e2e8f0;
                        border-radius: 0 0 12px 12px;">
                <table style="width: 100%; border-collapse: collapse; margin-top: 4px;">
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;width:140px;">Reason</td>
                        <td style="color:#0A2540;font-weight:600;">{html.escape(reason)}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Time</td>
                        <td style="color:#0A2540;">{ts}</td></tr>
                    <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;vertical-align:top;">Summary</td>
                        <td style="color:#0A2540;">{html.escape(user_summary)}</td></tr>
                    {extra_rows}
                </table>
            </div>
        </div>
        """

        message = Mail(
            from_email=cfg.email_from,
            to_emails=cfg.email_escalation_to,
            subject=subject,
            html_content=html_content,
        )
        SendGridAPIClient(api_key).send(message)
        log.info(f"Escalation email sent: {reason}")
    except Exception as e:
        log.error(f"Escalation email FAILED to send: {e}")
        return (
            "ESCALATION_FAILED: the team was NOT notified (send error). "
            "Apologize to the user, do not promise a follow-up, and offer a direct "
            "contact (phone/email) instead."
        )

    return "I've flagged this for our team and someone will follow up with you shortly."


# ── Eager KB index warm-up (best-effort) ─────────────────────────────────
try:
    _get_kb_index()
except Exception as _e:  # never crash import
    log.warning(f"KB warm-up skipped: {_e}")

print("✅ Tools loaded (Phase 1: persisted KB + idempotent booking).")
