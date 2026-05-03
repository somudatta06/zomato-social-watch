"""CLI entrypoint for the Zomato Social Watch scraper.

Usage:
  python main.py once                       # one fetch cycle, print summary
  python main.py watch                      # continuous loop (5-min default)
  python main.py watch --interval 180
  python main.py once --twitter twscrape    # force Twitter path
  python main.py once --twitter nitter
  python main.py health                     # connectivity probes
  python main.py list --limit 30            # show recent posts from DB
  python main.py list --source reddit
  python main.py stats                      # DB summary + recent runs
  python main.py actions                    # dry-run: show what WOULD fire
  python main.py actions --auto             # actually send Slack for P0 posts
  python main.py actions --post-id reddit:abc123 --force
  python main.py velocity                   # snapshot + score velocity (Phase δ)
  python main.py clusters                   # detect active clusters (Phase δ)
  python main.py handles refresh            # rebuild author tier cache (Phase γ)
  python main.py handles list --limit 30    # top handles by reach multiplier
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from social_watch import config
from social_watch.log import setup_logging
from social_watch.orchestrator import run_cycle, run_watch
from social_watch.scrapers import NitterScraper, RedditScraper, TwitterScraper
from social_watch.storage import Storage


async def cmd_once(storage: Storage, args: argparse.Namespace) -> int:
    results = await run_cycle(storage, twitter_via=args.twitter)
    print(
        json.dumps(
            [
                {
                    "scraper": r.scraper,
                    "seen": r.posts_seen,
                    "new": r.posts_new,
                    "errors": r.errors,
                    "duration_s": round(r.duration_s, 2),
                }
                for r in results
            ],
            indent=2,
        )
    )
    return 0 if all(not r.errors for r in results) else 1


async def cmd_watch(storage: Storage, args: argparse.Namespace) -> int:
    await run_watch(storage, interval=args.interval, twitter_via=args.twitter)
    return 0


async def cmd_health(storage: Storage, args: argparse.Namespace) -> int:
    scrapers = [RedditScraper(storage), TwitterScraper(storage), NitterScraper(storage)]
    all_ok = True
    for s in scrapers:
        ok = await s.health_check()
        all_ok = all_ok and ok
        status = "OK  " if ok else "FAIL"
        logger.info(f"  [{status}] {s.name}")
    return 0 if all_ok else 1


async def cmd_list(storage: Storage, args: argparse.Namespace) -> int:
    posts = await storage.recent_posts(limit=args.limit, source=args.source)
    if not posts:
        print("(no posts in DB yet — run `python main.py once` first)", file=sys.stderr)
        return 0
    for p in posts:
        head = f"[{p['source']}] {p['created_at']}"
        if p.get("author"):
            head += f" @{p['author']}"
        snippet = (p["content"] or "").replace("\n", " ")[:140]
        print(f"{head}")
        print(f"  {snippet}")
        print(f"  -> {p['url']}")
        print()
    return 0


async def cmd_stats(storage: Storage, args: argparse.Namespace) -> int:
    print(json.dumps(await storage.stats(), indent=2, default=str))
    return 0


async def cmd_responses(storage: Storage, args: argparse.Namespace) -> int:
    """Run reply-detection: Reddit comment scan + Twitter @zomatocare timeline scrape."""
    from social_watch.responses import check_all_responses
    result = await check_all_responses()
    print(json.dumps(result, indent=2, default=str))
    return 0


async def cmd_cleanup(storage: Storage, args: argparse.Namespace) -> int:
    """Re-evaluate every post in the DB against the current relevance filter
    and (optionally) delete posts that no longer pass.

    --dry-run prints what would be deleted without touching the DB.
    """
    import aiosqlite
    from social_watch import config as cfg
    from social_watch.scrapers.reddit import _is_zomato_relevant

    rejected: list[tuple[str, str, str, str]] = []  # (id, source, title_or_preview, reason)
    kept = 0
    async with aiosqlite.connect(str(cfg.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, source, content, url FROM posts ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        for r in rows:
            content = r["content"] or ""
            # For Reddit, "content" is "title\n\nbody"; split for proper title check.
            if r["source"] == "reddit" and "\n\n" in content:
                title, _, body = content.partition("\n\n")
            else:
                title, body = content[:200], content
            ok, reason = _is_zomato_relevant(title, body)
            if ok:
                kept += 1
            else:
                preview = (content or "").replace("\n", " ")[:100]
                rejected.append((r["id"], r["source"], preview, reason))

        print(f"\nKept:     {kept}")
        print(f"Rejected: {len(rejected)}")
        if rejected:
            print("\nSample of rejected posts (first 12):")
            for pid, src, prev, reason in rejected[:12]:
                print(f"  [{src}] {pid}")
                print(f"     reason: {reason}")
                print(f"     {prev!r}")

        if args.dry_run:
            print("\n(dry-run — no changes written. Run without --dry-run to delete.)")
            return 0

        if not rejected:
            print("Nothing to delete.")
            return 0

        ids = [r[0] for r in rejected]
        # Batched delete
        await db.executemany("DELETE FROM posts WHERE id = ?", [(i,) for i in ids])
        await db.commit()
        print(f"\nDeleted {len(ids)} irrelevant posts.")
    return 0


async def cmd_classify(storage: Storage, args: argparse.Namespace) -> int:
    """Classify posts: rules-first, then LLM upgrade if Gemini key set."""
    from social_watch.classifier import classify_backlog, is_llm_available
    if args.rules_only or not is_llm_available():
        if args.rules_only:
            logger.info("Classifier: rules-only mode (--rules-only)")
        else:
            logger.info("Classifier: rules-only (no GEMINI_API_KEY in .env)")
    else:
        logger.info("Classifier: rules + Gemini LLM")
    result = await classify_backlog(force=args.force, llm_enabled=not args.rules_only)
    print(json.dumps(result, indent=2))
    return 0


async def cmd_actions(storage: Storage, args: argparse.Namespace) -> int:
    """Fire Slack + Discord for P0 posts. Default is DRY-RUN (counts only);
    pass --auto to actually send. Pass --post-id to target a single post
    (--force overrides idempotency)."""
    from social_watch.actions import dispatch_for_post, dispatch_unactioned

    if args.post_id:
        result = await dispatch_for_post(args.post_id, force=args.force, trigger="cli")
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    dry = not args.auto
    if dry:
        logger.info("Actions: DRY-RUN — no webhook calls will be made. Pass --auto to fire.")
    else:
        logger.info("Actions: AUTO — firing configured webhooks for unactioned P0 posts.")
    summary = await dispatch_unactioned(limit=args.limit, dry_run=dry)
    print(json.dumps(summary, indent=2, default=str))
    return 0


async def cmd_test_actions(storage: Storage, args: argparse.Namespace) -> int:
    """Smoke-test webhooks: fire ONE synthetic P0 message to each configured
    channel. Use this right after pasting webhook URLs into .env to confirm
    they work — no waiting for a real P0 post.

        $ uv run python main.py test-actions

    Reports a per-channel pass/fail. Exits 0 only if every configured
    channel returned ok.
    """
    from social_watch.actions import slack as slack_mod
    from social_watch.actions import discord as discord_mod
    from social_watch.actions import email as email_mod
    from social_watch.actions import sheets as sheets_mod

    # Synthetic post that exercises the same code path as real P0 fires.
    sample_post = {
        "id": "test:smoke-001",
        "source": "twitter",
        "native_id": "smoke-001",
        "author": "smoke_test_user",
        "content": (
            "[SMOKE TEST] Zomato Social Watch action dispatcher check. "
            "If you can read this, the webhook is correctly wired. "
            "This is not a real escalation."
        ),
        "url": "https://x.com/smoke_test_user/status/smoke-001",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {},
        "priority_band": "P0",
        "priority_score": 0.99,
    }
    sample_cls = {
        "primary_topic": "smoke_test",
        "category": "consumer",
        "sentiment": "negative",
        "audience": ["customer-care", "trust-safety"],
        "tripwires_fired": [],
        "reasoning": "Synthetic smoke test to verify webhook wiring.",
    }
    sample_pri = {
        "score": 0.99,
        "band": "P0",
        "tripwire_override": False,
        "reason": "smoke test — synthetic P0",
        "contributions": {},
    }

    targets: list[tuple[str, Any, str | None, str]] = [
        ("slack",   slack_mod,   slack_mod.webhook_url(),   slack_mod.SLACK_WEBHOOK_ENV),
        ("discord", discord_mod, discord_mod.webhook_url(), discord_mod.DISCORD_WEBHOOK_ENV),
        ("email",   email_mod,   email_mod.webhook_url(),   email_mod.SMTP_WEBHOOK_NAME_HINT),
        ("sheets",  sheets_mod,  sheets_mod.webhook_url(),  sheets_mod.SHEETS_WEBHOOK_ENV),
    ]
    any_configured = False
    all_ok = True
    print()
    for name, mod, url, env_key in targets:
        if not url:
            print(f"  [{name:<7}] SKIP   — {env_key} is not set")
            continue
        any_configured = True
        try:
            payload, result = await mod.build_and_send(sample_post, sample_cls, sample_pri)
        except Exception as e:
            print(f"  [{name:<7}] ERROR  — {type(e).__name__}: {e}")
            all_ok = False
            continue
        if result.get("ok"):
            print(f"  [{name:<7}] OK     — status {result.get('status')}")
        else:
            print(f"  [{name:<7}] FAILED — {result.get('error')}")
            all_ok = False
    print()

    if not any_configured:
        print("No action channels configured. Set at least one of:")
        print("  SLACK_WEBHOOK_URL   (https://api.slack.com/messaging/webhooks)")
        print("  DISCORD_WEBHOOK_URL (channel → Edit → Integrations → Webhooks)")
        print("  SMTP_HOST/USER/PASS/EMAIL_TO (Gmail app password works)")
        print("  SHEETS_WEBHOOK_URL  (Google Apps Script web app — see actions/sheets.py)")
        return 1
    return 0 if all_ok else 1


async def cmd_velocity(storage: Storage, args: argparse.Namespace) -> int:
    """Phase δ: snapshot engagement counts for hot Twitter posts and
    re-derive the velocity signal in priority. Two stages:

      1. take_snapshots(budget=30) → Playwright fetches like/retweet/reply
         counts for top-N hottest posts <24h old, persists to
         engagement_snapshots.
      2. attach_velocity_to_classifications() → for posts with ≥2
         snapshots, splice the velocity score into priority_breakdown
         and recompute priority_score / priority_band.
    """
    from social_watch.velocity import (
        attach_velocity_to_classifications,
        take_snapshots,
    )
    snap = await take_snapshots(budget=args.budget)
    attach = await attach_velocity_to_classifications()
    print(json.dumps({"snapshots": snap, "attach": attach}, indent=2, default=str))
    return 0


async def cmd_handles(storage: Storage, args: argparse.Namespace) -> int:
    """Phase γ — author tier system management.

    Subcommands:
      refresh   Walk every unique author in `posts`, recompute their tier,
                and persist to the `handles` table. Idempotent.
      list      Show top-N handles sorted by reach multiplier.
    """
    from social_watch import handles as handles_mod
    from social_watch import config as cfg
    import aiosqlite

    sub = getattr(args, "handles_cmd", None)

    if sub == "refresh":
        logger.info("Refreshing handles cache from posts table...")
        result = await handles_mod.refresh_all_handles(str(cfg.DB_PATH))
        print(json.dumps(result, indent=2, default=str))
        return 0

    if sub == "list":
        async with aiosqlite.connect(str(cfg.DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT handle, source, tier, profile_class, multiplier,
                       total_posts, prior_complaints, watchlists
                FROM handles
                ORDER BY multiplier DESC, total_posts DESC
                LIMIT ?
                """,
                (args.limit,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            print(
                "(no handles yet — run `python main.py handles refresh` first)",
                file=sys.stderr,
            )
            return 0
        for r in rows:
            wl = r["watchlists"] or "[]"
            print(
                f"  {r['tier']:>3}  {r['profile_class']:<14} "
                f"{r['multiplier']:>4.1f}x  @{r['handle']:<28} "
                f"({r['source']}, {r['total_posts']} posts, "
                f"{r['prior_complaints']} complaints, watchlists={wl})"
            )
        return 0

    print(
        "usage: python main.py handles {refresh,list}",
        file=sys.stderr,
    )
    return 2


async def cmd_clusters(storage: Storage, args: argparse.Namespace) -> int:
    """Phase δ: detect post-batch clusters (5+ posts in 60min on same
    side+topic+geography). Recognizes ops_outage / coordinated_attack /
    restaurant_event patterns. Idempotent — safe to re-run."""
    from social_watch.clusters import detect_clusters, list_active
    detect = await detect_clusters()
    active = await list_active(limit=20)
    print(json.dumps({"detect": detect, "active": active}, indent=2, default=str))
    return 0


async def cmd_serve(storage: Storage, args: argparse.Namespace) -> int:
    """Launch the FastAPI dashboard with optional background scrape loop."""
    import os
    import uvicorn

    if args.no_watch:
        os.environ["SOCIAL_WATCH_AUTO_SYNC"] = "0"
        logger.info("Dashboard auto-sync DISABLED (--no-watch)")
    else:
        os.environ["SOCIAL_WATCH_AUTO_SYNC"] = "1"

    logger.info(f"Dashboard starting at http://{args.host}:{args.port}")
    cfg = uvicorn.Config(
        "social_watch.web.server:app",
        host=args.host,
        port=args.port,
        log_level="info",
        reload=args.reload,
    )
    server = uvicorn.Server(cfg)
    await server.serve()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="social-watch", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    once = sub.add_parser("once", help="run a single fetch cycle")
    once.add_argument(
        "--twitter",
        choices=["auto", "twscrape", "nitter"],
        default="auto",
        help="Twitter source strategy (default: auto = twscrape if accounts set, else nitter)",
    )

    watch = sub.add_parser("watch", help="continuous fetch loop")
    watch.add_argument("--interval", type=int, default=config.REFRESH_INTERVAL)
    watch.add_argument(
        "--twitter", choices=["auto", "twscrape", "nitter"], default="auto"
    )

    sub.add_parser("health", help="probe each scraper's connectivity / auth")

    listc = sub.add_parser("list", help="show recent posts from DB")
    listc.add_argument("--limit", type=int, default=20)
    listc.add_argument("--source", choices=["reddit", "twitter"], default=None)

    sub.add_parser("stats", help="DB summary + recent runs")

    classify = sub.add_parser(
        "classify",
        help="classify posts in DB (rules-first, LLM upgrade if Gemini key set)",
    )
    classify.add_argument("--force", action="store_true", help="re-classify already-classified posts")
    classify.add_argument("--rules-only", action="store_true", help="skip LLM stage even if key is set")

    cleanup = sub.add_parser(
        "cleanup",
        help="re-evaluate posts against the current relevance filter and delete irrelevant ones",
    )
    cleanup.add_argument("--dry-run", action="store_true", help="show what would be deleted without writing")

    sub.add_parser(
        "responses",
        help="check whether Zomato has already replied to scraped posts (Reddit + Twitter)",
    )

    actions = sub.add_parser(
        "actions",
        help="fire Slack + Discord webhooks for P0 posts (default: dry-run; pass --auto to actually send)",
    )
    actions.add_argument("--auto", action="store_true", help="actually send webhook messages (default: dry-run)")
    actions.add_argument("--post-id", default=None, help="target a single post by id (e.g. reddit:abc123)")
    actions.add_argument("--limit", type=int, default=20, help="cap on posts to dispatch in one sweep")
    actions.add_argument("--force", action="store_true", help="(with --post-id) re-fire even if already actioned")

    sub.add_parser(
        "test-actions",
        help="smoke-test configured webhooks (Slack + Discord) — fires one synthetic P0 message to each",
    )

    velocity = sub.add_parser(
        "velocity",
        help="snapshot engagement counts and recompute the velocity signal "
             "for the top-N hottest Twitter posts <24h old",
    )
    velocity.add_argument(
        "--budget", type=int, default=30,
        help="max number of posts to snapshot this run (default 30)",
    )

    sub.add_parser(
        "clusters",
        help="detect crisis/viral clusters in the last 60 minutes",
    )

    # Phase γ — author tier / watchlists
    handles = sub.add_parser(
        "handles",
        help="manage the author tier cache (refresh / list)",
    )
    handles_sub = handles.add_subparsers(dest="handles_cmd", required=True)
    handles_sub.add_parser(
        "refresh",
        help="walk all unique authors in `posts` and rebuild the handles table",
    )
    handles_list = handles_sub.add_parser(
        "list",
        help="show handles sorted by reach multiplier (highest first)",
    )
    handles_list.add_argument("--limit", type=int, default=30)

    serve = sub.add_parser("serve", help="launch the localhost dashboard (auto-syncs every 5 min)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--no-watch", action="store_true", help="disable background scrape+classify loop")

    return p


async def _main() -> int:
    args = build_parser().parse_args()
    setup_logging()
    storage = Storage(config.DB_PATH)
    await storage.init()
    handlers = {
        "once": cmd_once,
        "watch": cmd_watch,
        "health": cmd_health,
        "list": cmd_list,
        "stats": cmd_stats,
        "classify": cmd_classify,
        "cleanup": cmd_cleanup,
        "responses": cmd_responses,
        "actions": cmd_actions,
        "test-actions": cmd_test_actions,
        "velocity": cmd_velocity,
        "clusters": cmd_clusters,
        "handles": cmd_handles,
        "serve": cmd_serve,
    }
    return await handlers[args.cmd](storage, args)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
