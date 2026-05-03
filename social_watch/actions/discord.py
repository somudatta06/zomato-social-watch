"""Discord incoming-webhook dispatcher — parallel to slack.py.

Setup (one-time, ~30 seconds):
    1. In your Discord server, right-click the channel you want alerts in
       (e.g. #zomato-watch-p0) → "Edit Channel" → "Integrations" → "Webhooks".
    2. Click "New Webhook", name it "Zomato Social Watch", optionally pick
       an avatar, then click "Copy Webhook URL".
    3. Paste into `.env`:
           DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/.../...
    4. Restart the dashboard. P0 posts will fire to Discord automatically.

Why Discord alongside Slack:
    The brief requires ≥3 live external systems with read/write. Reddit
    (read) + X/Twitter (read) + Slack (write) covers it on paper, but
    if Slack flakes mid-demo we have nothing. Discord is the same DX as
    Slack (paste a webhook URL, no OAuth, no service account) and gives
    us a redundant write target — total of 4 connectors with 2 writes.

Discord embed shape mirrors Slack's Block Kit so a reviewer can compare
the two outputs side-by-side. The dispatcher considers a post "actioned"
if EITHER channel returned ok — a single failure doesn't block the queue.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
DASHBOARD_BASE_ENV = "DASHBOARD_BASE_URL"
DEFAULT_DASHBOARD_BASE = "http://localhost:8000"

SEND_TIMEOUT_S = 10.0

# Discord embed description has a 4096 char cap; we keep posts to 1500
# so there's headroom for surrounding markup and formatted fields.
CONTENT_TRUNCATE = 1500

# Priority band → embed color (decimal RGB, Discord's required format).
# Mirrors the Slack emoji set so support staff can read both at a glance.
_BAND_COLOR = {
    "P0": 0xDC2626,  # red-600
    "P1": 0xC2410C,  # orange-700
    "P2": 0x1D4ED8,  # blue-700
    "P3": 0x64748B,  # slate-500
}
_BAND_EMOJI = {"P0": "🚨", "P1": "🟠", "P2": "🔵", "P3": "⚪"}
_SOURCE_EMOJI = {"reddit": "🟧", "twitter": "✖️"}


def webhook_url() -> str | None:
    """Resolve webhook from env, returning None if unset."""
    url = (os.getenv(DISCORD_WEBHOOK_ENV) or "").strip()
    return url or None


def _truncate(text: str, n: int = CONTENT_TRUNCATE) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _topic_label(classification: dict[str, Any]) -> str:
    primary = classification.get("primary_topic")
    if primary and isinstance(primary, str):
        return primary.replace("_", " ").strip().title()
    cat = classification.get("category") or classification.get("side")
    if cat:
        return f"{cat.title()} issue"
    return "Critical Mention"


def _reasoning(classification: dict[str, Any], priority_breakdown: dict[str, Any]) -> str:
    if priority_breakdown.get("tripwire_override"):
        return priority_breakdown.get("reason", "Tripwire override (auto-escalated by rules)")
    pieces = []
    if priority_breakdown.get("reason"):
        pieces.append(priority_breakdown["reason"])
    cls_reason = classification.get("reasoning") or classification.get("rationale")
    if cls_reason:
        pieces.append(_truncate(str(cls_reason), 280))
    return " · ".join(pieces) if pieces else "P0 priority — auto-escalated."


def _audience_list(classification: dict[str, Any]) -> str:
    aud = classification.get("audience") or []
    return ", ".join(str(a) for a in aud) if aud else "(no audience tagged)"


def build_embed(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    dashboard_base: str | None = None,
) -> dict[str, Any]:
    """Produce a Discord webhook payload (single embed) for one P0 post.

    Pure function — no I/O — so unit tests are trivial.
    """
    band = priority_breakdown.get("band") or "P0"
    emoji = _BAND_EMOJI.get(band, "🚨")
    color = _BAND_COLOR.get(band, _BAND_COLOR["P0"])
    src = (post.get("source") or "?").lower()
    src_emoji = _SOURCE_EMOJI.get(src, "•")
    topic = _topic_label(classification)
    author = post.get("author") or "anonymous"
    url = post.get("url") or ""
    content = _truncate(post.get("content") or "", CONTENT_TRUNCATE)
    reasoning = _reasoning(classification, priority_breakdown)
    audience = _audience_list(classification)

    base = (dashboard_base or os.getenv(DASHBOARD_BASE_ENV) or DEFAULT_DASHBOARD_BASE).rstrip("/")
    dashboard_url = f"{base}/inbox?q={post.get('id') or ''}"

    pri_score = priority_breakdown.get("score")
    score_str = f"{float(pri_score):.2f}" if pri_score is not None else "—"

    created_at = post.get("created_at") or ""
    ts_line = created_at[:19].replace("T", " ") + " UTC" if created_at else "—"

    description_parts = []
    if content:
        # Discord supports markdown; >>> renders as a quote block.
        description_parts.append(f"> {content}")
    description_parts.append(f"\n**Why {band}:** {reasoning}")
    description_parts.append(f"**Route to:** {audience}")

    embed: dict[str, Any] = {
        "title": f"{emoji} {band} — {topic}",
        "description": "\n".join(description_parts),
        "url": url or None,
        "color": color,
        "fields": [
            {"name": "Author", "value": f"{src_emoji} `@{author}`", "inline": True},
            {"name": "Source", "value": src.title(), "inline": True},
            {"name": "Score", "value": f"`{score_str}`", "inline": True},
        ],
        "footer": {"text": f"Posted {ts_line} · zomato social watch"},
        "timestamp": created_at if created_at else None,
    }

    # Discord embed shape: omit None fields (some clients reject them).
    embed = {k: v for k, v in embed.items() if v is not None}

    # Action links go in the description as inline links since Discord
    # webhooks can't render interactive buttons (only bots can).
    links = []
    if url:
        links.append(f"[Open original]({url})")
    links.append(f"[Open dashboard]({dashboard_url})")
    embed["description"] += "\n\n" + " · ".join(links)

    return {
        "username": "Zomato Social Watch",
        # Fallback notification text for clients that suppress embeds:
        "content": f"{emoji} **{band}** — {topic} · @{author} on {src.title()}",
        "embeds": [embed],
        # Avoid pinging @everyone / @here even if the post text contains them.
        "allowed_mentions": {"parse": []},
    }


async def send_discord(payload: dict[str, Any], *, webhook: str | None = None) -> dict[str, Any]:
    """POST the payload to a Discord incoming webhook.

    Always returns a dict (never raises). Returns:
        { ok: bool, status: int, ts: iso8601, error: str | None }

    Discord returns 204 No Content on success (NOT 200). Some clients also
    accept ?wait=true to get back a message object — we don't need that.
    """
    target = webhook or webhook_url()
    sent_at = datetime.now(timezone.utc).isoformat()
    if not target:
        return {
            "ok": False,
            "status": 0,
            "ts": sent_at,
            "error": f"{DISCORD_WEBHOOK_ENV} is not set",
        }

    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as client:
            resp = await client.post(
                target,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        # Discord returns 204 on success, occasionally 200 with ?wait=true.
        if resp.status_code in (200, 204):
            return {"ok": True, "status": resp.status_code, "ts": sent_at, "error": None}
        body = (resp.text or "").strip()[:200]
        return {
            "ok": False,
            "status": resp.status_code,
            "ts": sent_at,
            "error": f"Discord returned {resp.status_code}: {body}",
        }

    except httpx.TimeoutException as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"timeout: {e}"}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"httpx_error: {e}"}
    except Exception as e:  # pragma: no cover
        logger.exception("[discord] unexpected send error")
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"{type(e).__name__}: {e}"}


async def build_and_send(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    webhook: str | None = None,
    dashboard_base: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (payload, result) so callers can persist both."""
    payload = build_embed(post, classification, priority_breakdown, dashboard_base=dashboard_base)
    result = await send_discord(payload, webhook=webhook)
    return payload, result


__all__ = [
    "build_embed",
    "send_discord",
    "build_and_send",
    "webhook_url",
    "DISCORD_WEBHOOK_ENV",
]
