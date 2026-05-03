"""Async orchestrator: runs scrapers in parallel, persists, schedules cycles.

A 'cycle' = one fetch pass across every configured scraper. Cycles run
concurrently across scrapers (so Reddit doesn't wait on Twitter), and posts
batch-insert into SQLite as they stream in. One scraper crashing is logged
but does not abort the cycle.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from loguru import logger

from . import config
from .models import Post
from .scrapers import NitterScraper, RedditScraper, TwitterScraper
from .scrapers.base import BaseScraper, ScraperResult
from .storage import Storage

_BATCH_SIZE = 50


async def _drain(scraper: BaseScraper, storage: Storage) -> ScraperResult:
    """Fully exhaust one scraper, persisting in batches."""
    result = ScraperResult(scraper=scraper.name)
    start = time.monotonic()
    run_id = await storage.start_run(scraper.name, query=None)

    batch: list[Post] = []
    error: str | None = None
    try:
        async for post in scraper.fetch():
            result.posts_seen += 1
            batch.append(post)
            if len(batch) >= _BATCH_SIZE:
                _, new = await storage.upsert_posts(batch)
                result.posts_new += new
                batch.clear()
        if batch:
            _, new = await storage.upsert_posts(batch)
            result.posts_new += new
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.exception(f"{scraper.name} crashed")
        result.errors.append(error)
    finally:
        result.duration_s = time.monotonic() - start
        await storage.finish_run(run_id, result.posts_seen, result.posts_new, error=error)
    return result


def _select_scrapers(storage: Storage, twitter_via: str) -> list[BaseScraper]:
    candidates: list[BaseScraper] = [RedditScraper(storage)]
    if twitter_via in ("auto", "twscrape"):
        candidates.append(TwitterScraper(storage))
    if twitter_via in ("auto", "nitter"):
        candidates.append(NitterScraper(storage))

    selected: list[BaseScraper] = []
    for s in candidates:
        if not s.is_configured():
            logger.info(f"  [skip] {s.name}: not configured")
            continue
        selected.append(s)
    if twitter_via == "twscrape" and not config.TWITTER_ACCOUNTS:
        logger.warning("--twitter twscrape requested but TWITTER_ACCOUNTS is empty")
    return selected


async def run_cycle(
    storage: Storage, *, twitter_via: str = "auto"
) -> list[ScraperResult]:
    scrapers = _select_scrapers(storage, twitter_via)
    logger.info(f"Cycle: {len(scrapers)} scrapers -> {[s.name for s in scrapers]}")
    results = await asyncio.gather(*(_drain(s, storage) for s in scrapers))
    for r in results:
        suffix = f" errors={len(r.errors)}" if r.errors else ""
        logger.info(
            f"  {r.scraper}: seen={r.posts_seen} new={r.posts_new} "
            f"duration={r.duration_s:.1f}s{suffix}"
        )
    return results


async def run_watch(
    storage: Storage,
    *,
    interval: int = config.REFRESH_INTERVAL,
    twitter_via: str = "auto",
) -> None:
    logger.info(f"Watch mode: interval={interval}s, twitter_via={twitter_via}")
    cycle = 0
    while True:
        cycle += 1
        logger.info(f"==== Cycle {cycle} @ {datetime.now(timezone.utc).isoformat()} ====")
        try:
            await run_cycle(storage, twitter_via=twitter_via)
        except Exception:
            logger.exception(f"Cycle {cycle} crashed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Watch cancelled")
            break
