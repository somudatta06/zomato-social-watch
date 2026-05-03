"""Backfill: re-run all posts through the new LLM-aware pipeline.

Idempotent. Safe to re-run. Forces re-classification of ALL posts
(including already-Gemini-classified ones) so the schema stays consistent
after any prompt or taxonomy change.

Usage:
    cd "/Users/somudatta/Downloads/zomato assignment"
    uv run python scripts/backfill_llm_classification.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Add repo root to path so `social_watch` imports cleanly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiosqlite

from social_watch import config
from social_watch.preclassifier import preclassify_all


async def main() -> int:
    print("Backfill: re-classifying all posts via Gemini-aware preclassifier")
    counts = await preclassify_all(force=True)

    # Aggregate post-run stats from DB
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        gemini_row = await (await db.execute(
            "SELECT COUNT(*) FROM posts "
            "WHERE json_extract(classification, '$.method') = 'gemini-classified'"
        )).fetchone()
        rules_row = await (await db.execute(
            "SELECT COUNT(*) FROM posts "
            "WHERE json_extract(classification, '$.method') = 'rules-only'"
        )).fetchone()
        sarc_row = await (await db.execute(
            "SELECT COUNT(*) FROM posts "
            "WHERE json_extract(classification, '$.sarcasm_detected') = 1"
        )).fetchone()
        sub_tw_row = await (await db.execute(
            "SELECT COUNT(*) FROM posts "
            "WHERE json_extract(classification, '$.sub_tripwire') IS NOT NULL "
            "AND json_extract(classification, '$.sub_tripwire') != ''"
        )).fetchone()

        gemini = gemini_row[0] if gemini_row else 0
        rules = rules_row[0] if rules_row else 0
        sarc = sarc_row[0] if sarc_row else 0
        sub_tw = sub_tw_row[0] if sub_tw_row else 0

    print("\nBackfill summary:")
    print(f"  {gemini} classified by Gemini")
    print(f"  {rules} fell back to rules-only")
    print(f"  {sarc} sarcasm caught")
    print(f"  {sub_tw} sub-tripwires assigned")
    print(f"\nFull counts: {json.dumps(counts, indent=2, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
