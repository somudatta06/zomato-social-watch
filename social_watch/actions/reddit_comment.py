"""Reddit comment connector — turns Reddit into a bidirectional channel.

The scraper reads Reddit via public JSON endpoints (no auth). Writing
back requires OAuth, but the easiest path is the **personal-use
"script app"** flow — instant credentials from reddit.com/prefs/apps,
no review queue, password grant.

Setup (one-time, ~5 minutes):
    1. Visit https://www.reddit.com/prefs/apps
    2. "Create another app" → choose **"script"** (not "web app")
    3. Name: ``Zomato Social Watch``  ·  redirect URI: ``http://localhost``
    4. Copy the two opaque strings:
         • client_id  — appears under the app's name (looks like ``Abc12d-_xy``)
         • client_secret — labeled "secret"
    5. Add to ``.env``:
           REDDIT_CLIENT_ID=...
           REDDIT_CLIENT_SECRET=...
           REDDIT_USERNAME=krazybionic            # the throwaway account
           REDDIT_PASSWORD=...
           REDDIT_USER_AGENT="ZomatoSocialWatch/0.1 by u/krazybionic"
    6. Restart the dashboard.

Manual-only by design (same risk profile as Twitter reply): a misread
classifier signal becomes a public comment from the corporate-adjacent
account. Operator clicks "Comment on Reddit", reviews the templated
text in a modal, hits Send.

Implementation: raw httpx. PRAW would work too but adds a dependency
and the project's "no heavy frameworks" rule applies — the Reddit OAuth
password grant is ~30 lines.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from .. import config

# ============================================================
# Config
# ============================================================

# Sentinel matching the slack/email/sheets pattern.
_CONFIGURED_SENTINEL = "reddit-script-app"

# Reddit OAuth + API endpoints
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_BASE = "https://oauth.reddit.com"

_TOKEN_TIMEOUT_S = 10.0
_COMMENT_TIMEOUT_S = 15.0

# Comment-character ceiling. Reddit's hard limit is 10000; we cap at
# 1500 to keep our replies short and not look like a wall of text.
_COMMENT_MAX_CHARS = 1500

# Match a Reddit submission URL → post ID. Accepts old.reddit, np.,
# and trailing-slash variants. Group 1 is the bare ID (e.g. "1sy5r0k").
_REDDIT_POST_ID_RE = re.compile(
    r"^https?://(?:www\.|old\.|np\.)?reddit\.com/r/[^/]+/comments/([A-Za-z0-9]+)",
    re.IGNORECASE,
)


# ============================================================
# Configuration check
# ============================================================

# Reddit auth uses 4 env vars. We define them locally so a future
# config refactor doesn't silently break us.
REDDIT_USERNAME_ENV = "REDDIT_USERNAME"
REDDIT_PASSWORD_ENV = "REDDIT_PASSWORD"


def _username() -> str:
    return (os.getenv(REDDIT_USERNAME_ENV) or "").strip()


def _password() -> str:
    return (os.getenv(REDDIT_PASSWORD_ENV) or "").strip()


def webhook_url() -> str | None:
    """Sentinel for the dispatcher's truthiness check."""
    if (
        config.REDDIT_CLIENT_ID
        and config.REDDIT_CLIENT_SECRET
        and _username()
        and _password()
    ):
        return _CONFIGURED_SENTINEL
    return None


def is_configured() -> bool:
    """Friendly alias for webhook_url() truthiness."""
    return webhook_url() is not None


# ============================================================
# Comment text templates
# ============================================================
#
# Reddit comments are longer-form than tweets — readers expect a
# couple of sentences and a clear action. Tone is supportive,
# non-corporate. No marketing language.

_DEFAULT_REPLY = (
    "Hi u/{user}, sorry to hear this happened. We'd like to look into it "
    "for you — could you DM us your order ID and the issue, or open a "
    "ticket via the Help section in the app? — Team Zomato"
)

_AUDIENCE_TEMPLATES: dict[str, str] = {
    "customer-care": (
        "Hi u/{user}, sorry about this. If you DM us your order ID we'll "
        "look into it right away. — Team Zomato"
    ),
    "ops": (
        "Thanks for flagging u/{user} — sending this to our ops team. "
        "DM us your order ID and we'll track it end-to-end."
    ),
    "safety": (
        "Hi u/{user}, food-safety reports go straight to escalation. "
        "Could you DM us your order ID and any photos? We'll follow up."
    ),
    "legal": (
        "Hi u/{user}, thanks for raising this. We'll have someone "
        "reach out via DM to follow up properly."
    ),
    "founder-office": (
        "Thanks for flagging this u/{user}. We're looking into it. "
        "— Team Zomato"
    ),
    "pr": (
        "Hi u/{user}, thank you for raising this — DM us so we can "
        "follow up properly."
    ),
}


def build_reply_text(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any] | None = None,  # noqa: ARG001
) -> str:
    """Pick a templated comment based on the post's audience tags.

    Parallel to ``twitter_reply.build_reply_text`` — same fall-through,
    same audience matching, just different copy and a bigger char cap.
    """
    user = (post.get("author") or "").lstrip("/").lstrip("u").lstrip("/").strip() or "there"
    audience = classification.get("audience") or []

    template = _DEFAULT_REPLY
    if isinstance(audience, list):
        for aud in audience:
            key = str(aud).strip().lower()
            if key in _AUDIENCE_TEMPLATES:
                template = _AUDIENCE_TEMPLATES[key]
                break

    text = template.format(user=user)
    if len(text) > _COMMENT_MAX_CHARS:
        text = text[: _COMMENT_MAX_CHARS - 1].rstrip() + "…"
    return text


# ============================================================
# URL parsing
# ============================================================

def extract_post_id(reddit_url: str) -> str | None:
    """Pull the post ID out of a reddit URL. Returns None if the URL
    doesn't look like a submission link."""
    if not reddit_url:
        return None
    m = _REDDIT_POST_ID_RE.match(reddit_url)
    return m.group(1) if m else None


def thing_id(reddit_url: str) -> str | None:
    """Reddit's API expects a fullname-style id: ``t3_<postid>`` for
    submissions, ``t1_<commentid>`` for comments. We only support
    top-level submission replies for now."""
    pid = extract_post_id(reddit_url)
    return f"t3_{pid}" if pid else None


# ============================================================
# OAuth — script-app password grant
# ============================================================

async def _fetch_access_token() -> tuple[str | None, str | None]:
    """Exchange (client_id, client_secret, username, password) for a
    short-lived bearer token. Returns (token, error)."""
    if not is_configured():
        return None, "REDDIT_* env vars not all set"
    headers = {
        "User-Agent": config.REDDIT_USER_AGENT,
    }
    auth = (config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET)
    data = {
        "grant_type": "password",
        "username": _username(),
        "password": _password(),
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT_S) as client:
            resp = await client.post(_TOKEN_URL, headers=headers, auth=auth, data=data)
        if resp.status_code != 200:
            return None, f"reddit token endpoint returned {resp.status_code}: {resp.text[:200]}"
        body = resp.json()
        token = body.get("access_token")
        if not token:
            return None, f"no access_token in response: {body}"
        return token, None
    except httpx.HTTPError as e:
        return None, f"httpx_error: {e}"
    except Exception as e:  # pragma: no cover
        return None, f"{type(e).__name__}: {e}"


# ============================================================
# Sender — the actual comment
# ============================================================

async def comment_on_post(reddit_url: str, text: str) -> dict[str, Any]:
    """POST a comment under a Reddit submission. Always returns a dict.

    Returns:
        {
          "ok":     bool,
          "status": int,
          "ts":     iso8601,
          "error":  str | None,
          "comment_url": str | None,  # populated on success
        }
    """
    sent_at = datetime.now(timezone.utc).isoformat()

    if not is_configured():
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": "REDDIT_* env vars not all set",
            "comment_url": None,
        }

    parent_id = thing_id(reddit_url)
    if not parent_id:
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": f"could not extract reddit post id from URL: {reddit_url!r}",
            "comment_url": None,
        }

    text = (text or "").strip()
    if not text:
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": "empty comment text",
            "comment_url": None,
        }
    if len(text) > _COMMENT_MAX_CHARS:
        text = text[: _COMMENT_MAX_CHARS - 1].rstrip() + "…"

    token, err = await _fetch_access_token()
    if not token:
        return {
            "ok": False, "status": 0, "ts": sent_at,
            "error": err or "auth failed",
            "comment_url": None,
        }

    headers = {
        "Authorization": f"bearer {token}",
        "User-Agent": config.REDDIT_USER_AGENT,
    }
    payload = {
        "api_type": "json",
        "thing_id": parent_id,
        "text": text,
    }

    try:
        async with httpx.AsyncClient(timeout=_COMMENT_TIMEOUT_S) as client:
            resp = await client.post(
                f"{_API_BASE}/api/comment", headers=headers, data=payload
            )
        if resp.status_code != 200:
            return {
                "ok": False, "status": resp.status_code, "ts": sent_at,
                "error": f"reddit /api/comment returned {resp.status_code}: {resp.text[:200]}",
                "comment_url": None,
            }
        body = resp.json()
        wrap = body.get("json") or {}
        errors = wrap.get("errors") or []
        if errors:
            return {
                "ok": False, "status": 200, "ts": sent_at,
                "error": f"reddit returned errors: {errors}",
                "comment_url": None,
            }
        # Pull the new comment's permalink from the response if available.
        comment_url: str | None = None
        try:
            things = (wrap.get("data") or {}).get("things") or []
            if things:
                permalink = things[0].get("data", {}).get("permalink")
                if permalink:
                    comment_url = (
                        permalink
                        if permalink.startswith("http")
                        else f"https://reddit.com{permalink}"
                    )
        except Exception:
            comment_url = None

        return {
            "ok": True, "status": 200, "ts": sent_at,
            "error": None,
            "comment_url": comment_url,
        }
    except httpx.TimeoutException as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"timeout: {e}", "comment_url": None}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"httpx_error: {e}", "comment_url": None}
    except Exception as e:  # pragma: no cover
        logger.exception("[reddit_comment] unexpected send error")
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"{type(e).__name__}: {e}", "comment_url": None}


# ============================================================
# Convenience — matches the dispatcher contract
# ============================================================

async def build_and_send(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    text = build_reply_text(post, classification, priority_breakdown)
    target_url = post.get("url") or ""
    payload = {
        "channel": "reddit_comment",
        "reddit_url": target_url,
        "comment_text": text,
    }
    result = await comment_on_post(target_url, text)
    return payload, result


__all__ = [
    "build_reply_text",
    "comment_on_post",
    "build_and_send",
    "webhook_url",
    "is_configured",
    "extract_post_id",
    "thing_id",
]


# ============================================================
# Sanity test — pure (no network)
# ============================================================
# Run with: python -m social_watch.actions.reddit_comment
# Verifies: URL parsing + templating logic.

if __name__ == "__main__":
    url_cases = [
        ("https://reddit.com/r/india/comments/1sy5r0k/some_title/", "1sy5r0k"),
        ("https://www.reddit.com/r/mumbai/comments/abc123/", "abc123"),
        ("https://old.reddit.com/r/delhi/comments/xyz789/foo/bar/", "xyz789"),
        ("https://reddit.com/r/india/wiki/something", None),
        ("not a url", None),
        ("", None),
    ]
    text_cases = [
        ({"author": "alice"}, {"audience": ["customer-care"]}, "u/alice"),
        ({"author": "u/bob"}, {"audience": ["safety"]}, "u/bob"),
        ({"author": "carol"}, {"audience": ["legal", "pr"]}, "u/carol"),
        ({"author": ""},      {"audience": []},          "u/there"),
    ]
    fail = 0
    for url, want in url_cases:
        got = extract_post_id(url)
        ok = got == want
        if not ok:
            fail += 1
        print(f"{'PASS' if ok else 'FAIL'}: extract_post_id({url!r}) -> {got!r}  (want {want!r})")
    for post, cls, must_contain in text_cases:
        got = build_reply_text(post, cls)
        ok = must_contain in got and len(got) <= _COMMENT_MAX_CHARS
        if not ok:
            fail += 1
        print(f"{'PASS' if ok else 'FAIL'}: build_reply_text(author={post.get('author')!r}) -> contains {must_contain!r}? {must_contain in got}")
    print(f"\n{len(url_cases) + len(text_cases) - fail}/{len(url_cases) + len(text_cases)} passed")
    raise SystemExit(0 if fail == 0 else 1)
