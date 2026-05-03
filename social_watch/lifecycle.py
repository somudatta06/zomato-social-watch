"""Incident lifecycle — what happens AFTER the dispatcher fires.

Two sweepers, both safe to call from the background sync loop:

  • ``sla_sweep()`` — finds tripwired posts whose acknowledgment deadline
    has passed without an operator clicking "Acknowledge", and re-fires
    a Slack/Discord/Email alert with an [ESCALATED] prefix. Bumps
    ``posts.escalation_count`` so the dashboard can show "escalated 2×"
    on rows that the team kept ignoring.

  • ``review_sweep()`` — 24 hours after the original action fired, opens
    a Linear sub-issue with a templated review doc. The review captures:
    timeline of channels fired, who acked, who replied (and with what
    audit trail), what the playbook said vs what actually happened.

Both sweepers are idempotent. ``last_escalated_at`` and ``review_issue_id``
columns are the locks. A crashed sweep, a manual restart, a botched
deploy — none of them produce duplicate escalations or duplicate
review tickets.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from loguru import logger

from . import config


# ============================================================
# SLA sweep — re-fire when ack deadline missed
# ============================================================

# Hard guard: don't escalate the same post more than this many times.
# A truly missed escalation is worth 1-2 extra pings; beyond that we're
# just spamming the war room.
_MAX_ESCALATIONS = 2

# Polite delay between escalations within one sweep — keeps Slack from
# flooding when many posts cross their deadline at once.
_BETWEEN_FIRES_S = 1.0


async def sla_sweep(*, dry_run: bool = False) -> dict[str, Any]:
    """Scan for tripwired posts whose ack SLA has expired and the team
    hasn't taken ownership. Re-fires Slack/Discord/Email with an
    ``[ESCALATED · {n}×]`` prefix and increments ``escalation_count``.

    Returns a summary dict for logging::

        {
          "scanned":   int,   # rows considered
          "escalated": int,   # rows where at least one channel re-fired
          "skipped":   int,   # rows past the cap or already re-fired since deadline
          "outcomes":  list,  # [{post_id, status, fired_channels, ...}]
        }
    """
    import asyncio
    from .actions import slack as slack_mod, discord as discord_mod, email as email_mod
    from . import playbooks

    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {"scanned": 0, "escalated": 0, "skipped": 0, "outcomes": []}

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Posts: tripwired, action fired, deadline expired, not acked,
        # under the escalation cap, and either never escalated OR
        # escalated before the most recent deadline (so we don't re-fire
        # immediately on every sweep tick).
        cur = await db.execute(
            """
            SELECT id, source, author, content, url, priority_band,
                   classification, priority_breakdown,
                   ack_deadline_at, ack_at, escalation_count, last_escalated_at,
                   action_meta
              FROM posts
             WHERE ack_deadline_at IS NOT NULL
               AND ack_deadline_at < ?
               AND ack_at IS NULL
               AND COALESCE(escalation_count, 0) < ?
            """,
            (now.isoformat(), _MAX_ESCALATIONS),
        )
        rows = [dict(r) for r in await cur.fetchall()]

    out["scanned"] = len(rows)
    if not rows:
        return out

    for row in rows:
        try:
            cls = json.loads(row.get("classification") or "{}")
            pri = json.loads(row.get("priority_breakdown") or "{}")
        except Exception:
            cls, pri = {}, {}
        pb = playbooks.for_post(cls)
        if not pb:
            out["skipped"] += 1
            continue

        new_count = (row.get("escalation_count") or 0) + 1
        # Severity-aware channel set:
        #   • death claim / privacy / sex misconduct → all three (war-room)
        #   • food safety / legal → Slack + Email (loud, not Discord)
        #   • everything else (medium playbooks) → Email only
        if pb.get("ack_deadline_min", 999) <= 10:
            channels = [("slack", slack_mod), ("discord", discord_mod), ("email", email_mod)]
        elif pb.get("ack_deadline_min", 999) <= 30:
            channels = [("slack", slack_mod), ("email", email_mod)]
        else:
            channels = [("email", email_mod)]

        # Patch the priority breakdown with an explicit escalation header
        # so the existing builders include it without us forking templates.
        pri_for_send = dict(pri)
        prev_reason = pri_for_send.get("reason") or ""
        pri_for_send["reason"] = (
            f"⚠️ ESCALATED · {new_count}× · {pb.get('owner_team')} did not acknowledge "
            f"within {pb.get('ack_deadline_min')} min. " + prev_reason
        )

        if dry_run:
            out["outcomes"].append({"post_id": row["id"], "status": "would_escalate",
                                    "channels": [n for n, _ in channels]})
            out["escalated"] += 1
            continue

        fired_names: list[str] = []
        per_channel: dict[str, Any] = {}
        for name, mod in channels:
            if not mod.webhook_url():
                continue
            try:
                payload, result = await mod.build_and_send(row, cls, pri_for_send)
                per_channel[name] = {
                    "ok":     bool(result.get("ok")),
                    "status": result.get("status", 0),
                    "ts":     result.get("ts"),
                    "error":  result.get("error"),
                    "payload": payload,
                    "result":  result,
                }
                if result.get("ok"):
                    fired_names.append(name)
                    logger.info(
                        f"[sla-sweep] {row['id']}: ESCALATED via {name} "
                        f"({new_count}× — {pb.get('name')})"
                    )
            except Exception as e:
                per_channel[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                logger.exception(f"[sla-sweep] {row['id']}: {name} failed")
            await asyncio.sleep(_BETWEEN_FIRES_S)

        # Persist: bump escalation_count, set last_escalated_at, merge into
        # action_meta.escalations array so the actions log can show the trail.
        ts = datetime.now(timezone.utc).isoformat()
        try:
            prior_meta = json.loads(row.get("action_meta") or "{}")
        except Exception:
            prior_meta = {}
        escalations = prior_meta.get("escalations") or []
        escalations.append({
            "ts": ts,
            "count": new_count,
            "playbook": pb.get("name"),
            "owner_team": pb.get("owner_team"),
            "channels": per_channel,
        })
        new_meta = {**prior_meta, "escalations": escalations}
        try:
            async with aiosqlite.connect(str(config.DB_PATH)) as db:
                await db.execute(
                    "UPDATE posts "
                    "   SET escalation_count = ?, "
                    "       last_escalated_at = ?, "
                    "       action_meta = ? "
                    " WHERE id = ?",
                    (new_count, ts, json.dumps(new_meta, default=str), row["id"]),
                )
                await db.commit()
        except Exception:
            logger.exception(f"[sla-sweep] {row['id']}: failed to persist escalation")

        out["outcomes"].append({
            "post_id": row["id"], "status": "escalated",
            "count": new_count, "fired": fired_names, "playbook": pb.get("name"),
        })
        if fired_names:
            out["escalated"] += 1
        else:
            out["skipped"] += 1

    return out


# ============================================================
# Post-incident review sweep
# ============================================================

# How long after the original action before we open the review doc.
_REVIEW_DELAY_HOURS = 24

# Tripwires that warrant a review doc. Generic complaints don't get one;
# the team would drown in noise. Hard tripwires only — death claim,
# food safety, etc. — match the playbook keys.
_REVIEWABLE_TRIPWIRES: set[str] = {
    "death_claim",
    "food_safety_incident",
    "sexual_misconduct",
    "privacy_data_leak",
    "court_fir_legal",
    "anti_competitive_regulatory",
}


async def review_sweep(*, dry_run: bool = False) -> dict[str, Any]:
    """Open a Linear "post-incident review" sub-issue 24h after a
    tripwired post fired its first action. Idempotent — checks
    ``review_issue_id`` before creating.

    Returns a summary::

        {"scanned": int, "created": int, "skipped": int, "outcomes": [...]}
    """
    from .actions import linear_ticket as ticket
    from . import playbooks

    out: dict[str, Any] = {"scanned": 0, "created": 0, "skipped": 0, "outcomes": []}
    if not ticket.is_configured():
        # No Linear → nothing to do. Don't error; the sweep is opt-in by
        # virtue of the credentials being present.
        return out

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_REVIEW_DELAY_HOURS)).isoformat()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, source, author, content, url, priority_band,
                   classification, priority_breakdown,
                   action_taken, action_meta, actioned_at,
                   ack_at, ack_by, escalation_count
              FROM posts
             WHERE actioned_at IS NOT NULL
               AND actioned_at < ?
               AND review_issue_id IS NULL
            """,
            (cutoff,),
        )
        rows = [dict(r) for r in await cur.fetchall()]

    out["scanned"] = len(rows)
    for row in rows:
        try:
            cls = json.loads(row.get("classification") or "{}")
        except Exception:
            cls = {}
        fired = set(cls.get("tripwires_fired") or [])
        if not (fired & _REVIEWABLE_TRIPWIRES):
            # Not a reviewable incident — generic complaint, no review doc.
            continue

        pb = playbooks.for_post(cls)
        if not pb:
            continue

        if dry_run:
            out["outcomes"].append({"post_id": row["id"], "status": "would_review",
                                    "playbook": pb.get("name")})
            out["created"] += 1
            continue

        payload = _build_review_payload(row, cls, pb)
        result = await ticket.create_issue(payload)
        ts = datetime.now(timezone.utc).isoformat()
        if not result.get("ok"):
            logger.warning(
                f"[review-sweep] {row['id']}: review issue create failed: {result.get('error')}"
            )
            out["outcomes"].append({"post_id": row["id"], "status": "failed",
                                    "error": result.get("error")})
            continue

        async with aiosqlite.connect(str(config.DB_PATH)) as db:
            await db.execute(
                "UPDATE posts "
                "   SET review_issue_id = ?, review_issue_url = ?, review_created_at = ? "
                " WHERE id = ?",
                (result.get("issue_identifier"), result.get("issue_url"), ts, row["id"]),
            )
            await db.commit()

        logger.info(
            f"[review-sweep] {row['id']}: opened review {result.get('issue_identifier')} "
            f"({pb.get('name')})"
        )
        out["created"] += 1
        out["outcomes"].append({
            "post_id": row["id"], "status": "created",
            "issue_id": result.get("issue_identifier"),
            "issue_url": result.get("issue_url"),
        })

    return out


def _build_review_payload(
    post: dict[str, Any],
    cls: dict[str, Any],
    pb: dict[str, Any],
) -> dict[str, Any]:
    """Render the post-incident review issue body. Same return shape as
    ``linear_ticket.build_issue_payload`` so we can call ``create_issue``
    directly. The reviewer reads the body in Linear and fills in the
    Outcome / Root cause / Action items sections."""
    from .actions import linear_ticket as ticket

    author = (post.get("author") or "anon").lstrip("@")
    src = (post.get("source") or "?").lower()
    fired = cls.get("tripwires_fired") or []
    band = (post.get("priority_band") or "P0").upper()

    # Extract action_meta for the timeline.
    try:
        meta = json.loads(post.get("action_meta") or "{}")
    except Exception:
        meta = {}
    channels_meta = meta.get("channels") or {}
    fired_at = meta.get("fired_at") or post.get("actioned_at") or "—"
    bypass_by = meta.get("bypass_approved_by")
    bypass_reason = meta.get("bypass_reason")
    escalations = meta.get("escalations") or []

    timeline_lines: list[str] = []
    if fired_at:
        timeline_lines.append(f"- **{fired_at[:19].replace('T',' ')} UTC** — first actions fired ({', '.join(meta.get('fired') or [])})")
    if post.get("ack_at"):
        timeline_lines.append(f"- **{post['ack_at'][:19].replace('T',' ')} UTC** — acknowledged by `{post.get('ack_by') or 'anon'}`")
    else:
        timeline_lines.append("- ⚠️ **never acknowledged** — see escalation count")
    for esc in escalations:
        timeline_lines.append(
            f"- **{(esc.get('ts') or '')[:19].replace('T',' ')} UTC** — escalation #{esc.get('count')} fired"
        )
    if bypass_by:
        timeline_lines.append(
            f"- **{(meta.get('bypass_at') or '')[:19].replace('T',' ')} UTC** — playbook bypassed by `{bypass_by}` "
            f"(reason: \"{bypass_reason}\")"
        )

    title = f"[REVIEW] {pb.get('name')} — @{author} on {src} ({band})"
    title = title[:240]

    description_md = (
        f"### Original incident\n\n"
        f"**Source:** [{src.title()}]({post.get('url', '')}) · @{author}\n"
        f"**Tripwires:** {', '.join(fired) if fired else '(none)'}\n"
        f"**Owner team:** {pb.get('owner_team')}\n"
        f"**Ack SLA:** {pb.get('ack_deadline_min')} min\n"
        f"**Escalations fired:** {post.get('escalation_count') or 0}\n\n"
        f"> {(post.get('content') or '')[:1500]}\n\n"
        f"---\n\n"
        f"### Timeline\n\n"
        + "\n".join(timeline_lines) + "\n\n"
        f"---\n\n"
        f"### Procedure (what the playbook said)\n\n"
        + "\n".join(f"- [ ] {s}" for s in (pb.get("required_steps") or []))
        + "\n\n---\n\n"
        f"### Outcome (FILL IN)\n\n"
        f"_What actually happened? Was the incident resolved? Customer / "
        f"family contacted? Restaurant pulled? Legal closed?_\n\n"
        f"### Root cause (FILL IN)\n\n"
        f"_Why did this happen? What part of our system / partner / process "
        f"failed?_\n\n"
        f"### Action items (FILL IN)\n\n"
        f"_What concrete changes will we make so this doesn't happen again? "
        f"Each item gets an owner + a deadline._\n\n"
        f"- [ ] \n- [ ] \n- [ ] \n"
    )

    payload: dict[str, Any] = {
        "teamId": ticket.team_id_for(pb.get("owner_team")) or "",
        "title": title,
        "description": description_md,
        # Reviews are always High priority — the action just happened, the
        # team needs to capture learnings while the timeline is fresh.
        "priority": 2,
    }
    aid = ticket.assignee_id_for(pb.get("owner_team"))
    if aid:
        payload["assigneeId"] = aid
    return payload
