"""Noise filter — keeps the inbox a *signal* feed, not a firehose.

Design (per the conversation that triggered this):
    * No scores. No 0-1 floats. No "suspect" badges in the UI.
    * Each post is tagged with exactly ONE category at ingest. NULL means
      clean — those are the posts the operator actually sees.
    * Noise lives behind a collapsed sidebar section. The operator never
      reads a confidence number; they pick a bucket if they want one.

Categories (priority order — first match wins):
    bot         — empty/URL-only post, OR author tagged 'bot' tier
    promo       — voucher reselling, RT-to-win, crypto/airdrop scams
    job         — recruiter spam, "we're hiring", apply-now posts
    stock       — $ZOMATO cashtag, target prices, NSE/BSE chatter
    off_topic   — "zomato" used as a verb, OR mention without any
                  customer-care signal (no @-tag + no action verb)
    None        — clean. Default inbox shows only these.

Why categorical instead of scored:
    A score forces the human to interpret it. "0.62 noise" means the
    operator has to remember if 0.5 or 0.7 is the threshold, what each
    sub-score weight is, etc. A discrete bucket name is read once and
    understood. Speed-of-decision matters in a war-room dashboard.

Public API:
    categorize(post, classification, handle_row=None) -> str | None
    CATEGORY_LABELS                  # human display names per category
    CATEGORY_DISPLAY_ORDER           # for the sidebar
"""
from __future__ import annotations

import re
from typing import Any

# ============================================================
# Display metadata — used by the UI; the categorize function
# itself returns the bare key or None.
# ============================================================

CATEGORY_LABELS: dict[str, str] = {
    "promo":     "Promo & spam",
    "job":       "Jobs & hiring",
    "stock":     "Stock chatter",
    "off_topic": "Off-topic",
    "bot":       "Bots & low-effort",
}

# Order shown in the "Filtered out" sidebar section.
CATEGORY_DISPLAY_ORDER: list[str] = ["promo", "job", "stock", "off_topic", "bot"]

# Lucide icon name per category — keeps the sidebar visually parsable.
CATEGORY_ICONS: dict[str, str] = {
    "promo":     "tag",
    "job":       "briefcase",
    "stock":     "trending-up",
    "off_topic": "shuffle",
    "bot":       "bot",
}


# ============================================================
# Compiled patterns. Keep these readable — the operator may
# need to defend any false positive in code review.
# ============================================================

# Action verbs that signal "this is an actual customer concern" — used
# both for off_topic detection (their absence is suspicious) and to
# rescue a post that would otherwise look like noise.
_ACTION_VERBS_RE = re.compile(
    r"\b(order(?:ed|ing)?|deliver(?:y|ed|ing)?|refund(?:ed|s)?|cancel(?:led|ling|s)?|"
    r"agent|rider|support|complaint|food|restaurant|menu|payment|charged?|missing|"
    r"refused?|spoiled?|rotten|wrong|late|delay(?:ed)?|stale|poison(?:ed|ing)?|"
    r"hospital|emergency|fraud|scam|fssai|consumer\s*court|fir|legal|"
    r"@zomato|@zomatocare)\b",
    re.IGNORECASE,
)

# @mention of zomato or zomatocare — strong signal the post is directed
# at the brand. Even a vague rant becomes legitimately addressed when
# tagged this way.
_ZOMATO_MENTION_RE = re.compile(r"@zomato(?:care)?\b", re.IGNORECASE)

# ----- promo / spam -----
_PROMO_PATTERNS = [
    # Voucher / coupon / gift-card reselling — usually paired with DM/contact prompts
    re.compile(
        r"\b(zomato|food|gift)\s*"
        r"(voucher|coupon|gift\s*card|gc|promo\s*code)s?\b"
        r".{0,80}\b(sell|selling|buy|buying|dm\s+me|whatsapp|reseller|@\d+)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\b\d+%\s*(off|discount|cashback)\b.{0,60}\b(use|code|coupon|dm|whatsapp)", re.IGNORECASE),
    # RT-to-win / giveaway hashtags
    re.compile(r"\b(rt|retweet)\s+(and|to)\s+(win|get|join|qualify)\b", re.IGNORECASE),
    re.compile(r"#(giveaway[a-z_]*|freegift|win[a-z]*alert|contest[a-z_]*)\b", re.IGNORECASE),
    # Crypto / airdrop / scam
    re.compile(r"\b(airdrop|presale|10x|100x|shitcoin|memecoin|defi)\b", re.IGNORECASE),
    re.compile(r"\bguaranteed\s+(profit|returns?|gains?)\b", re.IGNORECASE),
    re.compile(r"\b(double|triple)\s+your\s+money\b", re.IGNORECASE),
    # Reseller marketplace patterns ("[H] [W] trade")
    re.compile(r"\[\s*[HW]\s*\].{0,40}\bzomato\b.{0,60}\[\s*[HW]\s*\]", re.IGNORECASE),
    re.compile(r"\b(selling|buying)\b.{0,30}\bzomato\s*(gc|gift|voucher|credit)", re.IGNORECASE),
]

# ----- jobs / recruiter -----
_JOB_PATTERNS = [
    re.compile(r"\b(zomato|company|we|they)\s+(is|are|'re)\s+hiring\b", re.IGNORECASE),
    re.compile(r"\b(zomato.{0,30}hiring|hiring.{0,30}zomato)\b", re.IGNORECASE),
    re.compile(r"\bjob\s+(opening|opportunity|alert|posting|notification)\b.{0,60}\bzomato\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bzomato\b.{0,30}\b(career|careers|opening|vacancy|recruit(?:er|ing|ment))", re.IGNORECASE),
    re.compile(r"\b(apply\s+(now|here|via)|application\s+(open|link))\b.{0,60}\bzomato\b", re.IGNORECASE | re.DOTALL),
    # Resume/profile spam
    re.compile(r"\b(my|share)\s+(resume|cv)\b.{0,60}\bzomato\b", re.IGNORECASE | re.DOTALL),
]

# ----- stock / finance -----
_STOCK_PATTERNS = [
    re.compile(r"\$ZOMATO\b"),                                     # cashtag
    re.compile(r"\bZOMATO\b\s+(share|stock)\s+(price|target|tgt|sl)", re.IGNORECASE),
    re.compile(r"\btarget\s+(price|tgt|sl|stop[\s-]*loss)\b.{0,40}\bzomato\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bzomato\b.{0,40}\btarget\s+(price|tgt|sl|stop[\s-]*loss)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(NSE|BSE|NIFTY|sensex)\b.{0,80}\bzomato\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bzomato\b.{0,40}\b(buy|sell|short)\s+at\s+₹?\s*\d", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bzomato\b.{0,40}\b(intraday|swing|positional)\s+(buy|trade|call|tip)", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bzmto\b", re.IGNORECASE),                         # ticker shorthand
]

# ----- off-topic / verb usage -----
# Verb usage of zomato is a clear signal — but rare. Most off-topic
# posts are ones where "zomato" appears with no @-tag and no action verb.
_OFFTOPIC_VERB_RE = re.compile(r"\bzomato(ing|ed|s)\b", re.IGNORECASE)

# ----- empty / low-effort content -----
_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)
_PURE_EMOJI_RE = re.compile(r"^[\W_]+$")  # no alphanumerics at all


# ============================================================
# Categorize
# ============================================================

def _is_empty_post(content: str) -> bool:
    """True if the content has effectively no text. URL-only, single
    emoji, or fewer than 12 alphanumeric characters."""
    if not content:
        return True
    s = content.strip()
    if len(s) < 12:
        return True
    if _URL_ONLY_RE.match(s):
        return True
    if _PURE_EMOJI_RE.match(s):
        return True
    # Strip URLs and check what's left
    no_urls = re.sub(r"https?://\S+", "", s).strip()
    alphanum = re.sub(r"[^a-zA-Z0-9]", "", no_urls)
    return len(alphanum) < 12


def _matches_any(content: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(content) for p in patterns)


def categorize(
    post: dict[str, Any],
    classification: dict[str, Any] | None = None,
    handle_row: dict[str, Any] | None = None,
) -> str | None:
    """Return the noise category for a post, or None if it's clean.

    Args:
        post: dict with at least 'content' and 'author'.
        classification: classifier output (sentiment, side, tripwires_fired).
                        Optional — categorization runs without it but uses
                        it as a tripwire-rescue signal when available.
        handle_row: row from `handles` table for this author. Used to
                    short-circuit to 'bot' for known bot accounts.

    Returns:
        One of the strings in CATEGORY_DISPLAY_ORDER, or None for clean.
    """
    content = post.get("content") or ""
    classification = classification or {}

    # ----- TRIPWIRE RESCUE -----
    # Any post that fired a real tripwire (food safety, court FIR, etc.)
    # is by definition NOT noise — even if it would otherwise look like
    # spam. Skip categorization entirely.
    fired = classification.get("tripwires_fired") or []
    if fired:
        return None

    # ----- BOT (priority 1) -----
    # Author tagged as bot tier wins immediately, regardless of text.
    if handle_row:
        if (handle_row.get("profile_class") or "").lower() == "bot":
            return "bot"
        if (handle_row.get("tier") or "").upper() == "T7":  # bot tier in our scheme
            return "bot"
    # Empty or URL-only content with no @-mention is a bot/low-effort post.
    if _is_empty_post(content) and not _ZOMATO_MENTION_RE.search(content):
        return "bot"

    # ----- PROMO (priority 2) -----
    if _matches_any(content, _PROMO_PATTERNS):
        return "promo"

    # ----- JOB (priority 3) -----
    if _matches_any(content, _JOB_PATTERNS):
        return "job"

    # ----- STOCK (priority 4) -----
    if _matches_any(content, _STOCK_PATTERNS):
        return "stock"

    # ----- OFF-TOPIC (priority 5) -----
    # Verb usage is a clear signal.
    if _OFFTOPIC_VERB_RE.search(content):
        return "off_topic"

    # Mention with no customer-care signals AND no @-tag is borderline.
    # We're conservative here — only flag if the post is also short
    # (long posts probably contain real grievance even if no exact verb
    # match), to avoid suppressing real complaints.
    has_mention = bool(_ZOMATO_MENTION_RE.search(content))
    has_action = bool(_ACTION_VERBS_RE.search(content))
    if not has_mention and not has_action and len(content) < 120:
        # Final guard: if the post HAS any sentiment intensity (negative,
        # abusive), don't flag it — it's likely a frustrated customer
        # whose complaint we should still see.
        sentiment = (classification.get("sentiment") or "").lower()
        if sentiment not in ("negative", "abusive"):
            return "off_topic"

    return None


# ============================================================
# Bulk helper for backfill / pipeline integration
# ============================================================

def categorize_many(
    rows: list[dict[str, Any]],
    handle_lookup: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, str | None]:
    """Categorize a batch of posts. Returns {post_id: category_or_None}.

    Args:
        rows: list of post dicts (must include 'id', 'content', 'source',
              'author', and optionally 'classification' as either dict or
              JSON string).
        handle_lookup: optional dict keyed by (source, author_lower) →
                       handle row dict. Avoids per-row DB hits during
                       backfill.
    """
    import json as _json

    out: dict[str, str | None] = {}
    for r in rows:
        cls = r.get("classification")
        if isinstance(cls, str):
            try:
                cls = _json.loads(cls)
            except Exception:
                cls = {}
        author = (r.get("author") or "").lower().lstrip("@")
        source = r.get("source") or ""
        h = (handle_lookup or {}).get((source, author))
        out[r["id"]] = categorize(r, classification=cls or {}, handle_row=h)
    return out


__all__ = [
    "categorize",
    "categorize_many",
    "CATEGORY_LABELS",
    "CATEGORY_DISPLAY_ORDER",
    "CATEGORY_ICONS",
]
