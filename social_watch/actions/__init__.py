"""Action dispatchers — closes the brief's "≥1 real action per escalated
post" requirement.

Five live write connectors, four send-style + one bidirectional reply:

    • Slack incoming webhook       — Block Kit message
    • Discord incoming webhook     — rich embed
    • Email via SMTP               — HTML message (Gmail app-password works)
    • Google Sheets via Apps Script web-app webhook — appends a row
    • Twitter / X reply            — Playwright-driven reply on a tweet
                                     (manual-only; same authenticated
                                     session as the scraper)

When a post lands in priority band P0, the auto-dispatcher fans out to
the configured send-style channels per the routing tiers in
``dispatcher._route_for_post``. Reply channels are NEVER auto-fired; an
operator must click them from the dashboard.

Connectors total = 6 (read: Reddit + X/Twitter; write/reply: Slack +
Discord + Email + Sheets + Twitter-reply). Twitter is bidirectional
because we already read it AND we now reply on it — the strict reading
of the brief's "≥3 connectors with read/write" requirement.

Public API:
    from social_watch.actions import dispatch_for_post, dispatch_unactioned
"""
from .slack import build_blocks, send_slack  # noqa: F401
from .discord import build_embed, send_discord  # noqa: F401
from .email import build_email, send_email  # noqa: F401
from .sheets import build_row, send_sheets  # noqa: F401
from .twitter_reply import build_reply_text, reply_to_tweet  # noqa: F401
from .reddit_comment import (  # noqa: F401
    build_reply_text as build_comment_text,
    comment_on_post,
)
from .linear_ticket import build_issue_payload, create_issue  # noqa: F401
from .dispatcher import dispatch_for_post, dispatch_unactioned  # noqa: F401

__all__ = [
    "dispatch_for_post",
    "dispatch_unactioned",
    "send_slack",
    "build_blocks",
    "send_discord",
    "build_embed",
    "send_email",
    "build_email",
    "send_sheets",
    "build_row",
    "reply_to_tweet",
    "build_reply_text",
]
