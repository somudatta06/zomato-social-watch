"""Email action — sends a P0 alert via SMTP (Gmail by default).

Why this exists:
    The brief lists "email sent" as one of the action types. This module
    closes that requirement. Sits alongside Slack / Discord / Sheets in
    the dispatcher fan-out.

Setup (Gmail, ~30 seconds):
    1. Enable 2-Step Verification on your Google account if not already.
    2. Visit https://myaccount.google.com/apppasswords and create an
       App password named "Zomato Social Watch".
    3. Paste into .env:
           SMTP_HOST=smtp.gmail.com
           SMTP_PORT=587
           SMTP_USER=your.gmail@gmail.com
           SMTP_PASS=<the 16-char app password>
           EMAIL_FROM=your.gmail@gmail.com
           EMAIL_TO=ops@example.com,founder-office@example.com
    4. Restart the server. P0 posts will trigger emails automatically.

Other SMTP providers (Mailgun, SendGrid, AWS SES, Postmark) work the
same way — just swap host/port/credentials.

Design choice:
    Stdlib `smtplib` + `email.mime` instead of a third-party SMTP lib.
    Zero new dependencies, ~150 lines, identical behaviour. Async via
    asyncio.to_thread so the call doesn't block the event loop.
"""
from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html import escape
from typing import Any

from loguru import logger

# Env
SMTP_HOST_ENV = "SMTP_HOST"
SMTP_PORT_ENV = "SMTP_PORT"
SMTP_USER_ENV = "SMTP_USER"
SMTP_PASS_ENV = "SMTP_PASS"
EMAIL_FROM_ENV = "EMAIL_FROM"
EMAIL_TO_ENV = "EMAIL_TO"
DASHBOARD_BASE_ENV = "DASHBOARD_BASE_URL"
DEFAULT_DASHBOARD_BASE = "http://localhost:8000"

CONTENT_TRUNCATE = 1500
SEND_TIMEOUT_S = 15.0

_BAND_LABEL = {"P0": "P0 — CRITICAL", "P1": "P1 — High", "P2": "P2 — Medium", "P3": "P3 — Low"}
_BAND_COLOR = {"P0": "#DC2626", "P1": "#C2410C", "P2": "#1D4ED8", "P3": "#64748B"}


def webhook_url() -> str | None:
    """Returns a non-None sentinel string if all required SMTP env vars are
    set, None otherwise. Mirrors the slack/discord webhook_url() shape so
    the dispatcher can treat all four channels the same way.
    """
    if not (os.getenv(SMTP_HOST_ENV) or "").strip():
        return None
    if not (os.getenv(SMTP_USER_ENV) or "").strip():
        return None
    if not (os.getenv(SMTP_PASS_ENV) or "").strip():
        return None
    if not (os.getenv(EMAIL_TO_ENV) or "").strip():
        return None
    return f"smtp://{os.getenv(SMTP_HOST_ENV)}"


def _truncate(text: str, n: int = CONTENT_TRUNCATE) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _topic_label(classification: dict[str, Any]) -> str:
    primary = classification.get("primary_topic")
    if primary and isinstance(primary, str):
        return primary.replace("_", " ").strip().title()
    cat = classification.get("category") or classification.get("side")
    if cat:
        return f"{cat.title()} issue"
    return "Critical Mention"


def _reasoning(cls: dict[str, Any], pri: dict[str, Any]) -> str:
    if pri.get("tripwire_override"):
        return pri.get("reason", "Tripwire override (auto-escalated by rules)")
    pieces = []
    if pri.get("reason"):
        pieces.append(pri["reason"])
    if cls.get("reasoning"):
        pieces.append(_truncate(str(cls["reasoning"]), 280))
    return " · ".join(pieces) if pieces else "P0 priority"


def _audience(cls: dict[str, Any]) -> str:
    aud = cls.get("audience") or []
    return ", ".join(str(a) for a in aud) if aud else "—"


def build_email(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    dashboard_base: str | None = None,
) -> dict[str, Any]:
    """Returns dict ready for send_email(). Pure — no I/O — so trivially
    unit-testable. {"subject": str, "html": str, "text": str}.
    """
    band = priority_breakdown.get("band") or "P0"
    color = _BAND_COLOR.get(band, "#DC2626")
    band_label = _BAND_LABEL.get(band, band)
    src = (post.get("source") or "?").lower()
    topic = _topic_label(classification)
    author = post.get("author") or "anonymous"
    url = post.get("url") or ""
    content = _truncate(post.get("content") or "", CONTENT_TRUNCATE)
    why = _reasoning(classification, priority_breakdown)
    audience = _audience(classification)
    ts = (post.get("created_at") or "")[:19].replace("T", " ") + " UTC" if post.get("created_at") else "—"

    base = (dashboard_base or os.getenv(DASHBOARD_BASE_ENV) or DEFAULT_DASHBOARD_BASE).rstrip("/")
    dashboard_url = f"{base}/inbox?q={post.get('id') or ''}"

    score = priority_breakdown.get("score")
    score_str = f"{float(score):.2f}" if isinstance(score, (int, float)) else "—"

    subject = f"[{band}] {topic} — @{author} on {src.title()}"

    # Plain-text fallback for clients that suppress HTML
    text = (
        f"{band_label} — {topic}\n\n"
        f"Author    : @{author}\n"
        f"Source    : {src.title()}\n"
        f"Posted    : {ts}\n"
        f"Score     : {score_str}\n"
        f"Route to  : {audience}\n\n"
        f"POST:\n{content}\n\n"
        f"WHY {band}:\n{why}\n\n"
        f"Open original : {url or '(no URL)'}\n"
        f"Open dashboard: {dashboard_url}\n"
    )

    # HTML version — clean, brand-coloured banner, mobile-friendly
    html = f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0F172A;">
<table cellpadding="0" cellspacing="0" border="0" align="center" style="max-width:560px;width:100%;background:#fff;border:1px solid #E2E8F0;border-radius:12px;overflow:hidden;">
<tr><td style="background:{color};color:#fff;padding:14px 20px;font-size:13px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;">
{escape(band_label)} · {escape(topic)}
</td></tr>
<tr><td style="padding:20px;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="font-size:13.5px;line-height:1.6;">
<tr><td style="color:#64748B;width:90px;">Author</td><td><strong>@{escape(author)}</strong> · {escape(src.title())}</td></tr>
<tr><td style="color:#64748B;">Posted</td><td>{escape(ts)}</td></tr>
<tr><td style="color:#64748B;">Score</td><td><code style="background:#F1F5F9;padding:2px 6px;border-radius:4px;">{escape(score_str)}</code></td></tr>
<tr><td style="color:#64748B;">Route to</td><td>{escape(audience)}</td></tr>
</table>
<div style="margin:18px 0 8px;font-size:11px;color:#64748B;text-transform:uppercase;letter-spacing:.05em;font-weight:600;">Post</div>
<blockquote style="margin:0;padding:12px 14px;background:#F8FAFC;border-left:3px solid #E2E8F0;border-radius:4px;font-size:13.5px;line-height:1.55;color:#0F172A;white-space:pre-wrap;">{escape(content)}</blockquote>
<div style="margin:18px 0 8px;font-size:11px;color:#64748B;text-transform:uppercase;letter-spacing:.05em;font-weight:600;">Why {escape(band)}</div>
<div style="font-size:13px;color:#334155;line-height:1.55;">{escape(why)}</div>
<div style="margin-top:24px;">
<a href="{escape(url) if url else '#'}" style="display:inline-block;padding:9px 16px;background:{color};color:#fff;text-decoration:none;border-radius:6px;font-size:13px;font-weight:600;margin-right:8px;">Open original</a>
<a href="{escape(dashboard_url)}" style="display:inline-block;padding:9px 16px;background:#fff;color:#0F172A;border:1px solid #E2E8F0;text-decoration:none;border-radius:6px;font-size:13px;font-weight:600;">Open dashboard</a>
</div>
</td></tr>
<tr><td style="background:#F8FAFC;padding:12px 20px;font-size:11px;color:#64748B;border-top:1px solid #E2E8F0;">
Zomato Social Watch · automated alert
</td></tr>
</table>
</body></html>"""

    return {"subject": subject, "html": html, "text": text}


def _build_message(
    payload: dict[str, Any],
    *,
    sender: str,
    recipients: list[str],
) -> str:
    """Compose a multipart MIME message. Returns the str blob ready for
    smtplib.SMTP.sendmail()."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = payload["subject"]
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="zomato-social-watch")
    msg.attach(MIMEText(payload["text"], "plain", "utf-8"))
    msg.attach(MIMEText(payload["html"], "html", "utf-8"))
    return msg.as_string()


async def send_email(
    payload: dict[str, Any],
    *,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    sender: str | None = None,
    recipients: list[str] | None = None,
) -> dict[str, Any]:
    """Send the prepared payload via SMTP. Always returns a dict, never
    raises. Wraps blocking smtplib in asyncio.to_thread.
    """
    sent_at = datetime.now(timezone.utc).isoformat()

    h = host or os.getenv(SMTP_HOST_ENV) or ""
    p = int(port or os.getenv(SMTP_PORT_ENV) or 587)
    u = user or os.getenv(SMTP_USER_ENV) or ""
    pw = password or os.getenv(SMTP_PASS_ENV) or ""
    s = sender or os.getenv(EMAIL_FROM_ENV) or u
    rcpt = recipients or [
        x.strip() for x in (os.getenv(EMAIL_TO_ENV) or "").split(",") if x.strip()
    ]

    if not h or not u or not pw or not rcpt:
        return {
            "ok": False,
            "status": 0,
            "ts": sent_at,
            "error": "SMTP env vars incomplete (need SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO)",
        }

    raw = _build_message(payload, sender=s, recipients=rcpt)

    def _do_send():
        ctx = ssl.create_default_context()
        # 587 = STARTTLS, 465 = implicit TLS. Default to STARTTLS.
        if p == 465:
            with smtplib.SMTP_SSL(h, p, timeout=SEND_TIMEOUT_S, context=ctx) as srv:
                srv.login(u, pw)
                srv.sendmail(s, rcpt, raw)
        else:
            with smtplib.SMTP(h, p, timeout=SEND_TIMEOUT_S) as srv:
                srv.ehlo()
                srv.starttls(context=ctx)
                srv.ehlo()
                srv.login(u, pw)
                srv.sendmail(s, rcpt, raw)

    try:
        await asyncio.to_thread(_do_send)
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"SMTP auth failed: {e}"}
    except smtplib.SMTPException as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"SMTP error: {e}"}
    except Exception as e:  # pragma: no cover
        logger.exception("[email] unexpected send error")
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"{type(e).__name__}: {e}"}

    return {"ok": True, "status": 250, "ts": sent_at, "error": None, "to": rcpt}


async def build_and_send(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    webhook: str | None = None,  # accepted for dispatcher uniformity, ignored
    dashboard_base: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compose + send. Returns (payload, result) for dispatcher persistence."""
    payload = build_email(post, classification, priority_breakdown, dashboard_base=dashboard_base)
    result = await send_email(payload)
    return payload, result


# Cosmetic: name surfaced in dispatcher's "no webhooks configured" log
SMTP_WEBHOOK_NAME_HINT = "SMTP_HOST/SMTP_USER/SMTP_PASS/EMAIL_TO"


__all__ = [
    "build_email",
    "send_email",
    "build_and_send",
    "webhook_url",
    "SMTP_WEBHOOK_NAME_HINT",
]
