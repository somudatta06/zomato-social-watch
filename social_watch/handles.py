"""Author profile cache — the "who is this poster?" engine.

Every unique (handle, source) pair gets a row in the `handles` table. The
row carries:
  - profile_class + tier — the answer the priority scorer needs
  - reach multiplier — what compute_priority actually uses
  - watchlists — which curated lists this handle is on (press, politician...)
  - rolling stats — first_seen_at, last_seen_at, total_posts, prior_complaints

Tier mapping (see docs/CLASSIFICATION_DEEP_DIVE.md §8):

    Tier   Class            Multiplier   Trigger
    ----   --------------   ----------   ----------------------------------
    T0     authority        10.0         AUTHORITY_HANDLES watchlist
    T0f    founder          10.0         FOUNDER_HANDLES (Zomato leadership)
    T1     press            5.0          PRESS_HANDLES OR press bio keywords
    T1p    politician       5.0          POLITICIAN_HANDLES OR pol. bio kw
    T2     influencer       3.0          >1M followers OR (verified+niche)
    T3     power_user       2.0          100K-1M followers
    T4     active_citizen   1.5          10K-100K followers
    T5     regular          1.0          <10K, real profile (default)
    T6     anonymous        1.0          pseudonymous, low activity
    T7     bot              0.3          high volume + low diversity + new

Twitter `metadata` does NOT currently carry follower_count or verified
status (the Playwright scraper extracts engagement counts only). So the
follower-based tiers (T2/T3/T4) only fire if/when an upstream populates
them. Until then, the system gracefully degrades to T5 (1.0) for unknown
authors — which means existing posts don't get a wrong upgrade.

This module is intentionally synchronous-friendly: priority computation is
a tight loop and the handle lookup must be a fast indexed read on
(handle, source).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite
from loguru import logger

from . import watchlists


# ============================================================
# Tier → multiplier mapping (single source of truth)
# ============================================================
TIER_MULTIPLIERS: dict[str, float] = {
    "authority":      10.0,
    "founder":        10.0,
    "press":           5.0,
    "politician":      5.0,
    "influencer":      3.0,
    "power_user":      2.0,
    "active_citizen":  1.5,
    "regular":         1.0,
    "anonymous":       1.0,
    "bot":             0.3,
}

TIER_CODE: dict[str, str] = {
    "authority":      "T0",
    "founder":        "T0",
    "press":          "T1",
    "politician":     "T1",
    "influencer":     "T2",
    "power_user":     "T3",
    "active_citizen": "T4",
    "regular":        "T5",
    "anonymous":      "T6",
    "bot":            "T7",
}


# ============================================================
# Dataclass mirror of the `handles` row
# ============================================================
@dataclass
class Handle:
    handle: str
    source: str                                  # 'twitter' | 'reddit'
    tier: str                                    # 'T0'..'T7'
    profile_class: str                           # 'press', 'regular', ...
    multiplier: float
    watchlists: list[str] = field(default_factory=list)
    follower_count: int | None = None
    bio: str | None = None
    verified: bool = False
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    total_posts: int = 0
    prior_complaints: int = 0
    sentiment_30d_avg: float | None = None
    last_refreshed_at: str | None = None


# ============================================================
# Pure tier classifier — no DB
# ============================================================
def compute_tier(
    handle: str | None,
    metadata: dict[str, Any] | None,
    bio: str | None = None,
    source: str = "twitter",
) -> tuple[str, float, list[str]]:
    """Classify a handle into a profile_class. No DB access — purely
    deterministic from the inputs.

    Returns:
        (profile_class, multiplier, watchlist_memberships)
    """
    h = (handle or "").strip().lstrip("@").lower()
    bio_low = (bio or "").lower()
    metadata = metadata or {}

    # ----- Watchlist hits (always win over follower-count tiers) -----
    memberships = watchlists.memberships_for(h)
    if "founder" in memberships:
        return "founder", TIER_MULTIPLIERS["founder"], memberships
    if "authority" in memberships:
        return "authority", TIER_MULTIPLIERS["authority"], memberships
    # Authority/founder beats both press & politician when overlap exists
    # (e.g. PM is also on the politician list — authority wins).
    if "politician" in memberships:
        return "politician", TIER_MULTIPLIERS["politician"], memberships
    if "press" in memberships:
        return "press", TIER_MULTIPLIERS["press"], memberships

    # ----- Bio scan -----
    if bio_low:
        if any(k in bio_low for k in watchlists.PRESS_BIO_KEYWORDS):
            return "press", TIER_MULTIPLIERS["press"], memberships + ["press"]
        if any(k in bio_low for k in watchlists.POLITICIAN_BIO_KEYWORDS):
            return "politician", TIER_MULTIPLIERS["politician"], memberships + ["politician"]

    # ----- Follower-count tiers (Twitter only — Reddit has no equivalent) -----
    followers = metadata.get("user_followers")
    if followers is None:
        followers = metadata.get("follower_count")
    verified = bool(
        metadata.get("user_verified")
        or metadata.get("verified")
        or metadata.get("is_verified")
    )

    try:
        followers_int = int(followers) if followers is not None else None
    except (TypeError, ValueError):
        followers_int = None

    if followers_int is not None:
        if followers_int >= 1_000_000:
            return "influencer", TIER_MULTIPLIERS["influencer"], memberships
        if followers_int >= 100_000:
            return "power_user", TIER_MULTIPLIERS["power_user"], memberships
        if followers_int >= 10_000:
            return "active_citizen", TIER_MULTIPLIERS["active_citizen"], memberships

    # Verified-blue without follower count — bump to influencer if the bio
    # suggests niche (food/tech/business). Conservative: only if strong hit.
    if verified and bio_low and any(
        kw in bio_low for kw in (
            "food", "chef", "restaurant", "tech", "business", "startup",
            "founder", "ceo", "investor", "vc",
        )
    ):
        return "influencer", TIER_MULTIPLIERS["influencer"], memberships

    # ----- Bot heuristic (very conservative) -----
    # Only fire if metadata explicitly suggests bot-like volume + new account.
    account_age_days = metadata.get("account_age_days")
    posts_per_day = metadata.get("posts_per_day")
    try:
        if (
            account_age_days is not None
            and posts_per_day is not None
            and int(account_age_days) < 30
            and float(posts_per_day) > 50
        ):
            return "bot", TIER_MULTIPLIERS["bot"], memberships
    except (TypeError, ValueError):
        pass

    # ----- Anonymous vs regular -----
    # Reddit handles are largely pseudonymous by default. Twitter handles
    # without bio + without follower data → assume regular until shown otherwise.
    if source == "reddit":
        return "anonymous", TIER_MULTIPLIERS["anonymous"], memberships
    return "regular", TIER_MULTIPLIERS["regular"], memberships


# ============================================================
# Async DB helpers
# ============================================================
async def is_watchlisted(
    conn: aiosqlite.Connection, handle: str, list_name: str
) -> bool:
    """True if the handle has an explicit row on the named watchlist."""
    h = (handle or "").strip().lstrip("@").lower()
    if not h:
        return False
    cur = await conn.execute(
        "SELECT 1 FROM watchlist_memberships WHERE handle = ? AND list_name = ? LIMIT 1",
        (h, list_name),
    )
    return await cur.fetchone() is not None


async def upsert_handle(
    conn: aiosqlite.Connection,
    *,
    handle: str,
    source: str,
    tier: str,
    profile_class: str,
    multiplier: float,
    watchlists_list: list[str] | None = None,
    follower_count: int | None = None,
    bio: str | None = None,
    verified: bool = False,
    posted_at: str | None = None,
) -> None:
    """Insert or update a handle row. Idempotent.

    `posted_at` should be the post's created_at; it's used to roll
    first_seen_at / last_seen_at and bump total_posts atomically.
    """
    h = (handle or "").strip().lstrip("@").lower()
    if not h:
        return
    now = datetime.now(timezone.utc).isoformat()
    posted_at = posted_at or now
    wl_json = json.dumps(sorted(set(watchlists_list or [])))

    # On conflict: update the changeable fields, bump total_posts,
    # widen first_seen / last_seen as needed.
    await conn.execute(
        """
        INSERT INTO handles (
            handle, source, tier, profile_class, multiplier, watchlists,
            follower_count, bio, verified,
            first_seen_at, last_seen_at, total_posts, prior_complaints,
            sentiment_30d_avg, last_refreshed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, ?)
        ON CONFLICT(handle, source) DO UPDATE SET
            tier             = excluded.tier,
            profile_class    = excluded.profile_class,
            multiplier       = excluded.multiplier,
            watchlists       = excluded.watchlists,
            follower_count   = COALESCE(excluded.follower_count, handles.follower_count),
            bio              = COALESCE(excluded.bio, handles.bio),
            verified         = handles.verified OR excluded.verified,
            first_seen_at    = MIN(handles.first_seen_at, excluded.first_seen_at),
            last_seen_at     = MAX(handles.last_seen_at, excluded.last_seen_at),
            total_posts      = handles.total_posts + 1,
            last_refreshed_at = excluded.last_refreshed_at
        """,
        (
            h, source, tier, profile_class, multiplier, wl_json,
            follower_count, bio, 1 if verified else 0,
            posted_at, posted_at, now,
        ),
    )

    # Mirror watchlist memberships into the explicit table for fast joins
    for list_name in watchlists_list or []:
        await conn.execute(
            """
            INSERT INTO watchlist_memberships (handle, source, list_name, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(handle, source, list_name) DO NOTHING
            """,
            (h, source, list_name, now),
        )


async def get_handle(
    conn: aiosqlite.Connection, handle: str, source: str
) -> Handle | None:
    """Read one handle row. Returns None if not seen yet."""
    h = (handle or "").strip().lstrip("@").lower()
    if not h:
        return None
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute(
        "SELECT * FROM handles WHERE handle = ? AND source = ?", (h, source)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return _row_to_handle(dict(row))


async def get_or_compute_handle(
    conn: aiosqlite.Connection,
    *,
    handle: str | None,
    source: str,
    metadata: dict[str, Any] | None = None,
    bio: str | None = None,
    posted_at: str | None = None,
) -> Handle | None:
    """The hot-path: classify the author of a post and persist the row.

    Reads existing row if present; either way recomputes the tier so
    re-running is a no-op when nothing has changed (idempotent).
    """
    if not handle:
        return None
    h = handle.strip().lstrip("@").lower()
    if not h:
        return None

    profile_class, multiplier, wl = compute_tier(h, metadata, bio, source)
    tier = TIER_CODE.get(profile_class, "T5")

    # Pull verified + follower_count from metadata if present
    follower = metadata.get("user_followers") if metadata else None
    if follower is None and metadata:
        follower = metadata.get("follower_count")
    try:
        follower_int = int(follower) if follower is not None else None
    except (TypeError, ValueError):
        follower_int = None

    verified = bool(
        metadata
        and (metadata.get("user_verified") or metadata.get("verified"))
    )

    await upsert_handle(
        conn,
        handle=h,
        source=source,
        tier=tier,
        profile_class=profile_class,
        multiplier=multiplier,
        watchlists_list=wl,
        follower_count=follower_int,
        bio=bio,
        verified=verified,
        posted_at=posted_at,
    )
    return await get_handle(conn, h, source)


async def get_author_multiplier(
    conn: aiosqlite.Connection, handle: str | None, source: str
) -> float:
    """Hot-path read for compute_priority. 1.0 if not seen yet (safe default).

    Synchronous-feeling: a single indexed lookup. The caller must already
    have an open aiosqlite connection.
    """
    if not handle:
        return 1.0
    h = handle.strip().lstrip("@").lower()
    if not h:
        return 1.0
    cur = await conn.execute(
        "SELECT multiplier FROM handles WHERE handle = ? AND source = ?",
        (h, source),
    )
    row = await cur.fetchone()
    if row and row[0] is not None:
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return 1.0
    return 1.0


# ============================================================
# Bulk seed — populate watchlist_memberships from the curated lists
# ============================================================
async def seed_watchlists(conn: aiosqlite.Connection) -> int:
    """Insert/refresh rows in watchlist_memberships for every seed handle.
    Idempotent. Returns count of (handle, source, list_name) rows touched.
    """
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    # Seed for both Twitter and Reddit; the join in priority code is keyed
    # on (handle, source) so cross-source matches don't accidentally fire.
    # Most curated handles are Twitter-specific anyway.
    for handle, list_name in watchlists.seed_pairs():
        for src in ("twitter",):
            await conn.execute(
                """
                INSERT INTO watchlist_memberships (handle, source, list_name, added_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(handle, source, list_name) DO NOTHING
                """,
                (handle, src, list_name, now),
            )
            n += 1
    return n


# ============================================================
# Refresh — walk every unique (author, source) in posts and rebuild handles
# ============================================================
async def refresh_all_handles(db_path: str) -> dict[str, Any]:
    """Walk all unique authors in `posts` and rebuild their `handles` row.

    Idempotent — safe to re-run. Used by `python main.py handles refresh`
    and by Phase γ first-time backfill.

    Returns a stats dict.
    """
    stats: dict[str, Any] = {
        "scanned": 0,
        "upserted": 0,
        "by_class": {},
        "watchlist_seeded": 0,
    }
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # Seed the static watchlists first so explicit T0/T1 hits are recognized
        stats["watchlist_seeded"] = await seed_watchlists(db)

        # Aggregate: per (author, source), grab the latest metadata blob
        # for follower/verified hints (cheap MAX on created_at).
        cur = await db.execute(
            """
            SELECT p.author, p.source, p.metadata, p.created_at
            FROM posts p
            JOIN (
                SELECT author, source, MAX(created_at) AS mx
                FROM posts
                WHERE author IS NOT NULL AND author != ''
                GROUP BY author, source
            ) latest
              ON p.author = latest.author
             AND p.source = latest.source
             AND p.created_at = latest.mx
            """
        )
        rows = await cur.fetchall()

        for r in rows:
            stats["scanned"] += 1
            try:
                metadata = json.loads(r["metadata"]) if r["metadata"] else {}
            except Exception:
                metadata = {}

            h = await get_or_compute_handle(
                db,
                handle=r["author"],
                source=r["source"],
                metadata=metadata,
                bio=None,                  # bio scraping is v2
                posted_at=r["created_at"],
            )
            if h is not None:
                stats["upserted"] += 1
                stats["by_class"][h.profile_class] = (
                    stats["by_class"].get(h.profile_class, 0) + 1
                )

        # Recompute total_posts and first/last_seen authoritatively from posts
        # so re-runs don't double-count via the +1 bump in upsert_handle.
        await db.execute(
            """
            UPDATE handles
            SET
                total_posts = COALESCE(
                    (SELECT COUNT(*) FROM posts
                     WHERE posts.author = handles.handle
                       AND posts.source = handles.source),
                    handles.total_posts
                ),
                first_seen_at = COALESCE(
                    (SELECT MIN(created_at) FROM posts
                     WHERE posts.author = handles.handle
                       AND posts.source = handles.source),
                    handles.first_seen_at
                ),
                last_seen_at  = COALESCE(
                    (SELECT MAX(created_at) FROM posts
                     WHERE posts.author = handles.handle
                       AND posts.source = handles.source),
                    handles.last_seen_at
                )
            """
        )

        # Recompute prior_complaints (negative-sentiment posts in last 7d)
        await db.execute(
            """
            UPDATE handles
            SET prior_complaints = COALESCE(
                (SELECT COUNT(*) FROM posts
                 WHERE posts.author = handles.handle
                   AND posts.source = handles.source
                   AND posts.classification IS NOT NULL
                   AND json_extract(posts.classification, '$.sentiment') IN ('negative', 'abusive')
                   AND posts.created_at >= datetime('now', '-7 days')),
                0
            )
            """
        )
        await db.commit()

    logger.info(
        f"handles.refresh_all_handles: scanned={stats['scanned']} "
        f"upserted={stats['upserted']} by_class={stats['by_class']}"
    )
    return stats


# ============================================================
# Internals
# ============================================================
def _row_to_handle(row: dict[str, Any]) -> Handle:
    try:
        wl = json.loads(row.get("watchlists") or "[]")
    except Exception:
        wl = []
    return Handle(
        handle=row["handle"],
        source=row["source"],
        tier=row.get("tier") or "T5",
        profile_class=row.get("profile_class") or "regular",
        multiplier=float(row.get("multiplier") or 1.0),
        watchlists=wl,
        follower_count=row.get("follower_count"),
        bio=row.get("bio"),
        verified=bool(row.get("verified")),
        first_seen_at=row.get("first_seen_at"),
        last_seen_at=row.get("last_seen_at"),
        total_posts=int(row.get("total_posts") or 0),
        prior_complaints=int(row.get("prior_complaints") or 0),
        sentiment_30d_avg=row.get("sentiment_30d_avg"),
        last_refreshed_at=row.get("last_refreshed_at"),
    )
