"""Engagement velocity — snapshot-based growth tracking for the priority score.

Activates the `velocity` signal in `social_watch/priority.py` (currently
stubbed at 0) by re-fetching engagement counts for recently-posted tweets
and comparing them across snapshots. Growth-per-hour above ~30 (3× the
~10/hr baseline cited by Brandwatch/Pulsar — see deep-dive §7.1) maps to
a normalized score of 1.0.

Twitter only for v1. Reddit's `score`/`num_comments` already live in the
post's `metadata` and aren't worth the extra HTTP cost to re-fetch — the
classifier doesn't need sub-hour Reddit precision and the per-post
re-scrape would dwarf any other Reddit traffic we make.

Architecture:

  TIER 1 — take_snapshots()
            Pick top-N highest-priority unchecked Twitter posts younger
            than 24h. For each one, navigate to its URL and read the
            current like/retweet/reply/view counts via page.evaluate.
            Persist into engagement_snapshots.

  TIER 2 — compute_velocity_score(post_id)
            Read the latest two snapshots; compute
            (engagement_now - engagement_prev) / age_minutes;
            normalize as min(1, growth_per_hour / 30).

  TIER 3 — attach_velocity_to_classifications()
            For every post we just snapshotted, recompute the velocity
            score, splice it into the post's priority_breakdown JSON,
            recompute the score by summing contributions, persist back.

The first time a post gets a snapshot the velocity is 0 (one data point
isn't growth). The second snapshot gives us our first real measurement.

Budget: 30 posts per cycle hard cap. Playwright calls are ~10s each, so
this caps at ~5 minutes of work per cycle in the worst case. Failures on
any individual snapshot are logged and skipped — never abort the batch.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from loguru import logger

from . import config

# ============================================================
# Tunables (kept narrow on purpose — operator-tunable, not magic)
# ============================================================

# Skip posts older than this — engagement growth past 24h is rarely
# decision-actionable, and we want the snapshot budget on hot posts.
_MAX_AGE_HOURS = 24

# Don't snapshot the same post more often than this — wastes the budget.
_MIN_RESNAPSHOT_MINUTES = 10

# 30 = ~3× baseline of ~10 engagements/hour (deep-dive §7.1)
_GROWTH_BASELINE = 30.0

# Ceiling on the Playwright work per cycle.
_DEFAULT_BUDGET = 30

_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ============================================================
# Schema — runs idempotently on every init
# ============================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS engagement_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL,
    source          TEXT NOT NULL,
    snapshot_at     TIMESTAMP NOT NULL,
    like_count      INTEGER,
    retweet_count   INTEGER,
    reply_count     INTEGER,
    view_count      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_eng_snap_post_at
    ON engagement_snapshots(post_id, snapshot_at);
CREATE INDEX IF NOT EXISTS idx_eng_snap_at
    ON engagement_snapshots(snapshot_at);
"""


async def ensure_schema() -> None:
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


# ============================================================
# Engagement extraction — match the scraper's pattern
# ============================================================

# Walks the FIRST visible <article data-testid="tweet"> on a tweet's
# permalink page and returns its engagement counts. The first article on
# `/<user>/status/<id>` is the tweet itself; subsequent articles are
# replies and we skip them.
_TWEET_ENGAGEMENT_JS = r"""
() => {
  const art = document.querySelector('article[data-testid="tweet"]');
  if (!art) return null;
  const stat = (testid) => {
    const el = art.querySelector('[data-testid="' + testid + '"]');
    if (!el) return null;
    const span = el.querySelector('span');
    return span ? span.innerText.trim() : null;
  };
  return {
    reply_count: stat('reply'),
    retweet_count: stat('retweet'),
    like_count: stat('like'),
    view_count: stat('analytics'),
  };
}
"""


# Twitter formats counts as "1,234", "1.2K", "3.4M", or just "5". Parse
# back to an int; missing/unparseable → None.
def _parse_count(s: str | None) -> int | None:
    if s is None:
        return None
    t = s.strip().replace(",", "")
    if not t:
        return 0
    m = re.match(r"^([\d.]+)\s*([KMB]?)$", t, re.I)
    if not m:
        # Sometimes Twitter shows "Reply" with no count → treat as 0
        return 0 if t.lower() in ("", "reply", "retweet", "like", "view") else None
    val = float(m.group(1))
    suffix = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return int(val * mult)


def _engagement_total(snap: dict[str, Any]) -> int:
    """Combined weighted engagement, matching priority._signal_reach.
    Likes + 5×retweets + 0.5×replies (views are ambient, excluded)."""
    likes = int(snap.get("like_count") or 0)
    retweets = int(snap.get("retweet_count") or 0)
    replies = int(snap.get("reply_count") or 0)
    return int(likes + 5 * retweets + 0.5 * replies)


# ============================================================
# Helper: shared Playwright X context with cookies
# (mirrors social_watch.responses._make_x_context)
# ============================================================

async def _make_x_context(browser: Any) -> Any:
    context = await browser.new_context(
        user_agent=_DESKTOP_UA,
        viewport={"width": 1366, "height": 900},
    )
    await context.add_cookies([
        {
            "name": "auth_token",
            "value": config.TWITTER_COOKIE_AUTH_TOKEN,
            "domain": ".x.com", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax",
        },
        {
            "name": "ct0",
            "value": config.TWITTER_COOKIE_CT0,
            "domain": ".x.com", "path": "/",
            "secure": True, "httpOnly": False, "sameSite": "Lax",
        },
    ])
    return context


# ============================================================
# TIER 1 — take_snapshots
# ============================================================

async def take_snapshots(*, budget: int = _DEFAULT_BUDGET) -> dict[str, Any]:
    """Fetch fresh engagement counts for top-N hot Twitter posts <24h old.

    Selects posts by priority_score DESC then age. Skips posts that already
    have a snapshot in the last `_MIN_RESNAPSHOT_MINUTES` (those are still
    'fresh' enough — wait for the gap to reopen).

    Returns counts dict for caller logging. Never raises on a single bad
    post; logs and continues.
    """
    await ensure_schema()
    counts: dict[str, Any] = {
        "selected": 0, "snapshotted": 0, "errors": 0, "skipped_recent": 0
    }
    if not (config.TWITTER_COOKIE_USERNAME and config.TWITTER_COOKIE_AUTH_TOKEN):
        logger.debug("velocity.take_snapshots skipped: no Twitter cookies")
        return counts
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("velocity.take_snapshots: playwright not installed")
        return counts

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_MAX_AGE_HOURS)).isoformat()
    recent_cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=_MIN_RESNAPSHOT_MINUTES)
    ).isoformat()

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Pick top-N posts that:
        #  * are Twitter
        #  * are <24h old
        #  * either have NEVER been snapshotted, or last snapshot was
        #    older than _MIN_RESNAPSHOT_MINUTES (so we can compute deltas)
        cur = await db.execute(
            """
            SELECT p.id, p.native_id, p.url, p.priority_score,
                   p.created_at,
                   (SELECT MAX(snapshot_at) FROM engagement_snapshots s
                    WHERE s.post_id = p.id) AS last_snap
            FROM posts p
            WHERE p.source = 'twitter'
              AND p.url IS NOT NULL
              AND p.created_at >= ?
            ORDER BY COALESCE(p.priority_score, 0) DESC, p.created_at DESC
            LIMIT ?
            """,
            (cutoff, max(budget * 3, budget)),  # over-fetch then filter
        )
        candidates = [dict(r) for r in await cur.fetchall()]

    targets: list[dict[str, Any]] = []
    for c in candidates:
        if c.get("last_snap") and c["last_snap"] > recent_cutoff:
            counts["skipped_recent"] += 1
            continue
        targets.append(c)
        if len(targets) >= budget:
            break
    counts["selected"] = len(targets)
    if not targets:
        logger.info(f"velocity.take_snapshots: no eligible posts (skipped_recent={counts['skipped_recent']})")
        return counts

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await _make_x_context(browser)
            for t in targets:
                try:
                    snap = await _snapshot_one(context, t["url"])
                except Exception as e:
                    # Defensive: any browser/timeout/parse failure is per-post
                    logger.debug(f"velocity: snapshot {t['native_id']} failed: {e}")
                    counts["errors"] += 1
                    continue
                if snap is None:
                    counts["errors"] += 1
                    continue
                async with aiosqlite.connect(str(config.DB_PATH)) as db:
                    await db.execute(
                        """
                        INSERT INTO engagement_snapshots
                          (post_id, source, snapshot_at,
                           like_count, retweet_count, reply_count, view_count)
                        VALUES (?, 'twitter', ?, ?, ?, ?, ?)
                        """,
                        (
                            t["id"],
                            datetime.now(timezone.utc).isoformat(),
                            snap.get("like_count"),
                            snap.get("retweet_count"),
                            snap.get("reply_count"),
                            snap.get("view_count"),
                        ),
                    )
                    await db.commit()
                counts["snapshotted"] += 1
        finally:
            await browser.close()

    logger.info(
        f"velocity.take_snapshots: selected={counts['selected']} "
        f"snapshotted={counts['snapshotted']} errors={counts['errors']} "
        f"skipped_recent={counts['skipped_recent']}"
    )
    return counts


async def _snapshot_one(context: Any, tweet_url: str) -> dict[str, Any] | None:
    """Navigate to a tweet URL and read engagement counts. Returns parsed
    int counts, or None if the tweet isn't reachable."""
    page = await context.new_page()
    try:
        await page.goto(tweet_url, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_selector('article[data-testid="tweet"]', timeout=8000)
        except Exception:
            return None
        # Brief settle so reaction counts hydrate
        await page.wait_for_timeout(800)
        raw = await page.evaluate(_TWEET_ENGAGEMENT_JS)
        if not raw:
            return None
        return {
            "like_count": _parse_count(raw.get("like_count")),
            "retweet_count": _parse_count(raw.get("retweet_count")),
            "reply_count": _parse_count(raw.get("reply_count")),
            "view_count": _parse_count(raw.get("view_count")),
        }
    finally:
        await page.close()


# ============================================================
# TIER 2 — compute_velocity_score
# ============================================================

async def compute_velocity_score(post_id: str) -> float:
    """Velocity = (engagement_now - engagement_prev) / age_minutes,
    normalized as min(1, growth_per_hour / 30).

    With <2 snapshots the score is 0 (need two points for a delta). One
    delta is sufficient — we don't smooth across more than two for v1
    because reaction counts are noisy and the latest delta is the most
    decision-relevant signal."""
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT snapshot_at, like_count, retweet_count, reply_count, view_count
            FROM engagement_snapshots
            WHERE post_id = ?
            ORDER BY snapshot_at DESC
            LIMIT 2
            """,
            (post_id,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    if len(rows) < 2:
        return 0.0
    now_row, prev_row = rows[0], rows[1]
    eng_now = _engagement_total(now_row)
    eng_prev = _engagement_total(prev_row)
    try:
        ts_now = datetime.fromisoformat(str(now_row["snapshot_at"]).replace("Z", "+00:00"))
        ts_prev = datetime.fromisoformat(str(prev_row["snapshot_at"]).replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if ts_now.tzinfo is None:
        ts_now = ts_now.replace(tzinfo=timezone.utc)
    if ts_prev.tzinfo is None:
        ts_prev = ts_prev.replace(tzinfo=timezone.utc)
    age_min = (ts_now - ts_prev).total_seconds() / 60.0
    if age_min <= 0:
        return 0.0
    delta = max(0, eng_now - eng_prev)
    growth_per_hour = (delta / age_min) * 60.0
    return float(min(1.0, growth_per_hour / _GROWTH_BASELINE))


# ============================================================
# TIER 3 — attach_velocity_to_classifications
# ============================================================

# Must match priority._WEIGHTS["velocity"] — kept as a constant here so
# we don't import the priority module just for one number (keeps this
# module callable from the priority module as well, no cycle).
_VELOCITY_WEIGHT = 0.15


def _band_for_score(score: float) -> str:
    if score >= 0.85:
        return "P0"
    if score >= 0.65:
        return "P1"
    if score >= 0.40:
        return "P2"
    return "P3"


async def attach_velocity_to_classifications() -> dict[str, int]:
    """For every post that has 2+ snapshots, recompute the velocity score
    and splice it into the post's priority_breakdown JSON. The score is
    re-derived by summing contributions (no need to re-run the full
    priority calculation — only one signal changed, and the formula is
    additive).

    Tripwire-overridden posts are skipped (their score is locked at 1.0).
    """
    counts = {"considered": 0, "updated": 0, "skipped_tripwire": 0, "skipped_no_breakdown": 0}
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Posts that have >=2 snapshots
        cur = await db.execute(
            """
            SELECT p.id, p.priority_score, p.priority_band, p.priority_breakdown
            FROM posts p
            WHERE p.id IN (
                SELECT post_id FROM engagement_snapshots
                GROUP BY post_id
                HAVING COUNT(*) >= 2
            )
            """
        )
        rows = [dict(r) for r in await cur.fetchall()]

        for r in rows:
            counts["considered"] += 1
            raw_breakdown = r.get("priority_breakdown")
            if not raw_breakdown:
                counts["skipped_no_breakdown"] += 1
                continue
            try:
                breakdown = json.loads(raw_breakdown)
            except Exception:
                counts["skipped_no_breakdown"] += 1
                continue
            if breakdown.get("tripwire_override"):
                counts["skipped_tripwire"] += 1
                continue

            v = await compute_velocity_score(r["id"])
            signals = breakdown.setdefault("signals", {})
            contributions = breakdown.setdefault("contributions", {})
            signals["velocity"] = round(v, 4)
            contributions["velocity"] = round(v * _VELOCITY_WEIGHT, 4)

            score = sum(float(x) for x in contributions.values())
            score = max(0.0, min(1.0, score))
            band = _band_for_score(score)

            # Refresh the "reason" string with new top contributors
            pos = sorted(
                ((k, v2) for k, v2 in contributions.items() if v2 > 0),
                key=lambda kv: -kv[1],
            )
            top = ", ".join(f"{k}={v2:.2f}" for k, v2 in pos[:3])
            breakdown["reason"] = f"top contributors: {top}" if top else "low-signal post"
            breakdown["score"] = round(score, 4)
            breakdown["band"] = band

            await db.execute(
                """
                UPDATE posts SET
                    priority_score = ?,
                    priority_band = ?,
                    priority_breakdown = ?
                WHERE id = ?
                """,
                (
                    round(score, 4),
                    band,
                    json.dumps(breakdown, default=str),
                    r["id"],
                ),
            )
            counts["updated"] += 1
        await db.commit()

    logger.info(
        f"velocity.attach: considered={counts['considered']} "
        f"updated={counts['updated']} "
        f"skipped_tripwire={counts['skipped_tripwire']} "
        f"skipped_no_breakdown={counts['skipped_no_breakdown']}"
    )
    return counts
