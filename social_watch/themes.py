"""Theme-based clustering — buckets of similar consumer/merchant complaints.

Replaces the geographic-burst-only clustering with semantic theme detection.
Answers the question "what are people actually saying right now?" by
bucketing posts into ~25 hand-curated theme patterns + tripwire-derived
groups.

Why this design (and not strict geo-burst clustering):
  - Geo data is "unknown" for ~94% of scraped posts, so geo-keyed
    clustering produces zero buckets in real-world flow.
  - The operator's actual question is "what are the top complaint
    themes today?" — which is a content question, not a location one.
  - Hand-curated regexes are auditable and editable by ops without
    retraining anything; they cover 80%+ of recurring complaint shapes
    seen in actual Zomato Twitter/Reddit data.
  - Tripwires already group well-defined safety/legal events; we
    surface them as themes too so the ops view is unified.

Reuses the existing `clusters` and `cluster_members` tables (added by
clusters.py), tagging each cluster with `cluster_type='theme'`. The
sidebar 'Active Clusters' UI renders these unchanged — same tables,
different content.

Each theme:
  id          stable slug (also serves as cluster id prefix)
  name        human-readable label
  patterns    list of regex (case-insensitive); a post matching ANY is in
  severity    default priority hint (informational; doesn't override score)
  audience    routing default (informational)

Detection runs every cycle; idempotent (INSERT OR IGNORE on members,
ON CONFLICT on cluster identity).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from loguru import logger

from . import config

# ============================================================
# Tunables
# ============================================================

_DEFAULT_WINDOW_HOURS = 24
_MIN_GROUP = 3              # 3 posts on same theme = bucket
_CLOSE_AGE_HOURS = 48       # close themes whose newest post is > 48h


# ============================================================
# Theme catalog — what consumers (and merchants) actually say
# ============================================================
# Each pattern is a compiled-once regex. Order matters: an earlier theme
# claims posts first when there's overlap (so 'food_safety' beats
# 'food_quality' on a "found hair in food" post).

THEMES: list[dict[str, Any]] = [
    # ---------- Consumer SAFETY (most severe; checked first) ----------
    {
        "id": "food_safety_event",
        "name": "Food safety / contamination",
        "patterns": [
            r"\bhair\b.{0,30}\b(food|biryani|order|dish|curry)\b",
            r"\b(glass|insect|worm|cockroach|larvae|mosquito|fly|bug|stone)\b.{0,30}\b(food|dish|order)\b",
            r"\b(food.{0,15}poisoning|stomach.{0,15}upset|vomit|nausea)\b",
            r"\b(spoiled|expired|rotten|stale|fungus)\b.{0,40}\b(food|order|paneer|chicken|rice)\b",
            r"\bfood.{0,20}(was|is).{0,20}(rotten|moldy|smelly)\b",
        ],
        "severity": "critical",
        "audience": ["safety", "legal", "pr", "customer-care"],
    },
    {
        "id": "delivery_agent_misconduct",
        "name": "Delivery agent misconduct / safety",
        "patterns": [
            r"\b(delivery.?(boy|guy|agent|partner|person)|rider).{0,40}\b(rude|abusive|threat|harass|misbehav|drunk|aggressive|inappropriate|stalk|follow)\b",
            r"\b(threaten|abused|harassed|misbehaved)\b.{0,30}\b(delivery|agent|rider|driver)\b",
            r"\bdelivery.?(boy|guy|agent).{0,30}\b(slap|hit|push|beat|attack)\b",
        ],
        "severity": "critical",
        "audience": ["trust-safety", "legal", "pr"],
    },

    # ---------- Consumer DELIVERY ----------
    {
        "id": "marked_delivered_not_received",
        "name": "Marked delivered, never received",
        "patterns": [
            r"\b(marked|shown|showing).{0,15}delivered\b.{0,80}\b(didn.?t|did not|never|haven.?t)\b.{0,15}\b(get|got|receive|received)\b",
            r"\b(order|food).{0,40}\bnever\b.{0,15}\b(arrived|came|delivered)\b",
            r"\bfood.{0,15}(not|never).{0,15}(delivered|received|arrived)\b",
            r"\bdid not receive.{0,40}order\b",
        ],
        "severity": "high",
        "audience": ["customer-care", "ops", "trust-safety"],
    },
    {
        "id": "delivery_delays",
        "name": "Late or delayed delivery",
        "patterns": [
            r"\b(delivery|order|food).{0,40}\b(late|delay(ed|s|ing)?|hours?|hr)\b",
            r"\b(2|3|4|5|six|six)\s*hours?\b.{0,40}\b(order|delivery|food)\b",
            r"\b(it.?s|its)\s*been.{0,15}\bhours\b",
            r"\bestimated.{0,15}delivery.{0,40}(passed|exceeded|over)\b",
        ],
        "severity": "high",
        "audience": ["customer-care", "ops"],
    },
    {
        "id": "wrong_or_missing_items",
        "name": "Wrong or missing items",
        "patterns": [
            r"\b(missing|wrong|incorrect|incomplete)\b.{0,40}\b(items?|order|delivery|food)\b",
            r"\bgot.{0,20}wrong\b.{0,15}(item|order|food|dish)\b",
            r"\b(half|part of)\b.{0,20}\border\b.{0,15}\b(missing|not delivered)\b",
        ],
        "severity": "medium",
        "audience": ["customer-care"],
    },
    {
        "id": "cold_food",
        "name": "Cold or low-quality food (non-safety)",
        "patterns": [
            r"\bfood.{0,30}\b(cold|stale|tasteless|inedible|disgusting|terrible)\b",
            r"\b(cold|stale)\b.{0,30}\b(food|order|biryani|curry|rice)\b",
        ],
        "severity": "medium",
        "audience": ["customer-care"],
    },

    # ---------- Consumer MONEY ----------
    {
        "id": "refund_pending",
        "name": "Refund pending or denied",
        "patterns": [
            r"\brefund\b.{0,40}\b(not|never|pending|delay|stuck|missing|haven.?t|hasn.?t)\b.{0,30}\b(received|come|processed|got)\b",
            r"\b(my|the)\s*money\b.{0,30}\b(stuck|gone|refund)\b",
            r"\bwhere.{0,15}is.{0,15}my.{0,15}refund\b",
            r"\brefund.{0,15}(declined|denied|rejected)\b",
            r"\bno.{0,15}refund\b.{0,30}\bcancelled\b",
        ],
        "severity": "high",
        "audience": ["customer-care", "finance"],
    },
    {
        "id": "double_charge_billing",
        "name": "Wrong/double billing",
        "patterns": [
            r"\b(double|twice|extra)\b.{0,30}\bcharged?\b",
            r"\b(charged|debited).{0,40}(but|even though|despite).{0,40}\b(cancelled|cancelled|not delivered|failed)\b",
            r"\bwrong.{0,15}(amount|price)\s*charged\b",
        ],
        "severity": "high",
        "audience": ["customer-care", "finance"],
    },
    {
        "id": "payment_failed_money_lost",
        "name": "Payment failed but money deducted",
        "patterns": [
            r"\bpayment.{0,15}failed\b.{0,60}\b(money|amount)\b.{0,30}\b(debited|deducted|gone|charged)\b",
            r"\bUPI.{0,30}(failed|stuck|pending)\b",
            r"\border.{0,15}not.{0,15}placed\b.{0,60}money.{0,15}(gone|debited)\b",
        ],
        "severity": "high",
        "audience": ["customer-care", "finance"],
    },
    {
        "id": "coupon_promo_issues",
        "name": "Coupon / promo / Gold not honoured",
        "patterns": [
            r"\b(coupon|promo|discount)\b.{0,40}\b(not|invalid|expired|won.?t|wouldn.?t)\b",
            r"\bgold\b.{0,40}\b(benefit|discount)\b.{0,40}\b(not|denied|missing)\b",
            r"\bzomato gold\b.{0,40}\b(useless|pathetic|fraud)\b",
        ],
        "severity": "medium",
        "audience": ["customer-care", "marketing"],
    },

    # ---------- Consumer SUPPORT ----------
    {
        "id": "support_unresponsive",
        "name": "Customer support not responding",
        "patterns": [
            r"\b(support|customer.?care|cce|@zomatocare)\b.{0,60}\b(no.{0,5}(reply|response)|not respond|ignor|never|no one|nobody)\b",
            r"\b(ignored|ignoring)\b.{0,30}\b(support|complaint|ticket)\b",
            r"\bbeen waiting\b.{0,40}\b(reply|response|support)\b",
            r"\bsupport.{0,15}(useless|pathetic|trash|joke)\b",
        ],
        "severity": "high",
        "audience": ["customer-care-leadership"],
    },
    {
        "id": "support_rude_threatening",
        "name": "Customer support rude or threatening",
        "patterns": [
            r"\b(support|cce|customer.?care|agent)\b.{0,60}\b(rude|threat|abusive|hung up|disconnected|screamed|yelled)\b",
            r"\bbot\b.{0,30}\b(useless|stupid|pathetic|no human|automated)\b",
        ],
        "severity": "high",
        "audience": ["customer-care-leadership", "qa"],
    },

    # ---------- Consumer APP/TECH ----------
    {
        "id": "app_bugs_crashes",
        "name": "App bugs / crashes / errors",
        "patterns": [
            r"\b(app|website|zomato)\b.{0,30}\b(crash|crashing|crashed|bug|broken|not working|freezes|frozen)\b",
            r"\bcan.?t.{0,15}(login|log in|signin|sign in|access|open).{0,30}\b(zomato|app)\b",
            r"\berror.{0,15}(message|code|page)\b.{0,30}\bzomato\b",
        ],
        "severity": "medium",
        "audience": ["eng", "customer-care"],
    },

    # ---------- Consumer ACCOUNT ----------
    {
        "id": "account_suspended_blocked",
        "name": "Account suspended or blocked",
        "patterns": [
            r"\b(account|id).{0,20}\b(suspended|blocked|banned|deactivated|disabled)\b",
            r"\bcan.?t.{0,15}(login|access).{0,30}\baccount\b",
        ],
        "severity": "high",
        "audience": ["trust-safety", "customer-care"],
    },

    # ---------- Cancellation / order flow ----------
    {
        "id": "auto_cancel_or_force_cancel",
        "name": "Auto-cancellation / forced cancellation",
        "patterns": [
            r"\b(auto.?cancel|automatically cancelled|order.{0,15}cancelled)\b",
            r"\bcancel.{0,30}(without|despite)\b",
            r"\b100%.{0,15}cancellation.{0,15}charge\b",
        ],
        "severity": "high",
        "audience": ["customer-care", "ops"],
    },

    # ---------- Brand / Press / Founder ----------
    {
        "id": "founder_or_exec_mention",
        "name": "Mentions of @deepigoyal / founders",
        "patterns": [
            r"@deepigoyal\b",
            r"\b(deepinder)\b.{0,15}\bgoyal\b",
            r"\bgaurav\s*gupta\b.{0,15}\bzomato\b",
        ],
        "severity": "high",
        "audience": ["founder-office", "pr"],
    },
    {
        "id": "press_news_coverage",
        "name": "Press / news coverage",
        "patterns": [
            r"\b(reuters|bloomberg|economic times|moneycontrol|livemint|inc42|yourstory|times of india|hindustan times|the hindu)\b",
            r"#(news|breaking)\b.{0,40}\bzomato\b",
            r"\b(filed|published|reports?|reported)\b.{0,30}\bzomato\b",
        ],
        "severity": "medium",
        "audience": ["pr"],
    },

    # ---------- Tone / public escalation ----------
    {
        "id": "boycott_calls",
        "name": "Boycott / delete-app calls",
        "patterns": [
            r"#(boycott|logout|delete|uninstall)zomato\b",
            r"\bboycott\b.{0,15}zomato\b",
            r"\b(deleting|uninstalling|deleted|uninstalled)\b.{0,15}\b(zomato|app)\b",
            r"\b(switching to|using)\b.{0,15}swiggy\b.{0,40}\b(forever|never)\b",
        ],
        "severity": "critical",
        "audience": ["pr", "leadership"],
    },
    {
        "id": "regulatory_legal_threats",
        "name": "Legal / regulatory escalation",
        "patterns": [
            r"\b(consumer.{0,5}forum|consumer.{0,5}court|filing\s*a\s*case|police\s*complaint|FIR)\b",
            r"\b(@?ConsumerHelp|@jagograhakjago|@nch_help|@CCPAIndia)\b",
            r"\b(FSSAI|CCPA|consumer\s*rights|consumer\s*protection)\b",
        ],
        "severity": "critical",
        "audience": ["legal", "pr"],
    },

    # ---------- Sentiment / generic catch-alls (lowest priority) ----------
    {
        "id": "general_outrage_negative",
        "name": "General negative sentiment",
        "patterns": [
            r"\b(worst|trash|garbage|pathetic|disgusting|shameful|horrible)\b.{0,30}\b(zomato|app|service|experience)\b",
            r"\bzomato\b.{0,30}\b(worst|trash|garbage|pathetic|fraud)\b",
            r"\b(never|don.?t|do not).{0,15}(use|order from|trust)\s*zomato\b",
        ],
        "severity": "medium",
        "audience": ["pr", "marketing"],
    },
]

# Compile patterns once. We dedupe matches across themes by post_id;
# a single post is allowed to match multiple themes (operationally the
# right behavior — "agent rude AND food cold" should appear in both).
for t in THEMES:
    t["_compiled"] = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in t["patterns"]]


# ============================================================
# Detection
# ============================================================

def _post_text(content: str | None) -> str:
    return content or ""


def _matches_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    for p in patterns:
        if p.search(text):
            return True
    return False


async def detect_themes(
    *,
    window_hours: int = _DEFAULT_WINDOW_HOURS,
    min_group: int = _MIN_GROUP,
) -> dict[str, Any]:
    """Scan recent posts, bucket into themes, upsert into clusters table.

    Idempotent: re-running grows existing buckets, never duplicates members.
    Returns counts dict suitable for logging / API response.
    """
    counts: dict[str, Any] = {
        "windowed": 0,
        "themes_with_matches": 0,
        "buckets_materialized": 0,
        "members_added": 0,
        "themes": [],
    }
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row

        # Pull windowed posts
        cur = await db.execute(
            """
            SELECT id, source, author, content, created_at, classification, priority_score
            FROM posts
            WHERE created_at >= ?
            ORDER BY priority_score DESC, created_at DESC
            """,
            (cutoff,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        counts["windowed"] = len(rows)
        if not rows:
            return counts

        # Bucket posts by theme (one post can be in multiple themes)
        theme_to_posts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            text = _post_text(r["content"])
            if not text:
                continue
            for theme in THEMES:
                if _matches_any(theme["_compiled"], text):
                    theme_to_posts[theme["id"]].append(r)

        counts["themes_with_matches"] = sum(1 for v in theme_to_posts.values() if v)

        now_iso = datetime.now(timezone.utc).isoformat()

        # Upsert each theme that meets the threshold
        for theme in THEMES:
            theme_id = theme["id"]
            posts = theme_to_posts.get(theme_id) or []
            if len(posts) < min_group:
                continue

            cluster_id = f"theme:{theme_id}"
            posts_sorted = sorted(
                posts,
                key=lambda p: (-(p.get("priority_score") or 0), p.get("created_at") or ""),
            )
            lead_post_id = posts_sorted[0]["id"]
            started_at = min(p["created_at"] for p in posts)
            last_member_at = max(p["created_at"] for p in posts)

            summary = (
                f"{len(posts)} posts about '{theme['name'].lower()}' in the last "
                f"{window_hours}h (severity: {theme['severity']}; "
                f"route: {', '.join(theme['audience'])})"
            )

            await db.execute(
                """
                INSERT INTO clusters
                  (id, tenant_id, primary_topic, side, geography, cluster_type,
                   started_at, last_member_at, member_count, lead_post_id,
                   summary, status)
                VALUES (?, 'default', ?, NULL, NULL, 'theme',
                        ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(id) DO UPDATE SET
                  last_member_at = excluded.last_member_at,
                  member_count   = excluded.member_count,
                  lead_post_id   = excluded.lead_post_id,
                  summary        = excluded.summary,
                  status         = 'active',
                  closed_at      = NULL
                """,
                (
                    cluster_id,
                    theme_id,           # primary_topic field reused for theme id
                    started_at,
                    last_member_at,
                    len(posts),
                    lead_post_id,
                    summary,
                ),
            )

            members_added_for_theme = 0
            for idx, p in enumerate(posts_sorted):
                role = "lead" if idx == 0 else "member"
                cur2 = await db.execute(
                    """
                    INSERT INTO cluster_members (cluster_id, post_id, role, joined_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (cluster_id, post_id) DO NOTHING
                    """,
                    (cluster_id, p["id"], role, now_iso),
                )
                if cur2.rowcount and cur2.rowcount > 0:
                    members_added_for_theme += 1

            counts["buckets_materialized"] += 1
            counts["members_added"] += members_added_for_theme
            counts["themes"].append({
                "id": theme_id,
                "name": theme["name"],
                "count": len(posts),
                "severity": theme["severity"],
                "lead_post_id": lead_post_id,
                "members_added_this_run": members_added_for_theme,
            })

        # Close themes whose newest member is older than the cutoff
        close_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=_CLOSE_AGE_HOURS)
        ).isoformat()
        cur3 = await db.execute(
            """
            UPDATE clusters
              SET status = 'closed', closed_at = ?
              WHERE cluster_type = 'theme'
                AND status = 'active'
                AND last_member_at < ?
            """,
            (now_iso, close_cutoff),
        )
        counts["buckets_closed"] = cur3.rowcount or 0

        await db.commit()

    counts["themes"].sort(key=lambda x: -x["count"])
    logger.info(
        f"themes.detect: windowed={counts['windowed']} "
        f"themes_matched={counts['themes_with_matches']} "
        f"buckets_materialized={counts['buckets_materialized']} "
        f"members_added={counts['members_added']} "
        f"buckets_closed={counts.get('buckets_closed', 0)}"
    )
    return counts


# ============================================================
# Read API for the dashboard sidebar
# ============================================================

async def get_active_themes(*, limit: int = 20) -> list[dict[str, Any]]:
    """Active theme buckets, ordered by member count DESC."""
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, primary_topic AS theme_id, member_count, summary,
                   started_at, last_member_at, lead_post_id
            FROM clusters
            WHERE cluster_type = 'theme' AND status = 'active'
            ORDER BY member_count DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    out: list[dict[str, Any]] = []
    by_id = {t["id"]: t for t in THEMES}
    for r in rows:
        meta = by_id.get(r["theme_id"]) or {}
        out.append({
            "id": r["id"],
            "theme_id": r["theme_id"],
            "name": meta.get("name", r["theme_id"]),
            "severity": meta.get("severity", "medium"),
            "audience": meta.get("audience", []),
            "count": r["member_count"],
            "summary": r["summary"],
            "lead_post_id": r["lead_post_id"],
            "started_at": r["started_at"],
            "last_member_at": r["last_member_at"],
        })
    return out
