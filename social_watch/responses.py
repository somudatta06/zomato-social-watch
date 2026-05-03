"""Detect whether Zomato has already replied to a post — Twitter only.

Reddit is intentionally out of scope: corporate Zomato presence on Reddit
is essentially zero, so tracking would generate noise without signal.

Twitter is two-tier:

  TIER 1  — broad sweep (every cycle, cheap)
            Scrape @zomatocare/with_replies, scroll deeply, extract every
            reply with its parent tweet ID, cross-reference against our DB.
            One Playwright navigation catches dozens of replies at once.

  TIER 2  — per-tweet thread check (priority queue, expensive)
            For high-urgency tagged tweets that Tier 1 didn't match AND are
            >30 min old, navigate to the tweet's own page and look for
            @zomatocare in the visible thread. ~10s per tweet, budgeted to
            5 per cycle.

Status semantics on `posts.zomato_response_status`:

  not_applicable — Reddit posts, OR Twitter posts that don't tag
                   @zomato/@zomatocare (organic mention, no reply expected)
  unchecked      — Twitter post that tagged Zomato; reply not yet verified
  replied        — Confirmed Zomato reply found
  no_reply       — Verified no reply (>24h since posting)

Outputs persist to:
  posts.zomato_response_status
  posts.zomato_response_url
  posts.zomato_response_at
  posts.zomato_response_checked_at
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from loguru import logger

from . import config

# ---------- known Zomato official handles ----------

ZOMATO_TWITTER_HANDLES = {
    h.lower() for h in (
        "zomato", "zomatocare",
    )
}

# Tunables
_TIER2_BUDGET_PER_CYCLE = 5         # per-tweet thread checks
_TIER2_MIN_AGE_MINUTES = 30          # don't deep-check posts younger than this
_NO_REPLY_AGE_HOURS = 24             # mark unchecked posts as no_reply after this

_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ============================================================
# Initial-status policy (called by scrapers on insert + by migration)
# ============================================================

def _twitter_tagged_zomato(text: str) -> bool:
    """True if the tweet body explicitly tags an official Zomato handle.
    These are the tweets where a reply IS expected."""
    t = (text or "").lower()
    return "@zomato" in t or "@zomatocare" in t


def initial_status_for_post(source: str, content: str) -> str:
    """Decide the starting zomato_response_status for a fresh post.
    Reddit and untagged Twitter mentions are not_applicable from the start
    — saves all downstream cost.
    """
    if source == "reddit":
        return "not_applicable"
    if source == "twitter":
        return "unchecked" if _twitter_tagged_zomato(content) else "not_applicable"
    return "not_applicable"


async def backfill_initial_statuses() -> dict[str, int]:
    """One-time migration: walk existing posts and apply the initial-status
    policy. Skips posts already marked `replied` (idempotent — never
    overwrites a confirmed reply)."""
    counts = {"reddit_not_applicable": 0, "twitter_not_applicable": 0, "twitter_unchecked": 0, "skipped_replied": 0}
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, source, content, zomato_response_status FROM posts")
        rows = await cur.fetchall()
        for r in rows:
            if r["zomato_response_status"] == "replied":
                counts["skipped_replied"] += 1
                continue
            target = initial_status_for_post(r["source"], r["content"] or "")
            await db.execute(
                "UPDATE posts SET zomato_response_status = ? WHERE id = ?",
                (target, r["id"]),
            )
            if r["source"] == "reddit":
                counts["reddit_not_applicable"] += 1
            elif target == "not_applicable":
                counts["twitter_not_applicable"] += 1
            else:
                counts["twitter_unchecked"] += 1
        await db.commit()
    logger.info(f"Backfill done: {counts}")
    return counts


# ============================================================
# TIER 1 — @zomatocare timeline broad sweep
# ============================================================

# JS that walks @zomatocare's /with_replies timeline.
#
# Twitter's with_replies UI renders each reply as TWO sibling <article>
# elements: [parent tweet] then [zomatocare's reply]. The parent_id is
# NOT a link inside the reply article — it lives in the PRECEDING article.
# Our extractor walks articles in DOM order and pairs each zomatocare-
# authored article with its immediate predecessor.
_EXTRACT_REPLIES_JS = r"""
() => {
  const out = [];
  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  const ownLinkOf = (art) => {
    // The FIRST status link containing a <time> child is the article's own permalink
    for (const a of art.querySelectorAll('a[href*="/status/"]')) {
      if (a.querySelector('time')) return a;
    }
    return null;
  };
  for (let i = 0; i < articles.length; i++) {
    const art = articles[i];
    const ownLink = ownLinkOf(art);
    if (!ownLink) continue;
    const ownHref = ownLink.getAttribute('href') || '';
    const ownMatch = ownHref.match(/^\/([^\/]+)\/status\/(\d+)/);
    if (!ownMatch) continue;
    const ownUser = ownMatch[1];
    const ownId = ownMatch[2];
    if (ownUser.toLowerCase() !== 'zomatocare') continue;
    // Look at the previous article to find the parent tweet
    if (i === 0) continue;
    const prev = articles[i - 1];
    const prevLink = ownLinkOf(prev);
    if (!prevLink) continue;
    const prevHref = prevLink.getAttribute('href') || '';
    const prevMatch = prevHref.match(/^\/([^\/]+)\/status\/(\d+)/);
    if (!prevMatch) continue;
    const parentUser = prevMatch[1];
    const parentId = prevMatch[2];
    // Skip zomatocare→zomatocare self-thread continuations
    if (parentUser.toLowerCase() === 'zomatocare') continue;
    const timeEl = art.querySelector('time[datetime]');
    out.push({
      reply_id: ownId,
      reply_url: 'https://x.com' + ownHref,
      parent_user: parentUser,
      parent_id: parentId,
      iso_date: timeEl ? timeEl.getAttribute('datetime') : null,
    });
  }
  // Dedup by reply_id
  const seen = new Set();
  return out.filter(r => seen.has(r.reply_id) ? false : (seen.add(r.reply_id), true));
}
"""


async def tier1_sweep_zomatocare_timeline() -> dict[str, int]:
    """Scrape @zomatocare's reply timeline and bulk-update matched parents."""
    counts = {"replies_seen": 0, "matched": 0}
    if not (config.TWITTER_COOKIE_USERNAME and config.TWITTER_COOKIE_AUTH_TOKEN):
        logger.debug("Tier1 skipped: no cookies")
        return counts
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return counts

    replies: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await _make_x_context(browser)
            page = await context.new_page()
            try:
                await page.goto(
                    "https://x.com/zomatocare/with_replies",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                try:
                    await page.wait_for_selector(
                        'article[data-testid="tweet"]', timeout=10000
                    )
                except Exception:
                    logger.warning("Tier1: no tweets visible on @zomatocare/with_replies")
                    return counts
                # Scroll deeper than before — enterprise version pulls 7 pages
                for _ in range(7):
                    await page.mouse.wheel(0, 3000)
                    await page.wait_for_timeout(1500)
                replies = await page.evaluate(_EXTRACT_REPLIES_JS)
            finally:
                await page.close()
        finally:
            await browser.close()

    counts["replies_seen"] = len(replies)
    if not replies:
        return counts

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        for r in replies:
            parent_id = r.get("parent_id")
            if not parent_id:
                continue
            iso = r.get("iso_date")
            try:
                resp_at = (
                    datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    if iso else None
                )
            except Exception:
                resp_at = None
            cur = await db.execute(
                """
                UPDATE posts SET
                  zomato_response_status = 'replied',
                  zomato_response_url = ?,
                  zomato_response_at = COALESCE(?, zomato_response_at),
                  zomato_response_checked_at = ?
                WHERE source = 'twitter'
                  AND native_id = ?
                  AND (zomato_response_status IS NULL OR zomato_response_status != 'replied')
                """,
                (
                    r["reply_url"],
                    resp_at.isoformat() if resp_at else None,
                    datetime.now(timezone.utc).isoformat(),
                    parent_id,
                ),
            )
            if cur.rowcount and cur.rowcount > 0:
                counts["matched"] += 1
        await db.commit()

    logger.info(
        f"Tier1 sweep: scraped {counts['replies_seen']} @zomatocare replies, "
        f"matched {counts['matched']} parents"
    )
    return counts


# ============================================================
# TIER 2 — per-tweet thread check (priority queue)
# ============================================================

# JS to find a @zomatocare reply on a single tweet's thread page
_FIND_ZOMATOCARE_REPLY_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('article[data-testid="tweet"]').forEach(art => {
    const allLinks = Array.from(art.querySelectorAll('a[href*="/status/"]'));
    let replyHref = null;
    for (const a of allLinks) {
      if (a.querySelector('time')) {
        replyHref = a.getAttribute('href');
        break;
      }
    }
    if (!replyHref) return;
    const m = replyHref.match(/^\/([^\/]+)\/status\/(\d+)/);
    if (!m) return;
    const user = m[1];
    if (user.toLowerCase() !== 'zomatocare') return;
    const time = art.querySelector('time[datetime]');
    out.push({
      reply_id: m[2],
      reply_url: 'https://x.com' + replyHref,
      iso_date: time ? time.getAttribute('datetime') : null,
    });
  });
  return out;
}
"""


async def tier2_per_tweet_check(*, budget: int = _TIER2_BUDGET_PER_CYCLE) -> dict[str, int]:
    """Pick the top-N highest-urgency unchecked tagged-Zomato tweets older
    than _TIER2_MIN_AGE_MINUTES and check each tweet's thread directly."""
    counts = {"checked": 0, "replied": 0, "still_no_reply": 0, "errors": 0}
    if not (config.TWITTER_COOKIE_USERNAME and config.TWITTER_COOKIE_AUTH_TOKEN):
        return counts
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return counts

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=_TIER2_MIN_AGE_MINUTES)).isoformat()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, native_id, url, content, score FROM posts
            WHERE source = 'twitter'
              AND zomato_response_status = 'unchecked'
              AND created_at < ?
            ORDER BY score DESC, created_at DESC
            LIMIT ?
            """,
            (cutoff_iso, budget),
        )
        targets = [dict(r) for r in await cur.fetchall()]
        if not targets:
            return counts

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await _make_x_context(browser)
            for t in targets:
                try:
                    found = await _check_one_tweet_thread(context, t["url"])
                    counts["checked"] += 1
                except Exception as e:
                    logger.debug(f"Tier2 {t['native_id']} crash: {e}")
                    counts["errors"] += 1
                    continue
                async with aiosqlite.connect(str(config.DB_PATH)) as db:
                    if found:
                        counts["replied"] += 1
                        await db.execute(
                            """
                            UPDATE posts SET
                              zomato_response_status = 'replied',
                              zomato_response_url = ?,
                              zomato_response_at = COALESCE(?, zomato_response_at),
                              zomato_response_checked_at = ?
                            WHERE id = ?
                            """,
                            (
                                found.get("reply_url"),
                                found.get("iso_date"),
                                datetime.now(timezone.utc).isoformat(),
                                t["id"],
                            ),
                        )
                    else:
                        counts["still_no_reply"] += 1
                        # Just record the check attempt — leave status='unchecked'
                        # until _NO_REPLY_AGE_HOURS, then promote to 'no_reply'
                        await db.execute(
                            "UPDATE posts SET zomato_response_checked_at = ? WHERE id = ?",
                            (datetime.now(timezone.utc).isoformat(), t["id"]),
                        )
                    await db.commit()
        finally:
            await browser.close()

    logger.info(
        f"Tier2 per-tweet: checked={counts['checked']} replied={counts['replied']} "
        f"still_no_reply={counts['still_no_reply']} errors={counts['errors']}"
    )
    return counts


async def _check_one_tweet_thread(context: Any, tweet_url: str) -> dict[str, Any] | None:
    """Navigate to a tweet's page and look for @zomatocare in the thread."""
    page = await context.new_page()
    try:
        await page.goto(tweet_url, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_selector('article[data-testid="tweet"]', timeout=10000)
        except Exception:
            return None
        for _ in range(3):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(1200)
        replies = await page.evaluate(_FIND_ZOMATOCARE_REPLY_JS)
        return replies[0] if replies else None
    finally:
        await page.close()


# ============================================================
# AGE-BASED PROMOTION: unchecked → no_reply after threshold
# ============================================================

async def promote_aged_unchecked_to_no_reply() -> int:
    """Twitter posts that have been 'unchecked' for >24h are marked
    no_reply (we've had enough chances to detect a reply via Tier 1; if
    we haven't found one by now, none will arrive)."""
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=_NO_REPLY_AGE_HOURS)
    ).isoformat()
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        cur = await db.execute(
            """
            UPDATE posts SET
              zomato_response_status = 'no_reply',
              zomato_response_checked_at = COALESCE(zomato_response_checked_at, ?)
            WHERE source = 'twitter'
              AND zomato_response_status = 'unchecked'
              AND created_at < ?
            """,
            (datetime.now(timezone.utc).isoformat(), cutoff_iso),
        )
        await db.commit()
        promoted = cur.rowcount or 0
    if promoted:
        logger.info(f"Aged unchecked → no_reply: {promoted} posts")
    return promoted


# ============================================================
# Helper: shared Playwright X context with cookies
# ============================================================

async def _make_x_context(browser: Any) -> Any:
    context = await browser.new_context(
        user_agent=_DESKTOP_UA,
        viewport={"width": 1366, "height": 900},
    )
    await context.add_cookies([
        {
            "name": "auth_token",
            "value": config.TWITTER_COOKIE_AUTH_TOKEN,
            "domain": ".x.com", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax",
        },
        {
            "name": "ct0",
            "value": config.TWITTER_COOKIE_CT0,
            "domain": ".x.com", "path": "/",
            "secure": True, "httpOnly": False, "sameSite": "Lax",
        },
    ])
    return context


# ============================================================
# Top-level orchestration
# ============================================================

async def check_all_responses() -> dict[str, Any]:
    """Run the full response detection pipeline once.

    1. Backfill any new posts with their initial status (cheap; idempotent).
    2. Tier 1 broad sweep of @zomatocare.
    3. Tier 2 per-tweet thread checks (budgeted).
    4. Promote aged unchecked → no_reply.

    Reddit is intentionally NOT checked.
    """
    out: dict[str, Any] = {}
    try:
        out["initial_statuses"] = await backfill_initial_statuses()
    except Exception as e:
        logger.exception("backfill_initial_statuses crashed")
        out["initial_statuses"] = {"error": str(e)}
    try:
        out["tier1"] = await tier1_sweep_zomatocare_timeline()
    except Exception as e:
        logger.exception("tier1_sweep crashed")
        out["tier1"] = {"error": str(e)}
    try:
        out["tier2"] = await tier2_per_tweet_check()
    except Exception as e:
        logger.exception("tier2_per_tweet_check crashed")
        out["tier2"] = {"error": str(e)}
    try:
        out["aged_promoted"] = await promote_aged_unchecked_to_no_reply()
    except Exception as e:
        logger.exception("aged-promotion crashed")
        out["aged_promoted"] = {"error": str(e)}
    return out
