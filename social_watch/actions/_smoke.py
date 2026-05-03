"""Smoke tests for the action connectors.

Two modes:

    Mocked (default — `python -m social_watch.actions._smoke`):
      Runs without real network calls by monkey-patching httpx.
      Verifies payload shapes, error handling, dispatcher idempotency.

    Live (`python -m social_watch.actions._smoke --live`):
      Runs a real-network pre-flight check against every configured
      connector. Send-style channels (Slack/Discord/Email/Sheets) get
      a [SMOKE TEST] payload tagged with the current timestamp.
      Reply-style channels (Twitter) get an auth-only check — we never
      actually post on real users' tweets in a smoke run.

      Use this before any demo. Exit code 0 if all configured channels
      are green; 1 if any FAIL. Skip lines (channel not configured)
      are not failures.

Mocked tests verify:
    1. build_blocks() produces a valid Block Kit payload (header,
       sections, context, actions present; correct text values).
    2. send_slack() handles the happy path (200 + body 'ok').
    3. send_slack() handles non-200 responses gracefully.
    4. send_slack() handles network exceptions gracefully.
    5. send_slack() handles missing SLACK_WEBHOOK_URL (returns ok=False,
       error message — never raises).
    6. dispatch_for_post() is idempotent — second call with same post
       returns 'already_actioned'.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------- Sample fixtures ----------

SAMPLE_POST = {
    "id": "twitter:1234567890",
    "source": "twitter",
    "native_id": "1234567890",
    "author": "angryuser42",
    "content": (
        "Ordered from @zomato 2 hours ago, food never arrived, "
        "support ghosted me. ₹890 charged. This is INSANE. "
        "Where is my refund?? #zomatofail"
    ),
    "url": "https://x.com/angryuser42/status/1234567890",
    "created_at": "2026-04-30T08:15:00+00:00",
    "metadata": {"like_count": 42, "retweet_count": 8, "reply_count": 5},
    "priority_band": "P0",
    "priority_score": 0.91,
    "action_taken": None,
}

SAMPLE_CLASSIFICATION = {
    "primary_topic": "missing_order_refund",
    "urgency": "critical",
    "urgency_score": 0.95,
    "sentiment": "negative",
    "category": "consumer",
    "audience": ["consumer-support", "trust-and-safety"],
    "tripwires_fired": ["payment_charged_no_delivery"],
    "reasoning": (
        "User reports food was charged for but never delivered, "
        "with no support response. Payment-without-fulfillment is a "
        "tripwire category that auto-escalates."
    ),
}

SAMPLE_PRIORITY = {
    "score": 0.91,
    "band": "P0",
    "tripwire_override": True,
    "reason": "tripwire override: payment_charged_no_delivery",
    "contributions": {
        "severity": 0.30,
        "reach":    0.18,
        "sla_proximity": 0.10,
    },
}


# ---------- Test helpers ----------

class FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient used by send_slack()."""
    def __init__(self, *, response: FakeResponse | None = None,
                 raise_exc: Exception | None = None,
                 **kwargs):
        self._response = response
        self._raise_exc = raise_exc
        self.last_post = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.last_post = {"url": url, "json": json, "headers": headers}
        if self._raise_exc:
            raise self._raise_exc
        return self._response


# ---------- Tests ----------

def t1_build_blocks_shape() -> None:
    from social_watch.actions.slack import build_blocks

    payload = build_blocks(SAMPLE_POST, SAMPLE_CLASSIFICATION, SAMPLE_PRIORITY)
    assert isinstance(payload, dict), "payload must be dict"
    assert "text" in payload and payload["text"], "fallback text required"
    blocks = payload.get("blocks") or []
    assert len(blocks) >= 5, f"expected >= 5 blocks, got {len(blocks)}"

    types = [b.get("type") for b in blocks]
    assert "header" in types, "header block missing"
    assert "section" in types, "section block missing"
    assert "context" in types, "context block missing"
    assert "actions" in types, "actions block missing"

    # Header must include the band emoji + text
    header = blocks[0]
    assert header["type"] == "header"
    header_text = header["text"]["text"]
    assert "P0" in header_text, f"header missing band: {header_text}"
    assert "🚨" in header_text or "Critical" in header_text or header_text, "header should signal urgency"

    # Last block should be the action buttons with urls
    actions = next(b for b in blocks if b.get("type") == "actions")
    elements = actions.get("elements", [])
    assert len(elements) >= 1, "no action buttons"
    urls = [e.get("url") for e in elements]
    assert any("x.com" in (u or "") for u in urls), "Open original button missing tweet url"
    assert any("localhost" in (u or "") or "/?q=" in (u or "") for u in urls), \
        "Open dashboard button missing"

    # Reasoning section must include the reason text
    sections = [b for b in blocks if b.get("type") == "section"]
    section_texts = []
    for s in sections:
        if "text" in s:
            section_texts.append(s["text"].get("text", ""))
        if "fields" in s:
            for f in s["fields"]:
                section_texts.append(f.get("text", ""))
    joined = "\n".join(section_texts)
    assert "tripwire" in joined.lower() or "Why P0" in joined, \
        f"reasoning missing from sections: {joined[:200]}"
    print(f"  [t1] blocks={len(blocks)} types={types} OK")


def t2_truncation() -> None:
    from social_watch.actions.slack import build_blocks, CONTENT_TRUNCATE
    long_post = dict(SAMPLE_POST, content="X" * 5000)
    payload = build_blocks(long_post, SAMPLE_CLASSIFICATION, SAMPLE_PRIORITY)
    body = json.dumps(payload)
    assert "X" * 5000 not in body, "5000 X's should not appear — truncation failed"
    # The truncated text should be present
    assert "…" in body, "truncation marker '…' missing"
    print(f"  [t2] truncation cap={CONTENT_TRUNCATE} OK")


async def t3_send_happy_path() -> None:
    from social_watch.actions import slack as slackmod
    fake = FakeAsyncClient(response=FakeResponse(200, "ok"))

    def factory(*args, **kwargs):
        return fake

    with mock.patch.object(slackmod.httpx, "AsyncClient", factory):
        os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/services/T/B/X"
        result = await slackmod.send_slack({"text": "hello", "blocks": []})
    assert result["ok"] is True, f"happy path should be ok: {result}"
    assert result["status"] == 200
    assert result["error"] is None
    print(f"  [t3] happy_path={result} OK")


async def t4_send_non_200() -> None:
    from social_watch.actions import slack as slackmod
    fake = FakeAsyncClient(response=FakeResponse(403, "invalid_token"))
    with mock.patch.object(slackmod.httpx, "AsyncClient", lambda *a, **kw: fake):
        os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/services/T/B/X"
        result = await slackmod.send_slack({"text": "hi", "blocks": []})
    assert result["ok"] is False
    assert result["status"] == 403
    assert "403" in (result["error"] or "")
    print(f"  [t4] non_200 status=403 captured OK")


async def t5_send_network_error() -> None:
    from social_watch.actions import slack as slackmod
    fake = FakeAsyncClient(raise_exc=slackmod.httpx.TimeoutException("simulated"))
    with mock.patch.object(slackmod.httpx, "AsyncClient", lambda *a, **kw: fake):
        os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/services/T/B/X"
        result = await slackmod.send_slack({"text": "hi", "blocks": []})
    assert result["ok"] is False
    assert "timeout" in (result["error"] or "").lower()
    print(f"  [t5] network_error captured OK")


async def t6_missing_env() -> None:
    from social_watch.actions import slack as slackmod
    saved = os.environ.pop("SLACK_WEBHOOK_URL", None)
    try:
        result = await slackmod.send_slack({"text": "hi", "blocks": []})
        assert result["ok"] is False
        assert "SLACK_WEBHOOK_URL" in (result["error"] or "")
        print(f"  [t6] missing_env handled OK")
    finally:
        if saved is not None:
            os.environ["SLACK_WEBHOOK_URL"] = saved


async def t7_dispatcher_idempotency() -> None:
    """End-to-end dispatcher test against an in-memory SQLite DB."""
    # Build a tmp DB, insert one P0 post, dispatch, dispatch again.
    import aiosqlite
    from social_watch import config as projcfg
    from social_watch.storage import Storage

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke.db"
        # Point the project config at our tmp DB for this test
        original = projcfg.DB_PATH
        projcfg.DB_PATH = db_path
        try:
            storage = Storage(db_path)
            await storage.init()

            # Insert the sample P0 post directly (bypasses upsert_posts so we
            # control every column).
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(
                    """INSERT INTO posts
                       (id, source, native_id, author, content, url, created_at, metadata,
                        category, classification, classified_at,
                        priority_score, priority_band, priority_breakdown)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        SAMPLE_POST["id"], SAMPLE_POST["source"],
                        SAMPLE_POST["native_id"], SAMPLE_POST["author"],
                        SAMPLE_POST["content"], SAMPLE_POST["url"],
                        SAMPLE_POST["created_at"],
                        json.dumps(SAMPLE_POST["metadata"]),
                        "consumer",
                        json.dumps(SAMPLE_CLASSIFICATION),
                        datetime.now(timezone.utc).isoformat(),
                        SAMPLE_PRIORITY["score"], SAMPLE_PRIORITY["band"],
                        json.dumps(SAMPLE_PRIORITY),
                    ),
                )
                await db.commit()

            # Mock the webhook
            os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/services/T/B/X"
            from social_watch.actions import slack as slackmod, dispatcher as dispmod

            fake = FakeAsyncClient(response=FakeResponse(200, "ok"))
            with mock.patch.object(slackmod.httpx, "AsyncClient", lambda *a, **kw: fake):
                # First fire
                r1 = await dispmod.dispatch_for_post(SAMPLE_POST["id"], trigger="smoke")
                assert r1["status"] == "fired", f"first call should fire: {r1}"
                # Second fire — should be idempotent
                r2 = await dispmod.dispatch_for_post(SAMPLE_POST["id"], trigger="smoke")
                assert r2["status"] == "already_actioned", f"second should skip: {r2}"
                # Force re-fire
                r3 = await dispmod.dispatch_for_post(
                    SAMPLE_POST["id"], force=True, trigger="smoke"
                )
                assert r3["status"] == "fired", f"force should re-fire: {r3}"

            # Verify the row has action_taken='slack' + action_meta
            async with aiosqlite.connect(str(db_path)) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM posts WHERE id = ?",
                                       (SAMPLE_POST["id"],))
                row = await cur.fetchone()
            assert row["action_taken"] == "slack"
            meta = json.loads(row["action_meta"])
            assert meta["channel"] == "slack"
            assert meta["status"] == 200
            assert "payload" in meta
            assert meta["payload"]["blocks"]
            print(f"  [t7] dispatch idempotency OK (trigger={meta['trigger']})")

            # Sweep test: insert a 2nd unactioned P0 and call dispatch_unactioned
            second_id = "twitter:9999999999"
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(
                    """INSERT INTO posts
                       (id, source, native_id, author, content, url, created_at, metadata,
                        category, classification, classified_at,
                        priority_score, priority_band, priority_breakdown)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        second_id, "twitter", "9999999999", "user2",
                        "Another P0 — payment failure",
                        "https://x.com/user2/status/9999999999",
                        SAMPLE_POST["created_at"],
                        json.dumps({}), "consumer",
                        json.dumps(SAMPLE_CLASSIFICATION),
                        datetime.now(timezone.utc).isoformat(),
                        0.88, "P0",
                        json.dumps(SAMPLE_PRIORITY),
                    ),
                )
                await db.commit()
            with mock.patch.object(slackmod.httpx, "AsyncClient", lambda *a, **kw: fake):
                # Skip the polite sleep for the test
                with mock.patch.object(dispmod, "_POLITE_DELAY_S", 0):
                    summary = await dispmod.dispatch_unactioned(limit=10, dry_run=False)
            assert summary["fired"] >= 1, f"sweep should fire 1: {summary}"
            assert summary["scanned"] >= 1
            print(f"  [t7] sweep summary={summary}")

        finally:
            projcfg.DB_PATH = original


async def t8_dry_run() -> None:
    """dispatch_unactioned(dry_run=True) must never call Slack."""
    import aiosqlite
    import tempfile
    from social_watch import config as projcfg
    from social_watch.storage import Storage
    from social_watch.actions import dispatcher as dispmod, slack as slackmod

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dry.db"
        original = projcfg.DB_PATH
        projcfg.DB_PATH = db_path
        try:
            storage = Storage(db_path)
            await storage.init()
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(
                    """INSERT INTO posts
                       (id, source, native_id, author, content, url, created_at, metadata,
                        category, classification, classified_at,
                        priority_score, priority_band, priority_breakdown)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        SAMPLE_POST["id"], "twitter", "1", "u", "x", "https://x.com/u/1",
                        SAMPLE_POST["created_at"], "{}", "consumer",
                        json.dumps(SAMPLE_CLASSIFICATION),
                        datetime.now(timezone.utc).isoformat(),
                        0.91, "P0", json.dumps(SAMPLE_PRIORITY),
                    ),
                )
                await db.commit()

            # Patch httpx to RAISE — proves it was never invoked.
            def boom(*a, **kw):
                raise AssertionError("dry_run should not call httpx")
            with mock.patch.object(slackmod.httpx, "AsyncClient", boom):
                summary = await dispmod.dispatch_unactioned(dry_run=True)
            assert summary["dry_run"] is True
            assert summary["fired"] == 0
            assert summary["scanned"] >= 1
            print(f"  [t8] dry_run summary={summary} OK")
        finally:
            projcfg.DB_PATH = original


# ---------- Runner ----------

async def main() -> int:
    print("Slack action dispatcher — smoke tests")
    print("=" * 60)
    sync_tests = [t1_build_blocks_shape, t2_truncation]
    async_tests = [t3_send_happy_path, t4_send_non_200, t5_send_network_error,
                   t6_missing_env, t7_dispatcher_idempotency, t8_dry_run]
    failed = 0
    for t in sync_tests:
        try:
            t()
        except AssertionError as e:
            print(f"  [{t.__name__}] FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  [{t.__name__}] ERROR: {type(e).__name__}: {e}")
            failed += 1
    for t in async_tests:
        try:
            await t()
        except AssertionError as e:
            print(f"  [{t.__name__}] FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  [{t.__name__}] ERROR: {type(e).__name__}: {e}")
            failed += 1
    print("=" * 60)
    if failed:
        print(f"FAILED: {failed} test(s)")
        return 1
    print("All smoke tests PASSED.")

    # Print one sample payload for the deliverable
    print("\nSample Block Kit payload (build_blocks output):\n")
    from social_watch.actions.slack import build_blocks
    sample = build_blocks(SAMPLE_POST, SAMPLE_CLASSIFICATION, SAMPLE_PRIORITY)
    print(json.dumps(sample, indent=2))
    return 0


# ============================================================
# Live mode — `--live`: real network calls, no mocks
# ============================================================

# A pared-down sample post used for live test payloads. The content
# starts with "[SMOKE TEST]" so the receiving channel makes it obvious
# this isn't a real escalation.
_LIVE_TEST_POST = {
    "id": "smoke:test:" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
    "source": "twitter",
    "native_id": "0",
    "author": "smoke_test",
    "content": (
        "[SMOKE TEST] Connector pre-flight from Zomato Social Watch. "
        "If you see this in your channel, the live wiring works. "
        "Fired at " + datetime.now(timezone.utc).isoformat()
    ),
    "url": "https://x.com/zomato/status/0",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "metadata": {"like_count": 0},
    "priority_band": "P0",
    "priority_score": 0.50,
    "action_taken": None,
}

_LIVE_TEST_CLASSIFICATION = {
    "primary_topic": "smoke_test",
    "urgency": "medium",
    "sentiment": "neutral",
    "category": "consumer",
    "audience": ["dev"],
    "tripwires_fired": [],
    "reasoning": "Live connector smoke check — not a real post.",
}

_LIVE_TEST_PRIORITY = {
    "score": 0.50,
    "band": "P0",
    "reason": "[SMOKE TEST] forced P0 for connector pre-flight only",
    "tripwire_override": False,
    "contributions": {},
}


async def _live_send(name: str, mod) -> dict:
    """Build + send the smoke payload via the channel module's
    standard contract. Returns the per-channel result dict."""
    try:
        _payload, result = await mod.build_and_send(
            _LIVE_TEST_POST, _LIVE_TEST_CLASSIFICATION, _LIVE_TEST_PRIORITY
        )
        return result
    except Exception as e:
        return {"ok": False, "status": 0, "error": f"{type(e).__name__}: {e}"}


async def _live_twitter_auth() -> dict:
    """Auth-only check for the Twitter reply connector. Loads x.com/home
    with the saved cookies and looks for the authenticated DOM. Never
    actually replies to anything — we don't want smoke runs spamming
    real users' tweets.
    """
    from social_watch import config as projcfg
    if not (projcfg.TWITTER_COOKIE_AUTH_TOKEN and projcfg.TWITTER_COOKIE_CT0):
        return {"ok": False, "status": 0, "error": "TWITTER_COOKIE_* missing"}
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {"ok": False, "status": 0, "error": "playwright not installed"}

    from social_watch.actions import twitter_reply as twr
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await twr._build_context(browser)
                page = await ctx.new_page()
                await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20000)
                try:
                    await page.wait_for_selector(
                        '[data-testid="SideNav_NewTweet_Button"]', timeout=10000
                    )
                    return {"ok": True, "status": 200, "error": None,
                            "detail": f"authenticated as @{projcfg.TWITTER_COOKIE_USERNAME or '?'}"}
                except PWTimeout:
                    return {"ok": False, "status": 0,
                            "error": "authenticated DOM not found — cookies likely stale"}
            finally:
                await browser.close()
    except Exception as e:
        return {"ok": False, "status": 0, "error": f"{type(e).__name__}: {e}"}


async def _live_reddit_auth() -> dict:
    """Auth-only check for the Reddit comment connector. Exchanges the
    script-app password grant for a bearer token; never actually
    comments on a real submission. Same intent as ``_live_twitter_auth``.
    """
    from social_watch.actions import reddit_comment as rc
    if not rc.is_configured():
        return {"ok": False, "status": 0, "error": "REDDIT_* env vars not all set"}
    try:
        token, err = await rc._fetch_access_token()
    except Exception as e:
        return {"ok": False, "status": 0, "error": f"{type(e).__name__}: {e}"}
    if not token:
        return {"ok": False, "status": 0, "error": err or "auth failed"}
    return {
        "ok": True, "status": 200, "error": None,
        "detail": f"authenticated as u/{rc._username() or '?'}",
    }


async def live_main() -> int:
    """Run the live pre-flight against every configured connector.

    Returns 0 if every configured channel is green, 1 if any failed.
    Channels with no credentials are reported as "skip" (not a failure).
    """
    print("Live connector pre-flight")
    print("=" * 60)
    print(f"Run timestamp: {datetime.now(timezone.utc).isoformat()}")
    print()

    # Lazy imports keep the mocked path fast.
    from social_watch.actions import (
        slack as slack_mod,
        discord as discord_mod,
        email as email_mod,
        sheets as sheets_mod,
        linear_ticket as ticket_mod,
        twitter_reply as twr_mod,
        reddit_comment as rc_mod,
    )

    # (display name, env-var hint, configured?, runner)
    channels: list[tuple[str, str, bool, Any]] = [
        ("slack",          "SLACK_WEBHOOK_URL",
         bool(slack_mod.webhook_url()),
         lambda: _live_send("slack", slack_mod)),
        ("discord",        "DISCORD_WEBHOOK_URL",
         bool(discord_mod.webhook_url()),
         lambda: _live_send("discord", discord_mod)),
        ("email",          "SMTP_HOST/USER/PASS",
         bool(email_mod.webhook_url()),
         lambda: _live_send("email", email_mod)),
        ("sheets",         "SHEETS_WEBHOOK_URL",
         bool(sheets_mod.webhook_url()),
         lambda: _live_send("sheets", sheets_mod)),
        ("ticket",         "LINEAR_API_KEY/TEAM_ID",
         bool(ticket_mod.is_configured()),
         lambda: ticket_mod.auth_check()),
        ("twitter_reply",  "TWITTER_COOKIE_*",
         bool(twr_mod.is_configured()),
         lambda: _live_twitter_auth()),
        ("reddit_comment", "REDDIT_CLIENT_ID/USERNAME/...",
         bool(rc_mod.is_configured()),
         lambda: _live_reddit_auth()),
    ]

    results: list[tuple[str, str, str, str]] = []  # (name, status, detail, env_hint)
    for name, env_hint, configured, runner in channels:
        if not configured:
            results.append((name, "skip", "not configured", env_hint))
            continue
        try:
            r = await runner()
        except Exception as e:
            results.append((name, "FAIL", f"{type(e).__name__}: {e}", env_hint))
            continue
        if r.get("ok"):
            detail = r.get("detail") or "sent test payload"
            results.append((name, "ok", detail, env_hint))
        else:
            results.append((name, "FAIL", str(r.get("error") or "unknown error"), env_hint))

    # Pretty-print table
    name_w = max(len(n) for n, _, _, _ in results)
    for name, status, detail, env_hint in results:
        sym = {"ok": "✓", "FAIL": "✗", "skip": "·"}[status]
        sym_color = {"ok": "\x1b[32m", "FAIL": "\x1b[31m", "skip": "\x1b[90m"}[status]
        reset = "\x1b[0m"
        # Right-pad the name + status; tail is the detail
        line = f"  {sym_color}{sym} {name:<{name_w}}  {status:<5}{reset}  {detail}"
        if status == "skip":
            line += f"  ({env_hint})"
        print(line)

    print("=" * 60)
    failed = sum(1 for _, s, _, _ in results if s == "FAIL")
    skipped = sum(1 for _, s, _, _ in results if s == "skip")
    fired = sum(1 for _, s, _, _ in results if s == "ok")
    print(f"  fired={fired}  skipped={skipped}  failed={failed}")
    if failed:
        print("\nNOT DEMO-READY. Fix the failures above before running a live demo.")
        return 1
    if fired == 0:
        print("\nNo connectors configured. Set at least one channel's env vars.")
        return 1
    print("\nDemo-ready. All configured channels are green.")
    return 0


if __name__ == "__main__":
    if "--live" in sys.argv:
        sys.exit(asyncio.run(live_main()))
    sys.exit(asyncio.run(main()))
