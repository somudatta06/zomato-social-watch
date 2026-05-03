"""Watchlists — curated seed handle lists for the author tier system.

These are the rules-side answer to "who is this poster?". Every handle here
was added because losing a complaint from this account would be a
career-defining miss for the social team. The lists are deliberately small
(curated, auditable, defensible) and grow via explicit ops review — not by
ML drift.

Tier mapping (see docs/CLASSIFICATION_DEEP_DIVE.md §8):
    AUTHORITY_HANDLES    → T0 (10× reach multiplier, always-P0)
    POLITICIAN_HANDLES   → T0/T1 cohort, fed into politician_handle tripwire
    PRESS_HANDLES        → T1 (5× multiplier, always-PR routing)
    FOUNDER_HANDLES      → T0 — Zomato leadership treated as authority

Naming convention: handles stored WITHOUT the leading '@', lowercase. The
storage and lookup helpers always lowercase before comparing.

Sources:
- Tripwires (taxonomy/tripwires.yaml) seeds journalist_handle and
  politician_handle lists; we expand here so curators can edit one place.
- All handles are real, verified accounts on X/Twitter as of 2026-04.
- New entries require a real-world rationale comment (e.g. "Mint — Indian
  business press, top-3 financial daily").
"""
from __future__ import annotations

from typing import Iterable

# ============================================================
# T0 — Authority: regulators, heads-of-state, founder/CEO of Fortune 500.
# Reach multiplier 10×; routing forces P0 + founder-office cc.
# ============================================================
AUTHORITY_HANDLES: set[str] = {
    # Regulators with direct jurisdiction over Zomato/food-tech in India
    "cci_india",          # Competition Commission of India
    "rbi",                # Reserve Bank of India (payments / fintech overlap)
    "sebi_india",         # Securities and Exchange Board (listed-co regulator)
    "fssai_india",        # Food Safety and Standards Authority of India
    "consaff_govt",       # Department of Consumer Affairs
    # Head-of-state account — shared with politicians but T0 wins on
    # multiplier when the same handle appears in both sets.
    "narendramodi",       # PM of India
    "pmoindia",           # Prime Minister's Office
}


# ============================================================
# T0/T1 — Politicians: senior MPs, ministers, party-official handles.
# Most are T1 (5×); CMs/heads-of-government can be lifted to T0 by ops.
# Always-P0 routing via tripwires.yaml politician_handle anyway.
# ============================================================
POLITICIAN_HANDLES: set[str] = {
    # Cabinet ministers
    "narendramodi",       # PM
    "amitshah",           # Home Minister
    "piyushgoyal",        # Commerce & Industry Minister (consumer affairs)
    "nitin_gadkari",      # Road Transport & Highways
    "smriti_irani",       # Women & Child Dev (was Comm. Minister; senior BJP)
    "ashwinivaishnaw",    # Railways & Information Tech
    "dpradhanbjp",        # Education + senior BJP
    "rajnathsingh",       # Defence
    "drsjaishankar",      # External Affairs
    # Opposition senior leaders
    "rahulgandhi",        # INC senior MP
    "priyankagandhi",     # INC General Secretary
    "arvindkejriwal",     # AAP National Convenor
    "mlkhattar",          # Power & Housing Minister
    # Regional CMs & senior state leaders
    "naveenodisha",       # CM Odisha (former)
    "hdkumaraswamy",      # Karnataka — Heavy Industries Min
    "ysjagan",            # Andhra Pradesh — YSRCP
    "mkstalin",           # CM Tamil Nadu — DMK
    "mamataofficial",     # CM West Bengal — TMC
    "myogiadityanath",    # CM Uttar Pradesh
    "bhupendrapbjp",      # CM Gujarat
    # Party-official handles
    "bjp4india",
    "incindia",
    "aamaadmiparty",
    "aitcofficial",       # All India Trinamool Congress
    "shivsena_ab",        # Shiv Sena (UBT)
    "dmkitwing",          # DMK
}


# ============================================================
# T1 — Press: major Indian + global business/tech media handles.
# Reach multiplier 5×; PR drafts response, treat as interview request.
# ============================================================
PRESS_HANDLES: set[str] = {
    # Indian business / financial press (top-tier)
    "reutersindia",
    "bloombergquint",        # Now BQ Prime, kept for legacy
    "bqprime",
    "economictimes",
    "et_now",
    "ettech",                # ET Tech vertical (food-tech coverage)
    "livemint",
    "mint",
    "moneycontrolcom",
    "business_standard",
    "the_hindu",
    "indianexpress",
    "toi_business",
    "ndtv",
    "ndtvprofit",            # NDTV's business channel
    "ndtvindia",
    "cnbctv18news",
    "cnbctv18live",
    "businessworldmag",
    "bwbusinessworld",
    "financialexpress",
    "httweets",              # Hindustan Times
    "the_print",
    "news18dotcom",
    "news18india",
    # Indian tech / startup press (Zomato-relevant)
    "inc42",
    "yourstory",
    "entrackr",
    "moneycontrolpro",
    "mojonews_in",
    "the_ken",
    "the_morningcontext",
    # Global business press (story breakouts hit India fast)
    "reuters",
    "reutersbiz",
    "bloomberg",
    "bloombergasia",
    "bloomberglive",
    "ft",
    "ftcomtech",
    "wsj",
    "wsjbusiness",
    "forbes",
    "forbesindia",
    "fortunemagazine",
    "techcrunch",
    "theverge",
}


# ============================================================
# T0 — Founder/leadership: Zomato's own senior people.
# Reach multiplier 10× (anything they say IS the brand voice). Routing
# forces founder-office attention; never auto-reply on these handles.
# ============================================================
FOUNDER_HANDLES: set[str] = {
    "deepigoyal",         # Deepinder Goyal — founder/CEO
    "gaurav_tw",          # Gaurav Gupta — co-founder (alt handle in tripwires)
    "kabeerbiswas",       # Kabeer Biswas — Co-founder (district / blinkit lineage)
    "akshanttgoyal",      # Akshant Goyal — CFO
    "zomato",             # Brand handle itself
    "zomatocare",         # Customer-care brand handle
    "zomatoin",           # Regional brand handle
}


# ============================================================
# Bio-keyword → tier hints (case-insensitive substring match).
# Used by handles.compute_tier when bio is available. Order matters —
# first match wins, more specific patterns first.
# ============================================================
PRESS_BIO_KEYWORDS: tuple[str, ...] = (
    "journalist",
    "reporter",
    "correspondent",
    "news anchor",
    "anchor at",
    "editor at",
    "editor-in-chief",
    "editor in chief",
    "deputy editor",
    "managing editor",
    "bureau chief",
    "columnist",
    "staff writer",
    "tech writer",
)

POLITICIAN_BIO_KEYWORDS: tuple[str, ...] = (
    "member of parliament",
    "lok sabha",
    "rajya sabha",
    "minister of",
    "cabinet minister",
    "union minister",
    "bjp mp",
    "congress mp",
    "aap mp",
    "tmc mp",
    "mp,",                    # "MP, Bangalore South" style
    "mla,",
    "former minister",
    "chief minister",
    "deputy cm",
)


# ============================================================
# Public registry — name → set, for the dashboard / admin UI.
# ============================================================
_REGISTRY: dict[str, set[str]] = {
    "authority":   AUTHORITY_HANDLES,
    "politician":  POLITICIAN_HANDLES,
    "press":       PRESS_HANDLES,
    "founder":     FOUNDER_HANDLES,
}


# ============================================================
# Helpers
# ============================================================
def _normalize(handle: str | None) -> str:
    """Lowercase, strip leading '@'. Empty string for None."""
    if not handle:
        return ""
    return handle.strip().lstrip("@").lower()


def get_watchlist(name: str) -> set[str]:
    """Return the seed set for a watchlist by name. Empty set if unknown."""
    return set(_REGISTRY.get(name.lower(), set()))


def all_watchlist_names() -> list[str]:
    return sorted(_REGISTRY.keys())


def memberships_for(handle: str | None) -> list[str]:
    """Which watchlists does this handle belong to?
    Returns a sorted list of list_names. Lowercase-normalized comparison."""
    h = _normalize(handle)
    if not h:
        return []
    return sorted(name for name, members in _REGISTRY.items() if h in members)


def seed_pairs() -> Iterable[tuple[str, str]]:
    """Yield (handle, list_name) for every seed entry. Used by the
    watchlist_memberships table backfill."""
    for name, members in _REGISTRY.items():
        for h in members:
            yield (_normalize(h), name)
