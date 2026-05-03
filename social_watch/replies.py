"""LLM-drafted reply suggestions for high-priority posts.

Provider-agnostic: prefers Gemini (free tier 250 RPD on 2.5 Flash) when
GEMINI_API_KEY is set, falls back to Anthropic Claude when only
ANTHROPIC_API_KEY is set. Same shared system prompt + same return shape
either way, so the UI doesn't care which provider answered.

Why this exists:
    The brief asks for an "AI-Native" tool. Classification is well-served
    by deterministic rules + cheap LLM for edge cases — no need to spend
    LLM tokens on structured 1-of-N labelling. Where LLMs earn their
    keep is in the customer-facing surface: drafting an apology, a
    refund offer, an escalation acknowledgement — the kind of nuanced
    wording that rules can't write and the social team would normally
    hand-craft.

So this module owns one narrow job: take a P0/P1 post, hand the LLM the
post text + classification + priority breakdown, and get back a
ready-to-paste reply in Zomato's voice. The operator sees a draft, edits
if needed, copies, and ships.

Public API:
    is_available() -> bool
    draft_reply(post, classification, priority_breakdown) -> dict
    active_provider() -> str | None       # "gemini" | "anthropic" | None

Returned dict shape:
    {
        "ok":       bool,
        "reply":    str | None,
        "tone":     str | None,           # empathetic / factual / escalatory / celebratory
        "channel":  str | None,           # public_reply / dm_request / founder_office / no_reply
        "rationale": str | None,          # one-sentence "why this draft"
        "model":    str | None,
        "provider": str | None,           # "gemini" or "anthropic"
        "error":    str | None,
        "ts":       iso8601,
    }

Failure mode:
    Returns ok=False with a human-readable error if no key is set, the
    SDK isn't installed, or the call fails. Never raises.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from loguru import logger
from pydantic import BaseModel, Field

# Env keys
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# Defaults (can be overridden via env)
DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"

# Hard cap on the suggested reply length. Twitter is 280 free-tier and a
# longer draft just creates trim work for the operator.
MAX_REPLY_CHARS = 280


# ----------------------------------------------------------------------
# Output schema (used by Gemini structured output; mirrored by Claude)
# ----------------------------------------------------------------------

class ReplyDraft(BaseModel):
    """Strict schema for the LLM response. Gemini enforces this via
    response_schema; Claude is asked to produce the same JSON shape and
    we json-parse manually."""
    reply: Optional[str] = Field(
        None,
        description="The suggested reply, ≤280 chars. Set to null when channel is no_reply.",
    )
    tone: Literal["empathetic", "factual", "celebratory", "escalatory"] = Field(
        ..., description="Overall tone of the draft."
    )
    channel: Literal["public_reply", "dm_request", "founder_office", "no_reply"] = Field(
        ..., description="Recommended response channel."
    )
    rationale: str = Field(..., description="One-sentence explanation of the choice.")


# ----------------------------------------------------------------------
# Provider detection
# ----------------------------------------------------------------------

def _has_gemini_key() -> bool:
    return bool((os.getenv(GEMINI_API_KEY_ENV) or "").strip())


def _has_anthropic_key() -> bool:
    return bool((os.getenv(ANTHROPIC_API_KEY_ENV) or "").strip())


def active_provider() -> str | None:
    """Returns "gemini" / "anthropic" / None — whichever provider's key is
    set AND whose SDK is importable. Gemini wins the tiebreak."""
    if _has_gemini_key():
        try:
            from google import genai  # noqa: F401
            return "gemini"
        except ImportError:
            pass
    if _has_anthropic_key():
        try:
            import anthropic  # noqa: F401
            return "anthropic"
        except ImportError:
            pass
    return None


def is_available() -> bool:
    return active_provider() is not None


# ----------------------------------------------------------------------
# Shared prompt
# ----------------------------------------------------------------------

_SYSTEM_PROMPT = """You are the customer care voice for Zomato — India's
largest food delivery platform. You draft replies for the @zomatocare
social team to use on Twitter/X and Reddit when a user posts a complaint
or question that needs a public response.

VOICE:
  - Warm, human, never corporate-robotic.
  - Acknowledge the user's frustration first, even if their post is angry.
  - Take responsibility plainly. Avoid weasel words ("we apologize for any
    inconvenience" — say "I'm sorry this happened").
  - Use first-person plural ("we") sparingly; "I" is more human in social.
  - Hindi/Hinglish is fine if the original post used it. Match the user's
    register.
  - Sign off with a single first name + " - Zomato" (e.g. "Riya - Zomato").

WHAT TO DO:
  - For order/refund issues: ask user to DM their order ID, promise a
    follow-up. Do NOT promise specific refund amounts; that's an ops call.
  - For food safety/poisoning: take it seriously, ask for order ID + any
    medical reports, escalate to safety team. NEVER deny or argue.
  - For delivery agent misconduct: apologize, ask for order ID, promise
    investigation. NEVER name the agent.
  - For founder/PR-tier mentions: keep it short, polite, redirect to DM.
  - For abusive/threatening posts: polite acknowledgement only, no apology
    for things they didn't actually experience. Don't engage with abuse.
  - For praise: brief thank-you, optionally ask permission to share.
  - For competitor mentions or off-topic: no reply (return reply=null,
    channel="no_reply").

CONSTRAINTS:
  - Reply MUST be <= 280 characters total.
  - DO NOT include hashtags.
  - DO NOT include @mentions other than the user (the platform handles
    that automatically).
  - DO NOT promise outcomes you can't guarantee (specific refund amounts,
    same-day resolution, etc.).
  - If a public reply isn't appropriate (abusive, legal threat, very
    high-profile handle), return reply=null and channel="dm_request" or
    "founder_office".

OUTPUT FORMAT:
  Return a strict JSON object only - no preamble, no markdown fences.
  {
    "reply":    string | null,    // <=280 chars
    "tone":     "empathetic" | "factual" | "celebratory" | "escalatory",
    "channel":  "public_reply" | "dm_request" | "founder_office" | "no_reply",
    "rationale": string           // 1-sentence explanation
  }
"""


def _build_user_prompt(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
) -> str:
    band = priority_breakdown.get("band", "?")
    score = priority_breakdown.get("score")
    score_str = f"{float(score):.2f}" if isinstance(score, (int, float)) else "—"
    tripwires = ", ".join(classification.get("tripwires_fired") or []) or "(none)"
    audience = ", ".join(str(a) for a in classification.get("audience") or []) or "(none)"
    sentiment = classification.get("sentiment", "?")
    side = classification.get("side") or classification.get("category") or "?"
    source = post.get("source", "?")
    author = post.get("author", "anonymous")
    content = (post.get("content") or "").strip()

    return f"""Draft a reply for the following social post.

CONTEXT (do not echo back, just use it):
  source         : {source}
  author         : @{author}
  priority_band  : {band}    (score {score_str})
  side           : {side}
  sentiment      : {sentiment}
  tripwires      : {tripwires}
  audience_route : {audience}

POST:
\"\"\"{content}\"\"\"

Now return the JSON object as specified."""


# ----------------------------------------------------------------------
# Result helpers
# ----------------------------------------------------------------------

def _empty_result(error: str | None = None, provider: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "reply": None,
        "tone": None,
        "channel": None,
        "rationale": None,
        "model": None,
        "provider": provider,
        "error": error,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _success(parsed: ReplyDraft, *, model: str, provider: str) -> dict[str, Any]:
    reply = parsed.reply
    if reply and len(reply) > MAX_REPLY_CHARS:
        reply = reply[: MAX_REPLY_CHARS - 1].rstrip() + "…"
    return {
        "ok": True,
        "reply": reply,
        "tone": parsed.tone,
        "channel": parsed.channel,
        "rationale": parsed.rationale,
        "model": model,
        "provider": provider,
        "error": None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ----------------------------------------------------------------------
# Gemini path
# ----------------------------------------------------------------------

async def _draft_with_gemini(
    user_prompt: str,
    *,
    model: str,
) -> dict[str, Any]:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return _empty_result(error="google-genai SDK is not installed", provider="gemini")

    api_key = os.getenv(GEMINI_API_KEY_ENV) or ""
    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        return _empty_result(error=f"gemini client init failed: {e}", provider="gemini")

    def _call():
        return client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=ReplyDraft,
                temperature=0.4,
            ),
        )

    try:
        import asyncio
        resp = await asyncio.to_thread(_call)
    except Exception as e:
        logger.exception("[replies] gemini call raised")
        return _empty_result(error=f"{type(e).__name__}: {e}", provider="gemini")

    # Gemini's structured output: prefer .parsed, fall back to text-then-json
    parsed = getattr(resp, "parsed", None)
    if parsed is None:
        raw = getattr(resp, "text", "") or ""
        if not raw.strip():
            return _empty_result(error="gemini returned empty response", provider="gemini")
        try:
            parsed = ReplyDraft.model_validate_json(raw)
        except Exception as e:
            logger.warning(f"[replies] gemini non-schema response: {raw[:200]}")
            return _empty_result(
                error=f"could not parse gemini response: {e}",
                provider="gemini",
            )

    if not isinstance(parsed, ReplyDraft):
        # SDK occasionally returns a list when schema is list-typed, or a
        # dict — coerce defensively.
        try:
            if isinstance(parsed, list) and parsed:
                parsed = parsed[0]
            if isinstance(parsed, dict):
                parsed = ReplyDraft.model_validate(parsed)
        except Exception as e:
            return _empty_result(error=f"unexpected parsed shape: {e}", provider="gemini")

    return _success(parsed, model=model, provider="gemini")


# ----------------------------------------------------------------------
# Anthropic path (fallback)
# ----------------------------------------------------------------------

async def _draft_with_anthropic(
    user_prompt: str,
    *,
    model: str,
) -> dict[str, Any]:
    try:
        import anthropic
    except ImportError:
        return _empty_result(error="anthropic SDK is not installed", provider="anthropic")

    client = anthropic.AsyncAnthropic()
    try:
        msg = await client.messages.create(
            model=model,
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.AuthenticationError as e:
        return _empty_result(error=f"auth error - bad API key: {e}", provider="anthropic")
    except anthropic.RateLimitError as e:
        return _empty_result(error=f"rate limited: {e}", provider="anthropic")
    except anthropic.APIError as e:
        return _empty_result(error=f"anthropic API error: {e}", provider="anthropic")
    except Exception as e:
        logger.exception("[replies] anthropic call raised")
        return _empty_result(error=f"{type(e).__name__}: {e}", provider="anthropic")

    raw = "".join(
        block.text for block in msg.content if getattr(block, "type", None) == "text"
    ).strip()
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()

    try:
        parsed = ReplyDraft.model_validate_json(raw)
    except Exception:
        try:
            data = json.loads(raw)
            parsed = ReplyDraft.model_validate(data)
        except Exception as e:
            logger.warning(f"[replies] anthropic non-schema response: {raw[:200]}")
            return _empty_result(
                error=f"could not parse anthropic response: {e}",
                provider="anthropic",
            )

    return _success(parsed, model=model, provider="anthropic")


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

async def draft_reply(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Draft a customer-care reply via the configured LLM provider.

    Args:
        provider: "gemini" / "anthropic" / None.  None = auto (Gemini wins).
        model:    optional model override (e.g. "gemini-2.5-pro").

    Returns the result dict described at module docstring. Never raises.
    """
    chosen_provider = provider or active_provider()
    if chosen_provider is None:
        return _empty_result(
            error=(
                "Neither GEMINI_API_KEY nor ANTHROPIC_API_KEY is set in .env. "
                "Get a free Gemini key at https://aistudio.google.com/apikey"
            )
        )

    user_prompt = _build_user_prompt(post, classification, priority_breakdown)

    if chosen_provider == "gemini":
        return await _draft_with_gemini(
            user_prompt, model=model or DEFAULT_GEMINI_MODEL
        )
    if chosen_provider == "anthropic":
        return await _draft_with_anthropic(
            user_prompt, model=model or DEFAULT_ANTHROPIC_MODEL
        )
    return _empty_result(error=f"unknown provider: {chosen_provider}")


__all__ = [
    "is_available",
    "active_provider",
    "draft_reply",
    "ReplyDraft",
    "GEMINI_API_KEY_ENV",
    "ANTHROPIC_API_KEY_ENV",
    "MAX_REPLY_CHARS",
]
