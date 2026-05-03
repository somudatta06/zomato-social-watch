"""Daily executive briefing generator.

Produces a 9am-style memo summarising the last 24 hours: KPIs, top
themes, anomalies, emerging themes, and the top action items. The
output is data-first; the prose layer is optional and falls back to a
rule-based template when the LLM is rate-limited or unavailable.

Why data-first: a CMO reading at 9am cares about three numbers and
five pattern callouts. They don't read a 200-word polished essay; they
glance the metrics and zoom into what's unusual. The LLM is just gravy.

Public API:
    async def generate_brief() -> dict
        Returns the structured briefing dict that briefing.html renders.

Returned shape:
    {
        "generated_at": iso8601,
        "window": "last 24 hours",
        "headline": str,                          # one-liner highlight
        "key_metrics": {
            "mentions_today": int, "delta_pct_vs_yesterday": float,
            "p0_today": int, "overdue_now": int, "replied_today": int,
            "reply_rate_pct": float, "negative_pct": float,
        },
        "top_themes": [{name, severity, count, audience}, ...],   # top 5
        "anomalies": [{type, message, severity}, ...],
        "emerging_themes": [{name, today_count, yesterday_count}, ...],
        "top_risks": [str, ...],                  # 3 to 5 plain-language items
        "top_restaurants": [{name, count}, ...],  # top 3
        "top_cities": [{name, count, neg_pct}, ...],  # top 3
    }
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiosqlite
from loguru import logger

from . import config

IST = ZoneInfo("Asia/Kolkata")


def _humanize_delta(delta_pct: float) -> str:
    if abs(delta_pct) < 0.5:
        return "flat vs yesterday"
    sign = "up" if delta_pct > 0 else "down"
    return f"{sign} {abs(delta_pct):.0f}% vs yesterday"


async def _kpi_metrics(db: aiosqlite.Connection) -> dict[str, Any]:
    """Calendar-today vs calendar-yesterday KPIs (IST anchored)."""
    db.row_factory = aiosqlite.Row
    now_utc = datetime.now(timezone.utc)
    today_start_ist = now_utc.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = today_start_ist.astimezone(timezone.utc).isoformat()
    yest_start = (today_start_ist - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    stale_cutoff = (now_utc - timedelta(hours=2)).isoformat()

    async def scalar(sql: str, params: tuple = ()) -> int:
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return (row[0] if row else 0) or 0

    mentions_today = await scalar(
        "SELECT COUNT(*) FROM posts WHERE created_at >= ? AND noise_category IS NULL",
        (today_iso,),
    )
    mentions_yesterday = await scalar(
        "SELECT COUNT(*) FROM posts WHERE created_at >= ? AND created_at < ? "
        "AND noise_category IS NULL",
        (yest_start, today_iso),
    )
    delta_abs = mentions_today - mentions_yesterday
    delta_pct = (delta_abs * 100 / mentions_yesterday) if mentions_yesterday > 0 else 0.0

    p0_today = await scalar(
        "SELECT COUNT(*) FROM posts WHERE created_at >= ? "
        "AND priority_band = 'P0' AND noise_category IS NULL",
        (today_iso,),
    )
    overdue_now = await scalar(
        "SELECT COUNT(*) FROM posts WHERE "
        "json_extract(classification, '$.urgency') IN ('critical','high') "
        "AND created_at < ? AND zomato_response_status IN ('unchecked','no_reply') "
        "AND noise_category IS NULL",
        (stale_cutoff,),
    )
    replied_today = await scalar(
        "SELECT COUNT(*) FROM posts WHERE created_at >= ? "
        "AND zomato_response_status = 'replied' AND noise_category IS NULL",
        (today_iso,),
    )
    expected = await scalar(
        "SELECT COUNT(*) FROM posts WHERE created_at >= ? "
        "AND zomato_response_status IN ('replied','unchecked','no_reply') "
        "AND noise_category IS NULL",
        (today_iso,),
    )
    reply_rate = (replied_today * 100 / expected) if expected > 0 else 0.0
    negative_today = await scalar(
        "SELECT COUNT(*) FROM posts WHERE created_at >= ? AND noise_category IS NULL "
        "AND json_extract(classification, '$.sentiment') IN ('negative','abusive')",
        (today_iso,),
    )
    neg_pct = (negative_today * 100 / mentions_today) if mentions_today > 0 else 0.0

    return {
        "mentions_today": mentions_today,
        "mentions_yesterday": mentions_yesterday,
        "delta_pct_vs_yesterday": round(delta_pct, 1),
        "delta_abs": delta_abs,
        "p0_today": p0_today,
        "overdue_now": overdue_now,
        "replied_today": replied_today,
        "reply_rate_pct": round(reply_rate, 1),
        "negative_pct": round(neg_pct, 1),
    }


async def _top_themes(db: aiosqlite.Connection, limit: int = 5) -> list[dict[str, Any]]:
    """Top active themes with member count and severity."""
    try:
        cur = await db.execute(
            "SELECT id, primary_topic, summary, member_count, cluster_type "
            "FROM clusters WHERE status = 'active' "
            "ORDER BY member_count DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]
    except Exception:
        return []


async def _anomalies_and_emerging(db: aiosqlite.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect anomalies (volume spikes vs 7-day avg) and emerging themes
    (themes with posts today but ~0 yesterday).
    """
    db.row_factory = aiosqlite.Row
    now_utc = datetime.now(timezone.utc)
    today_iso = now_utc.astimezone(IST).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc).isoformat()
    seven_days_iso = (now_utc - timedelta(days=7)).isoformat()
    yest_start = (now_utc.astimezone(IST).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)).astimezone(timezone.utc).isoformat()

    anomalies: list[dict[str, Any]] = []

    # 1. Volume anomaly: today vs 7-day average
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? AND noise_category IS NULL",
        (today_iso,),
    )
    today_count = (await cur.fetchone())["c"]
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? AND created_at < ? "
        "AND noise_category IS NULL",
        (seven_days_iso, today_iso),
    )
    week_total = (await cur.fetchone())["c"]
    week_avg = week_total / 7 if week_total else 0
    if week_avg > 0:
        ratio = today_count / week_avg
        if ratio > 1.5:
            anomalies.append({
                "type": "volume_spike",
                "severity": "high" if ratio > 2.0 else "medium",
                "message": (
                    f"Volume up {ratio:.1f}x vs 7-day average "
                    f"({today_count} today vs {week_avg:.0f} typical)."
                ),
            })
        elif ratio < 0.5:
            anomalies.append({
                "type": "volume_lull",
                "severity": "low",
                "message": (
                    f"Volume down to {ratio:.1f}x of 7-day average. "
                    "Verify scrapers are healthy."
                ),
            })

    # 2. P0 spike
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? "
        "AND priority_band = 'P0' AND noise_category IS NULL",
        (today_iso,),
    )
    p0_today = (await cur.fetchone())["c"]
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? AND created_at < ? "
        "AND priority_band = 'P0' AND noise_category IS NULL",
        (seven_days_iso, today_iso),
    )
    p0_week = (await cur.fetchone())["c"]
    p0_avg = p0_week / 7 if p0_week else 0
    if p0_avg > 0 and p0_today > p0_avg * 1.5:
        anomalies.append({
            "type": "p0_spike",
            "severity": "high",
            "message": (
                f"P0 count is {p0_today} today vs {p0_avg:.0f} typical. "
                f"({(p0_today / p0_avg):.1f}x normal)."
            ),
        })

    # 3. Negative sentiment surge
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE created_at >= ? "
        "AND noise_category IS NULL "
        "AND json_extract(classification, '$.sentiment') IN ('negative','abusive')",
        (today_iso,),
    )
    neg_today = (await cur.fetchone())["c"]
    if today_count > 0:
        neg_pct = neg_today * 100 / today_count
        if neg_pct > 50:
            anomalies.append({
                "type": "sentiment_surge",
                "severity": "high",
                "message": f"{neg_pct:.0f}% of today's posts are negative or abusive (typical 30 to 40%).",
            })

    # 4. Emerging themes: clusters with last_member_at >= today_start
    #    and a low pre-today member count.
    emerging: list[dict[str, Any]] = []
    try:
        cur = await db.execute(
            """
            SELECT
                c.id,
                c.primary_topic,
                c.member_count,
                (SELECT COUNT(*) FROM cluster_members cm
                  JOIN posts p ON p.id = cm.post_id
                  WHERE cm.cluster_id = c.id AND p.created_at >= ?) AS today_count,
                (SELECT COUNT(*) FROM cluster_members cm
                  JOIN posts p ON p.id = cm.post_id
                  WHERE cm.cluster_id = c.id AND p.created_at >= ? AND p.created_at < ?) AS yesterday_count
            FROM clusters c
            WHERE c.status = 'active'
            ORDER BY today_count DESC
            LIMIT 8
            """,
            (today_iso, yest_start, today_iso),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            if r["today_count"] >= 3 and r["yesterday_count"] == 0:
                emerging.append({
                    "id": r["id"],
                    "name": r["primary_topic"],
                    "today_count": r["today_count"],
                    "yesterday_count": r["yesterday_count"],
                })
    except Exception:
        pass

    return anomalies, emerging


async def _top_restaurants_and_cities(db: aiosqlite.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pull the top 3 complained restaurants and cities from the last
    24 hours using the same extraction logic as the operations page.
    """
    try:
        from . import extraction as extr
        from . import restaurant_canon as canon
    except Exception:
        return [], []

    db.row_factory = aiosqlite.Row
    now_utc = datetime.now(timezone.utc)
    since = (now_utc - timedelta(days=1)).isoformat()
    cur = await db.execute(
        "SELECT content, classification, priority_band FROM posts "
        "WHERE created_at >= ? AND noise_category IS NULL",
        (since,),
    )
    rows = [dict(r) for r in await cur.fetchall()]

    rest_counts: Counter = Counter()
    city_counts: Counter = Counter()
    city_neg: Counter = Counter()
    for r in rows:
        text = r.get("content") or ""
        # Canonicalize before counting so brand aliases merge and
        # platform self-references / parser garbage drop out.
        for name in canon.canonicalize_many(extr.extract_restaurants(text)):
            rest_counts[name] += 1
        for c in extr.extract_cities(text):
            city_counts[c] += 1
            cls = json.loads(r.get("classification") or "{}")
            if cls.get("sentiment") in ("negative", "abusive"):
                city_neg[c] += 1

    rest_top = [
        {"name": n, "count": c}
        for n, c in rest_counts.most_common(3)
    ]
    city_top = [
        {
            "name": c,
            "count": city_counts[c],
            "neg_pct": round(city_neg[c] * 100 / city_counts[c]) if city_counts[c] else 0,
        }
        for c, _ in city_counts.most_common(3)
    ]
    return rest_top, city_top


def _compose_headline(metrics: dict[str, Any], anomalies: list[dict[str, Any]]) -> str:
    """One-liner that frames the day. Picks the strongest signal."""
    if any(a["severity"] == "high" for a in anomalies):
        first_high = next(a for a in anomalies if a["severity"] == "high")
        return first_high["message"]
    if metrics["overdue_now"] >= 50:
        return f"{metrics['overdue_now']} critical posts unanswered for over 2 hours. Action queue needs attention."
    if metrics["p0_today"] >= 30:
        return (
            f"{metrics['p0_today']} P0 posts today, {_humanize_delta(metrics['delta_pct_vs_yesterday'])}. "
            f"Reply rate at {metrics['reply_rate_pct']:.0f}%."
        )
    if metrics["delta_pct_vs_yesterday"] > 25:
        return (
            f"Volume up {metrics['delta_pct_vs_yesterday']:.0f}% vs yesterday. "
            "Keep an eye on the queue."
        )
    return (
        f"{metrics['mentions_today']} mentions today ({_humanize_delta(metrics['delta_pct_vs_yesterday'])}), "
        f"{metrics['p0_today']} critical, {metrics['reply_rate_pct']:.0f}% reply rate."
    )


def _compose_top_risks(
    metrics: dict[str, Any],
    anomalies: list[dict[str, Any]],
    emerging: list[dict[str, Any]],
    top_themes: list[dict[str, Any]],
    top_restaurants: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Produce 3 to 5 action items, each {text, link}, in priority order.

    The link is where clicking the risk row should take the operator —
    typically the queue or filtered inbox view that lets them act on it.
    """
    OVERDUE_LINK = "/inbox?overdue=1&window=week&sort=created"
    risks: list[dict[str, str]] = []
    # Anomalies are highest priority. Most anomalies are volume/queue-related,
    # so the overdue queue is the right default action.
    for a in anomalies:
        if a["severity"] == "high":
            risks.append({"text": a["message"], "link": OVERDUE_LINK})
    # Overdue queue
    if metrics["overdue_now"] >= 30:
        risks.append({
            "text": (
                f"Overdue queue at {metrics['overdue_now']}. "
                "Triage immediately, oldest first."
            ),
            "link": OVERDUE_LINK,
        })
    # Emerging themes — link to the cluster if we have its id
    for e in emerging[:2]:
        topic = (e["name"] or "unnamed").replace("_", " ").title()
        link = (
            f"/inbox?cluster_id={e['id']}&page=1"
            if e.get("id")
            else f"/inbox?q={quote(e['name'] or '')}&window=day"
        )
        risks.append({
            "text": f"Emerging theme '{topic}' jumped from 0 to {e['today_count']} posts today.",
            "link": link,
        })
    # Top themes if no anomalies surfaced
    if not risks and top_themes:
        t = top_themes[0]
        topic = (t.get("primary_topic") or "unnamed").replace("_", " ").title()
        risks.append({
            "text": f"Top active theme: '{topic}' with {t['member_count']} members. Review the cluster.",
            "link": f"/inbox?cluster_id={t['id']}&page=1" if t.get("id") else "/inbox",
        })
    # Restaurant risk
    if top_restaurants and top_restaurants[0]["count"] >= 10:
        r = top_restaurants[0]
        risks.append({
            "text": f"Restaurant '{r['name']}' has {r['count']} complaints in 24h. Consider partner audit.",
            "link": f"/inbox?q={quote(r['name'])}&window=week",
        })
    # Reply rate falling
    if metrics["reply_rate_pct"] < 30 and metrics["mentions_today"] > 100:
        risks.append({
            "text": (
                f"Reply rate at {metrics['reply_rate_pct']:.0f}%. "
                "Customer-care queue may be falling behind."
            ),
            "link": OVERDUE_LINK,
        })
    return risks[:5]


async def generate_brief() -> dict[str, Any]:
    """Produce the morning briefing. Always returns a populated dict."""
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        metrics = await _kpi_metrics(db)
        themes = await _top_themes(db, limit=5)
        anomalies, emerging = await _anomalies_and_emerging(db)
        top_restaurants, top_cities = await _top_restaurants_and_cities(db)

    headline = _compose_headline(metrics, anomalies)
    top_risks = _compose_top_risks(metrics, anomalies, emerging, themes, top_restaurants)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": "last 24 hours",
        "headline": headline,
        "key_metrics": metrics,
        "top_themes": [
            {
                "id": t.get("id"),
                "name": (t.get("primary_topic") or "unnamed").replace("_", " ").title(),
                "summary": t.get("summary") or "",
                "member_count": t.get("member_count", 0),
            }
            for t in themes
        ],
        "anomalies": anomalies,
        "emerging_themes": emerging,
        "top_risks": top_risks,
        "top_restaurants": top_restaurants,
        "top_cities": top_cities,
    }


__all__ = ["generate_brief"]
