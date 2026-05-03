"""FastAPI dashboard server.

Read-only, single-page dashboard rendered server-side via Jinja2.
Mirrors the Pulse design: left sidebar (counts + sections), top bar,
filter chips, sortable table. No JS framework — Tailwind via CDN, Lucide
icons via CDN, optional HTMX for partial updates later.

Run with:
  .venv/bin/uvicorn social_watch.web.server:app --reload --port 8000
or:
  python main.py serve --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiosqlite
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from .. import config

WEB_ROOT = Path(__file__).resolve().parent

# Direct Jinja2 (Starlette's Jinja2Templates wrapper has a Python 3.14
# cache-key hashing bug with non-trivial context dicts).
_jinja = Environment(
    loader=FileSystemLoader(str(WEB_ROOT / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=400,
    enable_async=False,
)

# ---------- background sync state ----------
# Module-level so the index route can show "Syncing now…" / last-sync time.
_sync_state: dict[str, Any] = {
    "is_running": False,
    "last_started": None,    # datetime | None
    "last_finished": None,   # datetime | None
    "last_error": None,      # str | None
    "cycle_count": 0,
}
_AUTO_SYNC = os.getenv("SOCIAL_WATCH_AUTO_SYNC", "1") != "0"


async def _background_sync_loop() -> None:
    """Continuously scrape + pre-classify on REFRESH_INTERVAL seconds.
    Each cycle:
      1. orchestrator.run_cycle  → fetch new posts (Reddit + Twitter)
      2. preclassifier.preclassify_all(force=False) → tag NEW posts only
    Errors are logged but don't kill the loop — one bad cycle shouldn't
    take the dashboard offline.
    """
    # Late imports so the server starts fast even if a scraper has heavy deps.
    from .. import clusters, themes, velocity
    from ..actions import dispatch_unactioned
    from ..orchestrator import run_cycle
    from ..preclassifier import preclassify_all
    from ..responses import check_all_responses
    from ..storage import Storage

    storage = Storage(config.DB_PATH)
    await storage.init()

    # Initial wait short — first sync runs ~3s after server boot so the
    # dashboard is responsive immediately and cookies/etc. have a moment to load.
    await asyncio.sleep(3)

    while True:
        _sync_state["is_running"] = True
        _sync_state["last_started"] = datetime.now(timezone.utc)
        _sync_state["cycle_count"] += 1
        logger.info(f"[bg-sync] cycle {_sync_state['cycle_count']} start")
        try:
            await run_cycle(storage)
            await preclassify_all(force=False)
            # Response detection runs after classification — non-blocking
            # in the sense that errors don't kill the loop. Twitter detector
            # only runs every Nth cycle to keep cost low (Playwright is heavy).
            try:
                if _sync_state["cycle_count"] % 1 == 0:  # every cycle for now
                    await check_all_responses()
            except Exception:
                logger.exception("[bg-sync] response check failed")
            # Phase δ: engagement velocity + cluster detection.
            # Snapshots top-30 hot posts → re-derives velocity score →
            # splices it into priority breakdown → recomputes priority.
            # Then groups recent posts into clusters for crisis detection.
            # Done BEFORE dispatch so the action dispatcher sees fresh
            # priority scores and any newly-formed clusters.
            try:
                await velocity.take_snapshots(budget=30)
                await velocity.attach_velocity_to_classifications()
                # Theme-based clustering (the operator's "what are people saying?"
                # question). Buckets by content pattern, not geography.
                await themes.detect_themes(window_hours=24, min_group=3)
                # Geographic burst detection still runs for true ops-outage
                # signals (30+ posts, same city, 30 min) — different cluster_type.
                await clusters.detect_clusters()
            except Exception:
                logger.exception("[bg-sync] velocity/themes/clusters failed")
            # Phase 3: auto-fire Slack for unactioned P0 posts. Wrapped in
            # try/except so a Slack outage or bad webhook can't take the
            # background loop offline.
            try:
                dispatch_summary = await dispatch_unactioned(limit=20, dry_run=False)
                if dispatch_summary.get("fired") or dispatch_summary.get("failed"):
                    logger.info(
                        f"[bg-sync] dispatch: scanned={dispatch_summary['scanned']} "
                        f"fired={dispatch_summary['fired']} "
                        f"failed={dispatch_summary['failed']} "
                        f"skipped={dispatch_summary['skipped']}"
                    )
            except Exception:
                logger.exception("[bg-sync] dispatch failed")

            # Phase κ: SLA sweep — re-fire when ack deadline has passed
            # without an operator click. Idempotent.
            try:
                from .. import lifecycle
                sla_summary = await lifecycle.sla_sweep()
                if sla_summary.get("escalated"):
                    logger.warning(
                        f"[bg-sync] sla-sweep: scanned={sla_summary['scanned']} "
                        f"escalated={sla_summary['escalated']} "
                        f"skipped={sla_summary['skipped']}"
                    )
            except Exception:
                logger.exception("[bg-sync] sla-sweep failed")

            # Phase λ: post-incident review sweep — opens a Linear
            # review sub-issue 24h after the original action fired,
            # for hard-tripwire incidents only. Idempotent.
            try:
                from .. import lifecycle
                review_summary = await lifecycle.review_sweep()
                if review_summary.get("created"):
                    logger.info(
                        f"[bg-sync] review-sweep: scanned={review_summary['scanned']} "
                        f"created={review_summary['created']} "
                        f"skipped={review_summary['skipped']}"
                    )
            except Exception:
                logger.exception("[bg-sync] review-sweep failed")

            # Phase μ: auto-reply policy — fire templated reply on
            # safe, classified, customer-care/ops Twitter posts.
            # Off by default (AUTO_REPLY_ENABLED=0); the sweep itself
            # short-circuits when disabled so this is cheap.
            try:
                from .. import auto_reply
                ar_summary = await auto_reply.sweep()
                if ar_summary.get("enabled"):
                    logger.info(
                        f"[bg-sync] auto-reply: scanned={ar_summary['scanned']} "
                        f"fired={ar_summary['fired']} "
                        f"skipped={ar_summary['skipped']} "
                        f"failed={ar_summary['failed']}"
                        f"{' (DRY)' if ar_summary.get('dry_run') else ''}"
                    )
            except Exception:
                logger.exception("[bg-sync] auto-reply failed")
            _sync_state["last_finished"] = datetime.now(timezone.utc)
            _sync_state["last_error"] = None
            logger.info(f"[bg-sync] cycle {_sync_state['cycle_count']} done")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _sync_state["last_error"] = f"{type(e).__name__}: {e}"
            logger.exception("[bg-sync] cycle failed")
        finally:
            _sync_state["is_running"] = False
        try:
            await asyncio.sleep(config.REFRESH_INTERVAL)
        except asyncio.CancelledError:
            raise


def _make_link_fn(current: dict[str, Any], base: str = "/inbox"):
    """Returns a Jinja-callable that merges current filters with overrides
    and produces a {base}?... URL. Used in the template as {{ link(side='consumer') }}.

    `base` defaults to /inbox since the dense feed lives there now;
    pulse.html doesn't use the link helper so it doesn't matter for that view.
    """
    def link(**overrides: Any) -> str:
        merged = {**current, **overrides}
        # When a filter changes (anything other than `page`), reset to page 1
        if any(k != "page" for k in overrides):
            merged["page"] = 1
        params: dict[str, str] = {}
        for k, v in merged.items():
            if v in (None, "", False):
                continue
            params[k] = "1" if v is True else str(v)
        return (base + "?" + urlencode(params)) if params else base
    return link


def _pagination_pages(current: int, total: int, max_visible: int = 7) -> list[int | None]:
    """Returns Gmail-style page list with elision (None marks an ellipsis).
       <=7 pages : [1, 2, 3, 4, 5, 6, 7]
       Near start: [1, 2, 3, 4, 5, None, 12]
       Near end  : [1, None, 8, 9, 10, 11, 12]
       Middle    : [1, None, 5, 6, 7, None, 12]
    """
    if total <= 0:
        return []
    if total <= max_visible:
        return list(range(1, total + 1))
    pages: list[int | None] = [1]
    left = max(2, current - 2)
    right = min(total - 1, current + 2)
    if left > 2:
        pages.append(None)
    pages.extend(range(left, right + 1))
    if right < total - 1:
        pages.append(None)
    pages.append(total)
    return pages


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("SUPABASE_RESTORE_ON_BOOT") == "1":
        from ..storage import Storage
        from ..supabase_mirror import restore_to_sqlite
        storage = Storage(config.DB_PATH)
        await storage.init()
        await restore_to_sqlite(config.DB_PATH)

    sync_task: asyncio.Task | None = None
    if _AUTO_SYNC:
        logger.info(
            f"[bg-sync] auto-sync ENABLED — every {config.REFRESH_INTERVAL}s. "
            f"Set SOCIAL_WATCH_AUTO_SYNC=0 (or `serve --no-watch`) to disable."
        )
        sync_task = asyncio.create_task(_background_sync_loop())
    else:
        logger.info("[bg-sync] auto-sync disabled")
    try:
        yield
    finally:
        if sync_task is not None:
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    lifespan=lifespan,
    title="Zomato Social Watch",
    docs_url="/api/docs",
)
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")


# ---------- helpers ----------

def _shorten_url(url: str | None) -> str:
    """Compact URL for the permalink chip: <host>/<first-seg>/.../<last-tail>.
    Example:
      https://x.com/Vivek/status/2049465210180960480
        → x.com/Vivek/...0480
      https://reddit.com/r/india/comments/1symzn7/some_title/
        → reddit.com/r/india/...mzn7
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        host = u.netloc.replace("www.", "")
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            return host
        first = parts[0]
        tail = parts[-1]
        # Drop slug-like long titles and keep ~5 chars of the actual ID
        tail_short = tail[-6:] if len(tail) > 6 else tail
        if len(parts) <= 2:
            return f"{host}/{first}/{tail_short}"
        return f"{host}/{first}/…{tail_short}"
    except Exception:
        return url[:36] + "…" if len(url) > 36 else url


def _author_profile_url(source: str | None, author: str | None) -> str | None:
    if not author:
        return None
    a = author.lstrip("@")
    if source == "twitter":
        return f"https://x.com/{a}"
    if source == "reddit":
        return f"https://reddit.com/u/{a}"
    return None


def _humanize_ago(iso_ts: str | None) -> str:
    if not iso_ts:
        return "—"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return iso_ts[:16]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# Zomato HQ is in Gurgaon, ops team is IST-based, so calendar boundaries
# (Today, Yesterday, custom date) are computed against Asia/Kolkata. UTC
# is fine for rolling windows since they're relative to "now".
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover  pre-3.9 / missing tzdata
    from datetime import timezone as _tz
    IST = _tz(timedelta(hours=5, minutes=30))


def _window_to_range(
    window: str,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[datetime, datetime | None]:
    """Resolve a window string to (since, until) UTC datetimes.

    `until` is None for "rolling up to now" windows (the SQL omits the
    upper bound). `since` is always set.

    Window vocabulary:
        today       calendar today, IST 00:00 onward
        yesterday   full previous calendar day, IST
        hour        rolling 1 hour
        day         rolling 24 hours (legacy alias)
        week        rolling 7 days (legacy alias)
        month       rolling 30 days
        all         everything
        custom      use from_date / to_date (YYYY-MM-DD), IST-anchored.
                    to_date is inclusive (end of that day).

    Default (unknown window) is `today`.
    """
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)
    today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)

    if window == "today":
        return today_start_ist.astimezone(timezone.utc), None
    if window == "yesterday":
        yest_start = today_start_ist - timedelta(days=1)
        return (
            yest_start.astimezone(timezone.utc),
            today_start_ist.astimezone(timezone.utc),
        )
    if window == "hour":
        return now_utc - timedelta(hours=1), None
    if window == "day":   # legacy rolling 24h
        return now_utc - timedelta(hours=24), None
    if window == "week":  # legacy rolling 7d
        return now_utc - timedelta(days=7), None
    if window == "month":
        return now_utc - timedelta(days=30), None
    if window == "all":
        return datetime(2000, 1, 1, tzinfo=timezone.utc), None
    if window == "custom":
        # Be liberal in what we accept: missing from_date falls back to today.
        try:
            since = (
                datetime.fromisoformat(from_date).replace(tzinfo=IST)
                .astimezone(timezone.utc)
            ) if from_date else today_start_ist.astimezone(timezone.utc)
        except Exception:
            since = today_start_ist.astimezone(timezone.utc)
        until: datetime | None = None
        if to_date:
            try:
                t = datetime.fromisoformat(to_date).replace(tzinfo=IST)
                # to_date is inclusive; advance to start of next day
                until = (t + timedelta(days=1)).astimezone(timezone.utc)
            except Exception:
                pass
        return since, until
    # Fallback default: today
    return today_start_ist.astimezone(timezone.utc), None


def _window_to_since(window: str) -> datetime:
    """Backwards-compat shim. New code should use _window_to_range()."""
    since, _ = _window_to_range(window)
    return since


async def _fetch_counts(db: aiosqlite.Connection) -> dict[str, Any]:
    db.row_factory = aiosqlite.Row
    cnt: dict[str, Any] = {}

    # Phase ε — every count below excludes noise so the sidebar numbers
    # match what the operator sees in the inbox. The "Filtered out" section
    # below pulls counts the OPPOSITE way (only noise rows).
    cur = await db.execute("SELECT COUNT(*) AS c FROM posts WHERE noise_category IS NULL")
    cnt["total"] = (await cur.fetchone())["c"]

    cur = await db.execute(
        "SELECT source, COUNT(*) AS c FROM posts WHERE noise_category IS NULL GROUP BY source"
    )
    cnt["by_source"] = {r["source"]: r["c"] for r in await cur.fetchall()}

    cur = await db.execute(
        "SELECT category, COUNT(*) AS c FROM posts "
        "WHERE category IS NOT NULL AND noise_category IS NULL GROUP BY category"
    )
    cnt["by_side"] = {r["category"]: r["c"] for r in await cur.fetchall()}

    # Per-noise-category counts for the "Filtered out" sidebar section.
    cur = await db.execute(
        "SELECT noise_category, COUNT(*) AS c FROM posts "
        "WHERE noise_category IS NOT NULL GROUP BY noise_category"
    )
    cnt["by_noise"] = {r["noise_category"]: r["c"] for r in await cur.fetchall()}
    cnt["noise_total"] = sum(cnt["by_noise"].values())

    # All counts computed in SQL via json_extract — fast, accurate, consistent
    # with the WHERE clauses used by _build_where above.
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(hours=2)).isoformat()

    # Critical: any post with urgency=critical (noise excluded)
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts "
        "WHERE json_extract(classification, '$.urgency') = 'critical' "
        "AND noise_category IS NULL"
    )
    cnt["critical"] = (await cur.fetchone())["c"]

    # Rule-flagged (subset of critical): any post with at least one tripwire fired
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts "
        "WHERE json_array_length(json_extract(classification, '$.tripwires_fired')) > 0 "
        "AND noise_category IS NULL"
    )
    cnt["tripwires"] = (await cur.fetchone())["c"]

    # Overdue / awaiting-reply: critical OR high urgency, >2h old, AND
    # Zomato has NOT replied AND a reply was actually expected (i.e., not
    # `not_applicable` — Reddit posts and untagged Twitter mentions don't
    # count as 'awaiting' since no reply was ever expected).
    cur = await db.execute(
        """
        SELECT COUNT(*) AS c FROM posts
        WHERE json_extract(classification, '$.urgency') IN ('critical', 'high')
          AND created_at < ?
          AND zomato_response_status IN ('unchecked', 'no_reply')
          AND noise_category IS NULL
        """,
        (stale_cutoff,),
    )
    cnt["stale"] = (await cur.fetchone())["c"]

    # Phase γ: per-tier post counts + freshness (for the sidebar Influence
    # section). Returns {tier: {count, latest_at, new_in_hour}} so the
    # sidebar can render a "NEW" chip + relative timestamp without a
    # second roundtrip. Tolerant of a fresh DB where the handles table
    # may not yet have rows.
    try:
        last_hour_iso = (now - timedelta(hours=1)).isoformat()
        cur = await db.execute(
            """
            SELECT h.profile_class AS pc,
                   COUNT(*) AS c,
                   MAX(p.created_at) AS latest_at,
                   SUM(CASE WHEN p.created_at >= ? THEN 1 ELSE 0 END) AS in_hour
            FROM posts p
            JOIN handles h
              ON h.handle = lower(replace(p.author, '@', ''))
             AND h.source = p.source
            WHERE p.author IS NOT NULL
            GROUP BY h.profile_class
            """,
            (last_hour_iso,),
        )
        rows = await cur.fetchall()
        cnt["by_tier"] = {r["pc"]: r["c"] for r in rows}
        # Richer per-tier dict for the sidebar.
        cnt["tier_detail"] = {
            r["pc"]: {
                "count":       r["c"] or 0,
                "latest_at":   r["latest_at"] or "",
                "new_in_hour": (r["in_hour"] or 0) > 0,
            }
            for r in rows
        }
    except Exception:
        cnt["by_tier"] = {}
        cnt["tier_detail"] = {}

    # Activity strip: last hour, last 24h
    last_hour = (now - timedelta(hours=1)).isoformat()
    last_day = (now - timedelta(hours=24)).isoformat()
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ?", (last_hour,)
    )
    cnt["last_hour"] = (await cur.fetchone())["c"]
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ?", (last_day,)
    )
    cnt["last_24h"] = (await cur.fetchone())["c"]
    # Zomato response status counts
    cur = await db.execute(
        "SELECT zomato_response_status, COUNT(*) AS c FROM posts GROUP BY zomato_response_status"
    )
    by_resp = {(r["zomato_response_status"] or "unchecked"): r["c"] for r in await cur.fetchall()}
    cnt["replied"] = by_resp.get("replied", 0)
    # Awaiting reply = posts that ARE expected to get a reply (Twitter
    # tweets that tagged @zomato/@zomatocare) AND haven't received one yet.
    # 'not_applicable' is excluded — Reddit posts and untagged Twitter
    # mentions are not in this queue.
    cur = await db.execute(
        """
        SELECT COUNT(*) AS c FROM posts
        WHERE zomato_response_status IN ('unchecked', 'no_reply')
          AND classification IS NOT NULL
          AND (
            json_extract(classification, '$.urgency') IN ('critical', 'high')
            OR json_extract(classification, '$.sentiment') IN ('negative', 'abusive')
          )
        """
    )
    cnt["awaiting_reply"] = (await cur.fetchone())["c"]

    # Phase δ: active cluster count for the sidebar. The clusters table
    # may not exist on a fresh DB (created lazily by clusters.detect_clusters);
    # treat that as zero rather than crashing the dashboard.
    try:
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM clusters WHERE status = 'active'"
        )
        row = await cur.fetchone()
        cnt["active_clusters"] = row["c"] if row else 0
    except Exception:
        cnt["active_clusters"] = 0

    # Phase ζ — operator-flagged posts. Counts noise-clean flagged posts so
    # the sidebar number matches what clicking "Flagged" actually shows.
    try:
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM posts "
            "WHERE flagged_at IS NOT NULL AND noise_category IS NULL"
        )
        row = await cur.fetchone()
        cnt["flagged"] = row["c"] if row else 0
    except Exception:
        cnt["flagged"] = 0
    return cnt


async def _fetch_active_clusters(
    db: aiosqlite.Connection, limit: int = 12
) -> list[dict[str, Any]]:
    """Active clusters for the sidebar. Newest-first by member count then
    last_member_at. Tolerant of a missing clusters table (returns []).

    Theme-typed clusters (cluster_type='theme') get their friendly display
    name + severity + audience joined from themes.THEMES so the sidebar
    shows 'Late or delayed delivery' instead of 'delivery_delays'.
    """
    from .. import themes as _themes
    theme_meta_by_id = {t["id"]: t for t in _themes.THEMES}
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute(
            """
            SELECT id, primary_topic, side, geography, cluster_type,
                   started_at, last_member_at, member_count, summary
            FROM clusters
            WHERE status = 'active'
            ORDER BY member_count DESC, last_member_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    except Exception:
        return []

    for r in rows:
        if r.get("cluster_type") == "theme":
            meta = theme_meta_by_id.get(r.get("primary_topic")) or {}
            r["display_name"] = meta.get("name", r.get("primary_topic") or "")
            r["severity"] = meta.get("severity", "medium")
            r["audience"] = meta.get("audience", [])
        else:
            r["display_name"] = r.get("primary_topic") or "—"
            r["severity"] = None
            r["audience"] = []
    return rows


def _build_where(
    *,
    source: str | None,
    side: str | None,
    urgency: str | None,
    tripwires_only: bool,
    since: datetime,
    until: datetime | None = None,
    search: str | None,
    response: str | None = None,
    overdue: bool = False,
    cluster_id: str | None = None,
    author_tier: str | None = None,
    noise: str | None = None,
    flagged: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    """Build SQL WHERE clauses + params for both list and count queries.
    All classification-based filters use SQLite json_extract so totals
    line up with the rendered list (no python-only filtering).

    `noise` semantics:
      None or "":  default — exclude noise posts (noise_category IS NULL)
      "<cat>":     show ONLY posts in that category (e.g. 'promo')
      "all":       show everything regardless of noise tag (debug/audit)
    """
    where: list[str] = ["created_at >= :since"]
    params: dict[str, Any] = {"since": since.isoformat()}

    # Optional upper bound — set for `yesterday` and `custom` windows.
    if until is not None:
        where.append("created_at < :until")
        params["until"] = until.isoformat()

    # Phase ε — default-exclude noise. Operator never sees promo/job/stock/
    # off_topic/bot posts in the inbox unless they explicitly ask for one
    # bucket via the "Filtered out" sidebar section.
    if noise == "all":
        pass  # show everything, no filter
    elif noise:
        where.append("noise_category = :noise_cat")
        params["noise_cat"] = noise
    else:
        where.append("noise_category IS NULL")
    # Phase ζ — operator flagging. ?flagged=1 narrows to flagged posts only.
    if flagged:
        where.append("flagged_at IS NOT NULL")
    if source:
        where.append("source = :source")
        params["source"] = source
    if side:
        where.append("category = :side")
        params["side"] = side
    if search:
        where.append("(content LIKE :q OR author LIKE :q)")
        params["q"] = f"%{search}%"
    if urgency:
        where.append("json_extract(classification, '$.urgency') = :urgency")
        params["urgency"] = urgency
    if tripwires_only:
        where.append("json_array_length(json_extract(classification, '$.tripwires_fired')) > 0")
    if cluster_id:
        # Filter to members of a specific cluster. Joins to cluster_members
        # via the post id. (No join in the FROM — easier to compose with
        # the rest of the WHERE-builder via an IN subquery.)
        where.append("id IN (SELECT post_id FROM cluster_members WHERE cluster_id = :cluster_id)")
        params["cluster_id"] = cluster_id
    # Phase γ: author-tier filter — joins to handles via correlated subquery
    # so we don't need to rewrite the rest of the WHERE machinery. The
    # lower(replace(...)) handles "@user" vs "user" mismatches.
    if author_tier:
        where.append(
            "EXISTS (SELECT 1 FROM handles h "
            "WHERE h.handle = lower(replace(posts.author, '@', '')) "
            "AND h.source = posts.source "
            "AND h.profile_class = :author_tier)"
        )
        params["author_tier"] = author_tier
    if response == "replied":
        where.append("zomato_response_status = 'replied'")
    elif response == "unchecked":
        where.append("(zomato_response_status IS NULL OR zomato_response_status = 'unchecked')")
    elif response == "awaiting":
        # 'Awaiting' is the action queue: a reply WAS expected (status in
        # unchecked/no_reply, never not_applicable) AND the post is urgent
        # or negative-sentiment.
        where.append(
            "zomato_response_status IN ('unchecked', 'no_reply') "
            "AND ("
            " json_extract(classification, '$.urgency') IN ('critical', 'high')"
            " OR json_extract(classification, '$.sentiment') IN ('negative', 'abusive')"
            ")"
        )
    elif response == "not_applicable":
        where.append("zomato_response_status = 'not_applicable'")

    # Overdue: explicit "No reply yet (>2h)" filter — matches the sidebar
    # 'stale' count exactly. Critical OR high, >2h old, no reply.
    # Uses an ISO-with-tz parameter so string comparison against created_at
    # (which is also ISO-with-tz) works correctly. SQLite's datetime('now')
    # returns NO timezone suffix → string compares fail.
    if overdue:
        where.append(
            "json_extract(classification, '$.urgency') IN ('critical', 'high') "
            "AND created_at < :stale_cutoff "
            "AND zomato_response_status IN ('unchecked', 'no_reply')"
        )
        params["stale_cutoff"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    return where, params


async def _count_posts(db: aiosqlite.Connection, **filter_args: Any) -> int:
    db.row_factory = aiosqlite.Row
    where, params = _build_where(**filter_args)
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE " + " AND ".join(where), params
    )
    row = await cur.fetchone()
    return row["c"] if row else 0


async def _fetch_posts(
    db: aiosqlite.Connection,
    *,
    source: str | None,
    side: str | None,
    urgency: str | None,
    tripwires_only: bool,
    since: datetime,
    until: datetime | None = None,
    search: str | None,
    limit: int,
    offset: int,
    sort: str,
    response: str | None = None,
    overdue: bool = False,
    cluster_id: str | None = None,
    author_tier: str | None = None,
    noise: str | None = None,
    flagged: bool = False,
) -> list[dict[str, Any]]:
    db.row_factory = aiosqlite.Row
    where, params = _build_where(
        source=source, side=side, urgency=urgency,
        tripwires_only=tripwires_only, since=since, until=until,
        search=search, response=response, overdue=overdue,
        cluster_id=cluster_id, author_tier=author_tier,
        noise=noise, flagged=flagged,
    )
    params["limit"] = limit
    params["offset"] = offset

    # `priority` is the new default — answers "which post should I tackle first?"
    # `score` (legacy: urgency_score) and `created` are kept for power users.
    order = {
        "priority": "priority_score DESC, created_at DESC",
        "score":    "score DESC, created_at DESC",
        "created":  "created_at DESC",
        "author":   "author ASC, created_at DESC",
    }.get(sort, "priority_score DESC, created_at DESC")

    # Phase γ: pull handle data via correlated subqueries — keeps the WHERE
    # column references unambiguous (no JOIN aliases to thread through
    # _build_where). One indexed lookup per row; SQLite caches.
    sql = (
        "SELECT id, source, native_id, author, content, url, created_at, metadata, "
        "category, score, classification, "
        "zomato_response_status, zomato_response_url, zomato_response_at, "
        "priority_score, priority_band, priority_breakdown, "
        "action_taken, action_meta, actioned_at, flagged_at, "
        "ack_at, ack_by, ack_deadline_at, escalation_count, last_escalated_at, "
        "review_issue_id, review_issue_url, review_created_at, "
        "(SELECT tier FROM handles h "
        " WHERE h.handle = lower(replace(posts.author, '@', '')) "
        "   AND h.source = posts.source) AS h_tier, "
        "(SELECT profile_class FROM handles h "
        " WHERE h.handle = lower(replace(posts.author, '@', '')) "
        "   AND h.source = posts.source) AS h_class, "
        "(SELECT multiplier FROM handles h "
        " WHERE h.handle = lower(replace(posts.author, '@', '')) "
        "   AND h.source = posts.source) AS h_multiplier, "
        "(SELECT watchlists FROM handles h "
        " WHERE h.handle = lower(replace(posts.author, '@', '')) "
        "   AND h.source = posts.source) AS h_watchlists "
        "FROM posts WHERE " + " AND ".join(where) +
        f" ORDER BY {order} LIMIT :limit OFFSET :offset"
    )
    cur = await db.execute(sql, params)
    rows = [dict(r) for r in await cur.fetchall()]

    now = datetime.now(timezone.utc)
    fresh_cutoff = now - timedelta(hours=1)
    aged_cutoff = now - timedelta(hours=24)
    stale_cutoff = now - timedelta(hours=2)

    out: list[dict[str, Any]] = []
    for r in rows:
        cls = json.loads(r["classification"]) if r["classification"] else {}
        meta = json.loads(r["metadata"]) if r["metadata"] else {}
        # Freshness flags
        is_new = False
        is_aged = False
        is_stale = False
        try:
            ts = datetime.fromisoformat((r["created_at"] or "").replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            is_new = ts >= fresh_cutoff
            is_aged = ts < aged_cutoff
            is_stale = (
                ts < stale_cutoff
                and cls.get("urgency") in ("critical", "high")
                and not r.get("action_taken")
            )
        except Exception:
            pass
        # Parse priority breakdown JSON if present
        breakdown = {}
        if r.get("priority_breakdown"):
            try:
                breakdown = json.loads(r["priority_breakdown"])
            except Exception:
                pass

        # Phase γ: tier badge data (None if author not yet classified)
        h_tier = r.get("h_tier")
        h_class = r.get("h_class")
        h_mult = r.get("h_multiplier")
        try:
            h_watchlists = json.loads(r["h_watchlists"]) if r.get("h_watchlists") else []
        except Exception:
            h_watchlists = []
        # Skip badge for the default tiers — keeps the row uncluttered.
        # Surface it for everything that's actually a noteworthy author.
        show_tier_badge = bool(h_class) and h_class not in ("regular", "anonymous")
        tier_label = {
            "authority":      "Authority",
            "founder":        "Founder",
            "press":          "Press",
            "politician":     "Politician",
            "influencer":     "Influencer",
            "power_user":     "Power user",
            "active_citizen": "Active",
            "bot":            "Bot",
        }.get(h_class or "", h_class or "")

        out.append({
            **r,
            "metadata": meta,
            "cls": cls,
            "ago": _humanize_ago(r["created_at"]),
            "preview": (r["content"] or "")[:280],
            "url_short": _shorten_url(r["url"]),
            "author_url": _author_profile_url(r["source"], r["author"]),
            "is_new": is_new,
            "is_aged": is_aged,
            "is_stale": is_stale,
            "priority_breakdown": breakdown,
            "tier": h_tier,
            "tier_class": h_class,
            "tier_multiplier": h_mult,
            "tier_label": tier_label,
            "tier_watchlists": h_watchlists,
            "show_tier_badge": show_tier_badge,
        })
    return out


# ---------- pulse helpers ----------

async def _pulse_kpis(db: aiosqlite.Connection) -> dict[str, Any]:
    """KPI rollups for the Pulse home page.

    Pattern (per the UX design conversation):
        - KPI numbers (Mentions today / Critical / Overdue / Replied) use
          CALENDAR boundaries — IST 00:00 today onward — so the "today"
          label in the UI matches the data underneath.
        - "vs yesterday" deltas use the FULL previous calendar day (IST
          00:00 yesterday → IST 00:00 today) for apples-to-apples reads
          on a morning standup.
        - The hourly volume chart at the bottom keeps a ROLLING 24-hour
          window so it never looks half-empty before noon.
    """
    db.row_factory = aiosqlite.Row
    now_utc = datetime.now(timezone.utc)
    today_start_utc, _ = _window_to_range("today")
    yest_start_utc, yest_end_utc = _window_to_range("yesterday")
    rolling_24h_iso = (now_utc - timedelta(hours=24)).isoformat()
    stale_cutoff = (now_utc - timedelta(hours=2)).isoformat()

    today_iso = today_start_utc.isoformat()
    yest_start_iso = yest_start_utc.isoformat()
    yest_end_iso = yest_end_utc.isoformat() if yest_end_utc else today_iso

    async def _scalar(sql: str, params: tuple = ()) -> int:
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return (row["c"] if row else 0) or 0

    # Calendar today
    today = await _scalar(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? "
        "AND noise_category IS NULL",
        (today_iso,),
    )
    # Full previous calendar day
    yday = await _scalar(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? AND created_at < ? "
        "AND noise_category IS NULL",
        (yest_start_iso, yest_end_iso),
    )
    delta_abs = today - yday
    delta_pct = (delta_abs * 100 / yday) if yday > 0 else 0

    p0 = await _scalar(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? AND priority_band = 'P0' "
        "AND noise_category IS NULL",
        (today_iso,),
    )
    neg = await _scalar(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? "
        "AND json_extract(classification, '$.sentiment') IN ('negative','abusive') "
        "AND noise_category IS NULL",
        (today_iso,),
    )
    # Overdue is a NOW-anchored monitoring metric (regardless of "today"):
    # critical/high posts older than 2 hours that nobody has replied to.
    overdue = await _scalar(
        """SELECT COUNT(*) AS c FROM posts
           WHERE json_extract(classification, '$.urgency') IN ('critical','high')
             AND created_at < ?
             AND zomato_response_status IN ('unchecked','no_reply')
             AND noise_category IS NULL""",
        (stale_cutoff,),
    )
    replied = await _scalar(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? "
        "AND zomato_response_status = 'replied' AND noise_category IS NULL",
        (today_iso,),
    )
    expected = await _scalar(
        """SELECT COUNT(*) AS c FROM posts
           WHERE created_at >= ?
             AND zomato_response_status IN ('replied','unchecked','no_reply')
             AND noise_category IS NULL""",
        (today_iso,),
    )
    reply_rate = (replied * 100 / expected) if expected > 0 else 0

    # Hourly volume — keep ROLLING 24h here so the chart never starts the
    # day looking empty. Footer label says "Last 24 hours" to match.
    cur = await db.execute(
        """SELECT strftime('%Y-%m-%d %H', created_at) AS hr,
                  COUNT(*) AS total,
                  SUM(CASE WHEN json_extract(classification, '$.sentiment')
                            IN ('negative','abusive') THEN 1 ELSE 0 END) AS neg
           FROM posts
           WHERE created_at >= ? AND noise_category IS NULL
           GROUP BY hr ORDER BY hr""",
        (rolling_24h_iso,),
    )
    hourly = [
        {"hr": r["hr"], "total": r["total"], "neg": r["neg"] or 0}
        for r in await cur.fetchall()
    ]

    # Auto-replied count + average time-to-reply (Phase μ).
    # Pulls from action_meta where trigger ∈ {auto_reply_v1, drain_v1}.
    auto_replied = 0
    auto_avg_ttr_seconds: float | None = None
    try:
        cur = await db.execute(
            """SELECT created_at, action_meta
                 FROM posts
                WHERE actioned_at IS NOT NULL
                  AND actioned_at >= ?
                  AND action_taken IS NOT NULL
                  AND instr(action_taken, 'twitter_reply') > 0
                  AND noise_category IS NULL""",
            (today_iso,),
        )
        rows = await cur.fetchall()
        ttr_samples: list[float] = []
        for r in rows:
            try:
                m = json.loads(r["action_meta"] or "{}")
            except Exception:
                continue
            if m.get("trigger") not in ("auto_reply_v1", "drain_v1"):
                continue
            auto_replied += 1
            try:
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                fired_at_iso = m.get("fired_at") or ""
                fired = datetime.fromisoformat(fired_at_iso.replace("Z", "+00:00"))
                if fired.tzinfo is None:
                    fired = fired.replace(tzinfo=timezone.utc)
                ttr_samples.append((fired - created).total_seconds())
            except Exception:
                continue
        if ttr_samples:
            auto_avg_ttr_seconds = sum(ttr_samples) / len(ttr_samples)
    except Exception:
        pass

    return {
        "today": {"count": today, "delta_pct": delta_pct, "delta_abs": delta_abs},
        "yesterday": yday,
        "negative": {"count": neg, "pct": (neg * 100 / today) if today > 0 else 0},
        "p0": p0,
        "overdue": overdue,
        "replied": {"count": replied, "rate_pct": reply_rate, "expected": expected},
        "auto_replied": {
            "count": auto_replied,
            "avg_ttr_seconds": auto_avg_ttr_seconds,
        },
        "hourly": hourly,
    }


async def _pulse_top_themes(
    db: aiosqlite.Connection, limit: int = 6
) -> list[dict[str, Any]]:
    """Top theme buckets enriched with one lead post excerpt per bucket —
    that's what makes the cards on the Pulse page actually readable.
    """
    rows = await _fetch_active_clusters(db, limit=limit)
    db.row_factory = aiosqlite.Row
    for r in rows:
        try:
            cur = await db.execute(
                """SELECT p.author, p.content, p.url, p.priority_band
                   FROM cluster_members cm
                   JOIN posts p ON p.id = cm.post_id
                   WHERE cm.cluster_id = ?
                   ORDER BY COALESCE(p.priority_score, 0) DESC, p.created_at DESC
                   LIMIT 1""",
                (r["id"],),
            )
            lead = await cur.fetchone()
            if lead:
                content = (lead["content"] or "").strip()
                r["lead_excerpt"] = (
                    content[:160] + ("…" if len(content) > 160 else "")
                )
                r["lead_author"] = lead["author"]
                r["lead_url"] = lead["url"]
                r["lead_band"] = lead["priority_band"]
            else:
                r["lead_excerpt"] = ""
        except Exception:
            r["lead_excerpt"] = ""
    return rows


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
async def pulse_home(request: Request):
    """Pulse — the new home view. Brandwatch-style overview with KPIs,
    hourly volume, top themes, action queue, and latest activity.
    The dense filterable feed lives at /inbox."""
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        counts = await _fetch_counts(db)
        active_clusters = await _fetch_active_clusters(db, limit=12)
        kpis = await _pulse_kpis(db)
        themes_with_lead = await _pulse_top_themes(db, limit=6)

        # Action queue: top 5 P0/P1 unanswered posts older than 2h
        action_q = await _fetch_posts(
            db,
            source=None, side=None, urgency=None,
            tripwires_only=False, since=_window_to_since("week"),
            search=None, response="awaiting", overdue=True,
            cluster_id=None, author_tier=None,
            limit=5, offset=0, sort="priority",
        )

        # Latest 8 across everything
        latest = await _fetch_posts(
            db,
            source=None, side=None, urgency=None,
            tripwires_only=False, since=_window_to_since("day"),
            search=None, response=None, overdue=False,
            cluster_id=None, author_tier=None,
            limit=8, offset=0, sort="created",
        )

        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT MAX(finished_at) AS t FROM fetch_runs WHERE error IS NULL"
        )
        row = await cur.fetchone()
        last_sync_iso = row["t"] if row and row["t"] else None

    # Connector status — read-only snapshot for the home strip.
    from ..actions.dispatcher import connector_health
    connectors = await connector_health()

    template = _jinja.get_template("pulse.html")
    html = template.render(
        active_view="pulse",
        counts=counts,
        active_clusters=active_clusters,
        kpis=kpis,
        themes_with_lead=themes_with_lead,
        action_q=action_q,
        latest=latest,
        connectors=connectors,
        filters={
            "source": "all", "side": "all", "urgency": "all",
            "window": "today", "q": "", "sort": "priority",
            "page": 1, "per_page": 50, "response": "all",
            "overdue": False, "cluster_id": "", "author_tier": "",
            "tripwires": False, "noise": "", "from": "", "to": "",
            "flagged": False,
        },
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        last_sync_ago=_humanize_ago(last_sync_iso),
        last_sync_iso=last_sync_iso,
        sync_state=_sync_state,
        auto_sync_enabled=_AUTO_SYNC,
        refresh_interval=config.REFRESH_INTERVAL,
    )
    return HTMLResponse(html)


@app.get("/themes", response_class=HTMLResponse)
async def themes_page(request: Request):
    """Discovery view — every active complaint theme as a card grid with
    sample post excerpts. Replaces the long bucket list in the sidebar.
    """
    from .. import themes as _themes_mod
    meta_by_id = {t["id"]: t for t in _themes_mod.THEMES}

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        counts = await _fetch_counts(db)
        # active_clusters is what the sidebar uses — keep limit modest there
        active_clusters = await _fetch_active_clusters(db, limit=12)
        # all_themes is what the page renders — fetch up to 50 so we never
        # silently hide a bucket the user might be looking for.
        all_themes = await _fetch_active_clusters(db, limit=50)

        # Hydrate each theme with its top 3 sample posts (highest priority first)
        db.row_factory = aiosqlite.Row
        for c in all_themes:
            try:
                cur = await db.execute(
                    """SELECT p.author, p.content, p.url, p.priority_band,
                              p.created_at, p.source
                       FROM cluster_members cm
                       JOIN posts p ON p.id = cm.post_id
                       WHERE cm.cluster_id = ?
                       ORDER BY COALESCE(p.priority_score, 0) DESC, p.created_at DESC
                       LIMIT 3""",
                    (c["id"],),
                )
                samples = [dict(r) for r in await cur.fetchall()]
                for s in samples:
                    s["ago"] = _humanize_ago(s["created_at"])
                    txt = (s["content"] or "").strip()
                    s["excerpt"] = txt[:220] + ("…" if len(txt) > 220 else "")
                c["sample_posts"] = samples
            except Exception:
                c["sample_posts"] = []
            # Pull description / patterns count from THEMES catalog
            meta = meta_by_id.get(c.get("primary_topic")) or {}
            c["pattern_count"] = len(meta.get("patterns") or [])
            # Last-activity timestamp: when did the most recent post in this
            # cluster land. Used in the discovery row as "last active 2h ago"
            # so the operator can spot stale vs hot themes at a glance.
            try:
                cur = await db.execute(
                    """SELECT MAX(p.created_at) AS last_at,
                              COUNT(*)         AS total
                         FROM cluster_members cm
                         JOIN posts p ON p.id = cm.post_id
                        WHERE cm.cluster_id = ?""",
                    (c["id"],),
                )
                row = await cur.fetchone()
                if row and row["last_at"]:
                    c["last_activity_iso"] = row["last_at"]
                    c["last_activity_ago"] = _humanize_ago(row["last_at"])
                else:
                    c["last_activity_ago"] = None
            except Exception:
                c["last_activity_ago"] = None

        cur = await db.execute(
            "SELECT MAX(finished_at) AS t FROM fetch_runs WHERE error IS NULL"
        )
        row = await cur.fetchone()
        last_sync_iso = row["t"] if row and row["t"] else None

    template = _jinja.get_template("themes.html")
    html = template.render(
        active_view="themes",
        counts=counts,
        active_clusters=active_clusters,
        all_themes=all_themes,
        filters={
            "source": "all", "side": "all", "urgency": "all",
            "window": "today", "q": "", "sort": "priority",
            "page": 1, "per_page": 50, "response": "all",
            "overdue": False, "cluster_id": "", "author_tier": "",
            "tripwires": False, "noise": "", "from": "", "to": "",
            "flagged": False,
        },
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        last_sync_ago=_humanize_ago(last_sync_iso),
        last_sync_iso=last_sync_iso,
        sync_state=_sync_state,
        auto_sync_enabled=_AUTO_SYNC,
        refresh_interval=config.REFRESH_INTERVAL,
    )
    return HTMLResponse(html)


@app.get("/operations", response_class=HTMLResponse)
async def operations_page(request: Request):
    """Operations view. Restaurant, geographic, and time-of-day intelligence
    over the last 7 days. Surfaces who's getting complained about most,
    where, and when.
    """
    from collections import Counter, defaultdict
    from .. import extraction
    from .. import restaurant_canon as canon

    since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    since_iso = since_dt.isoformat()

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        counts = await _fetch_counts(db)
        active_clusters = await _fetch_active_clusters(db, limit=12)
        db.row_factory = aiosqlite.Row

        # Pull all noise-clean posts from last 7 days.
        cur = await db.execute(
            """SELECT id, content, created_at, priority_band, classification
               FROM posts
               WHERE created_at >= ?
                 AND noise_category IS NULL
               ORDER BY created_at DESC""",
            (since_iso,),
        )
        rows = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT MAX(finished_at) AS t FROM fetch_runs WHERE error IS NULL"
        )
        row = await cur.fetchone()
        last_sync_iso = row["t"] if row and row["t"] else None

    # ---- Aggregate in Python ----
    rest_posts: dict[str, list[dict]] = defaultdict(list)
    city_posts: dict[str, list[dict]] = defaultdict(list)
    dish_posts: dict[str, list[dict]] = defaultdict(list)
    hour_total: Counter = Counter()
    hour_neg: Counter = Counter()
    location_tagged = 0

    for r in rows:
        text = r["content"] or ""
        try:
            cls = json.loads(r["classification"]) if r["classification"] else {}
        except Exception:
            cls = {}
        sentiment = cls.get("sentiment", "")

        # IST hour-of-day bucket.
        try:
            ts = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            hour_ist = ts.astimezone(IST).hour
        except Exception:
            hour_ist = None
        if hour_ist is not None:
            hour_total[hour_ist] += 1
            if sentiment in ("negative", "abusive"):
                hour_neg[hour_ist] += 1

        # Canonicalize restaurants so that platform self-references
        # ("Zomato"), parser garbage ("Zomato.They"), and brand aliases
        # (KFC / KFC_India / "Domino's" / Dominos) collapse to one
        # truthful row per brand. See ``social_watch/restaurant_canon.py``.
        rests = canon.canonicalize_many(extraction.extract_restaurants(text))
        cities = extraction.extract_cities(text)
        dishes = extraction.extract_dishes(text)
        if rests or cities:
            location_tagged += 1
        for name in rests:
            rest_posts[name].append(r)
        for c in cities:
            city_posts[c].append(r)
        for d in dishes:
            if sentiment in ("negative", "abusive"):
                dish_posts[d].append(r)

    # ---- Top 10 restaurants ----
    band_score = {"P0": 4, "P1": 3, "P2": 2, "P3": 1}
    top_restaurants = []
    for name, plist in sorted(rest_posts.items(), key=lambda kv: -len(kv[1]))[:10]:
        bands = Counter(p.get("priority_band") or "P3" for p in plist)
        # Lead excerpt: highest priority post (P0 > P1 > P2 > P3),
        # most recent on tiebreak.
        if plist:
            lead = sorted(
                plist,
                key=lambda p: (
                    -band_score.get(p.get("priority_band") or "P3", 1),
                    -(0 if not p.get("created_at") else 1),
                    p.get("created_at") or "",
                ),
            )[0]
        else:
            lead = None
        excerpt = ""
        if lead:
            txt = (lead.get("content") or "").strip().replace("\n", " ")
            excerpt = txt[:200] + ("…" if len(txt) > 200 else "")
        top_restaurants.append({
            "restaurant_name": name,
            "total_posts": len(plist),
            "post_band_breakdown": {
                "P0": bands.get("P0", 0),
                "P1": bands.get("P1", 0),
                "P2": bands.get("P2", 0),
                "P3": bands.get("P3", 0),
            },
            "top_complaint_excerpt": excerpt,
        })

    # ---- Top 10 cities ----
    city_heatmap = []
    for city, plist in sorted(city_posts.items(), key=lambda kv: -len(kv[1]))[:10]:
        total = len(plist)
        score_sum = 0.0
        neg_count = 0
        for p in plist:
            score_sum += band_score.get(p.get("priority_band") or "P3", 1)
            try:
                cls = json.loads(p["classification"]) if p["classification"] else {}
            except Exception:
                cls = {}
            if cls.get("sentiment") in ("negative", "abusive"):
                neg_count += 1
        city_heatmap.append({
            "city": city,
            "total_posts": total,
            "avg_priority_score": round(score_sum / total, 2) if total else 0.0,
            "neg_pct": round(100 * neg_count / total) if total else 0,
        })

    # ---- Hour patterns: 24 buckets ----
    hour_patterns = [
        {"hour": h, "total": hour_total.get(h, 0), "neg": hour_neg.get(h, 0)}
        for h in range(24)
    ]
    if any(x["total"] for x in hour_patterns):
        peak = max(hour_patterns, key=lambda x: x["total"])
    else:
        peak = hour_patterns[20]
    peak_hour = peak["hour"]
    peak_total = peak["total"]
    peak_neg_pct = round(100 * peak["neg"] / peak["total"]) if peak["total"] else 0

    def _fmt_hour(h: int) -> str:
        if h == 0:
            return "12am"
        if h < 12:
            return f"{h}am"
        if h == 12:
            return "12pm"
        return f"{h-12}pm"

    peak_hour_label = _fmt_hour(peak_hour)

    # ---- Top 5 complained dishes ----
    dish_complaints = [
        {"dish": d, "total": len(plist)}
        for d, plist in sorted(dish_posts.items(), key=lambda kv: -len(kv[1]))[:5]
    ]

    template = _jinja.get_template("operations.html")
    html = template.render(
        active_view="operations",
        counts=counts,
        active_clusters=active_clusters,
        top_restaurants=top_restaurants,
        city_heatmap=city_heatmap,
        hour_patterns=hour_patterns,
        peak_hour_label=peak_hour_label,
        peak_hour_total=peak_total,
        peak_hour_neg_pct=peak_neg_pct,
        dish_complaints=dish_complaints,
        location_tagged=location_tagged,
        filters={
            "source": "all", "side": "all", "urgency": "all",
            "window": "week", "q": "", "sort": "priority",
            "page": 1, "per_page": 50, "response": "all",
            "overdue": False, "cluster_id": "", "author_tier": "",
            "tripwires": False, "noise": "", "from": "", "to": "",
            "flagged": False,
        },
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        last_sync_ago=_humanize_ago(last_sync_iso),
        last_sync_iso=last_sync_iso,
        sync_state=_sync_state,
        auto_sync_enabled=_AUTO_SYNC,
        refresh_interval=config.REFRESH_INTERVAL,
    )
    return HTMLResponse(html)


@app.get("/briefing", response_class=HTMLResponse)
async def briefing_page(request: Request):
    """Daily executive briefing. A 9am morning memo summarising the last
    24 hours: KPIs, top themes, anomalies, emerging themes, top risks.
    Data-first: numbers and patterns are the value, prose is decoration.
    """
    from .. import briefings as briefings_mod

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        counts = await _fetch_counts(db)
        active_clusters = await _fetch_active_clusters(db, limit=12)
        cur = await db.execute(
            "SELECT MAX(finished_at) AS t FROM fetch_runs WHERE error IS NULL"
        )
        row = await cur.fetchone()
        last_sync_iso = row["t"] if row and row["t"] else None

    brief = await briefings_mod.generate_brief()

    template = _jinja.get_template("briefing.html")
    html = template.render(
        active_view="briefing",
        counts=counts,
        active_clusters=active_clusters,
        brief=brief,
        filters={
            "source": "all", "side": "all", "urgency": "all",
            "window": "today", "q": "", "sort": "priority",
            "page": 1, "per_page": 50, "response": "all",
            "overdue": False, "cluster_id": "", "author_tier": "",
            "tripwires": False, "noise": "", "from": "", "to": "",
            "flagged": False,
        },
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        last_sync_ago=_humanize_ago(last_sync_iso),
        last_sync_iso=last_sync_iso,
        sync_state=_sync_state,
        auto_sync_enabled=_AUTO_SYNC,
        refresh_interval=config.REFRESH_INTERVAL,
    )
    return HTMLResponse(html)


@app.get("/actions", response_class=HTMLResponse)
async def actions_log(
    request: Request,
    channel: str = Query(""),
    trigger: str = Query(""),  # auto_reply_v1 / drain_v1 / manual / "" for all
):
    """Audit log of every action fired in the last 24h.

    Walks ``posts.action_meta.channels`` to produce one row per
    (post, channel) action. Sorted newest first, capped at 200.
    Optional ``?channel=`` filter so the home connector strip can
    deep-link.
    """
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        counts = await _fetch_counts(db)
        active_clusters = await _fetch_active_clusters(db, limit=12)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, source, author, content, url, priority_band, "
            "       action_taken, action_meta, actioned_at "
            "FROM posts "
            "WHERE actioned_at IS NOT NULL "
            "ORDER BY actioned_at DESC "
            "LIMIT 500"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT MAX(finished_at) AS t FROM fetch_runs WHERE error IS NULL"
        )
        sync_row = await cur.fetchone()
        last_sync_iso = sync_row["t"] if sync_row and sync_row["t"] else None

    # Flatten into one row per (post, channel).
    actions: list[dict] = []
    for row in rows:
        try:
            meta = json.loads(row["action_meta"] or "{}")
        except Exception:
            meta = {}
        chans = meta.get("channels") if isinstance(meta, dict) else None
        if not isinstance(chans, dict):
            continue
        # Only filter by the surrounding meta's trigger (consistent across
        # the post's channels) — auto_reply_v1 / drain_v1 marks the whole
        # action_meta, not individual channel sub-rows.
        if trigger and meta.get("trigger") != trigger:
            continue
        for cname, cresult in chans.items():
            if channel and cname != channel:
                continue
            if not isinstance(cresult, dict):
                continue
            payload = cresult.get("payload") or {}
            inner_result = cresult.get("result") or {}
            # Prefer the URL of the artifact we *created* (Linear issue,
            # Twitter reply, Reddit comment) — that's the actionable link
            # for the operator. Fall back to the source post URL.
            target = (
                inner_result.get("issue_url")     # Linear ticket
                or inner_result.get("reply_url")  # Twitter reply
                or inner_result.get("comment_url")  # Reddit comment
                or payload.get("tweet_url")       # source tweet (fallback)
                or payload.get("reddit_url")      # source Reddit
                or payload.get("to")              # email
                or payload.get("recipient")
                or payload.get("channel_name")
                or row["url"]
                or ""
            )
            actions.append({
                "ts": cresult.get("ts") or row["actioned_at"] or "",
                "channel": cname,
                "ok": bool(cresult.get("ok")),
                "status": cresult.get("status"),
                "error": cresult.get("error"),
                "target": target,
                "post_id": row["id"],
                "post_url": row["url"],
                "post_source": row["source"],
                # Trigger source — "manual" / "auto" / "force" / "auto_reply_v1" /
                # "drain_v1". Lets the timeline distinguish operator-fired
                # from system-fired actions, and supports the ?trigger= filter.
                "trigger": meta.get("trigger") or "",
                "post_author": row["author"],
                "post_excerpt": (row["content"] or "")[:140],
                "priority_band": row["priority_band"],
            })

    # Newest first; cap at 200 after the per-channel filter.
    actions.sort(key=lambda a: a["ts"] or "", reverse=True)
    actions = actions[:200]

    # All known channels for the filter chips.
    known_channels = ["slack", "discord", "email", "sheets", "ticket",
                      "twitter_reply", "reddit_comment"]

    # ───────────────────────────────────────────────────────────────
    # Summary roll-up — what a CMO actually wants to know:
    #   • Did we keep up?  (total fired)
    #   • Are we shipping reliably?  (% success)
    #   • Where are the failures?  (by-channel + the failure list)
    # The page leads with this; the timeline is secondary.
    # ───────────────────────────────────────────────────────────────
    by_channel = {ch: {"fired": 0, "failed": 0} for ch in known_channels}
    failures: list[dict] = []
    total_fired = 0
    total_failed = 0
    for a in actions:
        ch = a["channel"]
        if ch not in by_channel:
            by_channel[ch] = {"fired": 0, "failed": 0}
        if a["ok"]:
            by_channel[ch]["fired"] += 1
            total_fired += 1
        else:
            by_channel[ch]["failed"] += 1
            total_failed += 1
            failures.append(a)

    # Friendly verbs per channel — used both in the by-channel cards and
    # in the timeline so the user reads sentences, not column data.
    channel_meta = {
        "slack":          {"label": "Slack",          "verb": "Sent Slack alert",        "noun": "alerts",     "icon": "slack",          "color": "indigo"},
        "discord":        {"label": "Discord",        "verb": "Sent Discord alert",      "noun": "alerts",     "icon": "message-circle", "color": "indigo"},
        "email":          {"label": "Email",          "verb": "Emailed the team",        "noun": "emails",     "icon": "mail",           "color": "emerald"},
        "sheets":         {"label": "Google Sheet",   "verb": "Added a row to the Google Sheet",   "noun": "rows",       "icon": "clipboard-list", "color": "emerald"},
        "ticket":         {"label": "Linear ticket",  "verb": "Opened Linear ticket",    "noun": "tickets",    "icon": "ticket",         "color": "violet"},
        "twitter_reply":  {"label": "Reply on X",     "verb": "Replied on X",            "noun": "replies",    "icon": "message-circle-reply", "color": "sky"},
        "reddit_comment": {"label": "Reddit comment", "verb": "Commented on Reddit",     "noun": "comments",   "icon": "message-square", "color": "orange"},
    }

    summary = {
        "total":     total_fired + total_failed,
        "succeeded": total_fired,
        "failed":    total_failed,
        "by_channel": by_channel,
        "failures":   failures[:10],   # cap: page surfaces top 10 failures
        "channels_used": sum(1 for c, v in by_channel.items() if v["fired"] > 0),
    }

    template = _jinja.get_template("actions.html")
    html = template.render(
        active_view="actions",
        counts=counts,
        active_clusters=active_clusters,
        actions=actions,
        active_channel=channel,
        known_channels=known_channels,
        summary=summary,
        channel_meta=channel_meta,
        filters={
            "source": "all", "side": "all", "urgency": "all",
            "window": "today", "q": "", "sort": "priority",
            "page": 1, "per_page": 50, "response": "all",
            "overdue": False, "cluster_id": "", "author_tier": "",
            "tripwires": False, "noise": "", "from": "", "to": "",
            "flagged": False,
        },
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        last_sync_ago=_humanize_ago(last_sync_iso),
        last_sync_iso=last_sync_iso,
        sync_state=_sync_state,
        auto_sync_enabled=_AUTO_SYNC,
        refresh_interval=config.REFRESH_INTERVAL,
    )
    return HTMLResponse(html)


@app.get("/inbox", response_class=HTMLResponse)
async def inbox(
    request: Request,
    source: str = Query("all"),
    side: str = Query("all"),
    urgency: str = Query("all"),
    tripwires: int = Query(0),
    window: str = Query("today"),
    from_date: str = Query("", alias="from"),  # YYYY-MM-DD, IST. Used when window=custom.
    to_date: str = Query("", alias="to"),      # YYYY-MM-DD, IST. Inclusive end.
    q: str = Query(""),
    sort: str = Query("priority"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=500),
    response: str = Query("all"),  # all | awaiting | replied | unchecked
    overdue: int = Query(0),       # 1 = filter to "No reply yet (>2h)" exactly
    cluster_id: str = Query(""),   # filter to members of one cluster (Phase δ)
    author_tier: str = Query(""),  # press | politician | authority | influencer ... (Phase γ)
    noise: str = Query(""),        # "" = clean only (default); 'promo' / 'job' / 'stock' / 'off_topic' / 'bot'; 'all' = no filter
    flagged: int = Query(0),       # 1 = show only operator-flagged posts (Phase ζ)
):
    since, until = _window_to_range(
        window, from_date=from_date or None, to_date=to_date or None
    )
    # When viewing a specific cluster the natural mental model is "show me
    # everything in this cluster regardless of when posts landed", so widen
    # the time window automatically. Users can still tighten it via the
    # window picker if they want.
    if cluster_id:
        since, until = _window_to_range("week")
    # When viewing a noise bucket the operator wants to see ALL posts in
    # that bucket, not just today's slice — these are rare and worth
    # auditing across the full window.
    if noise and noise != "all":
        since, until = _window_to_range("all")
    # Same idea for the Flagged view — flagged posts can be days old, the
    # operator wants to see all of them not just today's.
    if flagged:
        since, until = _window_to_range("all")
    filter_args = dict(
        source=None if source == "all" else source,
        side=None if side == "all" else side,
        urgency=None if urgency == "all" else urgency,
        tripwires_only=bool(tripwires),
        since=since,
        until=until,
        search=q.strip() or None,
        response=None if response == "all" else response,
        overdue=bool(overdue),
        cluster_id=cluster_id or None,
        author_tier=author_tier or None,
        noise=noise or None,
        flagged=bool(flagged),
    )
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        counts = await _fetch_counts(db)
        active_clusters = await _fetch_active_clusters(db, limit=12)
        total_filtered = await _count_posts(db, **filter_args)
        total_pages = max(1, (total_filtered + per_page - 1) // per_page)
        # Clamp page to valid range
        page = min(max(1, page), total_pages)
        offset = (page - 1) * per_page

        posts = await _fetch_posts(
            db,
            **filter_args,
            limit=per_page,
            offset=offset,
            sort=sort,
        )

        # Annotate each post with:
        #   • its incident playbook (if a hard tripwire fired) — so the
        #     row can render a "Death claim playbook" chip and the click
        #     modal can show the procedure.
        #   • the Linear ticket id (if the ticket channel succeeded) —
        #     so the status chip shows "Ticketed · ZOM-42" with a click
        #     through to the actual issue.
        from .. import playbooks
        for p in posts:
            try:
                cls = json.loads(p.get("classification") or "{}")
            except Exception:
                cls = {}
            try:
                meta = json.loads(p.get("action_meta") or "{}")
            except Exception:
                meta = {}
            p["_playbook"] = playbooks.for_post(cls)
            ticket_chan = (meta.get("channels") or {}).get("ticket") or {}
            ticket_result = ticket_chan.get("result") or {}
            p["_ticket_id"]  = ticket_result.get("issue_identifier")
            p["_ticket_url"] = ticket_result.get("issue_url")
            # Bypass audit (only set when an operator force-replied on a
            # locked playbook). Surfaces in the inbox row + actions log.
            p["_bypass_by"]     = meta.get("bypass_approved_by")
            p["_bypass_reason"] = meta.get("bypass_reason")

        # Auto-reply eligibility — annotate each post with green/yellow/
        # red/grey + the human-readable reason. The inbox row renders
        # this as a small risk dot. We compute it server-side so the
        # template stays simple and the dot renders on first paint.
        from .. import auto_reply
        async with aiosqlite.connect(str(config.DB_PATH)) as ardb:
            for p in posts:
                try:
                    cls = json.loads(p.get("classification") or "{}")
                    pri = json.loads(p.get("priority_breakdown") or "{}")
                except Exception:
                    cls, pri = {}, {}
                if not cls.get("method"):
                    p["_risk_color"]  = "grey"
                    p["_risk_label"]  = "Awaiting classification"
                    continue
                last = await auto_reply._last_reply_for_author(
                    ardb, p.get("author") or "", p.get("source") or ""
                )
                ok, reason = auto_reply.is_eligible(p, cls, pri, author_last_reply_at=last)
                if ok:
                    p["_risk_color"] = "green"
                    p["_risk_label"] = "Auto-reply eligible — will fire automatically when AUTO_REPLY_ENABLED=1, or via the Drain modal"
                elif reason in ("auto_action_safe=false", "priority_tripwire_override") or reason.startswith("audience_requires_human"):
                    p["_risk_color"] = "red"
                    p["_risk_label"] = "Human required — " + reason.replace("_", " ")
                elif (p.get("source") or "").lower() != "twitter":
                    p["_risk_color"] = "grey"
                    p["_risk_label"] = "Reddit auto-reply: coming soon. For now, use the manual Comment button."
                elif reason in ("zomato_already_replied", "already_replied"):
                    p["_risk_color"] = "grey"
                    p["_risk_label"] = "Already replied"
                else:
                    p["_risk_color"] = "yellow"
                    p["_risk_label"] = "Drain-eligible — " + reason.replace("_", " ")

        # last sync from fetch_runs
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT MAX(finished_at) AS t FROM fetch_runs WHERE error IS NULL"
        )
        row = await cur.fetchone()
        last_sync_iso = row["t"] if row and row["t"] else None

        # If a cluster_id is selected, fetch its row for the breadcrumb chip
        active_cluster_focus: dict[str, Any] | None = None
        if cluster_id:
            try:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    """SELECT id, primary_topic, side, geography, cluster_type,
                              member_count, summary, status
                       FROM clusters WHERE id = ?""",
                    (cluster_id,),
                )
                r = await cur.fetchone()
                active_cluster_focus = dict(r) if r else None
            except Exception:
                active_cluster_focus = None

    filters = {
        "source": source,
        "side": side,
        "urgency": urgency,
        "tripwires": bool(tripwires),
        "window": window,
        "q": q,
        "sort": sort,
        "page": page,
        "per_page": per_page,
        "response": response,
        "overdue": bool(overdue),
        "cluster_id": cluster_id,
        "author_tier": author_tier,
        "noise": noise,
        "from": from_date,
        "to": to_date,
        "flagged": bool(flagged),
    }

    # Pagination metadata — what the template renders the page-list from
    pagination = {
        "page": page,
        "per_page": per_page,
        "total_filtered": total_filtered,
        "total_pages": total_pages,
        "from_idx": offset + 1 if total_filtered else 0,
        "to_idx": min(offset + per_page, total_filtered),
        "pages": _pagination_pages(page, total_pages),
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }

    # Pre-compute the set of channels that are actually configured so the
    # inbox row can disable menu items for unconfigured ones up-front. Way
    # better than letting the user click and get a wall of error text.
    from ..actions.dispatcher import connector_health
    _conn_health = await connector_health()
    configured_channels: set[str] = {
        c["name"] for c in _conn_health if c.get("configured")
    }
    # Friendly env-var hints, used in the disabled-tooltip text so the
    # operator knows exactly what's missing.
    channel_env_hint: dict[str, str] = {
        "slack":          "SLACK_WEBHOOK_URL",
        "discord":        "DISCORD_WEBHOOK_URL",
        "email":          "SMTP_HOST / SMTP_USER / SMTP_PASS",
        "sheets":         "SHEETS_WEBHOOK_URL",
        "ticket":         "LINEAR_API_KEY + LINEAR_TEAM_ID",
        "twitter_reply":  "TWITTER_COOKIE_AUTH_TOKEN + TWITTER_COOKIE_CT0",
        "reddit_comment": "REDDIT_CLIENT_ID / SECRET / USERNAME / PASSWORD",
    }

    template = _jinja.get_template("dashboard.html")
    html = template.render(
        active_view="inbox",
        posts=posts,
        counts=counts,
        filters=filters,
        pagination=pagination,
        link=_make_link_fn(filters),
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        last_sync_ago=_humanize_ago(last_sync_iso),
        last_sync_iso=last_sync_iso,
        sync_state=_sync_state,
        auto_sync_enabled=_AUTO_SYNC,
        refresh_interval=config.REFRESH_INTERVAL,
        active_clusters=active_clusters,
        active_cluster_focus=active_cluster_focus,
        configured_channels=configured_channels,
        channel_env_hint=channel_env_hint,
    )
    return HTMLResponse(html)


@app.get("/api/post/{post_id:path}")
async def post_detail(post_id: str):
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
        if not row:
            return {"error": "not_found"}
        d = dict(row)
        d["metadata"] = json.loads(d["metadata"]) if d["metadata"] else {}
        d["classification"] = json.loads(d["classification"]) if d["classification"] else {}
        if d.get("priority_breakdown"):
            try:
                d["priority_breakdown"] = json.loads(d["priority_breakdown"])
            except Exception:
                pass
        # Surface the tiered routing decision so the UI can show
        # "would fire: Sheets + Email" without re-implementing the rules.
        try:
            from ..actions.dispatcher import _route_for_post
            d["routes"] = _route_for_post(
                d, d.get("classification") or {}, d.get("priority_breakdown") or {}
            )
        except Exception:
            d["routes"] = []
        return d


@app.post("/api/posts/{post_id:path}/flag")
async def toggle_flag(post_id: str):
    """Toggle the flag on a single post. Returns the new state.

    Operators flag posts they want to revisit later. NULL flagged_at
    means unflagged; a timestamp means flagged at that moment.

    Response shape:
        { ok: bool, post_id: str, flagged: bool, flagged_at: iso8601 | None }
    """
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT flagged_at FROM posts WHERE id = ?", (post_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return JSONResponse(
                {"ok": False, "error": "not_found"}, status_code=404
            )

        is_currently_flagged = row["flagged_at"] is not None
        if is_currently_flagged:
            await db.execute(
                "UPDATE posts SET flagged_at = NULL WHERE id = ?", (post_id,)
            )
            new_state, new_ts = False, None
        else:
            new_ts = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "UPDATE posts SET flagged_at = ? WHERE id = ?", (new_ts, post_id)
            )
            new_state = True
        await db.commit()

    return {
        "ok": True,
        "post_id": post_id,
        "flagged": new_state,
        "flagged_at": new_ts,
    }


@app.post("/api/posts/{post_id:path}/acknowledge")
async def acknowledge_post(post_id: str, request: Request):
    """Operator acknowledges responsibility for a tripwired post.

    Stops the SLA countdown — the row's ``ack_deadline_at`` was set
    when the dispatcher first fired actions on this post; clicking
    Acknowledge captures who took ownership and at what time.

    Body: ``{operator: "<name>"}`` (optional; defaults to "anonymous").
    Re-ack is a no-op (returns ok=True with the existing ack_at/ack_by).
    """
    operator = "anonymous"
    try:
        body = await request.json()
        if isinstance(body, dict):
            v = body.get("operator")
            if isinstance(v, str) and v.strip():
                operator = v.strip()[:120]
    except Exception:
        pass

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ack_at, ack_by FROM posts WHERE id = ?", (post_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

        if row["ack_at"]:
            return {
                "ok": True, "post_id": post_id,
                "already_acked": True,
                "ack_at": row["ack_at"], "ack_by": row["ack_by"],
            }

        now_iso = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE posts SET ack_at = ?, ack_by = ? WHERE id = ?",
            (now_iso, operator, post_id),
        )
        await db.commit()

    return {
        "ok": True, "post_id": post_id,
        "already_acked": False,
        "ack_at": now_iso, "ack_by": operator,
    }


def _action_status_to_http(status: str) -> int:
    """Shared HTTP code mapping for /api/actions/* endpoints."""
    return {
        "skipped:not_found":     404,
        "skipped:no_webhook":    503,
        "skipped:bad_data":      422,
        "skipped:not_p0":        409,
        "skipped:not_routable":  409,
        "blocked:playbook":      423,   # Locked — playbook bars this action
        "failed":                502,
    }.get(status, 200)


@app.post("/api/actions/slack/{post_id:path}")
async def fire_slack_action(post_id: str):
    """Manual trigger from the dashboard's per-row "Slack" menu item.
    Fires Slack ONLY (per-channel manual). Idempotent: re-clicking
    on a post whose Slack already fired returns ``already_actioned``.
    """
    from ..actions import dispatch_for_post
    result = await dispatch_for_post(post_id, trigger="manual", channels=["slack"])
    return JSONResponse(result, status_code=_action_status_to_http(result.get("status", "")))


@app.post("/api/actions/discord/{post_id:path}")
async def fire_discord_action(post_id: str):
    """Manual Discord trigger — same shape as Slack."""
    from ..actions import dispatch_for_post
    result = await dispatch_for_post(post_id, trigger="manual", channels=["discord"])
    return JSONResponse(result, status_code=_action_status_to_http(result.get("status", "")))


@app.post("/api/actions/email/{post_id:path}")
async def fire_email_action(post_id: str):
    """Manual email trigger."""
    from ..actions import dispatch_for_post
    result = await dispatch_for_post(post_id, trigger="manual", channels=["email"])
    return JSONResponse(result, status_code=_action_status_to_http(result.get("status", "")))


@app.post("/api/actions/sheets/{post_id:path}")
async def fire_sheets_action(post_id: str):
    """Manual sheet append."""
    from ..actions import dispatch_for_post
    result = await dispatch_for_post(post_id, trigger="manual", channels=["sheets"])
    return JSONResponse(result, status_code=_action_status_to_http(result.get("status", "")))


@app.post("/api/actions/ticket/{post_id:path}")
async def fire_ticket_action(post_id: str):
    """Manual Linear ticket creation."""
    from ..actions import dispatch_for_post
    result = await dispatch_for_post(post_id, trigger="manual", channels=["ticket"])
    return JSONResponse(result, status_code=_action_status_to_http(result.get("status", "")))


# ============================================================
# Drain — bulk operator-fired Twitter reply on the overdue queue.
# Three endpoints: preview (no side effects), run (starts a task,
# returns a job id), status (polled by the modal for progress).
# ============================================================

# In-memory job state. Cheap; one drain runs at a time per server.
# A restart wipes in-flight jobs — that's fine for a take-home / demo
# (production would persist to a queue). Cancel sets job["cancelled"]
# which the run loop checks between fires.
_drain_jobs: dict[str, dict[str, Any]] = {}
_drain_lock: asyncio.Lock | None = None


def _drain_get_lock() -> asyncio.Lock:
    """Module-level lock created lazily so it binds to the running loop."""
    global _drain_lock
    if _drain_lock is None:
        _drain_lock = asyncio.Lock()
    return _drain_lock


@app.get("/api/drain/preview")
async def drain_preview():
    """Return the audit-only preview the modal opens with: how many
    overdue posts are eligible to bulk-template, how many need human
    review, and a per-playbook breakdown of the human-review pile.

    No side effects. Safe to call repeatedly."""
    from .. import auto_reply, playbooks

    # Define "overdue" the same way the sidebar action queue does:
    # critical/high urgency, no Zomato reply, more than 2h old.
    cutoff_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    eligible_ids: list[str] = []
    needs_review_breakdown: dict[str, int] = {}
    sample_replies: list[dict[str, Any]] = []
    other_skipped = 0

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, source, author, content, url, created_at,
                   classification, priority_breakdown,
                   zomato_response_status, action_taken
              FROM posts
             WHERE source = 'twitter'
               AND created_at >= ?
               AND created_at <= ?
               AND (action_taken IS NULL OR instr(action_taken, 'twitter_reply') = 0)
               AND (zomato_response_status IS NULL OR zomato_response_status != 'replied')
               AND classification IS NOT NULL
               AND json_extract(classification, '$.urgency') IN ('critical', 'high')
             ORDER BY created_at ASC
             LIMIT 500
            """,
            (cutoff_24h, cutoff_2h),
        )
        rows = [dict(r) for r in await cur.fetchall()]

        for row in rows:
            try:
                cls = json.loads(row.get("classification") or "{}")
                pri = json.loads(row.get("priority_breakdown") or "{}")
            except Exception:
                cls, pri = {}, {}
            last = await auto_reply._last_reply_for_author(
                db, row.get("author") or "", row.get("source") or "twitter"
            )
            ok, reason = auto_reply.is_eligible(
                row, cls, pri, author_last_reply_at=last
            )
            if ok:
                eligible_ids.append(row["id"])
                # Sample one example per up-to-3 to show in the modal.
                if len(sample_replies) < 3:
                    from ..actions import twitter_reply as twr
                    sample_replies.append({
                        "post_id": row["id"],
                        "author":  row.get("author"),
                        "post_excerpt": (row.get("content") or "")[:140],
                        "reply_text":   twr.build_reply_text(row, cls, pri),
                    })
            else:
                # Bucket by playbook name when one applies; otherwise
                # by the eligibility reason (so "audience requires human"
                # rolls up cleanly).
                pb = playbooks.for_post(cls)
                if pb:
                    bucket = pb.get("name") or "Playbook"
                else:
                    # Map reasons to friendlier labels.
                    REASON_LABELS = {
                        "auto_action_safe=false": "Sensitive content (abuse / sarcasm / profanity)",
                        "audience_empty": "No audience tagged",
                    }
                    if reason.startswith("audience_requires_human:"):
                        bucket = "Audience needs human review"
                    elif reason.startswith("source_not_in_scope"):
                        bucket = "Reddit auto-reply (coming soon)"
                    elif reason.startswith("cooling_off"):
                        bucket = "Too fresh (under 2 min old)"
                    elif reason.startswith("author_throttled"):
                        bucket = "Author already replied to recently"
                    elif reason == "zomato_already_replied":
                        bucket = "Zomato already replied"
                    elif reason == "already_replied":
                        bucket = "Already auto-replied"
                    elif reason == "not_classified":
                        bucket = "Not yet classified"
                    elif reason == "priority_tripwire_override":
                        bucket = "Hard tripwire (priority override)"
                    else:
                        bucket = REASON_LABELS.get(reason, reason)
                needs_review_breakdown[bucket] = needs_review_breakdown.get(bucket, 0) + 1

    return {
        "total_overdue": len(rows),
        "eligible_count": len(eligible_ids),
        "needs_review_count": sum(needs_review_breakdown.values()),
        "eligible_post_ids": eligible_ids,
        "needs_review_breakdown": needs_review_breakdown,
        "sample_replies": sample_replies,
    }


@app.post("/api/drain/run")
async def drain_run(request: Request):
    """Start a drain job. Returns a job_id the client polls.

    Body::
        {
          "post_ids":     [...],            # required
          "throttle_sec": 1.0,              # default 1, clamped [1, 5]
          "operator":     "@you"            # for audit trail
        }
    """
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    post_ids: list[str] = list(body.get("post_ids") or [])
    if not post_ids:
        return JSONResponse({"ok": False, "error": "post_ids empty"}, status_code=400)
    if len(post_ids) > 200:
        return JSONResponse({"ok": False, "error": "post_ids exceeds 200 cap"}, status_code=400)
    throttle = float(body.get("throttle_sec") or 1.0)
    throttle = max(1.0, min(5.0, throttle))
    operator = (body.get("operator") or "anonymous").strip()[:120] or "anonymous"

    job_id = _new_job_id()
    job = {
        "id": job_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "operator": operator,
        "throttle_sec": throttle,
        "total": len(post_ids),
        "done": 0,
        "fired": 0,
        "skipped": 0,
        "failed": 0,
        "cancelled": False,
        "events": [],            # one entry per processed post
        "finished": False,
        "ended_at": None,
    }
    _drain_jobs[job_id] = job

    # Fire-and-forget the worker. The HTTP request returns immediately
    # with the job_id; the modal then polls /status/{job_id}.
    asyncio.create_task(_drain_worker(job_id, post_ids, throttle, operator))

    return {"ok": True, "job_id": job_id, "total": len(post_ids)}


def _new_job_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


async def _drain_worker(job_id: str, post_ids: list[str], throttle: float, operator: str) -> None:
    """The actual fire loop. Re-validates each post's eligibility
    server-side at fire time so a freshly-tripwired post (between
    preview and run) is silently skipped, not blasted."""
    from .. import auto_reply
    from ..actions import twitter_reply as twr

    job = _drain_jobs.get(job_id)
    if not job:
        return
    try:
        for idx, post_id in enumerate(post_ids):
            if job["cancelled"]:
                logger.info(f"[drain {job_id}] cancelled at {idx}/{len(post_ids)}")
                break
            event: dict[str, Any] = {"idx": idx, "post_id": post_id}
            try:
                async with aiosqlite.connect(str(config.DB_PATH)) as db:
                    db.row_factory = aiosqlite.Row
                    cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
                    row = await cur.fetchone()
                if not row:
                    event.update({"status": "skipped", "reason": "not_found"})
                    job["skipped"] += 1
                    continue
                row = dict(row)
                try:
                    cls = json.loads(row.get("classification") or "{}")
                    pri = json.loads(row.get("priority_breakdown") or "{}")
                except Exception:
                    cls, pri = {}, {}

                # Re-validate at fire time — defense against a fresh
                # tripwire landing between preview and run.
                async with aiosqlite.connect(str(config.DB_PATH)) as db:
                    last = await auto_reply._last_reply_for_author(
                        db, row.get("author") or "", row.get("source") or "twitter"
                    )
                ok, reason = auto_reply.is_eligible(
                    row, cls, pri, author_last_reply_at=last
                )
                if not ok:
                    event.update({"status": "skipped", "reason": reason})
                    job["skipped"] += 1
                    continue

                fired_at = datetime.now(timezone.utc)
                try:
                    payload, result = await twr.build_and_send(row, cls, pri)
                except Exception as e:
                    event.update({"status": "failed", "reason": f"exception:{type(e).__name__}"})
                    job["failed"] += 1
                    logger.exception(f"[drain {job_id}] {post_id}: build_and_send raised")
                    continue

                # Persist with the drain trigger marker.
                await auto_reply._persist_fire(
                    row, payload, result,
                    trigger="drain_v1",
                    eligibility_reason=f"drain by {operator}: {reason}",
                    fired_at=fired_at,
                )
                if result.get("ok"):
                    job["fired"] += 1
                    event.update({
                        "status":    "fired",
                        "reply_url": (result.get("reply_url") or ""),
                    })
                    async with aiosqlite.connect(str(config.DB_PATH)) as db:
                        await auto_reply._record_auto_reply(
                            db,
                            author=row.get("author") or "",
                            source=row.get("source") or "twitter",
                            post_id=post_id,
                            fired_at=fired_at,
                            trigger="drain_v1",
                        )
                else:
                    job["failed"] += 1
                    event.update({"status": "failed", "reason": result.get("error") or "send_failed"})
            except Exception as e:
                job["failed"] += 1
                event.update({"status": "failed", "reason": f"worker_exception:{type(e).__name__}"})
                logger.exception(f"[drain {job_id}] {post_id} unexpected error")
            finally:
                job["events"].append(event)
                job["done"] += 1
                # Throttle between fires (skip the last sleep — done is done).
                if idx < len(post_ids) - 1 and not job["cancelled"]:
                    await asyncio.sleep(throttle)
    finally:
        job["finished"] = True
        job["ended_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"[drain {job_id}] complete — fired={job['fired']} "
            f"skipped={job['skipped']} failed={job['failed']} "
            f"cancelled={job['cancelled']}"
        )


@app.get("/api/drain/status/{job_id}")
async def drain_status(job_id: str):
    """Polled by the modal every ~1 sec. Returns a snapshot of progress."""
    job = _drain_jobs.get(job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "job_not_found"}, status_code=404)
    # Echo a slim view; the events list is the heaviest part.
    return {
        "ok": True,
        "job_id": job_id,
        "total":     job["total"],
        "done":      job["done"],
        "fired":     job["fired"],
        "skipped":   job["skipped"],
        "failed":    job["failed"],
        "cancelled": job["cancelled"],
        "finished":  job["finished"],
        "started_at": job["started_at"],
        "ended_at":   job["ended_at"],
        "throttle_sec": job["throttle_sec"],
        # Latest 8 events so the modal can show a tail without growing
        # unboundedly. Earlier events are still in memory if needed.
        "events_tail": job["events"][-8:],
    }


@app.post("/api/drain/cancel/{job_id}")
async def drain_cancel(job_id: str):
    """Set the cancel flag — the worker will stop at the next iteration."""
    job = _drain_jobs.get(job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "job_not_found"}, status_code=404)
    job["cancelled"] = True
    return {"ok": True, "job_id": job_id, "cancelled": True}


@app.get("/api/actions/reddit_comment/{post_id:path}/preview")
async def preview_reddit_comment(post_id: str):
    """Preview the templated comment text for the modal. Mirror of the
    twitter_reply preview endpoint."""
    from ..actions import reddit_comment as rc
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    if (row["source"] or "").lower() != "reddit":
        return JSONResponse(
            {"ok": False, "error": "post is not from Reddit"},
            status_code=409,
        )
    post = dict(row)
    cls = json.loads(post.get("classification") or "{}")
    pri = json.loads(post.get("priority_breakdown") or "{}")
    text = rc.build_reply_text(post, cls, pri)
    return JSONResponse({
        "ok": True,
        "post_id": post_id,
        "reddit_url": post.get("url") or "",
        "post_author": post.get("author") or "",
        "post_excerpt": (post.get("content") or "")[:400],
        "comment_text": text,
        "configured": rc.is_configured(),
        "already_commented": "reddit_comment" in (post.get("action_taken") or ""),
    })


@app.post("/api/actions/reddit_comment/{post_id:path}")
async def fire_reddit_comment(post_id: str, request: Request):
    """Manual trigger from the dashboard's per-row "Comment on Reddit"
    button. Optional ``{text}`` body overrides the template.

    Optional playbook-bypass payload (Unblock & reply flow):
        {"force": true, "approver": "<name>", "reason": "<text>"}
    """
    from ..actions import dispatch_for_post, reddit_comment as rc
    from .. import playbooks

    override_text: str | None = None
    force = False
    approver: str | None = None
    reason:   str | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            t = body.get("text")
            if isinstance(t, str) and t.strip():
                override_text = t.strip()
            force    = bool(body.get("force"))
            approver = (body.get("approver") or "").strip() or None
            reason   = (body.get("reason") or "").strip() or None
    except Exception:
        pass

    if override_text is None:
        result = await dispatch_for_post(
            post_id, trigger="manual", channels=["reddit_comment"],
            force=force, force_approver=approver, force_reason=reason,
        )
        return JSONResponse(
            result,
            status_code=_action_status_to_http(result.get("status", "")),
        )

    # Operator-edited path — call comment_on_post directly, persist meta.
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
    if not row:
        return JSONResponse({"ok": False, "status": "skipped:not_found"}, status_code=404)
    post = dict(row)
    if (post.get("source") or "").lower() != "reddit":
        return JSONResponse(
            {"ok": False, "status": "skipped:bad_data", "error": "post is not from Reddit"},
            status_code=409,
        )
    if not rc.is_configured():
        return JSONResponse(
            {"ok": False, "status": "skipped:no_webhook", "error": "reddit credentials not configured"},
            status_code=503,
        )

    # Enforce playbook block on edited path too.
    cls_check = json.loads(post.get("classification") or "{}") if post.get("classification") else {}
    pb_check  = playbooks.for_post(cls_check)
    if pb_check and pb_check.get("block_auto_reply") and not force:
        return JSONResponse(
            {
                "ok": False, "post_id": post_id,
                "status": "blocked:playbook",
                "playbook":         pb_check.get("name"),
                "playbook_icon":    pb_check.get("icon"),
                "playbook_color":   pb_check.get("color"),
                "owner_team":       pb_check.get("owner_team"),
                "ack_deadline_min": pb_check.get("ack_deadline_min"),
                "banner":           pb_check.get("banner"),
                "required_steps":   pb_check.get("required_steps") or [],
                "channels_blocked": ["reddit_comment"],
                "reason": (
                    f"The {pb_check.get('name')} playbook blocks public replies. "
                    f"{pb_check.get('owner_team')} must approve before sending. "
                    f"Re-send with force=true after sign-off."
                ),
            },
            status_code=423,
        )

    reddit_url = post.get("url") or ""
    sent_at = datetime.now(timezone.utc).isoformat()
    result = await rc.comment_on_post(reddit_url, override_text)

    prior_meta_raw = post.get("action_meta") or "{}"
    try:
        prior_meta = json.loads(prior_meta_raw) or {}
    except Exception:
        prior_meta = {}
    prior_channels = prior_meta.get("channels") or {}
    payload = {
        "channel": "reddit_comment",
        "reddit_url": reddit_url,
        "comment_text": override_text,
        "operator_edited": True,
    }
    prior_channels["reddit_comment"] = {
        "ok": result.get("ok", False),
        "status": result.get("status", 0),
        "ts": result.get("ts") or sent_at,
        "error": result.get("error"),
        "payload": payload,
        "result": result,
    }
    new_meta = {
        **prior_meta,
        "fired_at": sent_at,
        "trigger": "manual_edit",
        "channels": prior_channels,
        "fired": sorted({n for n, r in prior_channels.items() if r.get("ok")}),
        "failed": sorted({n for n, r in prior_channels.items() if r.get("ok") is False}),
    }
    # Audit trail when a locked playbook was bypassed.
    if force and approver:
        new_meta["bypass_approved_by"] = approver
        new_meta["bypass_reason"]      = reason or "(no reason given)"
        new_meta["bypass_at"]          = sent_at
        new_meta["trigger"]            = "manual_edit_force"
    prior_taken = (post.get("action_taken") or "").split("+")
    union = sorted({s for s in prior_taken + new_meta["fired"] if s})
    action_taken = "+".join(union) if union else (post.get("action_taken") or "")

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        await db.execute(
            "UPDATE posts SET action_taken=?, action_meta=?, actioned_at=? WHERE id=?",
            (action_taken, json.dumps(new_meta, default=str), sent_at, post_id),
        )
        await db.commit()

    return JSONResponse(
        {
            "ok": result.get("ok", False),
            "post_id": post_id,
            "status": "fired" if result.get("ok") else "failed",
            "channels": ["reddit_comment"],
            "result": result,
        },
        status_code=200 if result.get("ok") else 502,
    )


@app.get("/api/actions/twitter_reply/{post_id:path}/preview")
async def preview_twitter_reply(post_id: str):
    """Preview the templated reply text for the modal.

    Returns the rule-based reply text the system *would* send. The
    operator sees this in the modal, can edit it, and ships the (maybe
    edited) text via POST. Single source of truth for the templates so
    the preview always matches what fires.
    """
    from ..actions import twitter_reply as twr
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    if (row["source"] or "").lower() != "twitter":
        return JSONResponse(
            {"ok": False, "error": "post is not from Twitter"},
            status_code=409,
        )
    post = dict(row)
    cls = json.loads(post.get("classification") or "{}")
    pri = json.loads(post.get("priority_breakdown") or "{}")
    text = twr.build_reply_text(post, cls, pri)
    return JSONResponse({
        "ok": True,
        "post_id": post_id,
        "tweet_url": post.get("url") or "",
        "tweet_author": post.get("author") or "",
        "tweet_excerpt": (post.get("content") or "")[:280],
        "reply_text": text,
        "configured": twr.is_configured(),
        "already_replied": "twitter_reply" in (post.get("action_taken") or ""),
    })


@app.post("/api/actions/twitter_reply/{post_id:path}")
async def fire_twitter_reply(post_id: str, request: Request):
    """Manual trigger from the dashboard's per-row "Reply on X" button.

    Bidirectional connector — turns Twitter from read-only into R+W.
    The modal's text is editable; if the request body contains
    ``{text: "..."}``, that text is sent verbatim. Otherwise the rule-
    based template is used.

    Optional playbook-bypass payload (Unblock & reply flow):
        {"force": true, "approver": "<name>", "reason": "<text>"}
    Sent only after the operator confirms a locked-playbook override
    in the modal. The dispatcher writes the audit fields to action_meta.

    Per-channel idempotency: re-clicking on a post whose Twitter reply
    already landed returns ``already_actioned``.
    """
    from ..actions import dispatch_for_post, twitter_reply as twr
    from .. import playbooks

    # Parse optional override text + force-bypass fields.
    override_text: str | None = None
    force = False
    approver: str | None = None
    reason:   str | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            t = body.get("text")
            if isinstance(t, str) and t.strip():
                override_text = t.strip()
            force    = bool(body.get("force"))
            approver = (body.get("approver") or "").strip() or None
            reason   = (body.get("reason") or "").strip() or None
    except Exception:
        # No body / not JSON — fine; fall through to template.
        pass

    if override_text is None:
        # Default path — dispatcher uses the template AND owns the
        # playbook-block guard + audit-trail persistence.
        result = await dispatch_for_post(
            post_id, trigger="manual", channels=["twitter_reply"],
            force=force, force_approver=approver, force_reason=reason,
        )
        return JSONResponse(
            result,
            status_code=_action_status_to_http(result.get("status", "")),
        )

    # Operator-edited path: ENFORCE playbook block here too. The dispatcher
    # owns this for templated replies; we replicate the same guard inline
    # so an edited reply on a death-claim post is just as safe.
    async with aiosqlite.connect(str(config.DB_PATH)) as db_check:
        db_check.row_factory = aiosqlite.Row
        cur = await db_check.execute(
            "SELECT classification FROM posts WHERE id = ?", (post_id,)
        )
        row_check = await cur.fetchone()
    cls_check = json.loads(row_check["classification"]) if (row_check and row_check["classification"]) else {}
    pb_check  = playbooks.for_post(cls_check)
    if pb_check and pb_check.get("block_auto_reply") and not force:
        return JSONResponse(
            {
                "ok": False, "post_id": post_id,
                "status": "blocked:playbook",
                "playbook":         pb_check.get("name"),
                "playbook_icon":    pb_check.get("icon"),
                "playbook_color":   pb_check.get("color"),
                "owner_team":       pb_check.get("owner_team"),
                "ack_deadline_min": pb_check.get("ack_deadline_min"),
                "banner":           pb_check.get("banner"),
                "required_steps":   pb_check.get("required_steps") or [],
                "channels_blocked": ["twitter_reply"],
                "reason": (
                    f"The {pb_check.get('name')} playbook blocks public replies. "
                    f"{pb_check.get('owner_team')} must approve before sending. "
                    f"Re-send with force=true after sign-off."
                ),
            },
            status_code=423,
        )

    # Operator-edited text path — bypass the templated build_and_send and
    # call reply_to_tweet directly so the user's exact words go out.
    # Still persist the action_meta + action_taken via the same merging
    # rules the dispatcher uses for explicit-channel triggers.
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
    if not row:
        return JSONResponse({"ok": False, "status": "skipped:not_found"}, status_code=404)

    post = dict(row)
    if (post.get("source") or "").lower() != "twitter":
        return JSONResponse(
            {"ok": False, "status": "skipped:bad_data", "error": "post is not from Twitter"},
            status_code=409,
        )
    if not twr.is_configured():
        return JSONResponse(
            {"ok": False, "status": "skipped:no_webhook", "error": "twitter cookies not configured"},
            status_code=503,
        )

    tweet_url = post.get("url") or ""
    sent_at = datetime.now(timezone.utc).isoformat()
    result = await twr.reply_to_tweet(tweet_url, override_text)

    # Persist into action_meta (merge with prior).
    prior_meta_raw = post.get("action_meta") or "{}"
    try:
        prior_meta = json.loads(prior_meta_raw) or {}
    except Exception:
        prior_meta = {}
    prior_channels = prior_meta.get("channels") or {}
    payload = {
        "channel": "twitter_reply",
        "tweet_url": tweet_url,
        "reply_text": override_text,
        "operator_edited": True,
    }
    prior_channels["twitter_reply"] = {
        "ok": result.get("ok", False),
        "status": result.get("status", 0),
        "ts": result.get("ts") or sent_at,
        "error": result.get("error"),
        "payload": payload,
        "result": result,
    }
    new_meta = {
        **prior_meta,
        "fired_at": sent_at,
        "trigger": "manual_edit",
        "channels": prior_channels,
        "fired": sorted({n for n, r in prior_channels.items() if r.get("ok")}),
        "failed": sorted({n for n, r in prior_channels.items() if r.get("ok") is False}),
    }
    # Audit trail when a locked playbook was bypassed.
    if force and approver:
        new_meta["bypass_approved_by"] = approver
        new_meta["bypass_reason"]      = reason or "(no reason given)"
        new_meta["bypass_at"]          = sent_at
        new_meta["trigger"]            = "manual_edit_force"
    prior_taken = (post.get("action_taken") or "").split("+")
    union = sorted({s for s in prior_taken + new_meta["fired"] if s})
    action_taken = "+".join(union) if union else (post.get("action_taken") or "")

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        await db.execute(
            "UPDATE posts SET action_taken=?, action_meta=?, actioned_at=? WHERE id=?",
            (action_taken, json.dumps(new_meta, default=str), sent_at, post_id),
        )
        await db.commit()

    return JSONResponse(
        {
            "ok": result.get("ok", False),
            "post_id": post_id,
            "status": "fired" if result.get("ok") else "failed",
            "channels": ["twitter_reply"],
            "result": result,
        },
        status_code=200 if result.get("ok") else 502,
    )


@app.post("/api/draft-reply/{post_id:path}")
async def draft_reply_endpoint(post_id: str):
    """Ask Claude to draft a customer-care reply for one post.

    Returns:
        200: { ok: true, reply, tone, channel, rationale, model, ts }
        404: { ok: false, error: "not_found" }
        503: { ok: false, error: "ANTHROPIC_API_KEY is not set" }
        500: { ok: false, error: <detailed message> }

    The endpoint is read-only — it does NOT post the reply anywhere. The
    frontend renders the draft in a modal where the operator can edit,
    copy, and ship it themselves. Keeps a human in the loop for every
    customer-facing message.
    """
    from .. import replies

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()

    if not row:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    post = dict(row)
    post["metadata"] = json.loads(post["metadata"]) if post["metadata"] else {}
    cls = json.loads(post["classification"]) if post["classification"] else {}
    pri = json.loads(post["priority_breakdown"]) if post["priority_breakdown"] else {}

    result = await replies.draft_reply(post, cls, pri)

    if not result.get("ok"):
        # Distinguish "not configured" from "actual API failure"
        err = (result.get("error") or "").lower()
        status_code = 503 if "not set" in err or "not installed" in err else 500
        return JSONResponse(result, status_code=status_code)

    return JSONResponse(result, status_code=200)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
