"""Auto-reply policy — the system that *closes the gap* between alert
and action.

Without this module, the dashboard surfaces "186 critical posts overdue"
and the team has to type the same templated reply ("Hi @user, sorry —
please DM us your order ID") 186 times by hand. A median post is a
templated response; humans should not be the typing layer for it.

This module owns one decision and one loop:

  • ``is_eligible(post, cls, pri) -> (bool, reason)``
        The single gate. Used by both the background sweep and the
        operator-driven drain modal so the same rules apply everywhere.

  • ``sweep()``
        Background tick. Pulls candidate posts, runs them through the
        gate, fires templated replies through the existing
        ``twitter_reply.build_and_send`` path. Throttled, capped, and
        guarded by a kill-switch env var so a misconfigured deploy
        never blasts the queue.

Defense in depth (10 stacked guards) is documented in the plan file
and enforced inline below. A misclassified post would have to slip
past every guard to fire; any false positive lands in
``action_meta.trigger = "auto_reply_v1"`` for audit + retraining.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from loguru import logger

from . import config

# ============================================================
# Config — every knob lives here so tomorrow's PM can re-tune
# the policy from one place.
# ============================================================

# Global kill-switch. Default OFF so a fresh deploy never auto-replies
# until an operator opts in.
ENABLE_ENV = "AUTO_REPLY_ENABLED"

# Optional dry-run — log what we'd fire, don't actually send.
DRY_RUN_ENV = "AUTO_REPLY_DRY_RUN"

# Replies per single sweep tick. With a 5-min cycle this caps at
# ~120/hr — well under Twitter's free-tier rate ceiling.
_SWEEP_CAP = 10

# Sleep between fires inside one sweep so we don't trip rate limits or
# look like a bot to X's anti-spam.
_FIRE_DELAY_S = 2.0

# How long after classification before we trust the LLM's tripwire pass.
# A post that just landed might still get a hard tripwire applied 30s
# later when the LLM batch runs; waiting protects against firing on a
# soon-to-be-tripwired post.
_COOLING_OFF_MIN = 2

# One auto-reply per author per this many minutes. Stops the bot from
# spamming the same angry user with N "DM us your order ID" replies if
# they post 5 complaints in 10 minutes.
_AUTHOR_THROTTLE_MIN = 60

# Channels considered safe to auto-reply on. Twitter only — Reddit
# threads are public and a tone-deaf comment gets screenshot.
_SAFE_SOURCES: frozenset[str] = frozenset({"twitter"})

# Audiences a bot can address with the existing template set without
# needing a human. Anything else (legal, pr, founder-office,
# trust-safety, safety) requires human review.
_SAFE_AUDIENCES: frozenset[str] = frozenset({"customer-care", "ops"})


# ============================================================
# Schema migration — author dedupe table
# ============================================================
#
# Could query action_meta for prior auto-fires, but parsing 10k+ JSON
# blobs every sweep is wasteful. One small append-only table indexed
# on (author, source) gives O(log N) "have we replied to this user
# recently?" lookups.

_DEDUPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_reply_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    author    TEXT NOT NULL,
    source    TEXT NOT NULL,
    post_id   TEXT NOT NULL,
    fired_at  TIMESTAMP NOT NULL,
    trigger   TEXT NOT NULL DEFAULT 'auto_reply_v1'
);
CREATE INDEX IF NOT EXISTS idx_auto_reply_log_author_source_ts
   ON auto_reply_log(author, source, fired_at DESC);
""".strip()


async def ensure_schema(db: aiosqlite.Connection | None = None) -> None:
    """Idempotent. Called once from server boot + safe to re-call."""
    own_conn = db is None
    if own_conn:
        db = await aiosqlite.connect(str(config.DB_PATH))
    try:
        await db.executescript(_DEDUPE_SCHEMA)
        await db.commit()
    finally:
        if own_conn and db is not None:
            await db.close()


# ============================================================
# Knobs / state
# ============================================================

def is_enabled() -> bool:
    return (os.getenv(ENABLE_ENV) or "0").strip().lower() in ("1", "true", "yes", "on")


def is_dry_run() -> bool:
    return (os.getenv(DRY_RUN_ENV) or "0").strip().lower() in ("1", "true", "yes", "on")


# ============================================================
# Eligibility gate (single source of truth)
# ============================================================

def is_eligible(
    post: dict[str, Any],
    classification: dict[str, Any] | None,
    priority_breakdown: dict[str, Any] | None,
    *,
    author_last_reply_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """The single gate. Used by both the bg sweep and the drain modal.

    ``author_last_reply_at`` is the timestamp of the most recent
    auto-reply we sent to this post's author (None if never).
    ``now`` defaults to UTC now; injected for testing.

    Returns (eligible, reason). The reason is a short label intended
    for ``action_meta`` and the audit log — both human-readable and
    grep-able.
    """
    cls = classification or {}
    pri = priority_breakdown or {}
    now = now or datetime.now(timezone.utc)

    # Guard 1: post must actually exist and have a URL we can reply to.
    if not post.get("url"):
        return False, "no_url"

    # Guard 2: source whitelist — Twitter only.
    src = (post.get("source") or "").lower()
    if src not in _SAFE_SOURCES:
        return False, f"source_not_in_scope:{src or 'unknown'}"

    # Guard 3: must be classified, not just pre-classified placeholder.
    if not cls.get("method"):
        return False, "not_classified"

    # Guard 4: classifier-flagged safe (no tripwires + no abuse + no
    # profanity + no sarcasm). This is THE primary gate — every other
    # check is defense in depth on top of it.
    if not cls.get("auto_action_safe"):
        return False, "auto_action_safe=false"

    # Guard 5: priority defense — if the priority engine forced a P0
    # via tripwire override, never auto-reply, even if classification
    # somehow reported safe.
    if pri.get("tripwire_override"):
        return False, "priority_tripwire_override"

    # Guard 6: audience whitelist — only customer-care and ops have
    # bot-safe templates in twitter_reply.build_reply_text. Anything
    # else (legal, pr, safety, founder-office, trust-safety) requires
    # human judgment.
    audience = cls.get("audience") or []
    if not isinstance(audience, list):
        return False, "audience_malformed"
    if not audience:
        return False, "audience_empty"
    aud_set = {str(a).strip().lower() for a in audience}
    if not aud_set.issubset(_SAFE_AUDIENCES):
        # At least one audience is outside our safe set.
        bad = aud_set - _SAFE_AUDIENCES
        return False, f"audience_requires_human:{','.join(sorted(bad))}"

    # Guard 7: don't re-reply if Zomato already replied or we already
    # auto-replied to this exact post.
    rs = (post.get("zomato_response_status") or "").lower()
    if rs == "replied":
        return False, "zomato_already_replied"
    taken = (post.get("action_taken") or "").split("+")
    if "twitter_reply" in taken:
        return False, "already_replied"

    # Guard 8: cooling-off window — give the LLM tripwire pass time to
    # apply. A post that JUST landed is dangerous because the rules
    # may say "auto_action_safe" but the LLM batch hasn't run yet to
    # detect e.g. sarcasm or a death allegation.
    created_iso = post.get("created_at") or ""
    try:
        created_at = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
    except Exception:
        return False, "created_at_unparseable"
    age = now - created_at
    if age < timedelta(minutes=_COOLING_OFF_MIN):
        return False, f"cooling_off:age={int(age.total_seconds())}s"

    # Guard 9: per-author throttle — at most one auto-reply per author
    # per _AUTHOR_THROTTLE_MIN minutes. Stops the bot from machine-
    # gunning the same user with the same template on a post burst.
    if author_last_reply_at is not None:
        delta = now - author_last_reply_at
        if delta < timedelta(minutes=_AUTHOR_THROTTLE_MIN):
            mins = int(delta.total_seconds() / 60)
            return False, f"author_throttled:last_reply_{mins}m_ago"

    return True, "eligible"


# ============================================================
# Author dedupe lookup
# ============================================================

async def _last_reply_for_author(
    db: aiosqlite.Connection,
    author: str,
    source: str,
) -> datetime | None:
    """Most recent auto-reply ts for this author. Returns None if never."""
    if not author:
        return None
    cur = await db.execute(
        "SELECT fired_at FROM auto_reply_log "
        " WHERE author = ? AND source = ? "
        " ORDER BY fired_at DESC LIMIT 1",
        (author.lstrip("@"), source.lower()),
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        ts = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


async def _record_auto_reply(
    db: aiosqlite.Connection,
    *,
    author: str,
    source: str,
    post_id: str,
    fired_at: datetime,
    trigger: str = "auto_reply_v1",
) -> None:
    """Append to auto_reply_log so the next sweep sees this author throttled."""
    await db.execute(
        "INSERT INTO auto_reply_log (author, source, post_id, fired_at, trigger) "
        "VALUES (?, ?, ?, ?, ?)",
        (author.lstrip("@"), source.lower(), post_id, fired_at.isoformat(), trigger),
    )
    await db.commit()


# ============================================================
# Sweep — the background tick
# ============================================================

async def sweep(*, dry_run: bool | None = None) -> dict[str, Any]:
    """One pass: scan candidates, fire eligible ones, return summary.

    Returns::

        {
          "enabled": bool,
          "scanned": int,
          "fired":   int,
          "skipped": int,
          "failed":  int,
          "outcomes": [{"post_id", "status", "reason", ...}],
        }

    Always returns a dict (never raises) so the bg loop is safe.
    """
    enabled = is_enabled()
    dry = is_dry_run() if dry_run is None else dry_run
    summary: dict[str, Any] = {
        "enabled": enabled,
        "dry_run": dry,
        "scanned": 0,
        "fired":   0,
        "skipped": 0,
        "failed":  0,
        "outcomes": [],
    }
    if not enabled:
        return summary

    await ensure_schema()

    # Pull candidates: classified twitter posts where we haven't replied,
    # not Zomato-replied, ordered by oldest first (clear backlog tail
    # before fresh arrivals).
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, source, author, content, url, created_at,
                   classification, priority_breakdown, priority_band,
                   zomato_response_status, action_taken
              FROM posts
             WHERE source = 'twitter'
               AND created_at >= ?
               AND (action_taken IS NULL OR instr(action_taken, 'twitter_reply') = 0)
               AND (zomato_response_status IS NULL OR zomato_response_status != 'replied')
               AND classification IS NOT NULL
               AND json_extract(classification, '$.auto_action_safe') = 1
             ORDER BY created_at ASC
             LIMIT 100
            """,
            (cutoff,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    summary["scanned"] = len(rows)
    if not rows:
        return summary

    # Lazy-import the connector so this module is cheap to import in
    # contexts that don't fire (tests, smoke).
    from .actions import twitter_reply as twr

    fired = 0
    for row in rows:
        if fired >= _SWEEP_CAP:
            break
        try:
            cls = json.loads(row.get("classification") or "{}")
            pri = json.loads(row.get("priority_breakdown") or "{}")
        except Exception:
            cls, pri = {}, {}

        async with aiosqlite.connect(str(config.DB_PATH)) as db:
            last = await _last_reply_for_author(db, row.get("author") or "", row.get("source") or "")

        eligible, reason = is_eligible(row, cls, pri, author_last_reply_at=last)
        if not eligible:
            summary["skipped"] += 1
            summary["outcomes"].append(
                {"post_id": row["id"], "status": "skipped", "reason": reason}
            )
            continue

        if dry:
            summary["fired"] += 1
            summary["outcomes"].append(
                {"post_id": row["id"], "status": "dry_run", "reason": reason}
            )
            logger.info(f"[auto-reply] DRY: would fire {row['id']} ({reason})")
            continue

        # Fire: same code path as the manual reply, so we get template
        # generation + Playwright session + verification.
        fired_at = datetime.now(timezone.utc)
        try:
            payload, result = await twr.build_and_send(row, cls, pri)
        except Exception as e:
            logger.exception(f"[auto-reply] {row['id']}: build_and_send raised")
            summary["failed"] += 1
            summary["outcomes"].append(
                {"post_id": row["id"], "status": "failed", "reason": f"exception:{type(e).__name__}"}
            )
            continue

        # Persist the action — same shape as dispatcher writes for
        # manual fires, plus the auto-reply trigger marker.
        await _persist_fire(
            row, payload, result,
            trigger="auto_reply_v1",
            eligibility_reason=reason,
            fired_at=fired_at,
        )

        if result.get("ok"):
            fired += 1
            summary["fired"] += 1
            summary["outcomes"].append(
                {"post_id": row["id"], "status": "fired", "reason": reason}
            )
            logger.info(f"[auto-reply] {row['id']}: fired ({reason})")
            # Record for author dedupe.
            async with aiosqlite.connect(str(config.DB_PATH)) as db:
                await _record_auto_reply(
                    db,
                    author=row.get("author") or "",
                    source=row.get("source") or "twitter",
                    post_id=row["id"],
                    fired_at=fired_at,
                    trigger="auto_reply_v1",
                )
        else:
            summary["failed"] += 1
            summary["outcomes"].append(
                {"post_id": row["id"], "status": "failed",
                 "reason": result.get("error") or "send_failed"}
            )
            logger.warning(
                f"[auto-reply] {row['id']}: send failed — {result.get('error')}"
            )

        # Throttle so we don't burst on Twitter.
        await asyncio.sleep(_FIRE_DELAY_S)

    return summary


async def _persist_fire(
    row: dict[str, Any],
    payload: dict[str, Any],
    result: dict[str, Any],
    *,
    trigger: str,
    eligibility_reason: str,
    fired_at: datetime,
) -> None:
    """Merge the auto-fire into posts.action_taken / action_meta using
    the same shape the dispatcher writes for manual fires. Future
    Activity-log queries / Home KPI / inbox status chip see one
    consistent format regardless of who pulled the trigger."""
    ts = fired_at.isoformat()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT action_taken, action_meta FROM posts WHERE id = ?", (row["id"],)
        )
        prev = await cur.fetchone()
        prior_meta: dict[str, Any] = {}
        if prev and prev["action_meta"]:
            try:
                prior_meta = json.loads(prev["action_meta"])
            except Exception:
                prior_meta = {}
        prior_channels = prior_meta.get("channels") or {}

        prior_channels["twitter_reply"] = {
            "ok":     bool(result.get("ok")),
            "status": result.get("status", 0),
            "ts":     result.get("ts") or ts,
            "error":  result.get("error"),
            "payload": payload,
            "result":  result,
        }
        new_meta = {
            **prior_meta,
            "fired_at": ts,
            "trigger": trigger,
            "auto_reply_eligibility_reason": eligibility_reason,
            "channels": prior_channels,
            "fired":  sorted({n for n, r in prior_channels.items() if r.get("ok")}),
            "failed": sorted({n for n, r in prior_channels.items() if r.get("ok") is False}),
        }
        prior_taken = (prev["action_taken"] if prev and prev["action_taken"] else "").split("+")
        union = sorted({s for s in prior_taken + new_meta["fired"] if s})
        action_taken = "+".join(union) if union else (prev["action_taken"] if prev else None)

        await db.execute(
            "UPDATE posts SET action_taken=?, action_meta=?, actioned_at=? WHERE id=?",
            (action_taken, json.dumps(new_meta, default=str), ts, row["id"]),
        )
        await db.commit()


# ============================================================
# Sanity tests — run with `python -m social_watch.auto_reply`
# Pure (no DB / no network). Exercises the eligibility gate's branches.
# ============================================================

if __name__ == "__main__":
    now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    old_post_iso = (now - timedelta(minutes=10)).isoformat()
    fresh_post_iso = (now - timedelta(seconds=30)).isoformat()

    base_post = {
        "id": "twitter:1",
        "source": "twitter",
        "author": "@alice",
        "url": "https://x.com/alice/status/1",
        "created_at": old_post_iso,
        "zomato_response_status": "unchecked",
        "action_taken": None,
    }
    base_cls = {
        "method": "rules-only",
        "auto_action_safe": True,
        "audience": ["customer-care"],
        "tripwires_fired": [],
    }
    base_pri = {"band": "P3", "tripwire_override": False}

    # All cases share a fixed `now` so the cooling-off math is deterministic.
    NOW = {"now": now}
    cases: list[tuple[str, dict, dict, dict, dict, bool, str]] = [
        # (name, post, cls, pri, kwargs, expected_eligible, expected_reason_substr)
        ("baseline customer-care",          base_post, base_cls, base_pri, NOW, True,  "eligible"),
        ("ops audience also ok",            base_post, {**base_cls, "audience": ["ops"]}, base_pri, NOW, True, "eligible"),
        ("not classified",                  base_post, {**base_cls, "method": None}, base_pri, NOW, False, "not_classified"),
        ("auto_action_safe=false",          base_post, {**base_cls, "auto_action_safe": False}, base_pri, NOW, False, "auto_action_safe"),
        ("tripwire_override",               base_post, base_cls, {**base_pri, "tripwire_override": True}, NOW, False, "priority_tripwire"),
        ("audience requires human (legal)", base_post, {**base_cls, "audience": ["legal"]}, base_pri, NOW, False, "audience_requires_human"),
        ("audience mixed (cc + pr)",        base_post, {**base_cls, "audience": ["customer-care", "pr"]}, base_pri, NOW, False, "audience_requires_human"),
        ("no audience",                     base_post, {**base_cls, "audience": []}, base_pri, NOW, False, "audience_empty"),
        ("reddit out of scope",             {**base_post, "source": "reddit"}, base_cls, base_pri, NOW, False, "source_not_in_scope"),
        ("zomato already replied",          {**base_post, "zomato_response_status": "replied"}, base_cls, base_pri, NOW, False, "zomato_already_replied"),
        ("twitter_reply already fired",     {**base_post, "action_taken": "twitter_reply"}, base_cls, base_pri, NOW, False, "already_replied"),
        ("post too fresh (cooling off)",    {**base_post, "created_at": fresh_post_iso}, base_cls, base_pri, NOW, False, "cooling_off"),
        ("author throttled",                base_post, base_cls, base_pri, {**NOW, "author_last_reply_at": now - timedelta(minutes=20)}, False, "author_throttled"),
        ("author throttle expired",         base_post, base_cls, base_pri, {**NOW, "author_last_reply_at": now - timedelta(minutes=90)}, True, "eligible"),
        ("missing url",                     {**base_post, "url": ""}, base_cls, base_pri, NOW, False, "no_url"),
    ]

    fail = 0
    for name, post, cls, pri, kw, expect_ok, expect_reason in cases:
        ok, reason = is_eligible(post, cls, pri, **kw)
        passed = (ok == expect_ok) and (expect_reason in reason)
        marker = "PASS" if passed else "FAIL"
        if not passed:
            fail += 1
        print(f"  {marker}  {name:36s}  -> ok={ok}, reason={reason!r}  (expected {expect_ok}, contains {expect_reason!r})")
    print(f"\n{len(cases) - fail}/{len(cases)} passed")
    raise SystemExit(0 if fail == 0 else 1)
