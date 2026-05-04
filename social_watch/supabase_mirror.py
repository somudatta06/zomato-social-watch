"""Shadow-write posts to Supabase Postgres via PostgREST.

Every post that lands in SQLite via Storage.upsert_posts() is also
mirrored to a Supabase 'posts' table so the durable record survives
ephemeral container restarts (e.g. Render free tier wipes /tmp).

Graceful degradation: if SUPABASE_URL or SUPABASE_SERVICE_KEY is
unset, mirror_posts() is a no-op. Network errors are logged but
never raised — the primary SQLite path is never blocked.

restore_to_sqlite() is the inverse: on a cold container boot with
an empty local DB, pull every post back from Supabase so the
dashboard isn't blank for the first 5 minutes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

import httpx
from loguru import logger


FULL_COLUMNS = (
    "id", "source", "native_id", "author", "content", "url",
    "created_at", "fetched_at", "metadata",
    "category", "score", "classification", "classified_at",
    "action_taken", "action_meta", "actioned_at",
    "zomato_response_status", "zomato_response_url",
    "zomato_response_at", "zomato_response_checked_at",
    "priority_score", "priority_band", "priority_breakdown",
    "noise_category", "flagged_at",
    "ack_at", "ack_by", "ack_deadline_at",
    "escalation_count", "last_escalated_at",
    "review_issue_id", "review_issue_url", "review_created_at",
)


def _is_configured() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


async def mirror_posts(rows: Iterable[dict[str, Any]]) -> None:
    """POST a batch of post rows to Supabase REST API.

    Uses `Prefer: resolution=ignore-duplicates` so re-runs are
    idempotent on the primary key (`id`). Failures are logged at
    WARNING and swallowed — SQLite is the source of truth, Supabase
    is best-effort.
    """
    if not _is_configured():
        return
    rows = list(rows)
    if not rows:
        return

    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/posts"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, headers=headers, json=rows)
        if r.status_code >= 400:
            logger.warning(
                f"[supabase] mirror returned {r.status_code}: {r.text[:200]}"
            )
        else:
            logger.debug(f"[supabase] mirrored {len(rows)} posts")
    except Exception as e:
        logger.warning(f"[supabase] mirror failed: {type(e).__name__}: {e}")


async def restore_to_sqlite(db_path: str | Path) -> int:
    """Pull every row from Supabase posts → INSERT OR IGNORE into local SQLite.

    Skips if the local table already has rows (don't clobber a warm DB).
    Returns the number of posts restored. Logs and returns 0 on any error.
    """
    if not _is_configured():
        return 0
    import aiosqlite

    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute("SELECT COUNT(*) FROM posts")
        row = await cur.fetchone()
        existing = row[0] if row else 0
    if existing > 0:
        logger.info(f"[supabase] restore skipped — local DB already has {existing} posts")
        return 0

    base_url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/posts"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    cols = ",".join(FULL_COLUMNS)
    PAGE = 1000
    offset = 0
    rows: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                r = await client.get(
                    base_url,
                    params={"select": cols, "order": "created_at.asc",
                            "limit": PAGE, "offset": offset},
                    headers=headers,
                )
                if r.status_code != 200:
                    logger.warning(f"[supabase] restore: HTTP {r.status_code}: {r.text[:200]}")
                    return 0
                batch = r.json()
                if not batch:
                    break
                rows.extend(batch)
                if len(batch) < PAGE:
                    break
                offset += PAGE
    except Exception as e:
        logger.warning(f"[supabase] restore failed: {type(e).__name__}: {e}")
        return 0

    if not rows:
        logger.info("[supabase] restore: no rows in remote table")
        return 0

    col_list = ", ".join(FULL_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in FULL_COLUMNS)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executemany(
            f"INSERT OR IGNORE INTO posts ({col_list}) VALUES ({placeholders})",
            rows,
        )
        await db.commit()
    logger.info(f"[supabase] restored {len(rows)} posts from Supabase to local SQLite")
    return len(rows)
