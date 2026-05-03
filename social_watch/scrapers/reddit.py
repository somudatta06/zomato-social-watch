"""Reddit scraper using public JSON endpoints — no authentication required.

Why we're not using PRAW: Reddit's "Responsible Builder Policy" (Nov 2025)
killed self-service API keys. New OAuth apps require a 7-day manual review.
The public JSON endpoints — `<reddit-url>.json` for any reddit URL — remain
free, unauthenticated, and return the same data shape PRAW does. They're
rate-limited to ~60 req/min for anon clients with descriptive User-Agents,
which is plenty for our 5-minute cycle.

Strategy per cycle:
  1. For each subreddit in REDDIT_SUBREDDITS: GET /r/{sub}/new.json
  2. For each free-text query in REDDIT_QUERIES: GET /search.json?sort=new
  3. Watermark per source-query for incremental fetching

We add a small inter-request delay to stay polite, respect Retry-After on
429, and use a descriptive User-Agent (Reddit explicitly recommends this —
generic UAs like httpx/python-requests get blocked harder).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx
from loguru import logger

from .. import config
from ..models import Post
from ..storage import Storage
from .base import BaseScraper

# Reddit's published anon quota is ~60 req/min for descriptive UAs. 1.5s
# between requests = 40 req/min, well under the ceiling.
_REQUEST_DELAY_S = 1.5

# Subreddit firehoses are noisy (general subs). We only keep posts that
# substantively reference Zomato. Note: blinkit / hyperpure / district /
# eternal-corporate are explicitly OUT OF SCOPE per project brief.
_BRAND_TOKENS = ("zomato", "deepinder")

# Posts that just list zomato among many other companies aren't really
# about zomato. We detect company-list patterns and reject them.
_OTHER_COMPANIES = (
    "swiggy", "instamart", "blinkit", "zepto", "bigbasket", "dunzo",
    "uber", "ola", "rapido", "amazon", "flipkart", "myntra", "meesho",
    "jio", "airtel", "vodafone", "vi ", "bsnl",
    "paytm", "phonepe", "google pay", "razorpay", "cred",
    "microsoft", "google", "meta", "facebook", "netflix", "adobe",
    "tcs", "infosys", "wipro", "hcl", "accenture",
    "byju", "unacademy", "vedantu",
    "nykaa", "purplle", "lenskart",
    "magicpin", "eatsure", "freshmenu",
)

# Zomato-specific context words. Strong signal that a post is *about* zomato
# (not just mentioning it as one of many).
_ZOMATO_CONTEXT = (
    "order", "ordered", "delivery", "delivered", "deliver",
    "refund", "refunded",
    "support", "@zomatocare", "zomatocare",
    "app", "tracking",
    "agent", "rider", "delivery boy", "delivery guy", "delivery person", "delivery partner",
    "food", "restaurant", "menu",
    "gold", "subscription",
    "founder", "ceo", "ipo", "stock", "share", "valuation", "earnings", "revenue",
    "merchant", "commission", "settlement", "payout", "kyc", "fssai",
    "review", "rating", "complaint",
)


class RedditScraper(BaseScraper):
    name = "reddit"

    def __init__(self, storage: Storage):
        self.storage = storage
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": config.REDDIT_USER_AGENT},
            )
        return self._client

    async def health_check(self) -> bool:
        client = await self._http()
        try:
            r = await client.get("https://www.reddit.com/r/india/new.json?limit=1")
            r.raise_for_status()
            data = r.json()
            return bool(data.get("data", {}).get("children"))
        except Exception as e:
            logger.error(f"Reddit health check failed: {e}")
            return False

    async def fetch(self) -> AsyncIterator[Post]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=config.REDDIT_MAX_AGE_HOURS)
        client = await self._http()

        # 1. Subreddit firehoses (with keyword filter on titles/selftext)
        for sub in config.REDDIT_SUBREDDITS:
            try:
                async for post in self._fetch_subreddit(client, sub, cutoff):
                    yield post
            except Exception as e:
                logger.warning(f"reddit:r/{sub} failed: {e}")
            await asyncio.sleep(_REQUEST_DELAY_S)

        # 2. Free-text searches across r/all (already topical, no keyword filter)
        for query in config.REDDIT_QUERIES:
            try:
                async for post in self._search(client, query, cutoff):
                    yield post
            except Exception as e:
                logger.warning(f"reddit:search:{query!r} failed: {e}")
            await asyncio.sleep(_REQUEST_DELAY_S)

    async def _fetch_subreddit(
        self, client: httpx.AsyncClient, sub: str, cutoff: datetime
    ) -> AsyncIterator[Post]:
        wm_key = f"reddit:r/{sub}"
        wm = await self.storage.get_watermark(wm_key)
        last_id = wm["last_native_id"] if wm else None

        url = (
            f"https://www.reddit.com/r/{sub}/new.json"
            f"?limit={config.REDDIT_LIMIT_PER_QUERY}"
        )
        children = await self._get_listing(client, url)
        if not children:
            return

        max_id = children[0]["data"]["id"]
        max_created = datetime.fromtimestamp(
            children[0]["data"]["created_utc"], tz=timezone.utc
        )

        for child in children:
            d = child["data"]
            if last_id and d["id"] == last_id:
                break
            created = datetime.fromtimestamp(d["created_utc"], tz=timezone.utc)
            if created < cutoff:
                continue
            ok, _ = _is_zomato_relevant(d.get("title") or "", d.get("selftext") or "")
            if not ok:
                continue
            yield self._submission_to_post(d)

        await self.storage.set_watermark(wm_key, max_id, max_created)

    async def _search(
        self, client: httpx.AsyncClient, query: str, cutoff: datetime
    ) -> AsyncIterator[Post]:
        wm_key = f"reddit:q:{query}"
        wm = await self.storage.get_watermark(wm_key)
        last_id = wm["last_native_id"] if wm else None

        url = (
            f"https://www.reddit.com/search.json"
            f"?q={quote(query)}&sort=new&t=day"
            f"&limit={config.REDDIT_LIMIT_PER_QUERY}"
        )
        children = await self._get_listing(client, url)
        if not children:
            return

        max_id = children[0]["data"]["id"]
        max_created = datetime.fromtimestamp(
            children[0]["data"]["created_utc"], tz=timezone.utc
        )

        for child in children:
            d = child["data"]
            if last_id and d["id"] == last_id:
                break
            created = datetime.fromtimestamp(d["created_utc"], tz=timezone.utc)
            if created < cutoff:
                continue
            # Reddit's search is fuzzy — apply the same relevance gate as
            # the subreddit firehose so search-noise doesn't bypass it.
            ok, _ = _is_zomato_relevant(d.get("title") or "", d.get("selftext") or "")
            if not ok:
                continue
            yield self._submission_to_post(d, query=query)

        await self.storage.set_watermark(wm_key, max_id, max_created)

    async def _get_listing(
        self, client: httpx.AsyncClient, url: str, *, retries: int = 2
    ) -> list[dict[str, Any]]:
        """Fetch a Reddit listing JSON. Returns the children list or [] on failure."""
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = await client.get(url)
                if r.status_code == 429:
                    retry_after = float(r.headers.get("Retry-After") or "5")
                    logger.warning(f"Reddit 429 on {url}; sleeping {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue
                r.raise_for_status()
                data = r.json()
                children = data.get("data", {}).get("children")
                return children if isinstance(children, list) else []
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
        if last_exc:
            raise last_exc
        return []

    @staticmethod
    def _submission_to_post(d: dict[str, Any], query: str | None = None) -> Post:
        body = d.get("title") or ""
        if d.get("selftext"):
            body = f"{body}\n\n{d['selftext']}".strip()
        return Post(
            source="reddit",
            native_id=d["id"],
            author=d.get("author"),
            content=body,
            url=f"https://reddit.com{d.get('permalink', '')}",
            created_at=datetime.fromtimestamp(d["created_utc"], tz=timezone.utc),
            metadata={
                "subreddit": d.get("subreddit"),
                "score": d.get("score"),
                "num_comments": d.get("num_comments"),
                "upvote_ratio": d.get("upvote_ratio"),
                "is_self": d.get("is_self"),
                "matched_query": query,
                "flair": d.get("link_flair_text"),
                "over_18": d.get("over_18"),
                "domain": d.get("domain"),
                "external_url": d.get("url"),
                "stickied": d.get("stickied"),
            },
        )


def _is_zomato_relevant(title: str, body: str) -> tuple[bool, str]:
    """Stricter relevance check than 'is the word zomato somewhere'.

    Returns (relevant, reason). Logic:
      - Brand token in TITLE  → strong yes (this is THE topic of the post)
      - 4+ other companies in body → reject (it's a list/career/comparison post,
        zomato is incidental even if mentioned)
      - Brand token + Zomato-specific context word → yes
      - Brand token only, no context → reject (passing reference)

    The point: distinguish posts ABOUT zomato from posts that just MENTION zomato.
    """
    title_low = (title or "").lower()
    body_low = (body or "").lower()
    full = f"{title_low} {body_low}"

    if not any(t in full for t in _BRAND_TOKENS):
        return False, "no zomato/deepinder mention"

    # Strong signal: brand token in the title — post is centrally about it
    if any(t in title_low for t in _BRAND_TOKENS):
        return True, "brand in title"

    # Detect company-list posts (zomato is one of many)
    other_company_hits = sum(1 for c in _OTHER_COMPANIES if c in full)
    zomato_count = full.count("zomato") + full.count("deepinder")
    if other_company_hits >= 4 and zomato_count <= 1:
        return False, f"company list ({other_company_hits} others, only {zomato_count} zomato)"

    # Body mention with Zomato-specific context (delivery / refund / founder / etc.)
    if any(c in full for c in _ZOMATO_CONTEXT):
        return True, "brand + context word"

    # Multiple zomato mentions even without explicit context — likely real
    if zomato_count >= 2:
        return True, "multiple zomato mentions"

    # Single mention, no context — passing reference, likely noise
    return False, "passing reference only"
