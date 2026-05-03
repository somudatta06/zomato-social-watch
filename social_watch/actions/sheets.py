"""Google Sheets action — appends one row per P0 fire via Apps Script webhook.

Why this exists:
    The brief lists "sheet row appended" as one of the action types. This
    closes that requirement. Sits alongside Slack / Discord / Email in
    the dispatcher fan-out.

    Crucially, this module does NOT use the Google Sheets API +
    service account + OAuth flow. That would mean: pip install gspread,
    create a GCP project, enable Sheets API, generate a service-account
    JSON, share the sheet with the service account email — easily
    20 minutes for a fresh user.

    Instead we use the same DX as Slack/Discord webhooks — the user
    pastes a single URL into .env. The URL points at a Google Apps
    Script "web app" they paste once and forget.

Setup (~3 minutes, one-time):
    1. Create a new Google Sheet.
    2. Add the header row, e.g.:
         Timestamp | Post ID | Band | Topic | Author | Source | URL | Score | Audience | Reasoning
    3. Extensions → Apps Script. In the editor that opens, paste:

           function doPost(e) {
             const data = JSON.parse(e.postData.contents);
             const sh = SpreadsheetApp.getActiveSheet();
             sh.appendRow([
               data.ts, data.post_id, data.band, data.topic, data.author,
               data.source, data.url, data.score, data.audience, data.reasoning
             ]);
             return ContentService
               .createTextOutput(JSON.stringify({ok: true}))
               .setMimeType(ContentService.MimeType.JSON);
           }

    4. Click "Deploy" → "New deployment" → type=Web app, execute as=Me,
       access="Anyone". Click Deploy. Copy the URL it gives you.
    5. Paste into .env:
           SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/AKfycb.../exec
    6. Restart the server. P0 posts will append rows automatically.

The Apps Script runs under your Google account, so it has implicit
write access to the sheet. No service account, no OAuth, no SDK. Free.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

SHEETS_WEBHOOK_ENV = "SHEETS_WEBHOOK_URL"
DASHBOARD_BASE_ENV = "DASHBOARD_BASE_URL"
DEFAULT_DASHBOARD_BASE = "http://localhost:8000"

SEND_TIMEOUT_S = 15.0


def webhook_url() -> str | None:
    url = (os.getenv(SHEETS_WEBHOOK_ENV) or "").strip()
    return url or None


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
        pieces.append(str(pri["reason"]))
    if cls.get("reasoning"):
        pieces.append(str(cls["reasoning"])[:280])
    return " · ".join(pieces) if pieces else "P0 priority"


def build_row(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
) -> dict[str, Any]:
    """Pure function — returns the JSON shape Apps Script expects.
    Match the column order in the doPost() snippet in the module docstring.
    """
    band = priority_breakdown.get("band") or "P0"
    score = priority_breakdown.get("score")
    score_str = f"{float(score):.4f}" if isinstance(score, (int, float)) else ""
    aud_list = classification.get("audience") or []
    audience = ", ".join(str(a) for a in aud_list)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "post_id": post.get("id") or "",
        "band": band,
        "topic": _topic_label(classification),
        "author": post.get("author") or "",
        "source": post.get("source") or "",
        "url": post.get("url") or "",
        "score": score_str,
        "audience": audience,
        "reasoning": _reasoning(classification, priority_breakdown),
        # Echo the post creation time so the sheet can sort by post age
        # rather than escalation time.
        "created_at": post.get("created_at") or "",
        # Stable identifier so the sheet can dedupe if the same post is
        # ever appended twice (e.g. operator force-fire after auto-fire).
        "fired_at": datetime.now(timezone.utc).isoformat(),
    }


async def send_sheets(
    payload: dict[str, Any], *, webhook: str | None = None
) -> dict[str, Any]:
    """POST the row to the Apps Script web app. Always returns a dict,
    never raises.

    Apps Script web apps return 200 on success. The body is whatever the
    user's doPost() returns — we don't enforce a shape because users may
    want to extend the script.
    """
    target = webhook or webhook_url()
    sent_at = datetime.now(timezone.utc).isoformat()
    if not target:
        return {
            "ok": False,
            "status": 0,
            "ts": sent_at,
            "error": f"{SHEETS_WEBHOOK_ENV} is not set",
        }

    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.post(
                target,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            return {"ok": True, "status": 200, "ts": sent_at, "error": None}
        body = (resp.text or "").strip()[:200]
        return {
            "ok": False,
            "status": resp.status_code,
            "ts": sent_at,
            "error": f"Sheets webhook returned {resp.status_code}: {body}",
        }

    except httpx.TimeoutException as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"timeout: {e}"}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"httpx_error: {e}"}
    except Exception as e:  # pragma: no cover
        logger.exception("[sheets] unexpected send error")
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"{type(e).__name__}: {e}"}


async def build_and_send(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    webhook: str | None = None,
    dashboard_base: str | None = None,  # accepted for dispatcher uniformity
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (payload, result) for dispatcher persistence."""
    payload = build_row(post, classification, priority_breakdown)
    result = await send_sheets(payload, webhook=webhook)
    return payload, result


__all__ = [
    "build_row",
    "send_sheets",
    "build_and_send",
    "webhook_url",
    "SHEETS_WEBHOOK_ENV",
]
