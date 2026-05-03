"""Gemini-based LLM classifier (batched, structured output, prompt-cached).

Cost discipline (per the cost-first dev preference):
  - Batch 15 posts per call (one Gemini round-trip vs 15)
  - Use Gemini 2.5 Flash (cheapest production-grade model, free tier 250 RPD)
  - Compress taxonomy into a flat (id, name, audience, urgency) table to keep
    the prompt small. Full hierarchy isn't needed at inference time.
  - Pydantic schema for structured JSON output (no regex parsing brittleness)
  - Graceful no-key fallback: returns [] so caller falls back to rules-only

Public API:
    is_llm_available() -> bool
    classify_with_llm(posts: list[PostInput]) -> list[Classification]
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, Field

from .. import config

ROOT = Path(__file__).resolve().parent.parent.parent
TAXONOMY_DIR = ROOT / "taxonomy"

# ---------- Pydantic schemas (Gemini structured output) ----------

class SubClaim(BaseModel):
    claim: str = Field(description="Short label of the sub-issue")
    topic_id: str = Field(description="Taxonomy leaf id, e.g. consumer.delivery.late")
    severity: str = Field(description="L1 / L2 / L3 / L4 / L5")


class LLMClassification(BaseModel):
    post_id: str
    side: str = Field(description="consumer / merchant / both / neither")
    primary_topic: str = Field(description="Taxonomy leaf id (e.g. consumer.delivery.late)")
    secondary_topics: list[str] = Field(default_factory=list)
    sub_claims: list[SubClaim] = Field(default_factory=list)
    sentiment: str = Field(description="positive / negative / neutral / mixed / abusive")
    tone_flags: list[str] = Field(default_factory=list)
    urgency: str = Field(description="critical / high / medium / low")
    urgency_score: float = Field(ge=0.0, le=1.0)
    audience: list[str] = Field(default_factory=list)
    author_role: str = Field(default="unknown")
    geography: str = Field(default="unknown")
    format: str = Field(description="complaint / question / review / opinion / news / meme / threat / promotion_spam / testimonial")
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human_review: bool
    reasoning: str = Field(description="2-3 sentence explanation")
    sarcasm_detected: bool = Field(
        default=False,
        description="True when literal sentiment differs from actual sentiment.",
    )
    sub_tripwire: str | None = Field(
        default=None,
        description="Optional sub-classification of a fired tripwire. Null if N/A.",
    )
    intent: str = Field(
        default="other",
        description="complaint | praise | inquiry | news_report | press_coverage | other",
    )


class LLMOverlay(BaseModel):
    """Lightweight overlay schema for the preclassify pipeline.

    Carries only the fields the LLM produces well: sentiment (with sarcasm
    correction), sarcasm flag, sub-tripwire taxonomy value, intent label,
    confidence, and a short reasoning. Rules-only baseline supplies side,
    tripwires, audience, geography. This keeps the prompt small and the
    output token count low, which is the cost lever for the backfill.
    """
    post_id: str
    sentiment: str = Field(
        description="positive / negative / neutral / mixed / abusive"
    )
    sarcasm_detected: bool = Field(
        default=False,
        description="True when literal sentiment differs from actual sentiment.",
    )
    sub_tripwire: str | None = Field(
        default=None,
        description="Optional sub-classification of a fired tripwire. Null if N/A.",
    )
    intent: str = Field(
        default="other",
        description="complaint | praise | inquiry | news_report | press_coverage | other",
    )
    confidence: float = Field(ge=0.0, le=1.0, default=0.7)
    reasoning: str = Field(default="", description="1-2 sentence explanation")


# ---------- Module init ----------

_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
_BATCH_SIZE = 15

# Lazy-loaded client (avoid import-time errors if SDK missing or key absent)
_client = None
_client_init_failed = False


def is_llm_available() -> bool:
    """True if GEMINI_API_KEY is set and the SDK can import."""
    if not os.getenv("GEMINI_API_KEY"):
        return False
    try:
        from google import genai  # noqa: F401
        return True
    except ImportError:
        return False


def _get_client():
    global _client, _client_init_failed
    if _client_init_failed:
        return None
    if _client is not None:
        return _client
    try:
        from google import genai
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        return _client
    except Exception as e:
        logger.error(f"Gemini client init failed: {e}")
        _client_init_failed = True
        return None


# ---------- Taxonomy compression for prompt ----------

def _flatten_categories(node: dict[str, Any], prefix: str, side: str, out: list[dict[str, Any]]) -> None:
    """Walk nested 'categories' tree, emit (id, name, audience, urgency) per leaf."""
    children = node.get("children")
    if not children:
        # leaf
        out.append({
            "id": f"{prefix}.{node['id']}" if prefix else node["id"],
            "side": side,
            "name": node.get("name", node["id"]),
            "audience": node.get("default_audience", []),
            "urgency": node.get("default_urgency", "medium"),
            "sensitivity": node.get("sensitivity_flags", []),
            "examples": node.get("examples", [])[:1],  # only 1 example to save tokens
        })
        return
    new_prefix = f"{prefix}.{node['id']}" if prefix else node["id"]
    for child in children:
        _flatten_categories(child, new_prefix, side, out)


def _load_compressed_taxonomy() -> str:
    """Build a compact taxonomy view for the prompt. Cached on first call."""
    if hasattr(_load_compressed_taxonomy, "_cache"):
        return _load_compressed_taxonomy._cache  # type: ignore[attr-defined]

    leaves: list[dict[str, Any]] = []
    for fn in ("consumer.yaml", "merchant.yaml"):
        with open(TAXONOMY_DIR / fn) as f:
            data = yaml.safe_load(f)
        side = data["side"]
        for top in data.get("categories", []):
            _flatten_categories(top, prefix="", side=side, out=leaves)

    # Render as a flat table the model can scan quickly
    lines = ["TAXONOMY (id | side | name | audience | urgency)"]
    for lf in leaves:
        aud = ",".join(lf["audience"]) if lf["audience"] else "-"
        sens = f" [⚠ {','.join(lf['sensitivity'])}]" if lf["sensitivity"] else ""
        lines.append(f"  {lf['id']} | {lf['side']} | {lf['name']} | {aud} | {lf['urgency']}{sens}")

    text = "\n".join(lines)
    _load_compressed_taxonomy._cache = text  # type: ignore[attr-defined]
    return text


# ---------- Prompt construction ----------

_SYSTEM_INSTRUCTION = """You are a classifier for Zomato's social-listening tool. \
You receive posts from Reddit and X (Twitter) about Zomato's CONSUMER app and \
MERCHANT app (excluding Blinkit, Eternal corporate, District, Hyperpure, which are out_of_scope).

For each post, output a structured classification with these axes:
- side: consumer | merchant | both | neither (use 'neither' for out_of_scope)
- primary_topic: a single leaf id from the taxonomy below
- secondary_topics: optional additional leaf ids if the post spans multiple
- sub_claims: each distinct issue raised in the post, with topic + severity (L1-L5)
- sentiment: positive | negative | neutral | mixed | abusive (no 'sarcastic' value here, sarcasm is its own boolean)
- sarcasm_detected: TRUE when the literal words read like one sentiment but the actual sentiment is the opposite. Phrases like "thanks for the bad service", "great job ruining my dinner", "wow what a wonderful refund delay" are sarcastic. When sarcasm_detected=true, set sentiment to the ACTUAL underlying sentiment (typically negative or abusive), not the literal one. Rules-based heuristics often miss sarcasm; you must catch it.
- sub_tripwire: optional sub-classification of a fired rules tripwire. Use the rules_tripwires hint per post. Allowed values per parent tripwire id:
    * food_safety_incident: poisoning_with_medical | foreign_object | contamination | temperature | allergic_reaction | packaging_only
    * death_claim: road_accident | workplace_violence | hate_crime | medical_emergency | suicide | other
    * court_fir_legal: threat_to_file | filed_action | regulator_complaint | media_threat
    * religious_caste_gender_sensitivity: caste | religion | gender | regional_origin | sexual_orientation
    For any other tripwire id, or none, set sub_tripwire=null.
- intent: complaint | praise | inquiry | news_report | press_coverage | other. 'inquiry' is for questions seeking info. 'news_report' is when the author is reporting an event neutrally. 'press_coverage' is journalistic coverage by a media outlet or journalist account.
- tone_flags: any of: accusation, threat, profanity, religious, caste, gender, political, satirical, factual
- urgency: critical | high | medium | low (and a 0-1 score)
- audience: which Zomato teams should see this (customer-care, merchant-ops, safety, legal, pr, founder-office, etc.)
- author_role: consumer_heavy_user | consumer_anonymous | merchant_owner_small | journalist | politician | influencer_food | bot_suspected | unknown | etc.
- geography: city | neighborhood | state | country | unknown
- format: complaint | question | review | opinion | news | meme | threat | promotion_spam | testimonial
- confidence: 0-1; LOW (<0.7) for ambiguous, unfamiliar, or multi-issue posts
- needs_human_review: TRUE if any of {tripwires likely, sensitivity flags, low confidence, novel pattern}
- reasoning: 2-3 sentences explaining the classification choices

Be conservative on auto-routing for sensitive content (religious, caste, political, safety, legal). \
When unsure, set needs_human_review=true and lower confidence. \
Routing safety beats classification confidence."""


def _build_prompt(posts: list[dict[str, Any]]) -> str:
    """Build the user-message portion of the prompt with the batch of posts."""
    taxonomy = _load_compressed_taxonomy()
    lines = [taxonomy, "", "POSTS TO CLASSIFY:"]
    for p in posts:
        sig = (
            f"\n[post_id={p['id']}]\n"
            f"  source: {p['source']}\n"
            f"  author: @{p.get('author') or 'anonymous'}\n"
            f"  posted: {p['created_at']}\n"
        )
        # Hint from rules-only pre-classification (saves the model time)
        if p.get("rules_hint"):
            sig += f"  rules_hint: {p['rules_hint']}\n"
        sig += f"  content: {(p.get('content') or '').strip()[:1200]}"
        lines.append(sig)
    lines.append(
        "\nReturn a JSON ARRAY with one classification per post, in the same order. "
        "post_id MUST match the input. primary_topic MUST be a valid taxonomy id from above."
    )
    return "\n".join(lines)


# ---------- Public entry ----------

async def classify_with_llm(
    post_inputs: list[dict[str, Any]],
    *,
    batch_size: int = _BATCH_SIZE,
) -> list[LLMClassification]:
    """Classify a list of posts via Gemini. Returns one Classification per
    input post (best-effort: skips entries that fail to parse).

    Each post_input is a dict with at least: id, source, author, content,
    created_at. Optional: rules_hint (str, pre-classification summary).

    Returns [] if no key set or SDK unavailable. Caller should fall back
    to rules-only.
    """
    if not is_llm_available():
        logger.info("LLM classifier: GEMINI_API_KEY not set, skipping LLM stage")
        return []

    client = _get_client()
    if client is None:
        return []

    from google.genai import types

    results: list[LLMClassification] = []
    for i in range(0, len(post_inputs), batch_size):
        batch = post_inputs[i : i + batch_size]
        prompt = _build_prompt(batch)
        logger.info(f"LLM classify: batch {i // batch_size + 1} ({len(batch)} posts)")
        try:
            resp = await _generate_async(
                client,
                contents=prompt,
                system_instruction=_SYSTEM_INSTRUCTION,
                response_schema=list[LLMClassification],
            )
            parsed = resp.parsed if hasattr(resp, "parsed") else None
            if parsed:
                results.extend(parsed)
            else:
                logger.warning(f"LLM batch {i // batch_size + 1}: parsed=None, falling back")
        except Exception as e:
            logger.warning(f"LLM batch {i // batch_size + 1} failed: {e}")
    logger.info(f"LLM classify: returned {len(results)} classifications for {len(post_inputs)} posts")
    return results


async def _generate_async(client, *, contents: str, system_instruction: str, response_schema):
    """Wrapper around Gemini's generate_content, async via a thread."""
    import asyncio
    from google.genai import types

    def _call():
        return client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0.2,
            ),
        )

    return await asyncio.to_thread(_call)


# ---------- Overlay path (preclassifier integration) ----------
#
# The overlay path is a slimmer, cheaper LLM call that only fills the four
# fields the rules cannot do well: sarcasm-corrected sentiment, sarcasm
# boolean, sub-tripwire taxonomy, and intent. The rules-only baseline
# already handles side, tripwire detection, geography, and audience routing,
# so we do not re-ask the model for those. This drops prompt size by ~10x
# vs the full LLMClassification schema.

_OVERLAY_SYSTEM_INSTRUCTION = """You are a sentiment and intent overlay classifier for Zomato's social-listening tool. \
For each post you produce a small JSON record with these fields:

- sentiment: positive | negative | neutral | mixed | abusive. Pick the ACTUAL underlying sentiment, not the literal one if sarcasm is present.
- sarcasm_detected: true when the literal words read like one sentiment but the actual sentiment is the opposite. Examples that ARE sarcasm: "thanks for the bad service", "great job ruining my dinner", "wow what a wonderful refund delay", "amazing as always (eye-roll)". When sarcasm_detected=true, sentiment MUST reflect the actual feeling (typically negative or abusive). Rules-based keyword scans miss sarcasm because they see 'thanks' or 'great' and call it positive. Your job is to catch this.
- sub_tripwire: optional sub-classification of a fired rules tripwire, using the rules_tripwires hint per post. Permitted values per parent tripwire:
    * food_safety_incident -> poisoning_with_medical | foreign_object | contamination | temperature | allergic_reaction | packaging_only
    * death_claim -> road_accident | workplace_violence | hate_crime | medical_emergency | suicide | other
    * court_fir_legal -> threat_to_file | filed_action | regulator_complaint | media_threat
    * religious_caste_gender_sensitivity -> caste | religion | gender | regional_origin | sexual_orientation
  For any other tripwire id, or when no tripwire fired, set sub_tripwire to null.
- intent: complaint | praise | inquiry | news_report | press_coverage | other. 'inquiry' is for questions seeking info. 'news_report' is when the author is reporting an event neutrally. 'press_coverage' is journalistic coverage by a media outlet or journalist account.
- confidence: 0-1, lower for ambiguous posts.
- reasoning: 1-2 sentences explaining the call, especially the sarcasm decision.

Be careful with non-English or code-mixed (Hindi, Tamil, etc.) posts. Read intent from context, not just keywords."""


def _build_overlay_prompt(posts: list[dict[str, Any]]) -> str:
    """Slim prompt for the overlay classifier. No taxonomy dump needed."""
    lines = ["POSTS TO CLASSIFY:"]
    for p in posts:
        sig = (
            f"\n[post_id={p['id']}]\n"
            f"  source: {p.get('source') or 'unknown'}\n"
            f"  author: @{p.get('author') or 'anonymous'}\n"
        )
        rt = p.get("rules_tripwires") or []
        if rt:
            sig += f"  rules_tripwires: {','.join(rt)}\n"
        rs = p.get("rules_sentiment")
        if rs:
            sig += f"  rules_sentiment_guess: {rs}\n"
        sig += f"  content: {(p.get('content') or '').strip()[:800]}"
        lines.append(sig)
    lines.append(
        "\nReturn a JSON ARRAY with one record per post, in the same order. "
        "post_id MUST match the input."
    )
    return "\n".join(lines)


async def classify_overlay_with_llm(
    post_inputs: list[dict[str, Any]],
    *,
    batch_size: int = _BATCH_SIZE,
) -> tuple[list[LLMOverlay], dict[str, int]]:
    """Run the slim overlay classifier on a list of posts. Returns (results, usage).

    `usage` is a dict with `prompt_tokens` and `output_tokens` summed across
    batches, for cost reporting. Empty list and zeros if LLM is unavailable.

    Each post_input is a dict with at least: id, content. Recommended:
    source, author, rules_tripwires (list[str]), rules_sentiment (str).
    """
    usage = {"prompt_tokens": 0, "output_tokens": 0, "batches": 0, "batches_failed": 0}
    if not is_llm_available():
        logger.info("LLM overlay: GEMINI_API_KEY not set, skipping LLM stage")
        return [], usage

    client = _get_client()
    if client is None:
        return [], usage

    results: list[LLMOverlay] = []
    for i in range(0, len(post_inputs), batch_size):
        batch = post_inputs[i : i + batch_size]
        prompt = _build_overlay_prompt(batch)
        batch_no = i // batch_size + 1
        logger.info(f"LLM overlay: batch {batch_no} ({len(batch)} posts)")
        try:
            resp = await _generate_async(
                client,
                contents=prompt,
                system_instruction=_OVERLAY_SYSTEM_INSTRUCTION,
                response_schema=list[LLMOverlay],
            )
            parsed = resp.parsed if hasattr(resp, "parsed") else None
            if parsed:
                results.extend(parsed)
            else:
                logger.warning(
                    f"LLM overlay batch {batch_no}: parsed=None, falling back"
                )
                usage["batches_failed"] += 1
            # Token usage tracking, best-effort
            meta = getattr(resp, "usage_metadata", None)
            if meta is not None:
                pt = getattr(meta, "prompt_token_count", 0) or 0
                ct = getattr(meta, "candidates_token_count", 0) or 0
                usage["prompt_tokens"] += int(pt)
                usage["output_tokens"] += int(ct)
            usage["batches"] += 1
        except Exception as e:
            logger.warning(f"LLM overlay batch {batch_no} failed: {e}")
            usage["batches_failed"] += 1
    logger.info(
        f"LLM overlay: returned {len(results)} of {len(post_inputs)} "
        f"(prompt_tokens={usage['prompt_tokens']}, output_tokens={usage['output_tokens']})"
    )
    return results, usage


# ---------- Sub-tripwire validation ----------

_SUB_TRIPWIRE_TAXONOMY: dict[str, set[str]] = {
    "food_safety_incident": {
        "poisoning_with_medical", "foreign_object", "contamination",
        "temperature", "allergic_reaction", "packaging_only",
    },
    "death_claim": {
        "road_accident", "workplace_violence", "hate_crime",
        "medical_emergency", "suicide", "other",
    },
    "court_fir_legal": {
        "threat_to_file", "filed_action", "regulator_complaint",
        "media_threat",
    },
    "religious_caste_gender_sensitivity": {
        "caste", "religion", "gender", "regional_origin",
        "sexual_orientation",
    },
}


def validate_sub_tripwire(
    sub_tripwire: str | None, tripwires_fired: list[str]
) -> str | None:
    """Validate a model-emitted sub_tripwire against the closed taxonomy.

    Returns the sub_tripwire if valid for any of the fired tripwires.
    Returns None if invalid, empty, or no taxonomy parent fired.
    """
    if not sub_tripwire:
        return None
    sub_tripwire = sub_tripwire.strip().lower()
    if not sub_tripwire or sub_tripwire == "null" or sub_tripwire == "none":
        return None
    for parent in tripwires_fired:
        allowed = _SUB_TRIPWIRE_TAXONOMY.get(parent)
        if allowed and sub_tripwire in allowed:
            return sub_tripwire
    return None


_VALID_INTENTS = {
    "complaint", "praise", "inquiry", "news_report",
    "press_coverage", "other",
}


def validate_intent(intent: str | None) -> str:
    """Coerce a model-emitted intent into the closed enum, defaulting to 'other'."""
    if not intent:
        return "other"
    val = intent.strip().lower()
    if val in _VALID_INTENTS:
        return val
    return "other"


_VALID_SENTIMENTS = {
    "positive", "negative", "neutral", "mixed", "abusive",
}


def validate_sentiment(sentiment: str | None, fallback: str = "neutral") -> str:
    """Coerce model-emitted sentiment into the closed enum."""
    if not sentiment:
        return fallback
    val = sentiment.strip().lower()
    if val in _VALID_SENTIMENTS:
        return val
    # Map legacy 'sarcastic' if the model returns it anyway
    if val == "sarcastic":
        return "negative"
    return fallback
