"""Action orchestrator — tiered routing across Sheets / Email / Slack / Discord.

Routing tiers (see `_route_for_post`):
  Tier 1 — Sheets (audit log, ALWAYS):
    Every escalated post (P0/P1/P2 or P3-with-tripwire) appends a row.
    Doesn't drown in chat scroll; searchable, exportable.

  Tier 2 — Email (per team, P1+):
    P0 + P1 posts trigger an email tagged with the audience routing.
    The "team needs to action this week" tier.

  Tier 3 — Slack + Discord (war room, P0 + hard tripwires only):
    Genuinely urgent: food safety, court action, sexual misconduct,
    privacy leak, regulatory threat. The "wake someone up" tier.

Public entrypoints:

  • `dispatch_for_post(post_id, force=False)`
        Targeted single-post fire. Looks up the post + classification +
        priority, calls `_route_for_post` to pick channels, fires each
        configured one, persists action_taken / action_meta / actioned_at.
        Returns a status dict for the CLI or web endpoint.

  • `dispatch_unactioned(limit=30, dry_run=False)`
        Sweeper for the background loop. Pulls P0/P1/P2 posts where
        action_taken IS NULL, applies routing, fires each (1 req/sec
        polite throttle). Posts that route to nothing are skipped silently.

Idempotency:
  We never fire twice for the same post unless `force=True`. action_taken
  on the row is the lock. Double-clicking the dashboard button never
  produces a double-message.

Defensive behaviour:
  • Routing decides no channels → `skipped:not_routable`.
  • Routing wants channels but none configured → `skipped:no_webhook`.
  • Bad classification/priority → `skipped:bad_data`.
  • Some channels fire OK, others fail → post is marked actioned with
    the successful channels in action_taken; failures live in
    action_meta.channels for postmortem.

Persistence shape (posts.action_taken / action_meta):
    action_taken = "sheets+email"  (channels that succeeded, joined by '+')
    action_meta  = JSON dict {
        "fired_at":  iso8601,
        "trigger":   "auto" | "manual" | "force",
        "routes":    ["sheets", "email"],          # what routing decided
        "skipped_unconfigured": [],                # routes we wanted but lacked URLs
        "channels":  { name: per-channel result }, # incl. payload + status
        "fired":     ["sheets", "email"],
        "failed":    [],
    }
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from loguru import logger

from .. import config
from . import slack
from . import discord as discord_action
from . import email as email_action
from . import sheets as sheets_action
from . import twitter_reply as twitter_reply_action
from . import reddit_comment as reddit_comment_action
from . import linear_ticket as ticket_action

# Sweep tunables
_POLITE_DELAY_S = 1.0      # incoming-webhooks aren't metered, but be a good citizen
_DEFAULT_LIMIT = 30         # protect background loop from accidental floods


# ============================================================
# Tiered routing — three signal levels, three different audiences.
# ============================================================
#
# Tier 1 — Sheets (audit log, ALWAYS):
#   Every escalated post (P0/P1/P2 with tripwires) appends a row.
#   This is the team's "system of record". Doesn't drown in chat scroll;
#   searchable, exportable, useful for retrospectives.
#
# Tier 2 — Email (per team, P1+):
#   P0 + P1 posts trigger an email to the team. Subject is tagged with
#   the audience routing so the recipient can filter their inbox.
#
# Tier 3 — Slack (war room, P0 + hard tripwires only):
#   Only the genuinely urgent: food safety, court action, sexual misconduct,
#   privacy leak, etc. Discord is fired in parallel as a redundant channel.

# Tripwires that justify a "wake someone up" Slack alert. A founder mention
# does NOT belong here (every angry consumer @-tags @deepigoyal); only
# threats to public safety, legal, regulatory, or PR reputation make it.
_HARD_SLACK_TRIPWIRES: set[str] = {
    "food_safety_incident",
    "death_claim",
    "sexual_misconduct",
    "court_fir_legal",
    "religious_caste_gender_sensitivity",
    "privacy_data_leak",
    "anti_competitive_regulatory",
    "insider_leak",
}


def _route_for_post(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority: dict[str, Any],
) -> list[str]:
    """Decide which channels should fire for this post.

    Returns a list like ["sheets", "email"] in fire order. Empty list
    means the post doesn't escalate to anything (e.g. P3 with no tripwire).
    """
    band = (priority.get("band") or "").upper()
    fired = set(classification.get("tripwires_fired") or [])
    routes: list[str] = []

    # Tier 1: Sheets — every escalated post lands here.
    # P0/P1/P2 are auto-eligible. P3 is eligible only if a tripwire fired
    # (which means it's a soft-elevation case the operator should still see).
    if band in ("P0", "P1", "P2") or fired:
        routes.append("sheets")

    # Tier 2: Email — moderate priority, team needs to act this week.
    if band in ("P0", "P1"):
        routes.append("email")

    # Tier 2.5: Linear ticket — durable workflow item for every P0.
    # Slack tells you "wake up", a ticket is what gets *assigned and
    # closed*. We fire on P0 only (any tripwire or none) so the
    # ticket queue stays signal-rich. P1 posts go to email + sheets;
    # if the team decides a P1 needs a ticket they create it manually.
    if band == "P0":
        routes.append("ticket")

    # Tier 3: Slack/Discord war-room — only the genuinely urgent.
    # Requires P0 AND a hard tripwire. A founder mention alone won't
    # flood the war room.
    if band == "P0" and (fired & _HARD_SLACK_TRIPWIRES):
        routes.append("slack")
        routes.append("discord")  # fired in parallel as a redundant channel

    return routes


# Map channel name to module so the dispatcher can iterate.
#
# Channels split into two groups by intent:
#  • Auto-fire-eligible (sheets/email/slack/discord): considered by
#    ``_route_for_post`` and the background sweep. These are "send-style"
#    — they push outbound messages to a passive channel.
#  • Manual-only (twitter_reply, reddit_comment): NEVER auto-fire because
#    a misclassified post becomes a public reply from the corporate
#    handle. Reachable only by passing ``channels=[...]`` explicitly to
#    ``dispatch_for_post`` (i.e., from the dashboard manual button).
_CHANNEL_MODS: dict[str, Any] = {
    "slack":          slack,
    "discord":        discord_action,
    "email":          email_action,
    "sheets":         sheets_action,
    "ticket":         ticket_action,
    "twitter_reply":  twitter_reply_action,
    "reddit_comment": reddit_comment_action,
}

# Channels that may NEVER fire automatically. Listed explicitly so a
# future contributor can't add a manual-only channel to ``_route_for_post``
# without seeing this guardrail.
_MANUAL_ONLY_CHANNELS: frozenset[str] = frozenset({"twitter_reply", "reddit_comment"})


# ============================================================
# Cluster-aware fire-once dedup (Phase iota)
# ============================================================
# When 13 posts about the same incident land, the team should be paged
# ONCE, not 13 times. First post in a cluster fires the full tier
# routing. Subsequent posts in the same cluster only append to Sheets
# (audit log) and skip Email/Slack/Discord. When the cluster's member
# count crosses [5, 10, 25, 50, 100], we fire ONE volume-update alert
# to Slack + Discord per milestone.
#
# Toggleable via env var ENABLE_CLUSTER_DEDUP (default 1). Set to 0
# to revert to legacy per-post fan-out.

ENABLE_CLUSTER_DEDUP_ENV = "ENABLE_CLUSTER_DEDUP"
_VOLUME_MILESTONES: list[int] = [5, 10, 25, 50, 100]


def _cluster_dedup_enabled() -> bool:
    val = (os.getenv(ENABLE_CLUSTER_DEDUP_ENV) or "1").strip().lower()
    return val in ("1", "true", "yes", "on")


async def _lookup_cluster_for_post(post_id: str) -> str | None:
    """Find the cluster_id this post belongs to. Returns None if no
    cluster, the cluster_members table is missing, or any error.
    """
    try:
        async with aiosqlite.connect(str(config.DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT cluster_id FROM cluster_members WHERE post_id = ? LIMIT 1",
                (post_id,),
            )
            row = await cur.fetchone()
            if row:
                return row["cluster_id"]
    except Exception:
        return None
    return None


async def _lookup_cluster_alert(
    db: aiosqlite.Connection, cluster_id: str
) -> dict[str, Any] | None:
    """Fetch the cluster_alerts row for this cluster, or None if no row exists."""
    try:
        cur = await db.execute(
            "SELECT cluster_id, first_alerted_at, first_post_id, "
            "       last_volume_at, last_volume_count, milestones_fired "
            "FROM cluster_alerts WHERE cluster_id = ?",
            (cluster_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["milestones_fired_list"] = json.loads(d.get("milestones_fired") or "[]")
        except Exception:
            d["milestones_fired_list"] = []
        return d
    except Exception:
        return None


async def _lookup_cluster_meta(
    db: aiosqlite.Connection, cluster_id: str
) -> dict[str, Any]:
    """Pull display-friendly cluster info (topic, summary, geography, member_count)."""
    try:
        cur = await db.execute(
            "SELECT primary_topic, summary, geography, member_count, cluster_type "
            "FROM clusters WHERE id = ?",
            (cluster_id,),
        )
        row = await cur.fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return {}


async def _get_cluster_volume(
    db: aiosqlite.Connection, cluster_id: str
) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM cluster_members WHERE cluster_id = ?",
        (cluster_id,),
    )
    row = await cur.fetchone()
    return row["c"] if row else 0


def _milestones_crossed(
    current_count: int, fired_list: list[int]
) -> list[int]:
    """Return milestones in _VOLUME_MILESTONES that current_count >= threshold
    AND that haven't been fired yet. Handles multi-jump (e.g. 3 to 12 crosses
    both 5 and 10).
    """
    return [m for m in _VOLUME_MILESTONES if current_count >= m and m not in fired_list]


async def _register_first_alert(
    db: aiosqlite.Connection,
    cluster_id: str,
    post_id: str,
    now_iso: str,
) -> None:
    """Insert a cluster_alerts row marking this cluster as alerted-once.
    INSERT OR IGNORE makes it safe against races where two posts of the
    same cluster fire concurrently.
    """
    await db.execute(
        "INSERT OR IGNORE INTO cluster_alerts "
        "(cluster_id, first_alerted_at, first_post_id, last_volume_at, "
        " last_volume_count, milestones_fired) VALUES (?, ?, ?, ?, ?, ?)",
        (cluster_id, now_iso, post_id, now_iso, 1, "[]"),
    )
    await db.commit()


async def _record_volume_update(
    db: aiosqlite.Connection,
    cluster_id: str,
    current_volume: int,
    fired_list: list[int],
    now_iso: str,
) -> None:
    await db.execute(
        "UPDATE cluster_alerts SET last_volume_at = ?, last_volume_count = ?, "
        "milestones_fired = ? WHERE cluster_id = ?",
        (now_iso, current_volume, json.dumps(sorted(set(fired_list))), cluster_id),
    )
    await db.commit()


def _build_volume_update_payload(
    cluster_meta: dict[str, Any],
    current_volume: int,
    milestone: int,
    *,
    dashboard_base: str | None = None,
) -> dict[str, Any]:
    """Build a single volume-update message payload reusable for both
    Slack (Block Kit) and Discord (embed). Returns:
        {"slack": <slack payload>, "discord": <discord payload>}.
    """
    base = (dashboard_base or os.getenv("DASHBOARD_BASE_URL") or "http://localhost:8000").rstrip("/")
    topic = (cluster_meta.get("primary_topic") or "Untitled cluster").replace("_", " ").title()
    summary = cluster_meta.get("summary") or ""
    geography = cluster_meta.get("geography") or ""
    cluster_id = cluster_meta.get("id") or ""
    dash_url = f"{base}/inbox?cluster_id={cluster_id}" if cluster_id else base

    text = f"VOLUME UPDATE: cluster '{topic}' now has {current_volume} posts (milestone {milestone} crossed)."

    slack_payload = {
        "text": text,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Volume update: {milestone} posts and counting", "emoji": True},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Topic:*\n{topic}"},
                    {"type": "mrkdwn", "text": f"*Members:*\n{current_volume}"},
                ],
            },
        ],
    }
    if summary:
        slack_payload["blocks"].append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary:*\n{summary[:500]}"}}
        )
    slack_payload["blocks"].append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f":bar_chart: milestone *{milestone}* crossed; geography *{geography or 'unknown'}*"},
            ],
        }
    )
    slack_payload["blocks"].append(
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Open in dashboard", "emoji": True}, "url": dash_url},
            ],
        }
    )

    discord_payload = {
        "username": "Zomato Social Watch",
        "content": f"**Volume update**: {topic} now has {current_volume} posts (milestone {milestone} crossed).",
        "embeds": [
            {
                "title": f"Volume update: {milestone} posts and counting",
                "description": (
                    f"**Topic:** {topic}\n"
                    f"**Members:** {current_volume}\n"
                    f"**Geography:** {geography or 'unknown'}\n\n"
                    + (f"{summary[:500]}\n\n" if summary else "")
                    + f"[Open in dashboard]({dash_url})"
                ),
                "color": 0xF59E0B,  # amber
            }
        ],
        "allowed_mentions": {"parse": []},
    }

    return {"slack": slack_payload, "discord": discord_payload}


async def _send_volume_update(
    payloads: dict[str, Any],
) -> dict[str, Any]:
    """Fire the volume-update payload to whichever of Slack/Discord is configured.
    Returns per-channel results. Channels with no webhook URL are skipped silently.
    """
    results: dict[str, Any] = {}
    if slack.webhook_url():
        try:
            results["slack"] = await slack.send_slack(payloads["slack"])
        except Exception as e:
            results["slack"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if discord_action.webhook_url():
        try:
            results["discord"] = await discord_action.send_discord(payloads["discord"])
        except Exception as e:
            results["discord"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return results


# ============================================================
# Helpers - DB row shaping
# ============================================================

def _parse_json(field: Any, default: Any) -> Any:
    if field is None:
        return default
    if isinstance(field, (dict, list)):
        return field
    try:
        return json.loads(field)
    except (TypeError, ValueError):
        return default


def _row_to_post(row: aiosqlite.Row) -> dict[str, Any]:
    """Convert a sqlite Row into the dict shape build_blocks expects."""
    d = dict(row)
    d["metadata"] = _parse_json(d.get("metadata"), {})
    return d


def _classification(row: aiosqlite.Row) -> dict[str, Any]:
    return _parse_json(dict(row).get("classification"), {})


def _priority_breakdown(row: aiosqlite.Row) -> dict[str, Any]:
    return _parse_json(dict(row).get("priority_breakdown"), {})


def _is_p0(row: dict[str, Any]) -> bool:
    """Source of truth for 'is this post P0'. Reads the priority_band column
    (set by social_watch.priority). Tripwires already force P0 in that
    module, so we don't need to recheck them here."""
    return (row.get("priority_band") or "").upper() == "P0"


def _prior_channel_results(post: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read ``action_meta.channels`` so per-channel idempotency works.

    Returns ``{channel_name: {ok, status, ...}}`` or ``{}`` if no prior
    fire. Tolerant of malformed JSON (returns empty dict, never raises).
    """
    meta = _parse_json(post.get("action_meta"), {})
    chans = meta.get("channels") if isinstance(meta, dict) else None
    return chans if isinstance(chans, dict) else {}


# ============================================================
# Persist helpers
# ============================================================

async def _set_ack_deadline_if_unset(post_id: str, deadline_iso: str) -> None:
    """Persist the ack_deadline_at on the post if not already set.
    Called once per post (when its first tripwired action fires) so the
    countdown timer in the dashboard can render and the SLA sweeper
    knows when to escalate. Idempotent — never overwrites a deadline
    that's already been written.
    """
    try:
        async with aiosqlite.connect(str(config.DB_PATH)) as db:
            await db.execute(
                "UPDATE posts "
                "   SET ack_deadline_at = ? "
                " WHERE id = ? AND ack_deadline_at IS NULL",
                (deadline_iso, post_id),
            )
            await db.commit()
    except Exception:
        logger.exception(f"[dispatch] {post_id}: failed to set ack_deadline_at")


async def _mark_actioned(
    post_id: str,
    *,
    action_taken: str,
    action_meta: dict[str, Any],
) -> None:
    """Single-row update of the action columns. Idempotent."""
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        await db.execute(
            """
            UPDATE posts
               SET action_taken = ?,
                   action_meta  = ?,
                   actioned_at  = ?
             WHERE id = ?
            """,
            (
                action_taken,
                json.dumps(action_meta, default=str),
                datetime.now(timezone.utc).isoformat(),
                post_id,
            ),
        )
        await db.commit()


# ============================================================
# Single-post dispatch
# ============================================================

async def connector_health() -> list[dict[str, Any]]:
    """Per-channel system status, used by the home-page status strip.

    Returns one dict per known channel::

        {
          "name":               "slack",
          "direction":          "W",                       # R, W, or R+W
          "configured":         True,                      # env vars present
          "last_fired_at":      "2026-05-03T...",          # iso8601 or None
          "last_fire_ok":       True,                      # last result
          "fire_count_24h":     12,                        # successful fires
        }

    Reads the last 24h of ``posts.action_meta`` JSON and walks the
    ``channels`` sub-dict on each row. The post table is small enough
    that scanning it on demand is fine; if this gets slow, cache.
    """
    # Channel direction map. Read-only scrapers (reddit/twitter as input)
    # are tracked separately at the route level — they aren't dispatch
    # channels. The bidirectional reply channels show R+W because they
    # both write here AND have a paired read scraper upstream.
    direction: dict[str, str] = {
        "slack":          "W",
        "discord":        "W",
        "email":          "W",
        "sheets":         "W",
        "ticket":         "W",
        "twitter_reply":  "R+W",
        "reddit_comment": "R+W",
    }

    health: dict[str, dict[str, Any]] = {
        name: {
            "name":           name,
            "direction":      direction.get(name, "W"),
            "configured":     bool(mod.webhook_url()),
            "last_fired_at":  None,
            "last_fire_ok":   None,
            "fire_count_24h": 0,
        }
        for name, mod in _CHANNEL_MODS.items()
    }

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        async with aiosqlite.connect(str(config.DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT action_meta FROM posts "
                "WHERE actioned_at IS NOT NULL AND actioned_at >= ?",
                (cutoff,),
            )
            rows = await cur.fetchall()
        for row in rows:
            meta_raw = row["action_meta"]
            if not meta_raw:
                continue
            try:
                meta = json.loads(meta_raw)
            except Exception:
                continue
            chans = meta.get("channels") if isinstance(meta, dict) else None
            if not isinstance(chans, dict):
                continue
            for cname, cresult in chans.items():
                if cname not in health:
                    continue
                if not isinstance(cresult, dict):
                    continue
                ok = bool(cresult.get("ok"))
                ts = cresult.get("ts")
                if ok:
                    health[cname]["fire_count_24h"] += 1
                if ts:
                    prior = health[cname]["last_fired_at"]
                    if prior is None or ts > prior:
                        health[cname]["last_fired_at"] = ts
                        health[cname]["last_fire_ok"] = ok
    except Exception as e:
        logger.warning(f"[connector_health] scan failed: {e}")

    # Stable display order: send-style first, then ticket, then reply-style.
    order = ["slack", "discord", "email", "sheets", "ticket",
             "twitter_reply", "reddit_comment"]
    return [health[n] for n in order if n in health]


async def dispatch_for_post(
    post_id: str,
    *,
    force: bool = False,
    trigger: str = "manual",
    channels: list[str] | None = None,
    force_approver: str | None = None,
    force_reason: str | None = None,
) -> dict[str, Any]:
    """Fire one or more channels for a single post.

    Args:
        post_id: PRIMARY KEY of the post (e.g. "reddit:abcdef" or "twitter:1234").
        force:   If True, fire even if already actioned. Also bypasses the
                 playbook ``block_auto_reply`` guard for reply channels.
                 Pair with ``force_approver`` + ``force_reason`` so the
                 bypass is auditable.
        trigger: Free-form label persisted in action_meta — "auto"
                 (background loop), "manual" (dashboard button), "force".
        channels: Optional explicit channel list. When set, bypasses
                  ``_route_for_post`` AND the cluster-dedup logic AND the
                  whole-post idempotency check. Idempotency falls back to
                  per-channel: we only short-circuit for channels that
                  already fired successfully according to ``action_meta``.
                  Use this from the dashboard manual buttons (Reply on X,
                  Send Slack, etc.).
        force_approver: Operator name or handle who authorized the
                        playbook bypass (only meaningful when force=True
                        AND a reply-channel is in ``channels``). Stored
                        in action_meta.bypass_approved_by for audit.
        force_reason:   Free-text justification for the bypass. Stored
                        in action_meta.bypass_reason. Required by the
                        dashboard's "Unblock & reply" modal.

    Returns a dict — never raises:
        { ok: bool, post_id: str, status: str, ... }

      status values:
        "fired"            — at least one channel returned ok, row updated
        "already_actioned" — every requested channel already fired (no-op)
        "skipped:not_p0"   — priority band wasn't P0
        "skipped:no_webhook" — none of the requested channels are configured
        "skipped:not_found"  — no DB row with that id
        "skipped:bad_data"   — missing classification/priority
        "skipped:not_routable" — auto-routing returned []
        "failed"           — every requested channel errored
    """
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()

    if row is None:
        return {"ok": False, "post_id": post_id, "status": "skipped:not_found"}

    post = _row_to_post(row)
    cls = _classification(row)
    pri = _priority_breakdown(row)

    # Idempotency. With auto-routing, action_taken on the row is the lock
    # (any channel fired = post done). With an explicit channels list,
    # we want per-channel idempotency: a post that was Slack'd earlier
    # should still accept a manual "Reply on X" later.
    if channels is None:
        if not force and post.get("action_taken"):
            return {
                "ok": True,
                "post_id": post_id,
                "status": "already_actioned",
                "action_taken": post["action_taken"],
            }
    else:
        prior = _prior_channel_results(post)
        if not force:
            requested = set(channels)
            already_ok = {n for n in requested if prior.get(n, {}).get("ok")}
            if already_ok == requested:
                return {
                    "ok": True,
                    "post_id": post_id,
                    "status": "already_actioned",
                    "action_taken": post.get("action_taken"),
                    "channels_already_fired": sorted(already_ok),
                }
            channels = [c for c in channels if c not in already_ok]

    # Auto-fire (channels=None) requires a classified, prioritized post — we
    # don't want the background loop blasting half-processed records to the
    # team. Manual triggers (channels=[...]) skip this gate: if the operator
    # clicked Send Slack on a P3 with empty priority_breakdown, that's a
    # deliberate choice. Channel modules already render safe defaults for
    # missing fields ("Critical Mention", "P0 priority — auto-escalated.",
    # etc.), so the message goes out cleanly.
    if channels is None and (not cls or not pri):
        return {
            "ok": False,
            "post_id": post_id,
            "status": "skipped:bad_data",
            "has_classification": bool(cls),
            "has_priority_breakdown": bool(pri),
        }

    # ── Playbook guard ─────────────────────────────────────────────
    # Reply-style channels (twitter_reply, reddit_comment) write a
    # *public* corporate response. If this post is a death claim, a
    # legal threat, or a privacy leak — the matching playbook tells us
    # NO public response without owner-team approval. Block the fire
    # at the dispatcher (single enforcement point) so a second-screen
    # ops person can't accidentally tweet from @zomatocare during a
    # crisis. Force=True bypasses (assumes legal sign-off in hand).
    if channels and not force:
        reply_channels_requested = [
            c for c in channels if c in _MANUAL_ONLY_CHANNELS
        ]
        if reply_channels_requested:
            from .. import playbooks
            pb = playbooks.for_post(cls)
            if pb and pb.get("block_auto_reply"):
                return {
                    "ok": False,
                    "post_id": post_id,
                    "status": "blocked:playbook",
                    "playbook": pb["name"],
                    "playbook_icon": pb.get("icon"),
                    "playbook_color": pb.get("color"),
                    "owner_team": pb["owner_team"],
                    "ack_deadline_min": pb.get("ack_deadline_min"),
                    "banner": pb.get("banner"),
                    "required_steps": pb.get("required_steps") or [],
                    "channels_blocked": reply_channels_requested,
                    "reason": (
                        f"The {pb['name']} playbook blocks public replies. "
                        f"{pb['owner_team']} must approve before sending. "
                        f"If you have explicit sign-off, retry with force=true."
                    ),
                }

    # Routing.
    if channels is not None:
        # Explicit override from the manual button. Skip auto-routing AND
        # cluster dedup. Manual triggers always proceed for what was
        # asked, even if the post wouldn't normally route.
        routes = list(channels)
        for ch in routes:
            if ch not in _CHANNEL_MODS:
                return {
                    "ok": False,
                    "post_id": post_id,
                    "status": "skipped:bad_data",
                    "error": f"unknown channel: {ch!r}",
                }
    else:
        # Tiered routing: which channels SHOULD fire for this specific post.
        # P3-no-tripwire returns []; P0+hard-tripwire returns all four.
        routes = _route_for_post(post, cls, pri)
        # Defence in depth: never let a manual-only channel auto-fire even
        # if some future routing rule slips it in.
        routes = [r for r in routes if r not in _MANUAL_ONLY_CHANNELS]

    if not routes and not force:
        return {
            "ok": False,
            "post_id": post_id,
            "status": "skipped:not_routable",
            "band": post.get("priority_band"),
        }

    # If forced (CLI --force flag), fall back to "any configured channel"
    # so power users can still fire on a post that wouldn't normally route.
    # Manual-only channels still excluded — force is for auto-eligible only.
    if force and not routes:
        routes = ["sheets", "email", "ticket", "slack", "discord"]

    # Filter routes to only those whose webhook/credentials are actually
    # configured. An unconfigured channel quietly drops out; the post still
    # fires on whatever IS configured. This keeps a partially-configured
    # dev env productive without alerting silently.
    selected_channels: list[tuple[str, Any]] = []
    skipped_unconfigured: list[str] = []
    for name in routes:
        mod = _CHANNEL_MODS.get(name)
        if mod and mod.webhook_url():
            selected_channels.append((name, mod))
        else:
            skipped_unconfigured.append(name)

    if not selected_channels:
        meta = {
            "fired_at": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "channels": {},
            "routes": routes,
            "skipped_unconfigured": skipped_unconfigured,
            "error": (
                f"routing wanted {routes} but none of those channels are "
                f"configured (paste a webhook URL into .env)"
            ),
        }
        logger.warning(
            f"[dispatch] {post_id}: routes={routes} but none configured, fire skipped"
        )
        return {
            "ok": False,
            "post_id": post_id,
            "status": "skipped:no_webhook",
            "meta": meta,
        }

    # Cluster-aware fire-once dedup. If this post is in a cluster that
    # already had its first alert fired, trim channels to ["sheets"] (audit
    # only) and remember to maybe fire a volume-update alert at the end.
    #
    # SKIPPED entirely for explicit-channels triggers (manual buttons): the
    # operator clicked Reply/Slack/etc. on this specific post; trimming
    # their action because some sibling cluster post fired earlier would
    # be surprising and wrong.
    cluster_id: str | None = None
    cluster_role: str = "lead"  # 'lead' = first post, 'follower' = subsequent
    follow_up_volume: int | None = None
    follow_up_crossed: list[int] = []
    cluster_meta: dict[str, Any] = {}
    if channels is None and _cluster_dedup_enabled():
        cluster_id = await _lookup_cluster_for_post(post_id)

    if cluster_id:
        try:
            async with aiosqlite.connect(str(config.DB_PATH)) as cdb:
                cdb.row_factory = aiosqlite.Row
                alert_row = await _lookup_cluster_alert(cdb, cluster_id)
                if alert_row is not None:
                    # Follow-up post. Trim to Sheets-only audit append.
                    cluster_role = "follower"
                    selected_channels = [(n, m) for (n, m) in selected_channels if n == "sheets"]
                    follow_up_volume = await _get_cluster_volume(cdb, cluster_id)
                    fired_list = alert_row.get("milestones_fired_list") or []
                    follow_up_crossed = _milestones_crossed(follow_up_volume, fired_list)
                    cluster_meta = await _lookup_cluster_meta(cdb, cluster_id)
                    cluster_meta["id"] = cluster_id
                    cluster_meta["fired_list_before"] = fired_list
        except Exception as e:
            # Defensive: if any cluster lookup fails, fall back to legacy fan-out.
            logger.warning(f"[dispatch] {post_id}: cluster lookup failed ({e}), legacy fan-out")
            cluster_id = None
            cluster_role = "lead"

    fired_at = datetime.now(timezone.utc).isoformat()
    per_channel: dict[str, Any] = {}
    fired_names: list[str] = []
    failed_names: list[str] = []

    for name, mod in selected_channels:
        try:
            payload, result = await mod.build_and_send(post, cls, pri)
        except Exception as e:  # pragma: no cover — module-level failure
            logger.exception(f"[dispatch] {post_id}: {name} build/send raised")
            per_channel[name] = {
                "ok": False,
                "status": 0,
                "ts": fired_at,
                "error": f"{type(e).__name__}: {e}",
            }
            failed_names.append(name)
            continue

        per_channel[name] = {
            "status": result.get("status", 0),
            "ok": result.get("ok", False),
            "ts": result.get("ts"),
            "error": result.get("error"),
            "payload": payload,
            "result": result,
        }
        if result.get("ok"):
            fired_names.append(name)
            logger.info(
                f"[dispatch] {post_id}: {name} fired ok status={result.get('status')} "
                f"trigger={trigger}"
            )
        else:
            failed_names.append(name)
            logger.warning(
                f"[dispatch] {post_id}: {name} send FAILED status={result.get('status')} "
                f"err={result.get('error')}"
            )

    meta: dict[str, Any] = {
        "fired_at": fired_at,
        "trigger": trigger,
        "routes": routes,                       # what routing decided
        "skipped_unconfigured": skipped_unconfigured,  # configured channels we wanted but lacked
        "channels": per_channel,                 # per-channel results
        "fired": fired_names,
        "failed": failed_names,
    }

    # Audit trail for playbook bypass: when an operator force-replied on a
    # locked playbook (e.g., death claim), capture WHO authorized it and
    # WHY. Surfaced in the actions log + ticket body so the post-incident
    # review can trace every public response back to a named approver.
    if force and force_approver:
        meta["bypass_approved_by"] = force_approver
        meta["bypass_reason"]      = (force_reason or "").strip() or "(no reason given)"
        meta["bypass_at"]          = fired_at
        logger.warning(
            f"[dispatch] {post_id}: PLAYBOOK BYPASSED — "
            f"approver={force_approver!r} reason={force_reason!r}"
        )

    # Cluster-aware post-fire handling.
    volume_update_results: dict[int, Any] = {}
    if cluster_id:
        meta["cluster_id"] = cluster_id
        meta["cluster_role"] = cluster_role
        try:
            async with aiosqlite.connect(str(config.DB_PATH)) as cdb:
                cdb.row_factory = aiosqlite.Row
                if cluster_role == "lead" and fired_names:
                    # First post in this cluster successfully alerted at least
                    # one channel. Record the cluster_alerts row so subsequent
                    # posts dedup. INSERT OR IGNORE handles races.
                    await _register_first_alert(cdb, cluster_id, post_id, fired_at)
                elif cluster_role == "follower":
                    # Follow-up post. Already trimmed to sheets-only above.
                    # Fire one volume-update per crossed milestone, then record.
                    if follow_up_crossed:
                        # Fetch fresh cluster_meta with id field set, for payload
                        cluster_meta_full = await _lookup_cluster_meta(cdb, cluster_id)
                        cluster_meta_full["id"] = cluster_id
                        for milestone in follow_up_crossed:
                            payloads = _build_volume_update_payload(
                                cluster_meta_full,
                                follow_up_volume or 0,
                                milestone,
                            )
                            volume_update_results[milestone] = await _send_volume_update(payloads)
                    # Update last_volume_count + milestones_fired regardless
                    fired_before = (cluster_meta or {}).get("fired_list_before") or []
                    new_fired = sorted(set(fired_before + follow_up_crossed))
                    await _record_volume_update(
                        cdb, cluster_id,
                        follow_up_volume or 0,
                        new_fired,
                        fired_at,
                    )
                    meta["cluster_volume"] = follow_up_volume
                    meta["milestones_crossed"] = follow_up_crossed
                    meta["volume_update_results"] = volume_update_results
        except Exception as e:
            logger.warning(f"[dispatch] {post_id}: cluster post-fire error ({e})")
            meta["cluster_post_fire_error"] = f"{type(e).__name__}: {e}"

    if not fired_names and not volume_update_results:
        # Every channel failed and no volume-update ran. Leave action_taken
        # NULL so the next sweep retries.
        return {
            "ok": False,
            "post_id": post_id,
            "status": "failed",
            "meta": meta,
        }

    # At least one channel succeeded OR a volume-update ran. Record action.
    if cluster_role == "follower":
        # Sheets-only (or nothing) plus optional volume update.
        if fired_names:
            action_taken = "+".join(fired_names) + "+cluster_dedup"
        else:
            action_taken = "cluster_dedup"
        status = "fired_dedup"
    else:
        action_taken = "+".join(fired_names)
        status = "fired"

    # Explicit-channel triggers (manual buttons): merge with the post's
    # prior action history instead of overwriting. Without this, firing
    # Reply-on-X on a post that was already Slack'd would erase the Slack
    # record from action_meta.channels and from action_taken.
    if channels is not None:
        prior_meta_raw = _parse_json(post.get("action_meta"), {})
        prior_meta = prior_meta_raw if isinstance(prior_meta_raw, dict) else {}
        prior_channels = prior_meta.get("channels") or {}
        if isinstance(prior_channels, dict) and prior_channels:
            merged = {**prior_channels, **meta["channels"]}
            meta["channels"] = merged
            meta["fired"] = sorted({n for n, r in merged.items() if r.get("ok")})
            meta["failed"] = sorted(
                {n for n, r in merged.items() if r.get("ok") is False}
            )
        prior_taken = (post.get("action_taken") or "").split("+")
        union = sorted({s for s in prior_taken + meta["fired"] if s})
        action_taken = "+".join(union) if union else action_taken

    await _mark_actioned(post_id, action_taken=action_taken, action_meta=meta)

    # If this post matches an incident playbook, set the acknowledgment
    # deadline so the inbox can render a countdown and the SLA sweeper
    # knows when to escalate. ``_set_ack_deadline_if_unset`` is no-op
    # on subsequent fires for the same post.
    try:
        from .. import playbooks as _pb
        pb_now = _pb.for_post(cls)
        if pb_now and pb_now.get("ack_deadline_min"):
            from datetime import timedelta as _td
            deadline = (
                datetime.fromisoformat(fired_at.replace("Z", "+00:00"))
                + _td(minutes=int(pb_now["ack_deadline_min"]))
            ).isoformat()
            await _set_ack_deadline_if_unset(post_id, deadline)
    except Exception:
        logger.exception(f"[dispatch] {post_id}: ack_deadline computation failed")
    return {
        "ok": True,
        "post_id": post_id,
        "status": status,
        "fired": fired_names,
        "failed": failed_names,
        "meta": meta,
    }


# ============================================================
# Sweeper
# ============================================================

async def dispatch_unactioned(
    *,
    limit: int = _DEFAULT_LIMIT,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Find unactioned escalated posts and fire whichever channels each
    one routes to (per the tiered model in `_route_for_post`).

    Sweeps **P0/P1/P2** posts (broader than just P0 now), since the
    Sheet tier wants P2 audit rows and the Email tier wants P1+. Posts
    that routing decides aren't worth firing on (`P3-no-tripwire`) are
    silently skipped — they're noise.

    Args:
        limit:   safety cap so a backlog spike can't flood downstream
        dry_run: if True, just count what would fire, don't send anything

    Returns:
        { scanned, fired, failed, skipped, dry_run, outcomes }

    Idempotent: action_taken IS NULL in the WHERE clause means re-running
    only picks up posts that haven't been actioned yet. Failures don't set
    action_taken, so they will be retried on the next sweep.
    """
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id FROM posts
             WHERE priority_band IN ('P0', 'P1', 'P2')
               AND action_taken IS NULL
               AND classification IS NOT NULL
               AND noise_category IS NULL
             ORDER BY
               CASE priority_band WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                                  WHEN 'P2' THEN 2 ELSE 3 END,
               priority_score DESC,
               created_at DESC
             LIMIT ?
            """,
            (limit,),
        )
        post_ids = [r["id"] for r in await cur.fetchall()]

    out: dict[str, Any] = {
        "scanned": len(post_ids),
        "fired": 0,
        "failed": 0,
        "skipped": 0,
        "dry_run": dry_run,
        "outcomes": [],
    }
    if not post_ids:
        return out

    if dry_run:
        for pid in post_ids:
            out["outcomes"].append({"post_id": pid, "status": "dry_run"})
        out["skipped"] = len(post_ids)
        return out

    if (not slack.webhook_url()
        and not discord_action.webhook_url()
        and not email_action.webhook_url()
        and not sheets_action.webhook_url()):
        # Every fire would skip with the same reason — bail early.
        logger.warning(
            f"[dispatch] no action channels configured (Slack / Discord / Email / Sheets) "
            f"— {len(post_ids)} P0 posts queued, all skipped"
        )
        for pid in post_ids:
            out["outcomes"].append({"post_id": pid, "status": "skipped:no_webhook"})
        out["skipped"] = len(post_ids)
        return out

    for i, pid in enumerate(post_ids):
        if i > 0:
            try:
                await asyncio.sleep(_POLITE_DELAY_S)
            except asyncio.CancelledError:
                raise
        outcome = await dispatch_for_post(pid, trigger="auto")
        out["outcomes"].append(outcome)
        status = outcome.get("status", "")
        if status == "fired":
            out["fired"] += 1
        elif status == "failed":
            out["failed"] += 1
        else:
            out["skipped"] += 1

    return out


# Cosmetic: name we report when SLACK_WEBHOOK_URL is missing.
SLACK_WEBHOOK_NAME_HINT = slack.SLACK_WEBHOOK_ENV


__all__ = [
    "dispatch_for_post",
    "dispatch_unactioned",
]
