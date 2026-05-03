"""Priority scoring — the "what to tackle first" engine.

Implements §3 of `docs/CLASSIFICATION_DEEP_DIVE.md`: an 8-signal weighted
scalar that ranks every post 0–1, banded into P0–P3, with tripwires
forcing P0 regardless of score.

Hand-tuned over ML by design: every weight is auditable, the social-team
lead can adjust without retraining, ships in a day instead of six weeks.
ML layers on top *after* months of operator-corrected priorities.

For v1 (this file), some signals are stubbed at 0 because the data isn't
there yet (engagement velocity, cross-channel handle linking, counter-
narrative). The architecture supports them; they activate when the inputs
land. v1 carries the four signals that matter most:

    severity (30%) · reach (20%) · sla_proximity (10%) · repeat (10%)

Stubbed-at-zero signals don't break the math — the score is still a
valid 0–1 ranking, just compressed into the upper end of its range. As
v2 features land, the score expands toward the full 0–1 spread.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

# Forward import for type hints only — handles.py imports priority indirectly
# through pipeline.py, so we avoid a hard import here.
try:
    import aiosqlite as _aiosqlite_for_typing  # noqa: F401
except ImportError:
    pass

# ============================================================
# Weights — see §3.1 of the deep-dive doc for the rationale
# ============================================================
_WEIGHTS = {
    "severity":      0.30,
    "reach":         0.20,
    "velocity":      0.15,
    "sla_proximity": 0.10,
    "repeat":        0.10,
    "cross_channel": 0.08,
    "trust":         0.04,
    "counter":       0.03,
}

# SLA acknowledge-by minutes per urgency band. Industry medians.
# (Resolve-by is a separate, longer SLA — not used in score.)
_SLA_MINUTES_ACK = {
    "critical": 15,
    "high":     30,
    "medium":   120,
    "low":      480,
}

# ============================================================
# Tripwire severity tiers
# ============================================================
# Hard tripwires force P0 regardless of score (real emergencies — health,
# legal, safety, viral firestorm).
#
# Soft / "elevated" tripwires are signals that raise priority but should
# NOT force P0. A consumer @-tagging the founder is common — every angry
# customer does it — and treating each one as a 15-minute emergency floods
# the queue with P0 noise. Instead we boost the post's priority score by
# ELEVATED_BOOST and let the rest of the signals decide the actual band.
ELEVATED_TRIPWIRES: set[str] = {
    "founder_mention",
    "journalist_handle",
    "politician_handle",
    "boycott_coordinated",
}
ELEVATED_BOOST = 0.18  # raises base score; band threshold determines outcome

# Severity-tier multipliers (when sub_claims provide L1–L5).
_TIER_MAP = {"L1": 0.2, "L2": 0.4, "L3": 0.6, "L4": 0.8, "L5": 1.0}

# Reach normalization baseline. log10(1+P95) — set to 1000 engagement
# units; updated nightly from observed distribution in v2.
_REACH_LOG_BASELINE = math.log10(1 + 1000)


# ============================================================
# Author multiplier lookup — Phase γ
# ============================================================
async def get_author_multiplier(
    handle: str | None, source: str, conn: Any = None
) -> float:
    """Read multiplier from the `handles` table for (handle, source).

    Defaults to 1.0 (no boost) when the handle hasn't been classified yet,
    so backwards-compat is preserved: before Phase γ runs, everyone is T5.

    `conn` is an open aiosqlite.Connection. If None, opens a one-shot
    connection — which is fine for ad-hoc callers but the pipeline always
    passes its own conn for the hot loop.
    """
    if not handle:
        return 1.0
    h = handle.strip().lstrip("@").lower()
    if not h:
        return 1.0

    # Avoid cross-module circular imports by lazy-loading the storage path
    if conn is None:
        try:
            import aiosqlite
            from . import config
            async with aiosqlite.connect(str(config.DB_PATH)) as db:
                cur = await db.execute(
                    "SELECT multiplier FROM handles WHERE handle = ? AND source = ?",
                    (h, source),
                )
                row = await cur.fetchone()
        except Exception:
            return 1.0
    else:
        try:
            cur = await conn.execute(
                "SELECT multiplier FROM handles WHERE handle = ? AND source = ?",
                (h, source),
            )
            row = await cur.fetchone()
        except Exception:
            return 1.0

    if row and row[0] is not None:
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return 1.0
    return 1.0


# ============================================================
# Banding
# ============================================================
def band_for_score(score: float) -> str:
    """P0–P3 from the score. See §9 of deep-dive doc for SLA targets.

    Thresholds are calibrated for the v1 signal mix where 30% of weights
    (velocity, cross_channel, trust, counter) are stubbed at 0 — so the
    practical ceiling for non-tripwire posts is ~0.65. P0 is reserved
    for hard tripwire overrides (food safety, death, court FIR, etc.).
    When the stub signals come online in v2, raise P1 back to 0.65
    and P2 back to 0.40.
    """
    if score >= 0.85:
        return "P0"
    if score >= 0.55:
        return "P1"
    if score >= 0.35:
        return "P2"
    return "P3"


# ============================================================
# Per-signal computation — each returns 0–1 BEFORE weighting
# ============================================================

def _signal_severity(classification: dict[str, Any]) -> float:
    """Topic urgency × severity-tier multiplier. Multi-claim takes max."""
    urg = float(classification.get("urgency_score") or 0.0)
    sub = classification.get("sub_claims") or []
    if sub:
        max_tier = max(
            (_TIER_MAP.get((s.get("severity") or "").upper(), 0.0) for s in sub),
            default=0.0,
        )
        return max(urg, max_tier)
    return urg


def _signal_reach(metadata: dict[str, Any], source: str, author_tier: float = 1.0) -> float:
    """Log-normalized engagement × author_tier multiplier (capped at 1.0).

    Phase γ: when author_tier > 1.0 (press, politician, influencer...), we
    apply a "tier floor" so that even a zero-engagement post from a high-
    reach handle still surfaces. Rationale: a press handle's REACH is their
    follower base, not the post's current likes. A tweet with 0 likes from
    @inc42 is still potentially-viral; ranking it at reach=0 misses that.
    The floor is small (multiplier/20 so 5× → 0.25 floor, 10× → 0.50 floor)
    so it doesn't dominate for ordinary users (1× → 0 floor)."""
    if source == "twitter":
        likes = int(metadata.get("like_count") or 0)
        retweets = int(metadata.get("retweet_count") or 0)
        replies = int(metadata.get("reply_count") or 0)
        engagement = likes + 5 * retweets + 0.5 * replies
    elif source == "reddit":
        score = int(metadata.get("score") or 0)
        comments = int(metadata.get("num_comments") or 0)
        engagement = max(0, score) + comments
    else:
        engagement = 0

    # Tier-only floor for high-reach authors (no effect at multiplier=1.0)
    floor = max(0.0, (author_tier - 1.0) / 20.0)

    if engagement <= 0:
        return min(1.0, floor)
    norm = math.log10(1 + engagement) / _REACH_LOG_BASELINE
    return min(1.0, max(floor, norm * author_tier))


def _signal_velocity(
    prev_engagement: int | None,
    current_engagement: int | None,
    age_minutes: float | None,
) -> float:
    """Engagement growth rate; 1.0 at >=3× baseline. v1 stub: returns 0
    until per-post engagement snapshots land in v2."""
    if (
        prev_engagement is None
        or current_engagement is None
        or age_minutes is None
        or age_minutes <= 0
    ):
        return 0.0
    delta = max(0, current_engagement - prev_engagement)
    growth_per_hour = (delta / age_minutes) * 60
    # 3× baseline (~10 engagements/hr for an average post) → score 1.0
    return min(1.0, growth_per_hour / 30.0)


def _signal_sla_proximity(created_at_iso: str | None, urgency: str | None) -> float:
    """0 (just posted) → 1.0 (deadline reached or passed)."""
    if not created_at_iso:
        return 0.0
    sla_min = _SLA_MINUTES_ACK.get((urgency or "low").lower(), 480)
    try:
        ts = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    if age_min < 0:
        return 0.0
    return min(1.0, age_min / sla_min)


def _signal_repeat(prior_complaints: int) -> float:
    """0 (first), 0.33 (2nd), 0.67 (3rd), 1.0 (4th+). Patience-thinning."""
    if prior_complaints <= 1:
        return 0.0
    return min(1.0, (prior_complaints - 1) / 3)


def _signal_cross_channel(direct_match: bool, inferred_match: bool) -> float:
    """1.0 if same handle on both sources, 0.7 if inferred via embeddings.
    v1 stub: returns 0 until handle-linking lands in v2."""
    if direct_match:
        return 1.0
    if inferred_match:
        return 0.7
    return 0.0


def _signal_trust(handle_age_days: int | None = None, suspected_bot: bool = False) -> float:
    """Inversely weighted: low trust → small de-priority via the WEIGHT.
    Returns the *trust deficit* (1 - trust_score), so the contribution is
    NEGATIVE when applied (handled in compute()). Capped at 0.04 of total.

    v1 stub: returns 0 (no de-priority) for legitimate-looking handles.
    """
    if suspected_bot:
        return 1.0
    return 0.0


def _signal_counter_narrative(positive_reply_ratio: float = 0.0, total_replies: int = 0) -> float:
    """If happy users defending us in same thread, slight de-priority.
    v1 stub: returns 0 until thread-reply classification lands in v2."""
    if total_replies < 5:
        return 0.0
    if positive_reply_ratio > 0.6:
        return 1.0
    return 0.0


# ============================================================
# Public entry
# ============================================================
def compute_priority(
    post: dict[str, Any],
    classification: dict[str, Any],
    *,
    prior_complaints: int = 0,
    author_tier: float | None = None,
    author_multiplier: float = 1.0,
    velocity_inputs: tuple[int, int, float] | None = None,
    velocity_score: float = 0.0,
    cross_channel_direct: bool = False,
    cross_channel_inferred: bool = False,
    suspected_bot: bool = False,
    counter_ratio: float = 0.0,
    counter_total: int = 0,
) -> dict[str, Any]:
    """Compute priority score + band for one post.

    Args:
        post: row dict with at least source, created_at, metadata (json or dict)
        classification: parsed classification dict (urgency, urgency_score,
                        tripwires_fired, sub_claims, sentiment, etc.)
        prior_complaints: number of prior complaints from this handle
        author_multiplier: 1.0–10.0 reach multiplier from the handles table
                          (regular=1, press=5, authority/founder=10).
                          Phase γ wires this up; before then the default
                          1.0 keeps every post at baseline reach.
        author_tier: deprecated alias for author_multiplier — kept so older
                    callers don't break. If both are passed, author_tier wins.
        velocity_inputs: (prev_eng, current_eng, age_min) tuple if available
                        — legacy raw-input path.
        velocity_score: precomputed 0–1 velocity from the snapshot pipeline
                        (`social_watch.velocity.compute_velocity_score`).
                        When >0, takes precedence over velocity_inputs.
                        Default 0.0 preserves the original behavior for
                        callers that haven't wired up snapshots yet.
        cross_channel_direct: True if same handle on both Reddit and X
        cross_channel_inferred: True if embedding-similar across sources
        suspected_bot: True if author looks like a bot
        counter_ratio: positive_replies / total_replies in thread
        counter_total: total reply count

    Returns:
        {
            "score": float 0–1,
            "band":  "P0" | "P1" | "P2" | "P3",
            "signals": dict of signal_id → 0–1 raw value,
            "contributions": dict of signal_id → weighted contribution,
            "tripwire_override": bool,
            "reason": str (top-line explanation),
        }
    """
    # Backwards compat: callers that still pass author_tier work unchanged.
    if author_tier is not None:
        author_multiplier = author_tier
    # Tripwire handling — split into hard P0 override vs soft elevation.
    # A "hard" tripwire (food safety, court FIR, death claim, etc.) forces
    # P0. A "soft" tripwire (founder mention, journalist mention, etc.) is
    # a signal we boost the score with but don't auto-escalate, otherwise
    # every angry consumer @-tagging @deepigoyal floods the P0 queue.
    fired = classification.get("tripwires_fired") or []
    hard_fired = [t for t in fired if t not in ELEVATED_TRIPWIRES]
    soft_fired = [t for t in fired if t in ELEVATED_TRIPWIRES]
    if hard_fired:
        return {
            "score": 1.0,
            "band": "P0",
            "signals": {},
            "contributions": {},
            "tripwire_override": True,
            "reason": f"tripwire override: {', '.join(hard_fired)}",
        }
    # Soft tripwires fall through to the regular signal computation; the
    # ELEVATED_BOOST is added to the final score below.

    # Parse metadata if it's a JSON string (DB rows arrive as strings)
    metadata = post.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    # Velocity: prefer the precomputed snapshot-based score (Phase δ
    # `social_watch/velocity.py`). Fall back to the legacy raw-input
    # path so older callers keep working.
    if velocity_score and velocity_score > 0:
        velocity_raw = float(min(1.0, max(0.0, velocity_score)))
    else:
        velocity_raw = _signal_velocity(*(velocity_inputs or (None, None, None)))

    # Compute each signal (0–1 raw)
    raw = {
        "severity":      _signal_severity(classification),
        "reach":         _signal_reach(metadata, post.get("source", ""), author_multiplier),
        "velocity":      velocity_raw,
        "sla_proximity": _signal_sla_proximity(post.get("created_at"), classification.get("urgency")),
        "repeat":        _signal_repeat(prior_complaints),
        "cross_channel": _signal_cross_channel(cross_channel_direct, cross_channel_inferred),
        "trust":         _signal_trust(suspected_bot=suspected_bot),
        "counter":       _signal_counter_narrative(counter_ratio, counter_total),
    }

    # Apply weights — note: trust and counter are NEGATIVE contributors.
    contributions: dict[str, float] = {}
    for k, v in raw.items():
        sign = -1 if k in ("trust", "counter") else 1
        contributions[k] = round(sign * v * _WEIGHTS[k], 4)

    score = sum(contributions.values())

    # Soft tripwire elevation — boost the score (but don't auto-P0). This
    # makes founder/journalist/politician/boycott mentions land in P1-P2
    # range instead of P3, without flooding the P0 queue with @deepigoyal
    # tags from every angry customer.
    if soft_fired:
        score += ELEVATED_BOOST
        contributions["soft_tripwire"] = round(ELEVATED_BOOST, 4)

    score = max(0.0, min(1.0, score))

    # Top contributor for the "why" reason string
    pos_contribs = [(k, v) for k, v in contributions.items() if v > 0]
    pos_contribs.sort(key=lambda kv: -kv[1])
    top = ", ".join(f"{k}={v:.2f}" for k, v in pos_contribs[:3])

    reason = f"top contributors: {top}" if top else "low-signal post"
    if soft_fired:
        reason = f"soft elevation ({', '.join(soft_fired)}) + {reason}"

    return {
        "score": round(score, 4),
        "band": band_for_score(score),
        "signals": {k: round(v, 4) for k, v in raw.items()},
        "contributions": contributions,
        "tripwire_override": False,
        "reason": reason,
    }


# Convenience for the dashboard tooltip
def explain(priority: dict[str, Any]) -> str:
    """Render a multi-line, human-readable breakdown of a priority result."""
    if priority.get("tripwire_override"):
        return priority.get("reason", "tripwire override")
    lines = [f"score={priority['score']:.2f} → {priority['band']}"]
    for k, v in priority.get("contributions", {}).items():
        if v != 0:
            lines.append(f"  {k:<14}{v:+.3f}")
    return "\n".join(lines)
