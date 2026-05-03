"""SQLite layer.

Schema is intentionally future-proof: classification + action columns are
present but nullable, so Phase 2 (classify) and Phase 3 (escalate) plug in
without migrations.

Dedup is enforced by the PRIMARY KEY on `posts.id` ("{source}:{native_id}").
Watermarks let scrapers fetch only what's new since the last cycle.
`fetch_runs` gives us per-cycle observability for free.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    native_id       TEXT NOT NULL,
    author          TEXT,
    content         TEXT NOT NULL,
    url             TEXT,
    created_at      TIMESTAMP NOT NULL,
    fetched_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata        TEXT,                       -- JSON
    -- Phase 2: classification
    category        TEXT,
    score           REAL,
    classification  TEXT,                       -- JSON
    classified_at   TIMESTAMP,
    -- Phase 3: actions
    action_taken    TEXT,
    action_meta     TEXT,                       -- JSON
    actioned_at     TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_posts_source     ON posts(source);
CREATE INDEX IF NOT EXISTS idx_posts_created    ON posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_fetched    ON posts(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_score      ON posts(score DESC);

CREATE TABLE IF NOT EXISTS watermarks (
    source_query    TEXT PRIMARY KEY,
    last_native_id  TEXT,
    last_created_at TIMESTAMP,
    last_run_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    query           TEXT,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    posts_seen      INTEGER DEFAULT 0,
    posts_new       INTEGER DEFAULT 0,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON fetch_runs(started_at DESC);

-- Phase γ — Author tier system + watchlists
-- (see docs/CLASSIFICATION_DEEP_DIVE.md §8)
--
-- One row per (handle, source). A handle can appear on multiple watchlists
-- (PM is on both authority + politician); the `watchlists` JSON array
-- records all of them, while `watchlist_memberships` is the explicit
-- relational table for fast joins/filters.
CREATE TABLE IF NOT EXISTS handles (
    handle              TEXT NOT NULL,
    source              TEXT NOT NULL,
    tier                TEXT,            -- T0..T7
    profile_class       TEXT,            -- 'press', 'politician', ...
    multiplier          REAL,            -- reach multiplier (1.0..10.0)
    watchlists          TEXT,            -- JSON array of list_names
    follower_count      INTEGER,
    bio                 TEXT,
    verified            BOOLEAN DEFAULT 0,
    first_seen_at       TIMESTAMP,
    last_seen_at        TIMESTAMP,
    total_posts         INTEGER DEFAULT 0,
    prior_complaints    INTEGER DEFAULT 0,
    sentiment_30d_avg   REAL,
    last_refreshed_at   TIMESTAMP,
    PRIMARY KEY (handle, source)
);

CREATE INDEX IF NOT EXISTS idx_handles_class ON handles(profile_class);
CREATE INDEX IF NOT EXISTS idx_handles_tier  ON handles(tier);

CREATE TABLE IF NOT EXISTS watchlist_memberships (
    handle      TEXT NOT NULL,
    source      TEXT NOT NULL,
    list_name   TEXT NOT NULL,    -- 'press', 'politician', 'authority', 'founder'
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (handle, source, list_name)
);

CREATE INDEX IF NOT EXISTS idx_wlm_list ON watchlist_memberships(list_name);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            # Idempotent column additions for features that landed after the
            # original schema. SQLite has no "ADD COLUMN IF NOT EXISTS" so we
            # try-and-ignore on existing-column errors.
            for col_def in (
                "zomato_response_status TEXT DEFAULT 'unchecked'",  # unchecked / replied / no_reply
                "zomato_response_url TEXT",
                "zomato_response_at TIMESTAMP",
                "zomato_response_checked_at TIMESTAMP",
                # Phase α — priority scoring (see docs/CLASSIFICATION_DEEP_DIVE.md §3)
                "priority_score REAL",                              # 0–1
                "priority_band TEXT",                               # P0 / P1 / P2 / P3
                "priority_breakdown TEXT",                          # JSON: signals + contributions
                # Phase ε — noise filter. Categorical, not scored. NULL means
                # 'clean' — these are the posts that show in the default inbox.
                # Non-NULL values: 'promo', 'job', 'stock', 'off_topic', 'bot'.
                "noise_category TEXT",
                # Phase ζ — operator flagging. NULL = not flagged. When set,
                # holds the timestamp the operator clicked the star — lets us
                # sort flagged posts by flag-time, not by post creation.
                "flagged_at TIMESTAMP",
                # Phase κ — incident-playbook acknowledgment + escalation.
                # When a post fires a tripwired action, the dispatcher
                # writes ack_deadline_at = fired_at + playbook.ack_deadline_min.
                # An operator clicks "Acknowledge" → ack_at + ack_by are set.
                # If ack_deadline_at passes before ack_at, the SLA sweep
                # fires an [ESCALATED] re-alert and bumps escalation_count.
                "ack_at TIMESTAMP",
                "ack_by TEXT",
                "ack_deadline_at TIMESTAMP",
                "escalation_count INTEGER DEFAULT 0",
                "last_escalated_at TIMESTAMP",
                # Phase λ — post-incident review. 24h after a death-claim or
                # food-safety post is acked, the review-sweeper opens a Linear
                # sub-issue with a templated review doc. The id/url are pinned
                # to the post so the inbox row can link straight to it.
                "review_issue_id TEXT",
                "review_issue_url TEXT",
                "review_created_at TIMESTAMP",
            ):
                try:
                    await db.execute(f"ALTER TABLE posts ADD COLUMN {col_def}")
                except aiosqlite.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_response "
                "ON posts(zomato_response_status, score DESC)"
            )
            # Index for the urgency view's default sort (priority_score DESC)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_priority "
                "ON posts(priority_score DESC, created_at DESC)"
            )
            # Index for the noise filter — every inbox query has a
            # `WHERE noise_category IS NULL OR noise_category = ?` clause.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_noise "
                "ON posts(noise_category)"
            )
            # Partial index on flagged posts — sidebar "Flagged" filter
            # queries `WHERE flagged_at IS NOT NULL`, which benefits from
            # this much smaller index than a full table scan.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_flagged "
                "ON posts(flagged_at) WHERE flagged_at IS NOT NULL"
            )
            # Phase iota cluster-aware fire-once dispatcher. Tracks which
            # clusters have already had their first alert fired and which
            # volume milestones have been crossed. One row per cluster.
            # Idempotent: CREATE IF NOT EXISTS pattern.
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_alerts (
                    cluster_id          TEXT PRIMARY KEY,
                    first_alerted_at    TIMESTAMP NOT NULL,
                    first_post_id       TEXT NOT NULL,
                    last_volume_at      TIMESTAMP,
                    last_volume_count   INTEGER DEFAULT 0,
                    milestones_fired    TEXT DEFAULT '[]'
                )
                """
            )
            # Phase μ — auto-reply policy author dedupe. One row per
            # auto-fired reply so the next sweep can answer "did we
            # already reply to this user in the last hour?" in O(log n).
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS auto_reply_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    author    TEXT NOT NULL,
                    source    TEXT NOT NULL,
                    post_id   TEXT NOT NULL,
                    fired_at  TIMESTAMP NOT NULL,
                    trigger   TEXT NOT NULL DEFAULT 'auto_reply_v1'
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_auto_reply_log_author_source_ts "
                "ON auto_reply_log(author, source, fired_at DESC)"
            )
            await db.commit()

    async def upsert_posts(self, posts: Iterable[Post]) -> tuple[int, int]:
        """Bulk insert. Returns (seen, new_inserted)."""
        rows = [p.to_db_row() for p in posts]
        if not rows:
            return 0, 0
        async with aiosqlite.connect(self.path) as db:
            cur = await db.executemany(
                """
                INSERT OR IGNORE INTO posts
                  (id, source, native_id, author, content, url, created_at, metadata)
                VALUES
                  (:id, :source, :native_id, :author, :content, :url, :created_at, :metadata)
                """,
                rows,
            )
            await db.commit()
            new = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return len(rows), new

    async def get_watermark(self, source_query: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM watermarks WHERE source_query = ?",
                (source_query,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def set_watermark(
        self,
        source_query: str,
        last_native_id: str | None,
        last_created_at: datetime | None,
    ) -> None:
        ts = None
        if last_created_at is not None:
            if last_created_at.tzinfo is None:
                last_created_at = last_created_at.replace(tzinfo=timezone.utc)
            ts = last_created_at.astimezone(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO watermarks (source_query, last_native_id, last_created_at, last_run_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_query) DO UPDATE SET
                    last_native_id  = excluded.last_native_id,
                    last_created_at = excluded.last_created_at,
                    last_run_at     = CURRENT_TIMESTAMP
                """,
                (source_query, last_native_id, ts),
            )
            await db.commit()

    async def start_run(self, source: str, query: str | None) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO fetch_runs (source, query, started_at) VALUES (?, ?, ?)",
                (source, query, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
            return cur.lastrowid or 0

    async def finish_run(
        self,
        run_id: int,
        seen: int,
        new: int,
        error: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE fetch_runs
                SET finished_at = ?, posts_seen = ?, posts_new = ?, error = ?
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), seen, new, error, run_id),
            )
            await db.commit()

    async def recent_posts(
        self, limit: int = 50, source: str | None = None
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if source:
                cur = await db.execute(
                    "SELECT * FROM posts WHERE source = ? ORDER BY created_at DESC LIMIT ?",
                    (source, limit),
                )
            else:
                cur = await db.execute(
                    "SELECT * FROM posts ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            return [dict(r) for r in await cur.fetchall()]

    async def stats(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT COUNT(*) AS c FROM posts")
            total_row = await cur.fetchone()
            total = total_row["c"] if total_row else 0

            cur = await db.execute(
                "SELECT source, COUNT(*) AS c FROM posts GROUP BY source"
            )
            by_source = {r["source"]: r["c"] for r in await cur.fetchall()}

            cur = await db.execute(
                "SELECT * FROM fetch_runs ORDER BY started_at DESC LIMIT 10"
            )
            recent_runs = [dict(r) for r in await cur.fetchall()]

            return {
                "total_posts": total,
                "by_source": by_source,
                "recent_runs": recent_runs,
            }
