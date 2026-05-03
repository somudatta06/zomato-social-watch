"""Zomato Social Watch — Phase 1: scraper.

Pulls real, current posts about Zomato from Reddit and Twitter/X (free tier
only), normalizes them into a common Post schema, deduplicates against
SQLite, and persists for downstream classification + escalation.
"""
__version__ = "0.1.0"
