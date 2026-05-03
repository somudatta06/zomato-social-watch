"""Nitter fallback for Twitter.

Nitter is an alternative Twitter web frontend. We scrape its /search HTML.

REALITY CHECK (probed 2026-04): public Nitter is largely dead. Most domains
either 403/503, Cloudflare-challenge, or sit behind Anubis (JS proof-of-work).
Of ~14 known instances, zero returned a parseable search page on the day
this was written. We keep this scraper because:
  1. Instances do come and go — we want to be ready when one comes back.
  2. If you operate your own Nitter instance, this just works.
  3. It's a free zero-config attempt that costs us a few seconds.

The probe is rigorous: we hit /search?q=zomato&f=tweets directly and only
lock onto an instance if the response actually contains `.timeline-item`
elements (and not an Anubis/Cloudflare interstitial).

We tag results as `source="twitter"` (same data) but mark `via=nitter` in
metadata so downstream ranking can discount them if desired.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from .. import config
from ..models import Post
from ..storage import Storage
from .base import BaseScraper

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

# Markers that mean "Anubis / Cloudflare / similar JS-proof-of-work wall"
_CHALLENGE_MARKERS = (
    "making sure you",          # Anubis title
    "just a moment",            # Cloudflare
    "checking your browser",    # generic
    "anubis_challenge",
    "cf-challenge",
    "captcha required",
)


def _is_challenge_page(body: str) -> bool:
    lower = body.lower()
    return any(m in lower for m in _CHALLENGE_MARKERS)


class NitterScraper(BaseScraper):
    name = "nitter"

    def __init__(self, storage: Storage):
        self.storage = storage
        self._instance: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=config.NITTER_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
        return self._client

    async def _pick_instance(self) -> str | None:
        if self._instance:
            return self._instance
        client = await self._http()
        for inst in config.NITTER_INSTANCES:
            try:
                r = await client.get(
                    f"{inst}/search?q=zomato&f=tweets", timeout=8.0
                )
            except Exception as e:
                logger.debug(f"Nitter probe {inst}: {e}")
                continue
            if r.status_code != 200:
                logger.debug(f"Nitter probe {inst}: HTTP {r.status_code}")
                continue
            body = r.text
            if _is_challenge_page(body):
                logger.debug(f"Nitter probe {inst}: bot challenge page")
                continue
            if "timeline-item" not in body:
                logger.debug(f"Nitter probe {inst}: no .timeline-item in response")
                continue
            self._instance = inst
            logger.info(f"Nitter: locked onto {inst}")
            return inst
        logger.warning(
            "Nitter: no usable instances (all dead, blocked, or behind a bot challenge). "
            "Use twscrape (TWITTER_ACCOUNTS) for live Twitter data."
        )
        return None

    async def health_check(self) -> bool:
        return (await self._pick_instance()) is not None

    async def fetch(self) -> AsyncIterator[Post]:
        instance = await self._pick_instance()
        if not instance:
            return

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for query in config.NITTER_QUERIES:
            try:
                async for post in self._search(instance, query, cutoff):
                    yield post
            except Exception as e:
                logger.warning(f"nitter {query!r}: {e}")

    async def _search(
        self, instance: str, query: str, cutoff: datetime
    ) -> AsyncIterator[Post]:
        wm_key = f"nitter:{query}"
        wm = await self.storage.get_watermark(wm_key)
        last_id = wm["last_native_id"] if wm else None

        client = await self._http()
        url = f"{instance}/search?q={quote(query)}&f=tweets"
        try:
            r = await client.get(url)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"Nitter {url} failed: {e}; will re-pick instance next cycle")
            self._instance = None
            return

        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select(".timeline-item")
        if not items:
            return

        max_seen_id: str | None = None
        for item in items:
            try:
                post = self._parse_item(item, instance, query)
            except Exception as e:
                logger.debug(f"nitter parse: {e}")
                continue
            if post is None:
                continue
            if max_seen_id is None:
                max_seen_id = post.native_id
            if last_id and post.native_id == last_id:
                break
            if post.created_at < cutoff:
                continue
            yield post

        if max_seen_id:
            await self.storage.set_watermark(wm_key, max_seen_id, None)

    @staticmethod
    def _parse_item(item: Any, instance: str, query: str) -> Post | None:
        link = item.select_one(".tweet-link")
        if not link:
            return None
        href = link.get("href") or ""           # /username/status/12345#m
        parts = href.lstrip("/").split("/")
        if len(parts) < 3 or parts[1] != "status":
            return None
        username = parts[0]
        tid = parts[2].split("#")[0]

        # Date — title attribute on the inner <a>: "Mar 5, 2026 · 3:24 PM UTC"
        created: datetime | None = None
        date_el = item.select_one(".tweet-date a")
        if date_el and date_el.get("title"):
            for fmt in ("%b %d, %Y · %I:%M %p %Z", "%b %d, %Y · %H:%M %Z"):
                try:
                    created = datetime.strptime(date_el["title"], fmt).replace(
                        tzinfo=timezone.utc
                    )
                    break
                except Exception:
                    pass
        if created is None:
            created = datetime.now(timezone.utc)

        content_el = item.select_one(".tweet-content")
        content = content_el.get_text(" ", strip=True) if content_el else ""

        # Engagement counts
        stats: dict[str, str] = {}
        for stat in item.select(".tweet-stat"):
            text = stat.get_text(" ", strip=True)
            tokens = text.split()
            if len(tokens) >= 2:
                stats[tokens[-1].lower()] = tokens[0]

        return Post(
            source="twitter",
            native_id=tid,
            author=username,
            content=content,
            url=f"https://x.com/{username}/status/{tid}",
            created_at=created,
            metadata={
                "matched_query": query,
                "via": "nitter",
                "nitter_instance": instance,
                "stats": stats,
            },
        )
