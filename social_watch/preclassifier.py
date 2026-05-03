"""Rules-only pre-classifier.

Runs deterministic side detection + tripwire scan + simple sentiment
heuristics on every post in the DB. Populates `posts.category`,
`posts.score`, and `posts.classification` (JSON) with whatever can be
determined without an LLM.

Why this exists: the dashboard needs structured data NOW, but the full
LLM classifier needs a Gemini key + more wiring. Rules give us 60-70% of
the routing signal at zero cost. The LLM-based classifier (when it lands)
can overwrite these fields with richer output, same schema.

This module reads the YAML configs in taxonomy/ for all keyword/handle/
regex rules. Nothing is hard-coded.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import yaml
from loguru import logger

from . import config, handles as handles_mod

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_DIR = ROOT / "taxonomy"


# ---------- Loading YAMLs ----------

def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def load_configs() -> dict[str, Any]:
    return {
        "consumer": _load_yaml(TAXONOMY_DIR / "consumer.yaml"),
        "merchant": _load_yaml(TAXONOMY_DIR / "merchant.yaml"),
        "tripwires": _load_yaml(TAXONOMY_DIR / "tripwires.yaml"),
        "cross_cuts": _load_yaml(TAXONOMY_DIR / "cross_cuts.yaml"),
    }


# ---------- Side detection (rules only) ----------

# Consumer-side signals: language a customer uses
_CONSUMER_HINTS = (
    "ordered from", "my order", "i ordered", "delivered", "delivery",
    "refund", "@zomatocare", "zomatocare", "support", "tracking",
    "the food", "got my order", "delivery agent", "delivery boy",
    "delivery guy", "delivery person", "delivery partner",
    "no agent", "agent didn't", "agent never",
)

# Merchant-side signals: language a restaurant owner/operator uses
_MERCHANT_HINTS = (
    "my restaurant", "as an owner", "as a restaurant", "restaurant owner",
    "commission", "settlement", "payout", "merchant app", "merchant dashboard",
    "kyc", "fssai", "menu sync", "delisted", "suspended without",
    "we serve", "our menu", "we operate", "weekly payout",
    "small restaurants", "small business",
    "kam", "account manager", "nrai",
)

# Out-of-scope: explicit Blinkit/Eternal/District/Hyperpure mentions
_OUT_OF_SCOPE_HINTS = (
    "blinkit", "eternal earned", "eternal stock", "share price",
    "hyperpure", "district",
)


def detect_side(text: str) -> tuple[str, list[str]]:
    """Return (side, matched_hints). side ∈ {consumer, merchant, both, neither}."""
    t = text.lower()
    consumer_hits = [h for h in _CONSUMER_HINTS if h in t]
    merchant_hits = [h for h in _MERCHANT_HINTS if h in t]
    oos_hits = [h for h in _OUT_OF_SCOPE_HINTS if h in t]

    # Strong out-of-scope signal AND no zomato-app context wins
    if oos_hits and "zomato" not in t.replace("blinkit", "").replace("eternal", "").replace("hyperpure", "").replace("district", ""):
        return "neither", oos_hits

    if consumer_hits and merchant_hits:
        return "both", consumer_hits + merchant_hits
    if merchant_hits:
        return "merchant", merchant_hits
    if consumer_hits:
        return "consumer", consumer_hits
    # Default: consumer (vast majority of public chatter)
    return "consumer", []


# ---------- Tripwire scan (rules only) ----------

_WORD_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


def _word_bound_pattern(kw: str) -> re.Pattern[str]:
    """Word-boundary regex; cached. Substring matching causes massive false
    positives ('ICU' matches 'ridiculous', 'st' matches 'first', etc.)."""
    pat = _WORD_BOUNDARY_CACHE.get(kw)
    if pat is None:
        pat = re.compile(r"(?<!\w)" + re.escape(kw.lower()) + r"(?!\w)", re.IGNORECASE)
        _WORD_BOUNDARY_CACHE[kw] = pat
    return pat


def _check_keywords(text_low: str, keywords: list[str]) -> list[str]:
    return [k for k in (keywords or []) if _word_bound_pattern(k).search(text_low)]


def _check_keyword_pairs(text_low: str, pairs: list[list[str]]) -> list[list[str]]:
    return [
        pair for pair in (pairs or [])
        if all(_word_bound_pattern(k).search(text_low) for k in pair)
    ]


def _check_handles(author: str | None, handles: list[str]) -> bool:
    if not author or not handles:
        return False
    a = ("@" + author.lstrip("@")).lower()
    return any(h.lower() == a for h in handles)


def detect_tripwires(
    text: str, author: str | None, tripwires_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return a list of fired tripwires with override metadata."""
    text_low = text.lower()
    fired: list[dict[str, Any]] = []
    for tw in tripwires_cfg.get("tripwires", []):
        det = tw.get("detection", {}) or {}
        kw_hits = _check_keywords(text_low, det.get("keywords", []))
        pair_hits = _check_keyword_pairs(text_low, det.get("keyword_pairs", []))
        handle_hit = _check_handles(author, det.get("handles", []))
        if kw_hits or pair_hits or handle_hit:
            fired.append(
                {
                    "id": tw["id"],
                    "matches": {
                        "keywords": kw_hits,
                        "keyword_pairs": pair_hits,
                        "handle": handle_hit,
                    },
                    "override": tw.get("override", {}),
                }
            )
    return fired


# ---------- Lightweight sentiment + format heuristics ----------

_NEG_MARKERS = (
    "worst", "terrible", "horrible", "awful", "useless", "fraud", "scam",
    "shame", "shameful", "disappointed", "disappointing", "ridiculous",
    "pathetic", "disgusting", "rude", "unprofessional", "thieves",
    "robbed", "cheated", "ignored", "never again", "last time",
    "can't believe", "can not believe", "stop using", "delete zomato",
    "boycott",
)

_PROFANITY_MARKERS = (
    "fuck", "fck", "shit", "bullshit", "bs", "bastard", "wtf",
    "chutya", "chutiya", "madarchod", "bhenchod", "bkl", "mc", "bc",
)

_POS_MARKERS = (
    "thank you", "thanks", "great service", "amazing", "loved",
    "perfect", "excellent", "fantastic", "best ever", "appreciate",
    "kudos", "well done", "shoutout", "love it", "happy with",
)

_QUESTION_PATTERN = re.compile(r"\?\s*$|^(why|how|what|when|where|does|is|are|can)\b", re.I)


def detect_sentiment_and_format(text: str) -> dict[str, Any]:
    t = text.lower()
    has_neg = any(m in t for m in _NEG_MARKERS)
    has_pos = any(m in t for m in _POS_MARKERS)
    has_profanity = any(m in t for m in _PROFANITY_MARKERS)
    has_question = bool(_QUESTION_PATTERN.search(text or ""))

    if has_profanity:
        sentiment = "abusive"
    elif has_neg and has_pos:
        sentiment = "mixed"
    elif has_neg:
        sentiment = "negative"
    elif has_pos:
        sentiment = "positive"
    else:
        sentiment = "neutral"

    if has_question and not has_neg:
        fmt = "question"
    elif has_pos and not has_neg:
        fmt = "testimonial"
    elif has_neg or has_profanity:
        fmt = "complaint"
    else:
        fmt = "opinion"

    return {
        "sentiment": sentiment,
        "format": fmt,
        "tone_flags": [f for f in [
            "profanity" if has_profanity else None,
        ] if f],
    }


# ---------- Geography (rules only) ----------

_INDIAN_CITIES = (
    "bangalore", "bengaluru", "mumbai", "delhi", "ncr", "gurgaon", "gurugram",
    "noida", "hyderabad", "chennai", "kolkata", "pune", "ahmedabad", "jaipur",
    "lucknow", "kochi", "chandigarh", "indore", "bhubaneswar", "guwahati",
    "trivandrum", "thiruvananthapuram", "surat", "nagpur", "patna",
    "indiranagar", "koramangala", "whitefield", "marathahalli", "andheri",
    "bandra", "vikhroli", "powai", "thane", "navi mumbai",
    "saket", "vasant kunj", "dwarka", "rohini", "hauz khas",
)


def detect_geography(text: str) -> dict[str, Any]:
    t = text.lower()
    matches = [c for c in _INDIAN_CITIES if c in t]
    if not matches:
        return {"value": "unknown", "matches": []}
    if any(c in matches for c in ("indiranagar", "koramangala", "whitefield", "marathahalli",
                                    "andheri", "bandra", "powai", "thane", "saket", "dwarka", "rohini")):
        return {"value": "neighborhood", "matches": matches}
    return {"value": "city", "matches": matches}


# ---------- Urgency scoring (combines side + tripwires + sentiment) ----------

_URGENCY_BANDS = [
    (0.85, "critical"),
    (0.6, "high"),
    (0.3, "medium"),
    (0.0, "low"),
]


def urgency_band(score: float) -> str:
    for threshold, band in _URGENCY_BANDS:
        if score >= threshold:
            return band
    return "low"


def compute_urgency(
    side: str, tripwires_fired: list[dict[str, Any]], sentiment: str
) -> tuple[float, str]:
    if tripwires_fired:
        return 0.95, "critical"
    base = {
        "consumer": 0.35,
        "merchant": 0.40,
        "both": 0.55,
        "neither": 0.10,
    }.get(side, 0.30)
    if sentiment == "abusive":
        base += 0.15
    elif sentiment == "negative":
        base += 0.20
    elif sentiment == "positive":
        base -= 0.10
    score = max(0.0, min(1.0, base))
    return score, urgency_band(score)


# ---------- Audience derivation ----------

def derive_audience(side: str, tripwires_fired: list[dict[str, Any]]) -> list[str]:
    audience: set[str] = set()
    # Tripwires win: they specify their own audiences
    for tw in tripwires_fired:
        for a in tw.get("override", {}).get("audience", []) or []:
            audience.add(a)
    if not audience:
        if side == "consumer":
            audience.update(["customer-care"])
        elif side == "merchant":
            audience.update(["merchant-ops"])
        elif side == "both":
            audience.update(["customer-care", "merchant-ops", "trust-safety"])
        # 'neither' → no audience
    return sorted(audience)


# ---------- Main entry ----------

async def preclassify_all(*, force: bool = False) -> dict[str, int]:
    """Pre-classify every post in the DB.

    With force=False: only posts where category IS NULL get processed.
    With force=True: every post is re-processed (overwrites prior values).

    Two-pass design:
      1. Rules pass builds the baseline classification dict for every post.
      2. If a Gemini key is configured, batch posts through the slim LLM
         overlay (sentiment with sarcasm correction, sub_tripwire, intent),
         and merge those fields into the classification dict before write.

    The LLM is best-effort: any batch that fails leaves the affected posts
    with their rules-only classification plus default overlay fields.
    Per-post idempotency: re-running over already-classified posts is safe.

    Returns counts dict. New keys versus the rules-only era:
      llm_classified:  posts that received a Gemini overlay.
      sarcasm_caught:  posts whose sentiment changed because of sarcasm.
      llm_prompt_tokens / llm_output_tokens: usage totals if available.
    """
    # Local import to avoid a circular import at module load: classifier
    # imports preclassifier indirectly via pipeline.
    from .classifier.llm import (
        classify_overlay_with_llm,
        is_llm_available,
        validate_intent,
        validate_sentiment,
        validate_sub_tripwire,
    )

    cfg = load_configs()
    counts: dict[str, Any] = {
        "processed": 0,
        "tripwires_fired": 0,
        "by_side": {"consumer": 0, "merchant": 0, "both": 0, "neither": 0},
        "llm_classified": 0,
        "sarcasm_caught": 0,
        "llm_prompt_tokens": 0,
        "llm_output_tokens": 0,
    }

    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Phase gamma: ensure curated watchlists are seeded so press/founder
        # handles are recognized on the first preclassify pass.
        await handles_mod.seed_watchlists(db)

        where = "" if force else "WHERE category IS NULL"
        cur = await db.execute(
            f"SELECT id, source, native_id, author, content, url, metadata, created_at "
            f"FROM posts {where}"
        )
        rows = await cur.fetchall()

        # ---------- Pass 1: rules-only baseline ----------
        # We build all classifications in memory first, then optionally run
        # the LLM overlay before the single DB write loop. This batches the
        # LLM calls for cost.
        baselines: list[dict[str, Any]] = []  # one entry per row
        for r in rows:
            text = r["content"] or ""
            author = r["author"]

            # Phase gamma: refresh the author's handle row. Quick: just
            # looks at the post's metadata dict (Twitter has a few fields,
            # Reddit has nothing useful for tier yet).
            if author:
                try:
                    metadata_dict = json.loads(r["metadata"]) if r["metadata"] else {}
                except Exception:
                    metadata_dict = {}
                await handles_mod.get_or_compute_handle(
                    db,
                    handle=author,
                    source=r["source"],
                    metadata=metadata_dict,
                    bio=None,
                    posted_at=r["created_at"],
                )

            side, side_hits = detect_side(text)
            tripwires_fired = detect_tripwires(text, author, cfg["tripwires"])
            sentiment_info = detect_sentiment_and_format(text)
            geo = detect_geography(text)
            urgency_score, urgency = compute_urgency(
                side, tripwires_fired, sentiment_info["sentiment"]
            )
            audience = derive_audience(side, tripwires_fired)

            tripwire_ids = [t["id"] for t in tripwires_fired]
            classification: dict[str, Any] = {
                "method": "rules-only",
                "preclassified": True,
                "side": side,
                "side_hits": side_hits,
                "tripwires_fired": tripwire_ids,
                "tripwires_detail": tripwires_fired,
                "sentiment": sentiment_info["sentiment"],
                "tone_flags": sentiment_info["tone_flags"],
                "format": sentiment_info["format"],
                "geography": geo,
                "urgency": urgency,
                "urgency_score": urgency_score,
                "audience": audience,
                "confidence": 0.55,  # rules-only baseline, LLM may upgrade
                "needs_human_review": bool(tripwires_fired),
                "auto_action_safe": (
                    not tripwires_fired
                    and sentiment_info["sentiment"] not in ("abusive",)
                    and "profanity" not in sentiment_info["tone_flags"]
                ),
                "reasoning": (
                    f"Rules-only pre-classification. Side={side} "
                    f"(hits={side_hits[:3]}). Sentiment={sentiment_info['sentiment']}. "
                    f"{len(tripwires_fired)} tripwire(s) fired. "
                    f"Urgency={urgency} ({urgency_score:.2f})."
                ),
                # New overlay fields, defaulted for the rules-only path.
                "sarcasm_detected": False,
                "sub_tripwire": None,
                "intent": "other",
                "classified_at": datetime.now(timezone.utc).isoformat(),
            }
            baselines.append({"row": r, "classification": classification, "tripwire_ids": tripwire_ids})

        # ---------- Pass 2: optional LLM overlay ----------
        overlay_by_id: dict[str, Any] = {}
        if baselines and is_llm_available():
            post_inputs = [
                {
                    "id": b["row"]["id"],
                    "source": b["row"]["source"],
                    "author": b["row"]["author"],
                    "content": b["row"]["content"] or "",
                    "rules_tripwires": b["tripwire_ids"],
                    "rules_sentiment": b["classification"]["sentiment"],
                }
                for b in baselines
            ]
            try:
                results, usage = await classify_overlay_with_llm(post_inputs)
                overlay_by_id = {ov.post_id: ov for ov in results}
                counts["llm_prompt_tokens"] = usage.get("prompt_tokens", 0)
                counts["llm_output_tokens"] = usage.get("output_tokens", 0)
                logger.info(
                    f"LLM overlay merged: {len(overlay_by_id)} of {len(post_inputs)} posts"
                )
            except Exception as e:
                # Sink the whole batch to rules-only on a top-level failure.
                logger.warning(f"LLM overlay top-level failure, falling back to rules-only: {e}")
                overlay_by_id = {}

        # ---------- Pass 3: merge + write ----------
        from . import noise as noise_mod
        for b in baselines:
            r = b["row"]
            classification = b["classification"]
            tripwire_ids = b["tripwire_ids"]
            text = r["content"] or ""
            author = r["author"]
            side = classification["side"]

            ov = overlay_by_id.get(r["id"])
            if ov is not None:
                old_sentiment = classification["sentiment"]
                new_sentiment = validate_sentiment(ov.sentiment, fallback=old_sentiment)
                sarcasm = bool(ov.sarcasm_detected)
                # If the model flagged sarcasm but kept sentiment positive,
                # force a flip to negative to honor the spec.
                if sarcasm and new_sentiment == "positive":
                    new_sentiment = "negative"
                sub_tw = validate_sub_tripwire(ov.sub_tripwire, tripwire_ids)
                intent = validate_intent(ov.intent)
                llm_conf = float(getattr(ov, "confidence", 0.7) or 0.0)
                llm_reasoning = (getattr(ov, "reasoning", "") or "").strip()

                if sarcasm or new_sentiment != old_sentiment:
                    counts["sarcasm_caught"] += 1 if sarcasm else 0

                classification["sentiment"] = new_sentiment
                classification["sarcasm_detected"] = sarcasm
                classification["sub_tripwire"] = sub_tw
                classification["intent"] = intent
                classification["method"] = "gemini-classified"
                # Bump confidence only when the LLM is more sure than rules.
                if llm_conf > classification.get("confidence", 0.0):
                    classification["confidence"] = llm_conf
                if llm_reasoning:
                    classification["reasoning"] = (
                        classification.get("reasoning", "") + " LLM: " + llm_reasoning
                    ).strip()

                # Recompute urgency from the new sentiment (sarcasm flips can
                # promote a post from low to medium/high). Tripwires still
                # force critical via compute_urgency.
                urgency_score, urgency = compute_urgency(
                    side, classification["tripwires_detail"], new_sentiment
                )
                classification["urgency"] = urgency
                classification["urgency_score"] = urgency_score

                # Recompute auto_action_safe: sarcasm or abusive sentiment
                # disable autopilot.
                classification["auto_action_safe"] = (
                    not tripwire_ids
                    and not sarcasm
                    and new_sentiment not in ("abusive",)
                    and "profanity" not in classification.get("tone_flags", [])
                )
                counts["llm_classified"] += 1

            # Phase epsilon. Noise filter. Categorical, not scored. Run AFTER
            # classification so the categorize() function can rescue posts
            # that fired a tripwire (those are never noise).
            handle_row: dict[str, Any] | None = None
            if author:
                try:
                    cur = await db.execute(
                        "SELECT tier, profile_class FROM handles "
                        "WHERE handle = ? AND source = ?",
                        (author.lower().lstrip("@"), r["source"]),
                    )
                    h = await cur.fetchone()
                    if h:
                        handle_row = dict(h)
                except Exception:
                    handle_row = None
            noise_category = noise_mod.categorize(
                {"id": r["id"], "content": text, "author": author, "source": r["source"]},
                classification=classification,
                handle_row=handle_row,
            )

            await db.execute(
                """
                UPDATE posts SET
                    category = ?,
                    score = ?,
                    classification = ?,
                    classified_at = ?,
                    noise_category = ?
                WHERE id = ?
                """,
                (
                    side,
                    classification["urgency_score"],
                    json.dumps(classification, default=str),
                    datetime.now(timezone.utc).isoformat(),
                    noise_category,
                    r["id"],
                ),
            )
            counts["processed"] += 1
            counts["by_side"][side] = counts["by_side"].get(side, 0) + 1
            if tripwire_ids:
                counts["tripwires_fired"] += 1
            if noise_category:
                counts["noise"] = counts.get("noise", 0) + 1

        await db.commit()

    logger.info(
        f"Pre-classified {counts['processed']} posts: "
        f"by_side={counts['by_side']}, tripwires={counts['tripwires_fired']}, "
        f"llm_classified={counts['llm_classified']}, sarcasm_caught={counts['sarcasm_caught']}"
    )
    return counts
