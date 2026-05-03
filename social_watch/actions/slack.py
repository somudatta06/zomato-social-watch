"""Slack incoming-webhook dispatcher.

Setup (one-time, ~30 seconds):
    1. Visit https://api.slack.com/messaging/webhooks
    2. Click "Create your Slack app" → "From scratch" → name it
       "Zomato Social Watch" → pick the workspace.
    3. In the app's left nav, choose "Incoming Webhooks" and toggle ON.
    4. Click "Add New Webhook to Workspace", pick the channel
       (e.g. `#zomato-watch-p0`), authorize.
    5. Copy the webhook URL (looks like
       `https://hooks.slack.com/services/T.../B.../xxxx`)
       and paste into `.env` as:
           SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxxx
    6. Restart the dashboard. P0 posts will fire automatically.

Free-tier note: Slack does not meter incoming webhooks for paid or free
workspaces, so there is no rate-budget concern. The dispatcher still
applies a 1 req/sec polite throttle when batching.

The message itself is built with Slack's Block Kit JSON
(https://api.slack.com/block-kit). Each P0 post becomes one message with:
    • Header   — priority emoji + band + topic
    • Section  — author + source + permalink
    • Section  — post content (truncated to 600 chars)
    • Section  — classification reasoning (priority_breakdown.reason)
    • Context  — timestamp + audience routing list
    • Actions  — "Open original" + "Open dashboard" link buttons
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

# ============================================================
# Config
# ============================================================

SLACK_WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
DASHBOARD_BASE_ENV = "DASHBOARD_BASE_URL"          # optional override
DEFAULT_DASHBOARD_BASE = "http://localhost:8000"

# httpx timeout — Slack webhooks usually respond < 200 ms; 10 s is a generous
# upper bound that still keeps the background loop snappy on flaky networks.
SEND_TIMEOUT_S = 10.0

# Hard cap on message body so we don't blow past Slack's 3000-char text limit
# per section block. 600 chars + "…" leaves headroom for surrounding markup.
CONTENT_TRUNCATE = 600

# Priority emoji mapping
_BAND_EMOJI = {
    "P0": "🚨",
    "P1": "🟠",
    "P2": "🔵",
    "P3": "⚪",
}

# Source emoji
_SOURCE_EMOJI = {
    "reddit": "🟧",
    "twitter": "✖️",
}


# ============================================================
# Helpers
# ============================================================

def webhook_url() -> str | None:
    """Resolve the webhook URL from env, returning None if unset.
    Caller decides whether to log/skip — slack.py never raises on missing env.
    """
    url = (os.getenv(SLACK_WEBHOOK_ENV) or "").strip()
    return url or None


def _truncate(text: str, n: int = CONTENT_TRUNCATE) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _topic_label(classification: dict[str, Any]) -> str:
    """Human-readable topic for the header. Falls back gracefully."""
    primary = classification.get("primary_topic")
    if primary and isinstance(primary, str):
        return primary.replace("_", " ").strip().title()
    cat = classification.get("category") or classification.get("side")
    if cat:
        return f"{cat.title()} issue"
    return "Critical Mention"


def _reasoning(classification: dict[str, Any], priority_breakdown: dict[str, Any]) -> str:
    """Compose the 'why this is P0' explanation.
    Prefers priority_breakdown.reason (top contributors), falls back to
    classification.reasoning (LLM rationale), then a generic stub.
    """
    if priority_breakdown.get("tripwire_override"):
        return priority_breakdown.get("reason", "Tripwire override (auto-escalated by rules)")
    pieces = []
    pri_reason = priority_breakdown.get("reason")
    if pri_reason:
        pieces.append(pri_reason)
    cls_reason = classification.get("reasoning") or classification.get("rationale")
    if cls_reason:
        pieces.append(_truncate(str(cls_reason), 280))
    if not pieces:
        return "P0 priority — auto-escalated."
    return " · ".join(pieces)


def _audience_list(classification: dict[str, Any]) -> str:
    aud = classification.get("audience") or []
    if not aud:
        return "(no audience tagged)"
    return ", ".join(str(a) for a in aud)


# ============================================================
# Block Kit builder
# ============================================================

def build_blocks(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    dashboard_base: str | None = None,
) -> dict[str, Any]:
    """Produce a Slack incoming-webhook payload for one P0 post.

    Returns the full JSON dict ready to POST. Keep this pure — no I/O — so
    it's trivially unit-testable.
    """
    band = priority_breakdown.get("band") or "P0"
    emoji = _BAND_EMOJI.get(band, "🚨")
    src = (post.get("source") or "?").lower()
    src_emoji = _SOURCE_EMOJI.get(src, "•")
    topic = _topic_label(classification)
    author = post.get("author") or "anonymous"
    url = post.get("url") or ""
    content = _truncate(post.get("content") or "", CONTENT_TRUNCATE)
    reasoning = _reasoning(classification, priority_breakdown)
    audience = _audience_list(classification)

    base = (dashboard_base or os.getenv(DASHBOARD_BASE_ENV) or DEFAULT_DASHBOARD_BASE).rstrip("/")
    dashboard_url = f"{base}/?q={quote(str(post.get('id') or ''), safe='')}"

    score_line = ""
    pri_score = priority_breakdown.get("score")
    if pri_score is not None:
        try:
            score_line = f"  ·  score `{float(pri_score):.2f}`"
        except (TypeError, ValueError):
            pass

    created_at = post.get("created_at") or ""
    ts_line = created_at[:19].replace("T", " ") + " UTC" if created_at else "—"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {band} — {topic}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Author:*\n{src_emoji} `@{author}`"},
                {
                    "type": "mrkdwn",
                    "text": f"*Source:*\n<{url}|{src.title()} post>" if url else f"*Source:*\n{src.title()}",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Post:*\n>>> {content}" if content else "*Post:*\n_(empty)_",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Why P0:*\n{reasoning}{score_line}",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f":clock3: {ts_line}"},
                {"type": "mrkdwn", "text": f":busts_in_silhouette: route to *{audience}*"},
            ],
        },
    ]

    # Action row — only include "Open original" if we actually have a URL.
    action_elements: list[dict[str, Any]] = []
    if url:
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Open original", "emoji": True},
            "url": url,
            "style": "primary",
        })
    action_elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "Open dashboard", "emoji": True},
        "url": dashboard_url,
    })
    blocks.append({"type": "actions", "elements": action_elements})

    return {
        # `text` is the fallback shown in notifications / on clients that
        # don't render Block Kit. Must be present.
        "text": f"{emoji} {band} — {topic}: @{author} on {src}",
        "blocks": blocks,
    }


# ============================================================
# Sender
# ============================================================

async def send_slack(payload: dict[str, Any], *, webhook: str | None = None) -> dict[str, Any]:
    """POST the payload to a Slack incoming webhook.

    Always returns a dict (never raises). The caller persists this dict as
    `posts.action_meta` so we have a permanent record of what fired and
    what Slack said in response.

    Returns:
        {
          "ok":     bool,
          "status": int (HTTP code, 0 if no response),
          "ts":     iso8601 send time,
          "error":  str | None,   # human-readable failure description
        }
    """
    target = webhook or webhook_url()
    sent_at = datetime.now(timezone.utc).isoformat()
    if not target:
        return {
            "ok": False,
            "status": 0,
            "ts": sent_at,
            "error": f"{SLACK_WEBHOOK_ENV} is not set",
        }

    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as client:
            resp = await client.post(
                target,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        body = (resp.text or "").strip()
        ok = resp.status_code == 200 and body == "ok"
        if not ok:
            return {
                "ok": False,
                "status": resp.status_code,
                "ts": sent_at,
                "error": f"Slack returned {resp.status_code}: {body[:200]}",
            }
        return {"ok": True, "status": 200, "ts": sent_at, "error": None}

    except httpx.TimeoutException as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"timeout: {e}"}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"httpx_error: {e}"}
    except Exception as e:  # pragma: no cover — last-resort safety net
        logger.exception("[slack] unexpected send error")
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"{type(e).__name__}: {e}"}


# Convenience: build + send in one call (used by the dispatcher).
async def build_and_send(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    webhook: str | None = None,
    dashboard_base: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (payload, result) so callers can persist both."""
    payload = build_blocks(post, classification, priority_breakdown, dashboard_base=dashboard_base)
    result = await send_slack(payload, webhook=webhook)
    return payload, result


__all__ = [
    "build_blocks",
    "send_slack",
    "build_and_send",
    "webhook_url",
    "SLACK_WEBHOOK_ENV",
]
