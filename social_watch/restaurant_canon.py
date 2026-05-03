"""Canonical restaurant identity layer.

The raw extractor (`extraction.extract_restaurants`) returns string matches
from text. Some of those are platform self-references ("Zomato"), some are
parser garbage ("Zomato.They"), and many are aliases of the same brand
("KFC" vs "KFC_India" vs "KFCIndia", "Dominos" vs "Domino's", "McDonalds"
vs "McDonald's").

This module collapses those into a single canonical name per brand, or
drops the match entirely if it isn't a real restaurant. Aggregators call
``canonicalize()`` once per raw match before grouping.

Three stages, in order:

    raw match
       │
       ▼
    SANITIZE         length bounds + parser-garbage pattern reject
       │
       ▼
    STOPLIST         platform self-references + generic non-brand words
       │
       ▼
    CANONICALIZE     known brand alias map; long-tail pass-through with
                     a normalized "_india"/" india" suffix strip so e.g.
                     "Behrouz" and "Behrouz India" merge naturally
       │
       ▼
    canonical brand name (or None to drop)

When you find a new variant in the wild, add it to ``BRAND_ALIASES``.
When you find a new junk string, add it to ``PLATFORM_STOPLIST`` or
``GENERIC_STOPLIST``. That's the only thing this file is for.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Stage 2 — Stoplists
# ---------------------------------------------------------------------------

# Platform self-references. These show up because tweets often say
# "ordered from Zomato" — the platform is not a restaurant. Bucket
# this loosely: food platforms, quick-commerce, and the broader
# marketplace/courier set that frequently appears in "from <X>" text.
PLATFORM_STOPLIST: frozenset[str] = frozenset({
    # Zomato itself
    "zomato", "zomatocare", "zomato_india", "zomato india",
    "zomato delivery", "zomato hyperpure", "zomato pay",
    "zomato_in", "zomatoindia",
    # Direct competitors / parent / sister brands
    "swiggy", "swiggycares", "swiggy_in", "swiggy india",
    "blinkit", "zepto", "instamart", "eternal",
    # Generic marketplaces and couriers that get caught by "from <X>"
    "amazon", "amazonin", "amazon india", "flipkart", "meesho",
    "myntra", "nykaa", "ajio", "snapdeal", "shopsy",
    "dunzo", "porter", "rapido", "uber", "ola", "ubereats",
    "fedex", "delhivery", "bluedart", "dhl", "ekart",
    # Founders / handles that got crawled as restaurants
    "deepigoyal", "deepi goyal",
})

# Generic words that aren't a restaurant when they sneak in via the
# "from <Name>" phrase regex (e.g., "delivery from agent").
GENERIC_STOPLIST: frozenset[str] = frozenset({
    "food", "restaurant", "delivery", "order", "app", "agent",
    "customer", "service", "support", "team", "store", "merchant",
    "phone", "call", "executive", "rider", "captain",
})

# Geographic features that aren't cities (so the extraction city map
# doesn't catch them) but also aren't restaurants. These show up when
# a tweet says "ordered from flyover" or "got from highway dhaba".
# City names themselves are handled by extraction.CITY_NEIGHBORHOOD_MAP
# at the extractor layer, not here — keep this list focused on generic
# place-feature words.
LOCATION_STOPLIST: frozenset[str] = frozenset({
    "flyover", "highway", "expressway", "junction", "circle", "chowk",
    "market", "bazaar", "bazar", "mandi", "lane", "gali", "road",
    "station", "stop", "depot", "terminus", "platform",
    "mall", "plaza", "complex", "tower", "society", "colony",
    "sector", "phase", "block", "naka", "morh", "more", "nagar",
    "vihar", "puram", "pura", "bagh", "garden", "park",
})

# ---------------------------------------------------------------------------
# Stage 3 — Brand alias map
# ---------------------------------------------------------------------------

# Keys are normalized lookup keys (lowercase, alnum only, "_india"/" india"
# suffix stripped) — see ``_to_key``. Values are the canonical display name.
BRAND_ALIASES: dict[str, str] = {
    "kfc": "KFC",
    "mcdonalds": "McDonald's",
    "mcd": "McDonald's",
    "dominos": "Domino's Pizza",
    "domino": "Domino's Pizza",
    "dominospizza": "Domino's Pizza",
    "pizzahut": "Pizza Hut",
    "burgerking": "Burger King",
    "starbucks": "Starbucks",
    "subway": "Subway",
    "tacobell": "Taco Bell",
    "wendys": "Wendy's",
    "haldirams": "Haldiram's",
    "haldiram": "Haldiram's",
    "bikanervala": "Bikanervala",
    "saravanabhavan": "Saravana Bhavan",
    "behrouzbiryani": "Behrouz Biryani",
    "behrouz": "Behrouz Biryani",
    "faasos": "Faasos",
    "box8": "Box8",
    "eatfit": "EatFit",
    "freshmenu": "FreshMenu",
    "wowmomo": "Wow! Momo",
    "chaipoint": "Chai Point",
    "chaayos": "Chaayos",
    "theobroma": "Theobroma",
    "biryaniblues": "Biryani Blues",
    "paradisebiryani": "Paradise Biryani",
    "mainlandchina": "Mainland China",
    "ovenstory": "Oven Story",
    "lunchbox": "LunchBox",
    "sweettruth": "Sweet Truth",
    "smoor": "Smoor",
    "thirdwavecoffee": "Third Wave Coffee",
    "bluetokai": "Blue Tokai",
    "costacoffee": "Costa Coffee",
    "cafecoffeeday": "Cafe Coffee Day",
    "ccd": "Cafe Coffee Day",
    "barista": "Barista",
    "sagarratna": "Sagar Ratna",
    "punjabibynature": "Punjabi By Nature",
    "bhukkad": "Bhukkad",
}

# ---------------------------------------------------------------------------
# Stage 1 — Sanity patterns
# ---------------------------------------------------------------------------

# A period sandwiched between a word char and a Capital+lowercase pair is
# almost always a missed sentence boundary, not a brand name. Examples that
# should drop: "Zomato.They", "Hello.Stay".
_PARSER_GARBAGE = re.compile(r"\w\.[A-Z][a-z]")

# Strip a trailing country suffix ("_india", " india") so display names
# don't carry it and so the long-tail pass-through merges variants.
_INDIA_SUFFIX = re.compile(r"[_\s]+india$", re.IGNORECASE)

_OUTER_PUNCT = ".,!?:;\"'()[]{}_"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _to_key(name: str) -> str:
    """Stable lookup key: lowercase, drop a trailing India suffix
    ("_india", " india", or even glued-on "India"), then collapse to
    alnum-only.

    Examples::

        _to_key("KFC")            -> "kfc"
        _to_key("KFC_India")      -> "kfc"
        _to_key("KFC India")      -> "kfc"
        _to_key("KFCIndia")       -> "kfc"
        _to_key("Domino's Pizza") -> "dominospizza"
    """
    s = name.strip().lower().strip(_OUTER_PUNCT)
    s = _INDIA_SUFFIX.sub("", s).strip()
    key = re.sub(r"[^a-z0-9]+", "", s)
    # Glued "India" suffix that survived the separator-aware strip above.
    # Require at least 3 chars before "india" so we don't eat short keys.
    if key.endswith("india") and len(key) >= 8:
        key = key[:-5]
    return key


def _display_clean(name: str) -> str:
    """Strip surrounding punctuation and a trailing India suffix from
    a long-tail name we're passing through unchanged."""
    s = name.strip().strip(_OUTER_PUNCT)
    s = _INDIA_SUFFIX.sub("", s).strip()
    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def canonicalize(name: str) -> str | None:
    """Take a raw extracted restaurant name. Return the canonical brand
    name to count against, or ``None`` if the match should be dropped.

    Drop reasons (any one is enough):
      * empty / too short / too long
      * contains parser garbage (a missed sentence boundary)
      * is a platform self-reference (Zomato, Swiggy, …)
      * is a generic non-brand word (food, agent, …)
    """
    if not name:
        return None
    s = name.strip()
    if len(s) < 2 or len(s) > 60:
        return None
    if _PARSER_GARBAGE.search(s):
        return None

    raw_lower = s.lower().strip(_OUTER_PUNCT)
    if raw_lower in PLATFORM_STOPLIST:
        return None
    if raw_lower in GENERIC_STOPLIST:
        return None
    if raw_lower in LOCATION_STOPLIST:
        return None
    # First or last word of a multi-word candidate is a location
    # feature ("Sector 17", "Andheri Junction", "Bandra Lane"): drop.
    # Genuine restaurants we care about don't open or close on a
    # generic feature word.
    tokens = raw_lower.split()
    if len(tokens) >= 2 and (tokens[0] in LOCATION_STOPLIST or tokens[-1] in LOCATION_STOPLIST):
        return None

    key = _to_key(s)
    if not key:
        return None
    if key in BRAND_ALIASES:
        return BRAND_ALIASES[key]

    cleaned = _display_clean(s)
    return cleaned or None


def canonicalize_many(names: list[str]) -> list[str]:
    """Run ``canonicalize`` over a list and dedupe within the call.

    Useful when one post yields multiple raw matches that collapse to
    the same brand (e.g., a tweet mentioning both "@KFC_India" and
    "from KFC" should count as one mention of KFC, not two).
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        c = canonicalize(raw)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Sanity tests (run via `python -m social_watch.restaurant_canon`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cases: list[tuple[str, str | None]] = [
        # Platform self-reference
        ("Zomato", None),
        ("zomato", None),
        ("Zomato India", None),
        ("Swiggy", None),
        # Parser garbage
        ("Zomato.They", None),
        ("Hello.World", None),
        # Brand canonicalization
        ("KFC", "KFC"),
        ("KFC_India", "KFC"),
        ("KFC India", "KFC"),
        ("KFCIndia", "KFC"),
        ("Dominos", "Domino's Pizza"),
        ("Domino's", "Domino's Pizza"),
        ("Domino's Pizza", "Domino's Pizza"),
        ("McDonalds", "McDonald's"),
        ("McDonald's", "McDonald's"),
        # Generic words
        ("food", None),
        ("Agent", None),
        # Long tail pass-through
        ("Seema Chinese", "Seema Chinese"),
        ("Burger King", "Burger King"),
        # Length guards
        ("a", None),
        ("", None),
        # Marketplace stoplist
        ("Amazon", None),
        ("Flipkart", None),
        ("Dunzo", None),
        # Location features
        ("Flyover", None),
        ("Highway", None),
        ("Andheri Junction", None),
        ("Sector 17", None),
        ("Bandra Lane", None),
        # Real restaurants must still pass
        ("Wow! Momo", "Wow! Momo"),
        ("Behrouz Biryani", "Behrouz Biryani"),
    ]
    fail = 0
    for inp, want in cases:
        got = canonicalize(inp)
        ok = got == want
        if not ok:
            fail += 1
        print(f"{'PASS' if ok else 'FAIL'}: canonicalize({inp!r}) -> {got!r}  (want {want!r})")
    print(f"\n{len(cases) - fail}/{len(cases)} passed")
    raise SystemExit(0 if fail == 0 else 1)
