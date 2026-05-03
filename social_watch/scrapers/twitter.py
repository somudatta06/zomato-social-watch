"""Twitter / X scraper using Playwright + saved session cookies.

Why Playwright instead of twscrape (the obvious choice):
  In April 2026, twscrape v0.17.0's GraphQL response parser fails with
  `IndexError: list index out of range` on Twitter's current API responses
  (verified across `search`, `user_by_login`, and other endpoints; locks
  the account for 15 minutes after each failure). It's not an account
  problem — the parser hasn't kept up with Twitter's schema changes. So we
  drive a real headless Chromium instance, load the user's session
  cookies, and scrape rendered HTML directly. Slower per query (~10–20s)
  but immune to API-shape drift.

Auth:
  Same TWITTER_COOKIE_AUTH_TOKEN / TWITTER_COOKIE_CT0 the user already
  pasted in `.env`. No new setup needed.

Lifecycle:
  One browser + one context per `fetch()` call. Navigate `/search?...&f=live`
  for each query, scroll a few times to surface more tweets, extract via
  `page.evaluate()` (cleaner than walking the DOM from Python).

Robustness:
  - Detects login walls / "Sign in to X" interstitials → marks scraper
    unconfigured for the cycle (cookies expired) so we don't loop
  - Bounded scroll/wait timeouts so a slow page doesn't hang the cycle
  - Per-query try/except so one bad query doesn't kill the rest
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from urllib.parse import quote

from loguru import logger

from .. import config
from ..models import Post
from ..storage import Storage
from .base import BaseScraper

_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# How long to wait for tweets to appear after navigation
_TWEET_WAIT_MS = 8000
# Number of small scrolls to load more tweets per query
_SCROLL_TIMES = 4
# Pause between scrolls (lets tweets render)
_SCROLL_PAUSE_MS = 1500
# Per-query hard ceiling so a stuck page can't hang the cycle
_QUERY_TIMEOUT_S = 45


# JS that walks every <article data-testid="tweet"> on the page and emits
# a flat list of {id, user, url, iso_date, text, reply_to}. Doing this
# in-page is cleaner than chaining locator queries from Python.
_EXTRACT_JS = """
() => {
    const out = [];
    document.querySelectorAll('article[data-testid="tweet"]').forEach(art => {
        // Permalink anchor: /<user>/status/<id>
        const links = art.querySelectorAll('a[href*="/status/"]');
        let id = null, user = null, url = null;
        for (const l of links) {
            const href = l.getAttribute('href') || '';
            const m = href.match(/^\\/([^\\/]+)\\/status\\/(\\d+)/);
            if (m) { user = m[1]; id = m[2]; url = 'https://x.com' + href; break; }
        }
        if (!id) return;
        const timeEl = art.querySelector('time[datetime]');
        const iso_date = timeEl ? timeEl.getAttribute('datetime') : null;
        const textEl = art.querySelector('[data-testid="tweetText"]');
        const text = textEl ? textEl.innerText : '';
        // Engagement counts (best-effort; layout shifts)
        const stat = (testid) => {
            const el = art.querySelector('[data-testid="' + testid + '"]');
            if (!el) return null;
            const span = el.querySelector('span');
            return span ? span.innerText.trim() : null;
        };
        out.push({
            id: id,
            user: user,
            url: url,
            iso_date: iso_date,
            text: text,
            reply_count: stat('reply'),
            retweet_count: stat('retweet'),
            like_count: stat('like'),
            view_count: stat('analytics'),
        });
    });
    // Dedup within a single page render (Twitter sometimes duplicates DOM)
    const seen = new Set();
    return out.filter(t => seen.has(t.id) ? false : (seen.add(t.id), true));
}
"""


class TwitterScraper(BaseScraper):
    name = "twitter"

    def __init__(self, storage: Storage):
        self.storage = storage

    def is_configured(self) -> bool:
        return bool(
            config.TWITTER_COOKIE_USERNAME
            and config.TWITTER_COOKIE_AUTH_TOKEN
            and config.TWITTER_COOKIE_CT0
        )

    async def health_check(self) -> bool:
        if not self.is_configured():
            logger.info("Twitter: cookies not set — scraper disabled")
            return False
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed (uv pip install playwright)")
            return False
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await self._build_context(browser)
                page = await context.new_page()
                await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=15000)
                # x.com is an SPA — DOM at domcontentloaded is bare. Wait for the
                # authenticated chrome to render. If it never appears, cookies are bad.
                try:
                    await page.wait_for_selector(
                        '[data-testid="SideNav_NewTweet_Button"]', timeout=10000
                    )
                    ok = True
                except Exception:
                    ok = False
                await browser.close()
                if not ok:
                    logger.warning("Twitter cookies appear invalid (no authenticated UI). Re-extract.")
                return ok
        except Exception as e:
            logger.error(f"Twitter health check failed: {e}")
            return False

    async def fetch(self) -> AsyncIterator[Post]:
        if not self.is_configured():
            return
        try:
            from playwright.async_api import async_playwright, TimeoutError as PWTimeout
        except ImportError:
            logger.error("playwright not installed; skipping Twitter")
            return

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await self._build_context(browser)
                # Sanity: check cookies are accepted by hitting /home once
                if not await self._verify_session(context):
                    logger.warning("Twitter cookies invalid/expired — skipping cycle")
                    return
                for query in config.TWITTER_QUERIES:
                    try:
                        async for post in self._search(context, query, cutoff):
                            yield post
                    except PWTimeout as e:
                        logger.warning(f"twitter:{query!r}: page timeout ({e})")
                    except Exception as e:
                        logger.warning(f"twitter:{query!r}: {e}")
            finally:
                await browser.close()

    async def _build_context(self, browser: Any) -> Any:
        context = await browser.new_context(
            user_agent=_DESKTOP_UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        await context.add_cookies(
            [
                {
                    "name": "auth_token",
                    "value": config.TWITTER_COOKIE_AUTH_TOKEN,
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "ct0",
                    "value": config.TWITTER_COOKIE_CT0,
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                },
            ]
        )
        return context

    async def _verify_session(self, context: Any) -> bool:
        page = await context.new_page()
        try:
            await page.goto(
                "https://x.com/home",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            try:
                await page.wait_for_selector(
                    '[data-testid="SideNav_NewTweet_Button"]', timeout=10000
                )
                return True
            except Exception:
                return False
        except Exception as e:
            logger.warning(f"Twitter session verification failed: {e}")
            return False
        finally:
            await page.close()

    async def _search(
        self, context: Any, query: str, cutoff: datetime
    ) -> AsyncIterator[Post]:
        wm_key = f"twitter:{query}"
        wm = await self.storage.get_watermark(wm_key)
        last_id = wm["last_native_id"] if wm else None

        url = (
            f"https://x.com/search?q={quote(query)}"
            f"&src=typed_query&f=live"
        )

        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_selector(
                    'article[data-testid="tweet"]', timeout=_TWEET_WAIT_MS
                )
            except Exception:
                # No tweets — could be empty result or a "Something went wrong" page
                logger.debug(f"twitter:{query!r}: no tweets selector")
                return

            # Scroll to surface more tweets in the timeline
            for _ in range(_SCROLL_TIMES):
                await page.mouse.wheel(0, 2500)
                await page.wait_for_timeout(_SCROLL_PAUSE_MS)

            raw_tweets: list[dict[str, Any]] = await page.evaluate(_EXTRACT_JS)
        finally:
            await page.close()

        if not raw_tweets:
            return

        # Stable order: newest first by iso_date when present
        raw_tweets.sort(
            key=lambda t: t.get("iso_date") or "", reverse=True
        )

        max_seen_id: str | None = None
        max_seen_created: datetime | None = None
        for t in raw_tweets:
            tid = str(t["id"])
            iso = t.get("iso_date")
            if iso:
                try:
                    created = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                except Exception:
                    created = datetime.now(timezone.utc)
            else:
                created = datetime.now(timezone.utc)

            if max_seen_id is None:
                max_seen_id = tid
                max_seen_created = created

            if last_id and tid == last_id:
                break
            if created < cutoff:
                continue
            yield self._tweet_dict_to_post(t, query, created)

        if max_seen_id:
            await self.storage.set_watermark(wm_key, max_seen_id, max_seen_created)

    @staticmethod
    def _tweet_dict_to_post(
        t: dict[str, Any], query: str, created: datetime
    ) -> Post:
        return Post(
            source="twitter",
            native_id=str(t["id"]),
            author=t.get("user"),
            content=t.get("text") or "",
            url=t.get("url") or f"https://x.com/i/status/{t['id']}",
            created_at=created,
            metadata={
                "matched_query": query,
                "via": "playwright",
                "reply_count": t.get("reply_count"),
                "retweet_count": t.get("retweet_count"),
                "like_count": t.get("like_count"),
                "view_count": t.get("view_count"),
            },
        )
