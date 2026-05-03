"""Runtime configuration: keywords, subreddits, queries, file paths.

Edit the lists below to tune what gets scraped. Secrets come from .env.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# === paths ===
DB_PATH = ROOT / os.getenv("DB_PATH", "social_watch.db")
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# === schedule ===
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "300"))

# ============================================================
# REDDIT (public JSON endpoints — no auth required)
# ============================================================
# Reddit recommends a descriptive UA in the form
# "<platform>:<app>:<version> (by /u/<username>)". Override in .env if you
# have a Reddit username — descriptive UAs get higher rate limits and are
# less likely to be blocked than bare httpx/python defaults.
REDDIT_USER_AGENT = os.getenv(
    "REDDIT_USER_AGENT",
    "zomato-social-watch:0.1 (research)",
)

# OAuth credentials are no longer required (we use public JSON endpoints).
# Kept here so a future PRAW path can pick them up without code changes.
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")

# Subreddits we firehose for new submissions every cycle.
# Indian metro subs catch local complaints; food subs catch product chatter;
# r/legaladviceindia catches refund/dispute posts; r/developersIndia catches
# app/bug reports.
REDDIT_SUBREDDITS = [
    "india",
    "bangalore",
    "mumbai",
    "delhi",
    "Chennai",
    "kolkata",
    "hyderabad",
    "pune",
    "IndianFood",
    "Zomato",
    "legaladviceindia",
    "developersIndia",
    "IndiaSpeaks",
    "AskIndia",
]

# Free-text searches across r/all (sorted by new). Catches mentions outside
# the subreddits we monitor directly.
# Scope: Zomato Consumer app + Zomato Merchant app ONLY. Blinkit / Hyperpure
# / District / Eternal-corporate are explicitly out of scope per project brief.
REDDIT_QUERIES = [
    "zomato",
    "zomato refund",
    "zomato delivery",
    "zomato gold",
    "zomato app",
    "zomato support",
    "zomato restaurant",
    "deepinder goyal",
]

# Skip submissions older than this — they're stale for a "real-time" watch.
REDDIT_MAX_AGE_HOURS = 24

# Cap per query to avoid burning rate budget on huge result sets.
REDDIT_LIMIT_PER_QUERY = 100

# ============================================================
# TWITTER / X (via twscrape)
# ============================================================
# Two ways to authenticate, in priority order:
#
# 1. Cookies (preferred). Get auth_token + ct0 from your browser DevTools
#    after logging into x.com. No login flow, no captcha possible, no email
#    IMAP required. Cookies last ~30 days.
TWITTER_COOKIE_USERNAME = os.getenv("TWITTER_COOKIE_USERNAME", "")
TWITTER_COOKIE_AUTH_TOKEN = os.getenv("TWITTER_COOKIE_AUTH_TOKEN", "")
TWITTER_COOKIE_CT0 = os.getenv("TWITTER_COOKIE_CT0", "")
#
# 2. Username/password + email (fallback). twscrape logs in for you and
#    reads any challenge codes from email via IMAP. Only used if cookies
#    above are empty. Format: user:pass:email:emailpass (comma-separated).
TWITTER_ACCOUNTS = os.getenv("TWITTER_ACCOUNTS", "")

TWSCRAPE_DB = ROOT / os.getenv("TWSCRAPE_DB", "accounts.db")

# Twitter advanced-search syntax. Mix of brand mentions, hashtags, account
# mentions, replies to support, and founder activity.
TWITTER_QUERIES = [
    "zomato",
    "@zomato",
    "@zomatocare",
    "#zomato",
    "#zomatofail",
    "#zomatocares",
    "from:deepigoyal",
    "to:zomato",
    "to:zomatocare",
    "zomato refund",
    "zomato delivery",
]

TWITTER_LIMIT_PER_QUERY = 50

# ============================================================
# NITTER (Twitter fallback — public instances; many dead in 2026)
# ============================================================
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.cz",
    "https://nitter.kavin.rocks",
    "https://nitter.lacontrevoie.fr",
    "https://nitter.tiekoetter.com",
    "https://nitter.pufe.org",
]

NITTER_QUERIES = [
    "zomato",
    "@zomato",
    "#zomato",
]

NITTER_TIMEOUT = 10
