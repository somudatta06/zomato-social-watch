"""Pure regex-based extractors for restaurants, cities, dishes, and order IDs.

No external deps. All public functions return ``list[str]`` with deduped,
canonicalised values. Used by the operations dashboard to roll up posts
by who, where, and what.

Run as ``python -m social_watch.extraction`` to execute the sanity tests
in the ``__main__`` block.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Whitelists / dictionaries
# ---------------------------------------------------------------------------

# Restaurant brands commonly mentioned in Indian food-delivery contexts.
# Stored as the canonical-cased name we want to surface in the UI. Apostrophe
# variants are added explicitly so we match both "Domino's" and "Dominos".
INDIAN_QSR_BRANDS: tuple[str, ...] = (
    "KFC",
    "McDonald's", "McDonalds",
    "Domino's", "Dominos",
    "Pizza Hut",
    "Subway",
    "Burger King",
    "Starbucks",
    "Haldiram's", "Haldiram",
    "Bikanervala",
    "Saravana Bhavan",
    "Behrouz Biryani", "Behrouz",
    "Faasos",
    "Box8",
    "EatFit",
    "FreshMenu",
    "Wow! Momo",
    "Chai Point",
    "Chaayos",
    "Theobroma",
    "Biryani Blues",
    "Paradise Biryani",
    "Mainland China",
    "Oven Story",
    "LunchBox",
    "Sweet Truth",
    "Smoor",
    "Third Wave Coffee",
    "Blue Tokai",
    "Costa Coffee",
    "Cafe Coffee Day", "CCD",
    "Barista",
    "Taco Bell",
    "Wendy's",
    "Sagar Ratna",
    "Punjabi By Nature",
    "Bhukkad",
)

# Map an alias (lowercased) to its canonical city.
CITY_NEIGHBORHOOD_MAP: dict[str, str] = {
    # Mumbai
    "mumbai": "Mumbai",
    "bombay": "Mumbai",
    "bandra": "Mumbai",
    "andheri": "Mumbai",
    "powai": "Mumbai",
    "kurla": "Mumbai",
    "borivali": "Mumbai",
    "worli": "Mumbai",
    "lower parel": "Mumbai",
    "dadar": "Mumbai",
    "juhu": "Mumbai",
    "vile parle": "Mumbai",
    "malad": "Mumbai",
    "goregaon": "Mumbai",
    "thane": "Mumbai",
    "navi mumbai": "Mumbai",

    # Delhi NCR
    "delhi": "Delhi",
    "new delhi": "Delhi",
    "connaught place": "Delhi",
    "cp": "Delhi",
    "gurgaon": "Delhi",
    "gurugram": "Delhi",
    "noida": "Delhi",
    "saket": "Delhi",
    "lajpat nagar": "Delhi",
    "gk": "Delhi",
    "greater kailash": "Delhi",
    "dwarka": "Delhi",
    "karol bagh": "Delhi",
    "hauz khas": "Delhi",
    "vasant kunj": "Delhi",
    "rohini": "Delhi",
    "janakpuri": "Delhi",

    # Bengaluru
    "bengaluru": "Bengaluru",
    "bangalore": "Bengaluru",
    "koramangala": "Bengaluru",
    "indiranagar": "Bengaluru",
    "hsr": "Bengaluru",
    "hsr layout": "Bengaluru",
    "whitefield": "Bengaluru",
    "btm": "Bengaluru",
    "btm layout": "Bengaluru",
    "mg road": "Bengaluru",
    "jayanagar": "Bengaluru",
    "jp nagar": "Bengaluru",
    "marathahalli": "Bengaluru",
    "electronic city": "Bengaluru",
    "hebbal": "Bengaluru",
    "yelahanka": "Bengaluru",

    # Hyderabad
    "hyderabad": "Hyderabad",
    "banjara hills": "Hyderabad",
    "hitec city": "Hyderabad",
    "hitech city": "Hyderabad",
    "gachibowli": "Hyderabad",
    "kondapur": "Hyderabad",
    "jubilee hills": "Hyderabad",
    "madhapur": "Hyderabad",
    "secunderabad": "Hyderabad",
    "begumpet": "Hyderabad",
    "kukatpally": "Hyderabad",

    # Chennai
    "chennai": "Chennai",
    "madras": "Chennai",
    "t nagar": "Chennai",
    "t. nagar": "Chennai",
    "adyar": "Chennai",
    "velachery": "Chennai",
    "omr": "Chennai",
    "anna nagar": "Chennai",
    "nungambakkam": "Chennai",
    "mylapore": "Chennai",
    "tambaram": "Chennai",
    "porur": "Chennai",

    # Kolkata
    "kolkata": "Kolkata",
    "calcutta": "Kolkata",
    "park street": "Kolkata",
    "salt lake": "Kolkata",
    "new town": "Kolkata",
    "howrah": "Kolkata",
    "ballygunge": "Kolkata",
    "dum dum": "Kolkata",

    # Pune
    "pune": "Pune",
    "koregaon park": "Pune",
    "viman nagar": "Pune",
    "hinjewadi": "Pune",
    "baner": "Pune",
    "aundh": "Pune",
    "kothrud": "Pune",
    "wakad": "Pune",
    "magarpatta": "Pune",
    "hadapsar": "Pune",

    # Standalone metros
    "ahmedabad": "Ahmedabad",
    "jaipur": "Jaipur",
    "lucknow": "Lucknow",
    "chandigarh": "Chandigarh",
    "kochi": "Kochi",
    "cochin": "Kochi",
    "indore": "Indore",
    "surat": "Surat",
    "bhopal": "Bhopal",
    "nagpur": "Nagpur",
    "coimbatore": "Coimbatore",
    "vizag": "Visakhapatnam",
    "visakhapatnam": "Visakhapatnam",
    "mysuru": "Mysuru",
    "mysore": "Mysuru",

    # Tier-2 / tier-3 cities. Adding these makes the city heatmap richer
    # and stops them from leaking into the restaurant list via "from <X>".
    "patna": "Patna",
    "ranchi": "Ranchi",
    "muzaffarpur": "Muzaffarpur",
    "gaya": "Gaya",
    "varanasi": "Varanasi",
    "banaras": "Varanasi",
    "prayagraj": "Prayagraj",
    "allahabad": "Prayagraj",
    "agra": "Agra",
    "kanpur": "Kanpur",
    "meerut": "Meerut",
    "ghaziabad": "Ghaziabad",
    "faridabad": "Faridabad",
    "amritsar": "Amritsar",
    "ludhiana": "Ludhiana",
    "jalandhar": "Jalandhar",
    "vadodara": "Vadodara",
    "baroda": "Vadodara",
    "rajkot": "Rajkot",
    "nashik": "Nashik",
    "nasik": "Nashik",
    "aurangabad": "Aurangabad",
    "kolhapur": "Kolhapur",
    "guwahati": "Guwahati",
    "bhubaneswar": "Bhubaneswar",
    "cuttack": "Cuttack",
    "raipur": "Raipur",
    "bilaspur": "Bilaspur",
    "jamshedpur": "Jamshedpur",
    "dhanbad": "Dhanbad",
    "siliguri": "Siliguri",
    "asansol": "Asansol",
    "durgapur": "Durgapur",
    "udaipur": "Udaipur",
    "jodhpur": "Jodhpur",
    "ajmer": "Ajmer",
    "kota": "Kota",
    "bikaner": "Bikaner",
    "dehradun": "Dehradun",
    "shimla": "Shimla",
    "srinagar": "Srinagar",
    "jammu": "Jammu",
    "madurai": "Madurai",
    "trichy": "Tiruchirappalli",
    "tiruchirappalli": "Tiruchirappalli",
    "salem": "Salem",
    "pondicherry": "Puducherry",
    "puducherry": "Puducherry",
    "thiruvananthapuram": "Thiruvananthapuram",
    "trivandrum": "Thiruvananthapuram",
    "thrissur": "Thrissur",
    "kozhikode": "Kozhikode",
    "calicut": "Kozhikode",
    "mangalore": "Mangaluru",
    "mangaluru": "Mangaluru",
    "hubli": "Hubballi",
    "hubballi": "Hubballi",
    "belgaum": "Belagavi",
    "belagavi": "Belagavi",
}

# Common dishes mentioned in Zomato complaints / cravings.
COMMON_DISHES: tuple[str, ...] = (
    "biryani", "pizza", "burger", "pasta", "sushi",
    "dosa", "idli", "paneer", "chicken",
    "chow mein", "noodles", "momos",
    "rolls", "kebab", "roti", "naan", "paratha",
    "samosa", "pav bhaji", "vada pav", "thali",
    "khichdi", "pulao", "fried rice", "manchurian", "tikka",
    "shawarma", "falafel", "bowl", "wrap", "sandwich",
    "cake", "donut", "ice cream",
    "coffee", "chai", "lassi", "smoothie", "salad",
)


# ---------------------------------------------------------------------------
# Pre-compiled regexes
# ---------------------------------------------------------------------------

# Order ID patterns. Zomato order IDs are 10 digits.
_ORDER_ID_LABELED_RE = re.compile(
    r"(?:order\s*(?:id|#|no\.?|number)?\s*[:\-]?\s*)#?(\d{10})\b",
    re.IGNORECASE,
)
_ORDER_ID_HASH_RE = re.compile(r"#(\d{10})\b")
# Bare 10-digit run we'll qualify with a proximity-to-food-context check.
_ORDER_ID_BARE_RE = re.compile(r"(?<!\d)(\d{10})(?!\d)")
_PHONE_NEIGHBORS_RE = re.compile(
    r"(?:\+91|\bphone\b|\bcall\b|\bmobile\b|\bcontact\b|\bnumber\b|\bcustomer\s*care\b|\bhelpline\b)",
    re.IGNORECASE,
)
_ORDER_CONTEXT_WORDS = (
    "pizza", "biryani", "meal", "order", "food", "delivery", "zomato",
    "swiggy", "refund", "delivered", "delayed", "parcel",
)

# Restaurant patterns.
# 1. Whitelist (canonical cased), built dynamically below.
# 2. Phrase pattern: "ordered from <Title Case Name>"
_REST_PHRASE_RE = re.compile(
    r"(?:from|ordered\s+from|order\s+at|food\s+from|got\s+from|delivery\s+from)\s+"
    r"([A-Z][\w&'.\-]*(?:\s+[A-Z][\w&'.\-]*){0,4})",
)
# 3. Twitter handle pattern, only matched if it looks brand-shaped.
_REST_HANDLE_RE = re.compile(r"@([A-Z][A-Za-z0-9_]{2,})")


def _build_brand_regex() -> re.Pattern[str]:
    """Compile a single alternation of all whitelisted brand spellings.

    Brands are sorted longest-first so multi-word brands like "Pizza Hut"
    win over a hypothetical shorter prefix. Word boundaries on both sides
    keep "KFC" from matching inside "KFCs" (we still allow trailing
    punctuation since ``\\b`` is zero-width).
    """
    sorted_brands = sorted(INDIAN_QSR_BRANDS, key=lambda s: -len(s))
    parts = []
    for b in sorted_brands:
        # Escape regex metachars (apostrophes, dots, exclamation, etc.).
        parts.append(re.escape(b))
    pattern = r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])"
    return re.compile(pattern, re.IGNORECASE)


_BRAND_RE = _build_brand_regex()
# Lookup map from a lowercased match back to canonical case. When two
# spellings exist (Haldiram vs Haldiram's), we keep the first one we see.
_BRAND_CANONICAL: dict[str, str] = {}
for _b in INDIAN_QSR_BRANDS:
    _BRAND_CANONICAL.setdefault(_b.lower(), _b)


def _build_city_regex() -> re.Pattern[str]:
    """Compile city/neighborhood alternation, longest-first so multi-word
    aliases like "New Delhi" win over "Delhi", and "T. Nagar" over "Nagar".
    """
    keys = sorted(CITY_NEIGHBORHOOD_MAP.keys(), key=lambda s: -len(s))
    parts = [re.escape(k) for k in keys]
    return re.compile(
        r"(?<![A-Za-z])(?:" + "|".join(parts) + r")(?![A-Za-z])",
        re.IGNORECASE,
    )


_CITY_RE = _build_city_regex()


def _build_dish_regex() -> re.Pattern[str]:
    items = sorted(COMMON_DISHES, key=lambda s: -len(s))
    parts = [re.escape(d) for d in items]
    return re.compile(
        r"(?<![A-Za-z])(?:" + "|".join(parts) + r")(?![A-Za-z])",
        re.IGNORECASE,
    )


_DISH_RE = _build_dish_regex()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_order_ids(text: str) -> list[str]:
    """Return deduped 10-digit Zomato order IDs found in ``text``.

    Heuristic blends labeled patterns ("order id 1234567890"), hash
    patterns ("#1234567890"), and bare digit runs that sit close to
    food-ordering vocabulary. Phone-number-shaped digits are filtered
    out by neighborhood scan around the match.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(oid: str, span: tuple[int, int]) -> None:
        if oid in seen:
            return
        # Reject if a phone-context word sits within 24 chars on either side.
        start, end = span
        window = text[max(0, start - 24): end + 24]
        if _PHONE_NEIGHBORS_RE.search(window):
            return
        seen.add(oid)
        found.append(oid)

    for m in _ORDER_ID_LABELED_RE.finditer(text):
        _add(m.group(1), m.span())
    for m in _ORDER_ID_HASH_RE.finditer(text):
        _add(m.group(1), m.span())
    # Bare digits only if there's food context within ~6 words on either side.
    lower = text.lower()
    for m in _ORDER_ID_BARE_RE.finditer(text):
        oid = m.group(1)
        if oid in seen:
            continue
        start, end = m.span()
        # Approximate "6 words" as 60 chars on each side.
        window = lower[max(0, start - 60): end + 60]
        if any(w in window for w in _ORDER_CONTEXT_WORDS):
            _add(oid, m.span())
    return found


def extract_restaurants(text: str) -> list[str]:
    """Return deduped canonical restaurant names found in ``text``.

    Three signals, applied in priority order so the whitelist always wins:
      1. Whitelist match against a curated brand list (canonical case).
      2. Phrase pattern: "ordered from <Title Case Name>".
      3. Twitter handle whose substring contains a known brand.
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        clean = name.strip().rstrip(".,;:!?")
        if not clean:
            return
        key = clean.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(clean)

    # 1. Whitelist matches first.
    for m in _BRAND_RE.finditer(text):
        canonical = _BRAND_CANONICAL.get(m.group(0).lower(), m.group(0))
        _add(canonical)

    # 2. "from <Name>" phrase. Skip if the captured Name is wholly a known
    #    city alias (Bandra, Andheri, etc.) so we don't claim a place as a
    #    restaurant.
    for m in _REST_PHRASE_RE.finditer(text):
        candidate = m.group(1).strip()
        if candidate.lower() in CITY_NEIGHBORHOOD_MAP:
            continue
        _add(candidate)

    # 3. Twitter-style handle, only if it contains a known brand substring.
    brand_lower = {b.lower().replace(" ", "").replace("'", "") for b in INDIAN_QSR_BRANDS}
    for m in _REST_HANDLE_RE.finditer(text):
        handle = m.group(1)
        h_low = handle.lower()
        if any(b and b in h_low for b in brand_lower):
            _add(handle)

    return out


def extract_cities(text: str) -> list[str]:
    """Return deduped canonical city names found in ``text``.

    Neighborhoods, aliases, and historical names map to the same canonical
    city, so "Bandra" and "Bombay" both resolve to "Mumbai".
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _CITY_RE.finditer(text):
        canonical = CITY_NEIGHBORHOOD_MAP.get(m.group(0).lower())
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def extract_dishes(text: str) -> list[str]:
    """Return deduped lowercased dish names found in ``text``."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _DISH_RE.finditer(text):
        d = m.group(0).lower()
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Sanity tests (run via `python -m social_watch.extraction`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cases = [
        ("extract_order_ids labeled",
         extract_order_ids("My order id 1234567890 is delayed"),
         ["1234567890"]),
        ("extract_order_ids phone-context filter",
         extract_order_ids("Call 9876543210 if delivery is late"),
         []),
        ("extract_restaurants from-phrase",
         extract_restaurants("Ordered biryani from Behrouz Biryani in Bandra"),
         ["Behrouz Biryani"]),
        ("extract_restaurants whitelist",
         extract_restaurants("KFC delivery was late"),
         ["KFC"]),
        ("extract_cities neighborhood + city",
         extract_cities("Order delayed in Indiranagar Bengaluru"),
         ["Bengaluru"]),
        ("extract_cities historical alias",
         extract_cities("Bombay traffic killed my order"),
         ["Mumbai"]),
        ("extract_dishes",
         extract_dishes("Cold pizza and soggy fries"),
         ["pizza"]),
    ]

    failed = 0
    for name, got, want in cases:
        ok = got == want
        if not ok:
            failed += 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")

    if failed:
        raise SystemExit(f"{failed} extraction test(s) failed")
    print("All extraction sanity tests passed.")
