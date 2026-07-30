# platform_api/usage_alerts.py — soft usage-limit warning emails (Phase 3 ticket 3.4).
#
# Triggered from platform_api/call_log.py right after a call is safely
# logged — voice minutes only change when a call ends, so that's the natural
# moment to recompute usage and check plan.status. Fail-soft throughout: any
# error here is logged and swallowed, never breaks the VAPI webhook response.
#
# Dedup: usage_notifications (tenant_id, period_start, threshold) has a hard
# PRIMARY KEY — INSERT ... ON CONFLICT DO NOTHING before sending means even
# concurrent webhook deliveries can send at most one email per tenant per
# period per threshold. managed (unlimited) plans never reach here:
# compute_tenant_usage()'s plan.status is None for them.

from __future__ import annotations

import html
import logging
from datetime import date

from platform_api.usage import compute_tenant_usage
from tenants import load_tenant, tenant_secret

log = logging.getLogger(__name__)

_SUBJECTS = {
    "approaching": "Approaching your included voice minutes — {company}",
    "over": "You've used all your included voice minutes — {company}",
}
_STATUS_LABELS = {
    "approaching": "Approaching limit",
    "over": "Over included minutes",
}


def _sendgrid_key(tenant_id: str) -> str | None:
    """Duplicated from tools._get_sendgrid_key (not imported): platform_api
    must never depend on tools.py/agents.py/graph.py — api.py mounting both
    is the only coupling between the agent runtime and the control plane
    (see platform_api/__init__.py). Both read the same tenant_secret() env
    convention, so this stays in sync by construction, not by copy-paste luck.
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
            log.warning("SENDGRID_API_KEY_B64 decode failed: %s", e)
    return None


def _try_reserve_notification(tenant_id: str, period_start: date, threshold: str) -> bool:
    """Atomically claim the right to send this tenant/period/threshold email.

    Returns True only if this call actually inserted the row — i.e. no one
    has sent this exact email yet this billing period. False on DB
    unavailable (fail closed on sending, not on the webhook).
    """
    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        return False
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO usage_notifications (tenant_id, period_start, threshold)
                VALUES (:tid, :period_start, :threshold)
                ON CONFLICT (tenant_id, period_start, threshold) DO NOTHING
                """
            ),
            {"tid": tenant_id, "period_start": period_start, "threshold": threshold},
        )
    return result.rowcount == 1


def _send_usage_email(tenant_id: str, data: dict, status: str) -> None:
    cfg = load_tenant(tenant_id)
    api_key = _sendgrid_key(tenant_id)
    if not api_key:
        log.warning(
            "Usage alert NOT sent for tenant %s (%s) — no SendGrid key configured.",
            tenant_id, status,
        )
        return

    plan = data["plan"]

    # Tenant's own escalation address, plus an optional internal Orchelix
    # copy — deduped for the default tenant itself, where they're the same.
    to_addrs = [cfg.email_escalation_to]
    default_escalation_to = load_tenant("default").email_escalation_to
    if default_escalation_to and default_escalation_to not in to_addrs:
        to_addrs.append(default_escalation_to)

    subject = _SUBJECTS[status].format(company=cfg.company_name)
    status_label = _STATUS_LABELS[status]

    html_content = f"""
    <div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: linear-gradient(135deg, #0A2540, #0e3460);
                    padding: 24px 28px; border-radius: 12px 12px 0 0;">
            <h1 style="color: #ffffff; margin: 0; font-size: 20px;">Esmi Usage Notice</h1>
            <p style="color: #00D4EE; margin: 4px 0 0; font-size: 12px;
                       letter-spacing: 0.06em; text-transform: uppercase;">
                {html.escape(cfg.company_name)}
            </p>
        </div>
        <div style="background: #f8f9fa; padding: 28px; border: 1px solid #e2e8f0;
                    border-radius: 0 0 12px 12px;">
            <table style="width: 100%; border-collapse: collapse; margin-top: 4px;">
                <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;width:160px;">Plan</td>
                    <td style="color:#0A2540;font-weight:600;">{html.escape(plan['label'])}</td></tr>
                <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Minutes used</td>
                    <td style="color:#0A2540;">{data['minutes']:.1f} of {plan['included_minutes']} min ({plan['percent_used']}%)</td></tr>
                <tr><td style="color:#94a3b8;text-transform:uppercase;font-size:12px;padding:10px 0;">Status</td>
                    <td style="color:#0A2540;font-weight:600;">{html.escape(status_label)}</td></tr>
            </table>
            <p style="color:#334155;margin-top:20px;line-height:1.5;">
                This is a usage notice only — <strong>Esmi keeps answering every call</strong>,
                nothing is blocked. If you'd like to discuss your plan, reply to this email
                or reach us at <a href="mailto:info@orchelix.com">info@orchelix.com</a>.
            </p>
        </div>
    </div>
    """

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=cfg.email_from,
        to_emails=to_addrs,
        subject=subject,
        html_content=html_content,
    )
    SendGridAPIClient(api_key).send(message)
    log.info("Usage alert email sent: tenant=%s status=%s to=%s", tenant_id, status, to_addrs)


def check_and_notify_usage(tenant_id: str) -> None:
    """After a call is logged, check whether this tenant just crossed a soft
    usage threshold (approaching/over) and send a one-time warning email.

    Fail-soft: any error here is logged and swallowed. Called from
    call_log.record_end_of_call() after the call row is safely stored — must
    never affect the 200 the VAPI webhook route returns regardless.
    """
    try:
        data = compute_tenant_usage(tenant_id)
        status = data["plan"].get("status")
        if status not in ("approaching", "over"):
            return  # ok, or managed/unlimited (status is None) — nothing to do

        period_start = date.fromisoformat(data["period_start"])
        if not _try_reserve_notification(tenant_id, period_start, status):
            return  # already sent this tenant/period/threshold

        _send_usage_email(tenant_id, data, status)
    except Exception:
        log.exception("Usage alert check failed for tenant %s — no email sent.", tenant_id)
