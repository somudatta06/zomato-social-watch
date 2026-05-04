"""One-time backfill: copy all posts from local SQLite to Supabase.

Run after the Supabase `posts` table is created and SUPABASE_URL +
SUPABASE_SERVICE_KEY are set in the environment. Idempotent — safe
to re-run; conflicting rows are ignored by the mirror.

    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... \\
    .venv/bin/python scripts/backfill_supabase.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from social_watch.supabase_mirror import FULL_COLUMNS, mirror_posts  # noqa: E402


COLUMNS = list(FULL_COLUMNS)
BATCH = 200


async def main() -> int:
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")):
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        return 1

    db_path = os.environ.get("DB_PATH", "social_watch.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM posts")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    print(f"Loaded {len(rows)} posts from {db_path}")
    if not rows:
        return 0

    sent = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        await mirror_posts(batch)
        sent += len(batch)
        print(f"  {sent}/{len(rows)}")
    print(f"Done. {sent} posts mirrored to Supabase.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
