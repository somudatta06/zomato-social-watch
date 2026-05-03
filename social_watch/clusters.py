"""Post-batch clustering — early-warning crisis detection.

Implements the "30 individual escalations vs. 1 ops alert" optimization
from the deep-dive doc §7.2. When 30+ posts arrive about the same topic
in the same city within 30 minutes, that's an ops outage, not 30
unrelated complaints — the team needs ONE alert, not 30.

Window: rolling last 60 minutes.
Grouping key: (side, primary_topic, geography_value, hour_bucket)
Threshold: ≥5 posts per group → cluster materializes.

We re-run on every cycle (idempotent — INSERT OR IGNORE on members,
ON CONFLICT on cluster identity) so a cluster grows as more posts land.

Cluster types we recognize (deep-dive §7.2):
  ops_outage          30+ posts, same topic, same city, ≤30 min
  coordinated_attack  20+ posts, same hashtag, mixed cities
  restaurant_event    15+ posts mentioning same restaurant
  news_wave           10+ posts citing same news article URL
  agent_incident      5+ posts about same delivery agent
  review_bombing      3+ posts about same handle complaint

For v1 we ship the structural clustering (the (side, topic, geo, time)
key) and the ops_outage / coordinated_attack / restaurant_event types.
The other types need data we don't have yet (URL extraction, named
agents). Adding them later is one helper function each — the rest of
the pipeline already supports them via the `cluster_type` column.

Summarization: stub-only for v1 ("32 posts about consumer.delivery.late
in Bangalore between 09:30 and 10:15 IST"). LLM summarization is v2.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from loguru import logger

from . import config

# ============================================================
# Tunables
# ============================================================

# Rolling window for "is this an active cluster?"
_WINDOW_MINUTES = 60

# Minimum group size before a cluster materializes
_MIN_GROUP = 5

# Time bucket — group posts that fall in the same hour-of-day bucket.
# (We don't use 30-min buckets because that splits genuine outages right
# at the threshold; 60-min plus a 60-min sliding window is plenty of
# resolution for "what's happening right now.")
_BUCKET_MINUTES = 60

# Mark a cluster `closed` if its newest member is this old.
_CLOSE_AGE_HOURS = 24

# Cluster-type thresholds (deep-dive §7.2)
_OPS_OUTAGE_MIN = 30
_OPS_OUTAGE_WINDOW_MIN = 30
_COORD_ATTACK_MIN = 20            # mixed-cities, same hashtag
_RESTAURANT_EVENT_MIN = 15

_VALID_GEO_VALUES = ("point", "neighborhood", "city", "state")


# ============================================================
# Schema
# ============================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    primary_topic   TEXT,
    side            TEXT,
    geography       TEXT,
    cluster_type    TEXT,
    started_at      TIMESTAMP NOT NULL,
    last_member_at  TIMESTAMP NOT NULL,
    member_count    INTEGER NOT NULL DEFAULT 0,
    lead_post_id    TEXT,
    summary         TEXT,
    closed_at       TIMESTAMP,
    status          TEXT NOT NULL DEFAULT 'active'  -- active | closed
);
CREATE INDEX IF NOT EXISTS idx_clusters_status_last
    ON clusters(status, last_member_at DESC);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id      TEXT NOT NULL,
    post_id         TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'member',  -- lead | member | outlier
    joined_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (cluster_id, post_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_members_post
    ON cluster_members(post_id);
"""


async def ensure_schema() -> None:
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


# ============================================================
# Cluster-key derivation
# ============================================================

def _bucket_iso(ts: datetime) -> str:
    """Floor a datetime to the configured bucket (default 60 min)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    minutes = (ts.minute // _BUCKET_MINUTES) * _BUCKET_MINUTES
    floored = ts.replace(minute=minutes, second=0, microsecond=0)
    return floored.isoformat()


def _primary_topic(cls: dict[str, Any]) -> str | None:
    """Extract a stable topic key from a classification dict.

    Tries (in order):
      1. classification.primary_topic
      2. classification.sub_claims[0].path
      3. tripwires_fired[0]  (e.g. 'food_safety_emergency')
    Returns None if nothing usable.
    """
    if not cls:
        return None
    pt = cls.get("primary_topic")
    if isinstance(pt, str) and pt.strip():
        return pt.strip()
    subs = cls.get("sub_claims") or []
    if subs and isinstance(subs, list):
        first = subs[0]
        if isinstance(first, dict):
            path = first.get("path") or first.get("topic")
            if isinstance(path, str) and path.strip():
                return path.strip()
    tw = cls.get("tripwires_fired") or []
    if tw and isinstance(tw, list) and tw[0]:
        return f"tripwire:{tw[0]}"
    return None


def _geography_value(cls: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Return (geo_key, matches). geo_key is None if too imprecise.

    Per spec: only `point|neighborhood|city|state` are precise enough to
    cluster on. `unknown` posts are skipped entirely (otherwise every
    no-location complaint piles into one giant fake cluster)."""
    geo = (cls or {}).get("geography") or {}
    val = geo.get("value")
    if val not in _VALID_GEO_VALUES:
        return None, []
    matches = geo.get("matches") or []
    if not matches:
        return None, []
    # First match is canonical. Lowercased so "Bangalore" ≡ "bangalore".
    canonical = str(matches[0]).strip().lower()
    return canonical, [str(m).lower() for m in matches]


def _hashtags(text: str) -> list[str]:
    """Extract hashtags. Used for coordinated_attack detection."""
    return [h.lower() for h in re.findall(r"#(\w{2,40})", text or "")]


def _restaurant_mentions(text: str) -> list[str]:
    """Heuristic restaurant-name extraction. Empty in v1 (we'd need a
    restaurant catalog). Stub returns []. Adding it later is one place."""
    return []


def _make_cluster_id(side: str, topic: str, geo: str, bucket_iso: str) -> str:
    """Stable, idempotent cluster id. Re-running the detector with the
    same inputs MUST produce the same id (hence INSERT OR IGNORE works)."""
    safe_topic = re.sub(r"[^a-z0-9._-]+", "_", topic.lower())[:60]
    safe_geo = re.sub(r"[^a-z0-9._-]+", "_", geo.lower())[:40]
    bucket = bucket_iso.replace(":", "").replace("-", "")[:13]  # YYYYMMDDTHHMM
    return f"{side}__{safe_topic}__{safe_geo}__{bucket}"


# ============================================================
# Cluster-type classifier (deep-dive §7.2)
# ============================================================

def _classify_cluster_type(
    *,
    member_count: int,
    geo_unique: int,
    hashtag_unique: int,
    span_minutes: float,
    same_restaurant: bool,
) -> str:
    """Decide which v1 cluster pattern this group matches. Order matters
    — first match wins, most actionable types first."""
    # Ops outage: tight time window, ONE city, lots of posts
    if (
        member_count >= _OPS_OUTAGE_MIN
        and span_minutes <= _OPS_OUTAGE_WINDOW_MIN
        and geo_unique <= 1
    ):
        return "ops_outage"
    # Coordinated attack: same hashtag across MULTIPLE cities
    if member_count >= _COORD_ATTACK_MIN and hashtag_unique >= 1 and geo_unique >= 3:
        return "coordinated_attack"
    # Restaurant-level event
    if member_count >= _RESTAURANT_EVENT_MIN and same_restaurant:
        return "restaurant_event"
    # Default: a topic cluster, no special routing
    return "topic_cluster"


# ============================================================
# Main detector
# ============================================================

async def detect_clusters() -> dict[str, Any]:
    """Run a full cluster-detection pass over the last `_WINDOW_MINUTES`.

    Idempotent: re-running creates no duplicates because cluster ids are
    deterministic and member rows use a composite primary key.

    Returns counts dict for caller logging."""
    await ensure_schema()
    counts: dict[str, Any] = {
        "windowed": 0,
        "groups_seen": 0,
        "groups_clustered": 0,
        "members_added": 0,
        "clusters_closed": 0,
    }

    since = (datetime.now(timezone.utc) - timedelta(minutes=_WINDOW_MINUTES)).isoformat()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, source, content, category, created_at, classification
            FROM posts
            WHERE created_at >= ?
              AND classification IS NOT NULL
            ORDER BY created_at ASC
            """,
            (since,),
        )
        rows = [dict(r) for r in await cur.fetchall()]

    counts["windowed"] = len(rows)
    if not rows:
        # Still need to close stale clusters even when no new posts
        await _close_stale_clusters(counts)
        return counts

    # Group by (side, topic, geo, bucket)
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        try:
            cls = json.loads(r["classification"]) if r["classification"] else {}
        except Exception:
            continue
        side = (r.get("category") or cls.get("side") or "").strip().lower()
        if not side or side == "neither":
            continue
        topic = _primary_topic(cls)
        if not topic:
            continue
        geo, _matches = _geography_value(cls)
        if not geo:
            continue
        try:
            ts = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        bucket = _bucket_iso(ts)
        key = (side, topic, geo, bucket)
        groups[key].append({
            "post_id": r["id"],
            "content": r.get("content") or "",
            "ts": ts,
            "cls": cls,
        })

    counts["groups_seen"] = len(groups)
    now_iso = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        for key, posts in groups.items():
            if len(posts) < _MIN_GROUP:
                continue
            side, topic, geo, bucket = key
            cluster_id = _make_cluster_id(side, topic, geo, bucket)

            posts.sort(key=lambda p: p["ts"])
            lead = posts[0]
            last_member_at = posts[-1]["ts"].isoformat()
            started_at = posts[0]["ts"].isoformat()
            geo_unique = len({
                m for p in posts
                for m in _geography_value(p["cls"])[1]
            })
            hashtag_unique = len({
                h for p in posts for h in _hashtags(p["content"])
            })
            span_minutes = (posts[-1]["ts"] - posts[0]["ts"]).total_seconds() / 60.0
            cluster_type = _classify_cluster_type(
                member_count=len(posts),
                geo_unique=geo_unique,
                hashtag_unique=hashtag_unique,
                span_minutes=span_minutes,
                same_restaurant=False,  # v1 stub
            )

            summary = _simple_summary(
                len(posts), topic, geo, posts[0]["ts"], posts[-1]["ts"]
            )

            # Upsert cluster row. ON CONFLICT updates the rolling fields
            # (last_member_at, member_count, summary, cluster_type) so a
            # growing cluster reflects its current state — but never
            # touches `id` / `started_at` / `lead_post_id` (immutable).
            await db.execute(
                """
                INSERT INTO clusters
                  (id, tenant_id, primary_topic, side, geography, cluster_type,
                   started_at, last_member_at, member_count, lead_post_id,
                   summary, status)
                VALUES
                  (?, 'default', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(id) DO UPDATE SET
                  last_member_at = excluded.last_member_at,
                  member_count = excluded.member_count,
                  cluster_type = excluded.cluster_type,
                  summary = excluded.summary,
                  status = 'active',
                  closed_at = NULL
                """,
                (
                    cluster_id, topic, side, geo, cluster_type,
                    started_at, last_member_at, len(posts), lead["post_id"],
                    summary,
                ),
            )

            # Insert members. INSERT OR IGNORE on the composite PK = idempotent.
            for i, p in enumerate(posts):
                role = "lead" if i == 0 else "member"
                cur = await db.execute(
                    """
                    INSERT OR IGNORE INTO cluster_members
                      (cluster_id, post_id, role, joined_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cluster_id, p["post_id"], role, now_iso),
                )
                if cur.rowcount and cur.rowcount > 0:
                    counts["members_added"] += 1

            # Splice cluster_id + cluster_role into each member's
            # classification JSON so the dashboard / downstream code can
            # filter without an extra join.
            for i, p in enumerate(posts):
                role = "lead" if i == 0 else "member"
                await _splice_cluster_into_classification(
                    db, p["post_id"], cluster_id, role
                )
            counts["groups_clustered"] += 1
        await db.commit()

    # Close clusters whose last_member_at is older than _CLOSE_AGE_HOURS
    await _close_stale_clusters(counts)

    logger.info(
        f"clusters.detect: windowed={counts['windowed']} "
        f"groups_seen={counts['groups_seen']} "
        f"clustered={counts['groups_clustered']} "
        f"members_added={counts['members_added']} "
        f"closed={counts['clusters_closed']}"
    )
    return counts


async def _splice_cluster_into_classification(
    db: aiosqlite.Connection, post_id: str, cluster_id: str, role: str
) -> None:
    """Read post.classification JSON, set cluster_id + cluster_role,
    write back. Tolerant of missing/corrupt JSON (skips silently)."""
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT classification FROM posts WHERE id = ?", (post_id,)
    )
    row = await cur.fetchone()
    if not row or not row["classification"]:
        return
    try:
        cls = json.loads(row["classification"])
    except Exception:
        return
    cls["cluster_id"] = cluster_id
    cls["cluster_role"] = role
    await db.execute(
        "UPDATE posts SET classification = ? WHERE id = ?",
        (json.dumps(cls, default=str), post_id),
    )


async def _close_stale_clusters(counts: dict[str, Any]) -> None:
    """Mark clusters whose last_member_at is older than _CLOSE_AGE_HOURS
    as `closed`. Doesn't reopen them — once closed, the cluster is done."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_CLOSE_AGE_HOURS)).isoformat()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        cur = await db.execute(
            """
            UPDATE clusters SET
              status = 'closed',
              closed_at = COALESCE(closed_at, ?)
            WHERE status = 'active'
              AND last_member_at < ?
            """,
            (datetime.now(timezone.utc).isoformat(), cutoff),
        )
        await db.commit()
        counts["clusters_closed"] = (cur.rowcount or 0)


# ============================================================
# Summarization (v1: simple deterministic; LLM is v2)
# ============================================================

def _simple_summary(
    n: int, topic: str, geo: str, first_ts: datetime, last_ts: datetime
) -> str:
    """Deterministic, no-LLM summary the dashboard / Slack can show."""
    def fmt(ts: datetime) -> str:
        # Show as IST clock time — easier to read on an Indian ops dashboard
        ist = ts.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return ist.strftime("%H:%M IST")
    if first_ts.date() == last_ts.date():
        when = f"between {fmt(first_ts)} and {fmt(last_ts)}"
    else:
        when = f"from {fmt(first_ts)} to {fmt(last_ts)}"
    geo_pretty = geo.title()
    return f"{n} posts about {topic} in {geo_pretty} {when}"


async def summarize_cluster(cluster_id: str) -> str:
    """Return the cluster's stored summary, or recompute one from members.
    LLM summarization is a v2 enhancement — for v1 we use the stored
    deterministic summary written by detect_clusters()."""
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT summary, primary_topic, geography, member_count,
                      started_at, last_member_at
               FROM clusters WHERE id = ?""",
            (cluster_id,),
        )
        row = await cur.fetchone()
    if not row:
        return ""
    if row["summary"]:
        return row["summary"]
    # Fall back to recomputing one from the metadata we have on the row.
    try:
        f = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
        l = datetime.fromisoformat(str(row["last_member_at"]).replace("Z", "+00:00"))
        if f.tzinfo is None:
            f = f.replace(tzinfo=timezone.utc)
        if l.tzinfo is None:
            l = l.replace(tzinfo=timezone.utc)
        return _simple_summary(
            row["member_count"], row["primary_topic"] or "?", row["geography"] or "?", f, l
        )
    except Exception:
        return f"{row['member_count']} posts about {row['primary_topic']}"


# ============================================================
# Read helpers — for dashboard
# ============================================================

async def list_active(limit: int = 20) -> list[dict[str, Any]]:
    await ensure_schema()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, primary_topic, side, geography, cluster_type,
                   started_at, last_member_at, member_count, summary
            FROM clusters
            WHERE status = 'active'
            ORDER BY member_count DESC, last_member_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def count_active() -> int:
    await ensure_schema()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM clusters WHERE status = 'active'"
        )
        row = await cur.fetchone()
        return row["c"] if row else 0


async def get_member_post_ids(cluster_id: str) -> list[str]:
    await ensure_schema()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT post_id FROM cluster_members WHERE cluster_id = ?",
            (cluster_id,),
        )
        return [r["post_id"] for r in await cur.fetchall()]
