# platform_api/signup.py — self-serve onboarding entry point (Phase 4 ticket
# 4.1, stage 2). Three routes:
#
#   GET  /platform/signup/slug-check   suggest + validate a tenant slug
#   POST /platform/signup              create the draft tenant + provisioning job
#   POST /platform/signup/{tid}/clerk-org   record the Clerk org (call 2)
#
# WHY TWO CALLS: the Clerk organization must be created with slug == tenant_id,
# and CLERK_SECRET_KEY lives on Vercel, not Railway. So the backend reserves
# the slug and seeds config (call 1), the Next.js route handler then creates
# the Clerk org with that exact slug, and reports the org id back (call 2).
# Putting CLERK_SECRET_KEY on Railway would collapse this into one call but
# spreads a high-value secret to a second service — deliberately not done.
#
# SECURITY: the browser never reaches these. The Next.js proxy injects
# X-Platform-Secret server-side (same PLATFORM_API_SECRET as every other
# /platform route) and forwards the Clerk user id in X-Platform-User. What is
# different from every other /platform route is that there is no X-Tenant-Id
# yet — the whole point is that the tenant doesn't exist — so require_tenant()
# is deliberately NOT used here; require_signup_user() takes its place as the
# thing that must be present.
#
# The created tenant is NOT live: onboarding_status starts at 'submitted' and
# only an admin approval moves it to 'active' (tenants.tenant_is_active gates
# voice, chat, and booking on that). plan starts at 'managed' regardless of
# what they asked for; requested_plan records the ask, and an admin confirms
# the real plan at approval time.

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from platform_api import provisioning as prov
from platform_api.plans import PLANS
from platform_api.security import verify_platform_secret
from tenants import _REGISTRY_DIR, clear_tenant_cache, normalize_tenant_id, tenant_secret

log = logging.getLogger(__name__)

router = APIRouter()

# Plan every self-serve tenant starts on, regardless of requested_plan.
# 'managed' is the unlimited/no-soft-limit catch-all (platform_api/plans.py),
# so a pending tenant can never trip a usage warning email for a plan nobody
# has actually agreed to. The admin assigns the real plan at approval.
INITIAL_PLAN = "managed"
# Billing lifecycle at signup. Set explicitly: the tenants.status column
# defaults to 'live' (alembic 0001), which would be wrong for a tenant that
# hasn't been approved or billed.
INITIAL_STATUS = "trial"
INITIAL_ONBOARDING_STATUS = "submitted"

_MAX_NAME_LEN = 200
_MAX_EMAIL_LEN = 254
_MAX_PHONE_LEN = 32
_SLUG_MAX_LEN = 48  # leaves room for a "-99" collision suffix under the 64 cap
_MAX_SLUG_ATTEMPTS = 100

_SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

# Slugs that must never become a tenant id: Orchelix's own tenant and staff
# org, plus path segments that would be confusing or collide with dashboard
# routes if they showed up as an org slug.
RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        "default",
        "orchelix",
        "orchelix-ai-consulting",
        "admin",
        "api",
        "platform",
        "dashboard",
        "signup",
        "sign-up",
        "login",
        "health",
        "webhooks",
        "static",
        "assets",
        "www",
        "app",
        "new",
        "test",
    }
)

# Per-user signup throttle. Keyed on the Clerk user id, not IP: every request
# arrives from Vercel's egress, so an IP-keyed limit would lump all users into
# one bucket (the same problem api._rate_limit_key exists to solve for /chat).
# In-process, so with multiple Railway replicas the effective limit multiplies
# — that's why _assert_no_pending_signup below does the real, durable check
# against the DB. This just blunts a hot loop.
_SIGNUP_WINDOW_SEC = 3600.0
_SIGNUP_MAX_PER_WINDOW = 3
_signup_hits: dict[str, list[float]] = {}
_signup_lock = threading.Lock()


class SignupRequest(BaseModel):
    company_name: str
    contact_email: str
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    business_tz: str = "America/Toronto"
    requested_plan: Optional[str] = None
    # Optional explicit slug from the wizard. The tenant id is permanent and
    # must equal the Clerk org slug, so the wizard shows it and lets the user
    # correct it before submitting. Omitted -> derived from company_name.
    tenant_id: Optional[str] = Field(default=None, max_length=64)


class ClerkOrgRequest(BaseModel):
    clerk_org_id: Optional[str] = None
    # Set instead of clerk_org_id when org creation failed on the Vercel side,
    # so the step lands as `failed` in the admin queue rather than silently
    # sitting at `pending` forever.
    error: Optional[str] = None


# ── helpers ───────────────────────────────────────────────────────────────────


def require_signup_user(request: Request) -> str:
    """The Clerk user id behind this signup, from X-Platform-User.

    Every other /platform route treats this header as a best-effort audit
    breadcrumb that defaults to "dashboard". Here it is REQUIRED: it is the
    only identity attached to a signup, and it is what the per-user throttle
    and the one-pending-signup rule key on. A missing header means the Next.js
    proxy is misconfigured, which must fail loudly rather than create
    unattributable tenants.
    """
    user = (request.headers.get("X-Platform-User") or "").strip()
    if not user:
        log.warning("Rejected signup: X-Platform-User header missing.")
        raise HTTPException(
            status_code=400, detail="X-Platform-User header is required for signup"
        )
    return user[:200]


def _throttle(user_id: str) -> None:
    now = time.monotonic()
    with _signup_lock:
        hits = [t for t in _signup_hits.get(user_id, []) if now - t < _SIGNUP_WINDOW_SEC]
        if len(hits) >= _SIGNUP_MAX_PER_WINDOW:
            log.warning("Throttled signup for user %s (%d in window).", user_id, len(hits))
            raise HTTPException(
                status_code=429,
                detail="Too many signup attempts — try again later or contact Orchelix.",
            )
        hits.append(now)
        _signup_hits[user_id] = hits


def slugify(company_name: str) -> str:
    """Company name -> candidate tenant slug matching tenants._TENANT_ID_RE."""
    s = (company_name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:_SLUG_MAX_LEN].strip("-")
    return s


def _slug_taken(conn, slug: str) -> bool:
    """True if slug is reserved, on disk, or already a row in tenants.

    All three sources are checked because they are three independent
    namespaces that must not collide: a self-serve signup must never be able
    to claim 'otro-nivel' (on disk), 'default' (reserved), or another
    self-serve tenant's slug (DB).
    """
    from sqlalchemy import text

    if slug in RESERVED_SLUGS:
        return True
    if (_REGISTRY_DIR / slug).is_dir():
        return True
    row = conn.execute(
        text("SELECT 1 FROM tenants WHERE id = :tid"), {"tid": slug}
    ).first()
    return row is not None


def allocate_slug(conn, base: str) -> str:
    """First free slug of base, base-2, base-3, ... Raises 400 if none."""
    if not base:
        raise HTTPException(
            status_code=400,
            detail="Could not derive a URL-safe id from that business name — "
            "please choose one explicitly.",
        )
    if not _slug_taken(conn, base):
        return base
    for n in range(2, _MAX_SLUG_ATTEMPTS + 1):
        candidate = f"{base[: _SLUG_MAX_LEN - len(str(n)) - 1]}-{n}"
        if not _slug_taken(conn, candidate):
            return candidate
    raise HTTPException(
        status_code=409, detail="Could not allocate a unique id — please choose one."
    )


def _validate_signup(body: SignupRequest) -> None:
    name = (body.company_name or "").strip()
    if not name or len(name) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail="company_name must be 1-200 characters")

    email = (body.contact_email or "").strip()
    if not email or len(email) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="contact_email must be a valid email address")

    if body.contact_phone and len(body.contact_phone.strip()) > _MAX_PHONE_LEN:
        raise HTTPException(status_code=400, detail="contact_phone is too long")
    if body.contact_name and len(body.contact_name.strip()) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail="contact_name is too long")

    # Validated here rather than at booking time: business_tz feeds every
    # calendar computation in tools.py, and a bad zone would surface as an
    # opaque runtime error on the tenant's first booking instead of a clear
    # 400 on the form field that caused it.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo((body.business_tz or "").strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise HTTPException(
            status_code=400,
            detail=f"business_tz '{body.business_tz}' is not a known IANA timezone "
            "(e.g. America/Toronto).",
        )

    if body.requested_plan is not None and body.requested_plan not in PLANS:
        raise HTTPException(
            status_code=400,
            detail=f"requested_plan must be one of: {', '.join(sorted(PLANS))}",
        )


def seed_config(body: SignupRequest, tenant_id: str) -> dict:
    """Build the tenant_configs v1 JSON from the signup fields.

    Shape matches tenants._config_from_file exactly (it is the same parser
    load_tenant uses). Two details that matter:

    - `pricing: []` is EXPLICIT, not omitted. A missing key inherits the
      default tenant's pricing, which would have this business's agent quoting
      Orchelix's own SaaS packages to its customers. See the matching comment
      in tenants._config_from_file.
    - `emails.from` is intentionally absent so it inherits Orchelix's verified
      SendGrid sender. Only booking_to / escalation_to are tenant-specific —
      the same split platform_api/config.py enforces for self-serve edits.
    """
    contact_email = body.contact_email.strip()
    return {
        "company_name": body.company_name.strip(),
        "business_tz": body.business_tz.strip(),
        "business_hours": [9, 17],
        "business_days": [0, 1, 2, 3, 4],
        "emails": {"booking_to": contact_email, "escalation_to": contact_email},
        "pricing": [],
        "pricing_note": (
            "We haven't published a price list here yet — I can take your details "
            "and have someone confirm exact pricing for you."
        ),
        "pricing_note_es": (
            "Todavía no tenemos la lista de precios publicada aquí — puedo tomar tus "
            "datos y que alguien te confirme el precio exacto."
        ),
        "services": {},
        "greeting": "",
        "transfer_phone": (body.contact_phone or "").strip(),
        # Voice Studio defaults (see tenants.py TenantConfig.voice_id/speed/
        # language_pref) — explicit here for the same reason pricing: [] is
        # explicit above: a missing key would inherit the default tenant's
        # dataclass default, which happens to be the same values today, but
        # relying on that coincidence is exactly the kind of silent inheritance
        # this file's other comments warn against.
        "voice_id": "",
        "speed": 1.0,
        "language_pref": "auto",
    }


# Onboarding states that count as "this user already has an application in
# flight". Rejected and active are excluded: someone who was declined can try
# again, and an existing customer can legitimately onboard a second business.
PENDING_SIGNUP_STATUSES = ("draft", "submitted", "provisioning", "review")


def pending_signup_tenant_id(conn, user_id: str) -> Optional[str]:
    """The tenant id of this user's in-flight application, or None.

    Shared by _assert_no_pending_signup (the write guard) and GET
    /platform/signup/mine (the resume lookup) so the two can never disagree
    about what "already applied" means — a drift there would show the wizard a
    "start fresh" screen that then 409s on submit.
    """
    from sqlalchemy import text

    row = conn.execute(
        text(
            """
            SELECT t.id FROM tenants t
            JOIN provisioning_jobs j ON j.tenant_id = t.id
            WHERE j.created_by = :uid
              AND t.onboarding_status = ANY(:statuses)
            ORDER BY j.created_at DESC
            LIMIT 1
            """
        ),
        {"uid": user_id, "statuses": list(PENDING_SIGNUP_STATUSES)},
    ).first()
    return row[0] if row is not None else None


def _assert_no_pending_signup(conn, user_id: str) -> None:
    """One in-flight signup per Clerk user.

    The durable half of the abuse controls (the in-process throttle above
    doesn't survive a restart or span replicas).
    """
    existing = pending_signup_tenant_id(conn, user_id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"You already have a business ('{existing}') waiting for approval. "
            "Contact Orchelix if you need to add another.",
        )


def _engine_or_503():
    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")
    return engine


def _sendgrid_key(tenant_id: str) -> Optional[str]:
    """Duplicated from tools._get_sendgrid_key / usage_alerts._sendgrid_key
    (not imported): platform_api must never depend on tools.py/agents.py/
    graph.py. Both read the same tenant_secret() env convention, so this stays
    in sync by construction, not by copy-paste luck."""
    key = tenant_secret(tenant_id, "SENDGRID_API_KEY")
    if key:
        return key
    key_b64 = tenant_secret(tenant_id, "SENDGRID_API_KEY_B64")
    if key_b64:
        try:
            import base64
            return base64.b64decode(key_b64).decode("utf-8")
        except Exception as e:
            log.warning("SENDGRID_API_KEY_B64 decode failed: %s", e)
    return None


def _notify_ops_new_signup(tenant_id: str, body: SignupRequest, user_id: str) -> None:
    """Email Orchelix ops when a new self-serve application arrives, so the
    admin onboarding queue isn't the only way to find out one exists.

    Fail-soft: never raises. The signup response the applicant is waiting on
    must not depend on SendGrid being configured or reachable.
    """
    try:
        from tenants import load_tenant

        api_key = _sendgrid_key("default")
        if not api_key:
            log.info(
                "New-signup email NOT sent for %s — no SendGrid key configured.",
                tenant_id,
            )
            return
        default_cfg = load_tenant("default")
        to_addr = default_cfg.email_escalation_to
        if not to_addr:
            return

        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        html_content = f"""
        <p>New Esmi self-serve application:</p>
        <ul>
          <li><strong>Business:</strong> {html.escape(body.company_name)}</li>
          <li><strong>Tenant id:</strong> {html.escape(tenant_id)}</li>
          <li><strong>Contact:</strong> {html.escape(body.contact_name or '—')}
              &lt;{html.escape(body.contact_email)}&gt;</li>
          <li><strong>Phone:</strong> {html.escape(body.contact_phone or '—')}</li>
          <li><strong>Requested plan:</strong> {html.escape(body.requested_plan or 'not specified')}</li>
          <li><strong>Clerk user:</strong> {html.escape(user_id)}</li>
        </ul>
        <p><a href="https://www.orchelix.com/dashboard/admin/onboarding">Review in the admin onboarding queue</a></p>
        """
        message = Mail(
            from_email=default_cfg.email_from,
            to_emails=to_addr,
            subject=f"New Esmi signup: {body.company_name} ({tenant_id})",
            html_content=html_content,
        )
        SendGridAPIClient(api_key).send(message)
        log.info("New-signup email sent: tenant=%s to=%s", tenant_id, to_addr)
    except Exception:
        log.exception(
            "New-signup email failed for tenant=%s — signup still recorded.", tenant_id
        )


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/platform/signup/mine")
def signup_mine(request: Request) -> dict:
    """This user's in-flight application, for resume + the pending screen.

    Why this exists: the signup sequence is three calls (reserve tenant ->
    create Clerk org -> record it), and a failure at step 2 or 3 leaves a real
    tenant row behind. Retrying the wizard would then hit
    _assert_no_pending_signup's 409 with no way forward. This is the recovery
    path — it tells the wizard "you already applied, here's where it stopped".

    DELIBERATELY NOT the admin shape (platform_api/onboarding.py's
    _tenant_out): the per-step `detail` blobs carry internal operator notes
    ("buy a local number... it costs money") and `error` carries raw upstream
    messages. Neither belongs in front of the customer. This returns a coarse
    resolved/total count plus the one flag the wizard actually acts on.
    """
    verify_platform_secret(request)
    user_id = require_signup_user(request)

    engine = _engine_or_503()

    from sqlalchemy import text

    with engine.connect() as conn:
        tenant_id = pending_signup_tenant_id(conn, user_id)
        if tenant_id is None:
            return {"tenant": None, "can_start_new": True, "needs_clerk_org": False}

        row = conn.execute(
            text(
                """
                SELECT id, company_name, business_tz, onboarding_status,
                       requested_plan, clerk_org_id, contact_name, contact_email,
                       contact_phone, submitted_at, rejected_reason
                FROM tenants WHERE id = :tid
                """
            ),
            {"tid": tenant_id},
        ).mappings().first()
        if row is None:  # raced with a delete; treat as "no application"
            return {"tenant": None, "can_start_new": True, "needs_clerk_org": False}

        job = prov.latest_job(conn, tenant_id)

    steps = (job or {}).get("steps") or []
    unresolved = set(prov.unresolved_steps(steps))
    # The resume trigger: the org was never created (or creation failed), so
    # the wizard should re-run steps 2-3 rather than starting over.
    needs_clerk_org = not row["clerk_org_id"] and prov.STEP_CLERK_ORG in unresolved

    return {
        "tenant": {
            "tenant_id": row["id"],
            "company_name": row["company_name"],
            "business_tz": row["business_tz"],
            "onboarding_status": row["onboarding_status"],
            "requested_plan": row["requested_plan"],
            "clerk_org_id": row["clerk_org_id"],
            "contact_name": row["contact_name"],
            "contact_email": row["contact_email"],
            "contact_phone": row["contact_phone"],
            "submitted_at": row["submitted_at"].isoformat() if row["submitted_at"] else None,
            "rejected_reason": row["rejected_reason"],
        },
        "job_status": (job or {}).get("status"),
        "steps_total": len(steps),
        "steps_resolved": len(steps) - len(unresolved),
        # Mirrors _assert_no_pending_signup exactly (same helper) — if this
        # said True while the write guard disagreed, the wizard would offer a
        # fresh start that 409s on submit.
        "can_start_new": False,
        "needs_clerk_org": needs_clerk_org,
    }


@router.get("/platform/signup/slug-check")
def signup_slug_check(request: Request, company_name: str = "", slug: str = "") -> dict:
    """Suggest a slug for a company name and/or report whether one is free.

    The wizard needs this because tenant_id is permanent and must equal the
    Clerk org slug — the user has to see it and be able to fix it before
    submitting, not discover a "-2" suffix after the fact.
    """
    verify_platform_secret(request)
    require_signup_user(request)

    engine = _engine_or_503()
    requested = (slug or "").strip().lower()
    suggestion_base = requested or slugify(company_name)

    with engine.connect() as conn:
        if requested:
            valid = bool(_SLUG_RE.match(requested)) and requested == normalize_tenant_id(requested)
            available = valid and not _slug_taken(conn, requested)
            return {
                "slug": requested,
                "valid": valid,
                "available": available,
                "suggestion": allocate_slug(conn, slugify(company_name) or requested)
                if not available
                else requested,
            }
        return {
            "slug": None,
            "valid": bool(suggestion_base),
            "available": None,
            "suggestion": allocate_slug(conn, suggestion_base) if suggestion_base else None,
        }


@router.post("/platform/signup", status_code=201)
def platform_signup(body: SignupRequest, request: Request) -> dict:
    """Create a draft tenant + provisioning job. Runs the two backend-side
    automated steps (tenant_row, config_seed) in ONE transaction.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy, same
    convention as every other /platform route.
    """
    verify_platform_secret(request)
    user_id = require_signup_user(request)
    _validate_signup(body)
    _throttle(user_id)

    engine = _engine_or_503()

    requested_slug = (body.tenant_id or "").strip().lower()
    if requested_slug:
        if not _SLUG_RE.match(requested_slug) or requested_slug != normalize_tenant_id(
            requested_slug
        ):
            raise HTTPException(
                status_code=400,
                detail="tenant_id must be 1-64 characters of lowercase letters, "
                "numbers, and hyphens.",
            )

    config = seed_config(body, "pending")

    # Validate the seed parses into a real TenantConfig BEFORE writing it —
    # same guard platform_api/config.py applies to self-serve edits, for the
    # same reason: a bad config must fail here, not at agent runtime.
    from sqlalchemy import text

    from tenants import _config_from_file

    with engine.begin() as conn:
        _assert_no_pending_signup(conn, user_id)

        if requested_slug:
            if _slug_taken(conn, requested_slug):
                raise HTTPException(
                    status_code=409,
                    detail=f"The id '{requested_slug}' is already taken — "
                    f"try '{allocate_slug(conn, requested_slug)}'.",
                )
            tenant_id = requested_slug
        else:
            tenant_id = allocate_slug(conn, slugify(body.company_name))

        try:
            _config_from_file(tenant_id, config)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Seed config is invalid: {e}")

        # ── step: tenant_row ──────────────────────────────────────────────
        # Plain INSERT, not ON CONFLICT: _slug_taken already ran inside this
        # transaction, and a genuine race should surface as a 409 rather than
        # silently overwriting somebody else's tenant.
        conn.execute(
            text(
                """
                INSERT INTO tenants
                    (id, status, plan, company_name, business_tz,
                     onboarding_status, contact_name, contact_email, contact_phone,
                     requested_plan, submitted_at)
                VALUES
                    (:id, :status, :plan, :company_name, :business_tz,
                     :onboarding_status, :contact_name, :contact_email, :contact_phone,
                     :requested_plan, now())
                """
            ),
            {
                "id": tenant_id,
                "status": INITIAL_STATUS,
                "plan": INITIAL_PLAN,
                "company_name": body.company_name.strip(),
                "business_tz": body.business_tz.strip(),
                "onboarding_status": INITIAL_ONBOARDING_STATUS,
                "contact_name": (body.contact_name or "").strip() or None,
                "contact_email": body.contact_email.strip(),
                "contact_phone": (body.contact_phone or "").strip() or None,
                "requested_plan": body.requested_plan,
            },
        )

        job_id = prov.create_job(conn, tenant_id, created_by=user_id)
        prov.set_step(
            conn, job_id, prov.STEP_TENANT_ROW, prov.STEP_DONE,
            detail={"tenant_id": tenant_id}, updated_by=user_id,
        )

        # ── step: config_seed ─────────────────────────────────────────────
        conn.execute(
            text(
                "INSERT INTO tenant_configs (tenant_id, version, config, published, created_by) "
                "VALUES (:tid, 1, :config, true, :by)"
            ),
            {"tid": tenant_id, "config": json.dumps(config), "by": f"signup:{user_id}"},
        )
        prov.set_step(
            conn, job_id, prov.STEP_CONFIG_SEED, prov.STEP_DONE,
            detail={"version": 1}, updated_by=user_id,
        )

        job_status = prov.recompute_job_status(conn, job_id)
        steps = prov.job_steps(conn, job_id)

    # The slug-check above may have cached "this tenant does not exist" for up
    # to 60s. Without this the tenant's own dashboard would 400 on every
    # /platform route until that entry expired.
    clear_tenant_cache(tenant_id)

    log.info(
        "Signup: tenant=%s job=%s by=%s requested_plan=%s (plan=%s until approval)",
        tenant_id, job_id, user_id, body.requested_plan, INITIAL_PLAN,
    )
    _notify_ops_new_signup(tenant_id, body, user_id)

    return {
        "tenant_id": tenant_id,
        "job_id": job_id,
        "onboarding_status": INITIAL_ONBOARDING_STATUS,
        "job_status": job_status,
        "plan": INITIAL_PLAN,
        "requested_plan": body.requested_plan,
        "steps": steps,
        # The caller (the Next.js route handler) must now create the Clerk org
        # with EXACTLY this slug and POST it back to the clerk-org route.
        "next": {
            "action": "create_clerk_org",
            "slug": tenant_id,
            "url": f"/platform/signup/{tenant_id}/clerk-org",
        },
    }


@router.post("/platform/signup/{tenant_id}/clerk-org")
def platform_signup_clerk_org(tenant_id: str, body: ClerkOrgRequest, request: Request) -> dict:
    """Record the Clerk organization created for this tenant (signup call 2).

    Idempotent: re-posting the SAME org id is a no-op success, so a retried
    Next.js request can't fail the flow. A DIFFERENT org id is a 409 — the
    clerk-slug-equals-tenant-id convention means a tenant has exactly one org,
    and silently repointing it would break every dashboard request for that
    tenant.
    """
    verify_platform_secret(request)
    user_id = require_signup_user(request)

    tid = normalize_tenant_id(tenant_id)
    if tid != (tenant_id or "").strip().lower():
        raise HTTPException(status_code=400, detail=f"Unknown tenant '{tenant_id}'")

    org_id = (body.clerk_org_id or "").strip()
    if not org_id and not body.error:
        raise HTTPException(
            status_code=400, detail="Provide either clerk_org_id or error."
        )
    if org_id and not org_id.startswith("org_"):
        # Same light, format-only validation admin.py applies to Stripe ids —
        # this app makes no Clerk API calls, so there is nothing to verify against.
        raise HTTPException(status_code=400, detail="clerk_org_id must start with 'org_'")

    engine = _engine_or_503()

    from sqlalchemy import text

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT clerk_org_id, onboarding_status FROM tenants WHERE id = :tid"
            ),
            {"tid": tid},
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown tenant '{tid}'")
        existing_org_id, onboarding_status = row

        job = prov.latest_job(conn, tid)
        if job is None:
            raise HTTPException(
                status_code=409, detail=f"Tenant '{tid}' has no provisioning job."
            )
        job_id = job["job_id"]

        # Failure report from the Vercel side.
        if not org_id:
            prov.set_step(
                conn, job_id, prov.STEP_CLERK_ORG, prov.STEP_FAILED,
                error=body.error[:1000], updated_by=user_id,
            )
            status = prov.recompute_job_status(conn, job_id)
            log.error("Signup: Clerk org creation failed for %s — %s", tid, body.error)
            return {
                "tenant_id": tid,
                "clerk_org_id": None,
                "job_status": status,
                "steps": prov.job_steps(conn, job_id),
            }

        if existing_org_id and existing_org_id != org_id:
            raise HTTPException(
                status_code=409,
                detail=f"Tenant '{tid}' is already linked to a different Clerk organization.",
            )

        if not existing_org_id:
            conn.execute(
                text("UPDATE tenants SET clerk_org_id = :org WHERE id = :tid"),
                {"org": org_id, "tid": tid},
            )

        prov.set_step(
            conn, job_id, prov.STEP_CLERK_ORG, prov.STEP_DONE,
            detail={"clerk_org_id": org_id}, updated_by=user_id,
        )
        job_status = prov.recompute_job_status(conn, job_id)

        # All automated work is done -> it's Orchelix's turn. Only advance
        # from a pre-review state, never drag an already-approved tenant back.
        if job_status == prov.JOB_NEEDS_REVIEW and onboarding_status in (
            "draft", "submitted", "provisioning",
        ):
            conn.execute(
                text("UPDATE tenants SET onboarding_status = 'review' WHERE id = :tid"),
                {"tid": tid},
            )
            onboarding_status = "review"

        steps = prov.job_steps(conn, job_id)

    clear_tenant_cache(tid)
    log.info("Signup: tenant=%s linked to Clerk org %s (job=%s)", tid, org_id, job_status)

    return {
        "tenant_id": tid,
        "clerk_org_id": org_id,
        "onboarding_status": onboarding_status,
        "job_status": job_status,
        "steps": steps,
        "unresolved_steps": prov.unresolved_steps(steps),
    }
