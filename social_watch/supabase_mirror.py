"""Shadow-write posts to Supabase Postgres via PostgREST.

Every post that lands in SQLite via Storage.upsert_posts() is also
mirrored to a Supabase 'posts' table so the durable record survives
ephemeral container restarts (e.g. Render free tier wipes /tmp).

Graceful degradation: if SUPABASE_URL or SUPABASE_SERVICE_KEY is
unset, mirror_posts() is a no-op. Network errors are logged but
never raised — the primary SQLite path is never blocked.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import httpx
from loguru import logger


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
        "Prefer": "resolution=ignore-duplicates",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers=headers, json=rows)
        if r.status_code >= 400:
            logger.warning(
                f"[supabase] mirror returned {r.status_code}: {r.text[:200]}"
            )
        else:
            logger.debug(f"[supabase] mirrored {len(rows)} posts")
    except Exception as e:
        logger.warning(f"[supabase] mirror failed: {type(e).__name__}: {e}")
