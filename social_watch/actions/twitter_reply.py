"""Twitter / X reply connector — turns Twitter into a bidirectional channel.

Reuses the existing authenticated Playwright session pattern from
``scrapers/twitter.py``. Same cookies (``auth_token``, ``ct0``) on the
``@forzomato`` throwaway account; nothing new to set up.

This is a **manual-only** connector by design: auto-replying from a
classifier signal is too high-risk for a take-home — a single misread
tweet becomes a public embarrassment from the corporate handle. The
demo flow is "operator clicks Reply, sees the templated text in a
modal, hits Send."

Setup (already done if Twitter scraping works):
    1. Cookies in `.env`:
           TWITTER_COOKIE_USERNAME=forzomato
           TWITTER_COOKIE_AUTH_TOKEN=...
           TWITTER_COOKIE_CT0=...
    2. Cookies last ~30 days; refresh via DevTools when stale.

Reply text generation: rules-first templated by ``priority_band`` +
``audience`` (matches the project's cost-discipline pattern in
``classifier/``). LLM overlay is intentionally NOT here — replies are
short, formulaic, and we don't want to spend the free-tier budget on
8 templated fragments.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from .. import config

# ============================================================
# Config
# ============================================================

# Sentinel returned by webhook_url() when cookies are present, so the
# dispatcher's "if mod.webhook_url()" check works uniformly across all
# channel modules. The dispatcher only checks truthiness.
_CONFIGURED_SENTINEL = "twitter-session"

# Twitter post URL → status ID. Accepts both x.com and twitter.com.
_STATUS_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)", re.IGNORECASE)

# Twitter's hard limit on tweet text. Reply must fit.
_TWEET_MAX_CHARS = 280

# Playwright timeouts. Reply takes longer than scraping because we wait
# for DOM mutations after typing + clicking Reply.
_PAGE_NAV_TIMEOUT_MS = 20_000
_SELECTOR_TIMEOUT_MS = 12_000
_POST_REPLY_TIMEOUT_MS = 10_000

_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


# ============================================================
# Configuration check (matches the slack/email/etc. pattern)
# ============================================================

def webhook_url() -> str | None:
    """Return a sentinel string when the connector is configured, else None.

    The dispatcher checks truthiness here; the actual cookies live on
    ``config`` and are read by ``_build_context``. We don't expose the
    cookies because they're secret.
    """
    if (
        config.TWITTER_COOKIE_AUTH_TOKEN
        and config.TWITTER_COOKIE_CT0
    ):
        return _CONFIGURED_SENTINEL
    return None


def is_configured() -> bool:
    """Friendly alias for webhook_url() truthiness."""
    return webhook_url() is not None


# ============================================================
# Reply text templates
# ============================================================

# Default fall-through reply. Friendly, short, action-oriented.
_DEFAULT_REPLY = (
    "Hi @{user}, sorry to hear this. Could you DM us your order ID so we can "
    "help right away? — Team Zomato"
)

# Audience-specific templates. Keys are values that show up in
# ``classification.audience``. Order matters: we pick the first match
# in the classification's audience list.
_AUDIENCE_TEMPLATES: dict[str, str] = {
    "customer-care": (
        "Hi @{user}, sorry about this. Please DM us your order ID and we'll "
        "look into it right away. — Team Zomato"
    ),
    "ops": (
        "Hi @{user}, thanks for flagging — DMing now so our ops team can "
        "track this end-to-end. — Team Zomato"
    ),
    "safety": (
        "Hi @{user}, we take food-safety reports seriously. Please DM us "
        "your order ID and a photo if possible — we'll escalate immediately."
    ),
    "legal": (
        "Hi @{user}, thank you for raising this. Our team will reach out "
        "via DM to follow up properly."
    ),
    "founder-office": (
        "Thanks for flagging, @{user}. We're looking into this and will "
        "follow up. — Team Zomato"
    ),
    "pr": (
        "Hi @{user}, thank you for raising this — we hear you and we're "
        "looking into it. Please DM us so we can follow up properly."
    ),
}


def build_reply_text(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any] | None = None,  # noqa: ARG001 — kept for contract symmetry
) -> str:
    """Pick a templated reply based on the post's audience tags.

    Falls back to a generic friendly reply when no audience matches or
    no template fits. Always returns a string ≤ 280 chars.
    """
    user = (post.get("author") or "").lstrip("@") or "there"
    audience = classification.get("audience") or []

    template = _DEFAULT_REPLY
    if isinstance(audience, list):
        for aud in audience:
            key = str(aud).strip().lower()
            if key in _AUDIENCE_TEMPLATES:
                template = _AUDIENCE_TEMPLATES[key]
                break

    text = template.format(user=user)
    if len(text) > _TWEET_MAX_CHARS:
        text = text[: _TWEET_MAX_CHARS - 1].rstrip() + "…"
    return text


# ============================================================
# Playwright session — mirrors scrapers/twitter.py
# ============================================================

async def _build_context(browser: Any) -> Any:
    """Build an authenticated Playwright context using the saved cookies.

    Mirrors the pattern in ``scrapers/twitter.py:_build_context`` so a
    cookie refresh in one place fixes both.
    """
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


# ============================================================
# Sender — the actual reply
# ============================================================

async def reply_to_tweet(tweet_url: str, text: str) -> dict[str, Any]:
    """Open ``tweet_url``, type ``text`` into the inline reply box, click
    Reply, verify the reply lands, return a result dict.

    Always returns a dict (never raises). The dispatcher persists this
    dict as ``posts.action_meta.channels.twitter_reply``.

    Returns:
        {
          "ok":     bool,
          "status": int  (0 if no HTTP exchange — Playwright fires DOM events),
          "ts":     iso8601,
          "error":  str | None,
          "reply_url": str | None,   # populated on success when we can extract it
        }
    """
    sent_at = datetime.now(timezone.utc).isoformat()

    # Validate inputs early so we don't open a browser for garbage.
    if not is_configured():
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": "twitter cookies not configured (TWITTER_COOKIE_AUTH_TOKEN / CT0 missing)",
            "reply_url": None,
        }
    if not _STATUS_RE.match(tweet_url or ""):
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": f"invalid tweet URL: {tweet_url!r}",
            "reply_url": None,
        }
    text = (text or "").strip()
    if not text:
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": "empty reply text",
            "reply_url": None,
        }
    if len(text) > _TWEET_MAX_CHARS:
        text = text[: _TWEET_MAX_CHARS - 1].rstrip() + "…"

    # Normalize twitter.com → x.com so the cookie domain matches.
    parsed = urlparse(tweet_url)
    if parsed.netloc.endswith("twitter.com"):
        tweet_url = tweet_url.replace("twitter.com", "x.com", 1)

    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": "playwright not installed (uv pip install playwright)",
            "reply_url": None,
        }

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await _build_context(browser)
                page = await context.new_page()

                # Navigate to the tweet.
                await page.goto(
                    tweet_url,
                    wait_until="domcontentloaded",
                    timeout=_PAGE_NAV_TIMEOUT_MS,
                )

                # Wait for the inline reply box. X renders the reply
                # composer right under the tweet on a status page.
                try:
                    await page.wait_for_selector(
                        '[data-testid="tweetTextarea_0"]',
                        timeout=_SELECTOR_TIMEOUT_MS,
                    )
                except PWTimeout:
                    return {
                        "ok": False, "status": 0, "ts": sent_at,
                        "error": "reply textarea not found (cookies may be stale)",
                        "reply_url": None,
                    }

                # Focus + type. We click first so the contenteditable receives focus
                # before keyboard events.
                await page.click('[data-testid="tweetTextarea_0"]')
                await page.keyboard.type(text, delay=15)

                # Wait a beat for the Reply button to enable (it activates
                # when the textarea has content).
                await page.wait_for_timeout(500)

                # Submit. The inline button on a status page is "tweetButtonInline";
                # the modal-style is "tweetButton". Try both.
                clicked = False
                for selector in (
                    '[data-testid="tweetButtonInline"]:not([aria-disabled="true"])',
                    '[data-testid="tweetButton"]:not([aria-disabled="true"])',
                ):
                    try:
                        btn = await page.wait_for_selector(selector, timeout=3000)
                        if btn:
                            await btn.click()
                            clicked = True
                            break
                    except PWTimeout:
                        continue

                if not clicked:
                    return {
                        "ok": False, "status": 0, "ts": sent_at,
                        "error": "reply submit button never enabled",
                        "reply_url": None,
                    }

                # Verify: the textarea should clear, OR we should see a
                # confirmation toast. We use the textarea-empty signal as the
                # primary check; it's the most reliable.
                landed = False
                try:
                    await page.wait_for_function(
                        """() => {
                            const ta = document.querySelector('[data-testid="tweetTextarea_0"]');
                            if (!ta) return true;  // no textarea = it submitted and rerendered
                            return (ta.textContent || '').trim() === '';
                        }""",
                        timeout=_POST_REPLY_TIMEOUT_MS,
                    )
                    landed = True
                except PWTimeout:
                    landed = False

                if not landed:
                    return {
                        "ok": False, "status": 0, "ts": sent_at,
                        "error": "reply did not appear to submit (textarea still has text)",
                        "reply_url": None,
                    }

                # Best-effort: extract the new reply's permalink. This is
                # nice-to-have, not required. If we can't find it, still
                # return ok=True.
                reply_url: str | None = None
                try:
                    # Our reply gets rendered in the timeline; find an article
                    # with our handle. Doesn't always work because Twitter's
                    # render is async and selectors are brittle.
                    handle = config.TWITTER_COOKIE_USERNAME or ""
                    if handle:
                        await page.wait_for_timeout(1000)
                        href = await page.evaluate(
                            f"""() => {{
                                const articles = document.querySelectorAll('article');
                                for (const a of articles) {{
                                    const link = a.querySelector('a[href*="/status/"]');
                                    if (!link) continue;
                                    const txt = a.textContent || '';
                                    if (txt.includes('@{handle}')) {{
                                        return link.href;
                                    }}
                                }}
                                return null;
                            }}"""
                        )
                        if href:
                            reply_url = href
                except Exception:
                    reply_url = None

                return {
                    "ok": True,
                    "status": 200,
                    "ts": sent_at,
                    "error": None,
                    "reply_url": reply_url,
                }

            finally:
                await browser.close()

    except Exception as e:  # pragma: no cover — last-resort safety net
        logger.exception("[twitter_reply] unexpected send error")
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": f"{type(e).__name__}: {e}",
            "reply_url": None,
        }


# ============================================================
# Convenience — matches the slack/email/etc. dispatcher contract
# ============================================================

async def build_and_send(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compose the reply text and post it. Returns (payload, result).

    Used by the dispatcher's per-channel iteration loop. The "payload"
    here is just the templated text + the target URL — no JSON body
    because Playwright drives the DOM directly.
    """
    text = build_reply_text(post, classification, priority_breakdown)
    target_url = post.get("url") or ""
    payload = {
        "channel": "twitter_reply",
        "tweet_url": target_url,
        "reply_text": text,
    }
    result = await reply_to_tweet(target_url, text)
    return payload, result


__all__ = [
    "build_reply_text",
    "reply_to_tweet",
    "build_and_send",
    "webhook_url",
    "is_configured",
]


# ============================================================
# Sanity test
# ============================================================
# Run with: python -m social_watch.actions.twitter_reply
# Tests are pure (no network) — they verify the templating logic only.
# Actual reply sending is exercised by the smoke harness with --live.

if __name__ == "__main__":
    cases = [
        # (post, classification, expected substring in reply)
        (
            {"author": "alice"},
            {"audience": ["customer-care"]},
            "alice",
        ),
        (
            {"author": "@bob"},  # leading @ should be stripped
            {"audience": ["safety"]},
            "food-safety",
        ),
        (
            {"author": "carol"},
            {"audience": ["legal", "pr"]},  # legal wins (first match)
            "follow up",
        ),
        (
            {"author": ""},  # empty author falls back to "there"
            {"audience": []},
            "there",
        ),
        (
            {"author": "dave"},
            {"audience": ["unknown-tag"]},  # unknown audience → default template
            "DM us your order ID",
        ),
    ]
    fail = 0
    for post, cls, expected in cases:
        got = build_reply_text(post, cls)
        ok = expected in got and len(got) <= _TWEET_MAX_CHARS
        if not ok:
            fail += 1
        print(f"{'PASS' if ok else 'FAIL'}: build_reply_text(author={post.get('author')!r}, audience={cls.get('audience')}) -> {got!r}")
    print(f"\n{len(cases) - fail}/{len(cases)} passed")
    raise SystemExit(0 if fail == 0 else 1)
