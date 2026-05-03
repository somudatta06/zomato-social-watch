"""Classifier package — orchestrates rules-first then LLM-fallback.

Public API:
    from social_watch.classifier import classify_backlog, is_llm_available

`classify_backlog()` runs the full pipeline on every NULL-classified post:
  1. Rules layer  (preclassifier — sides, tripwires, sentiment, geography)
  2. LLM layer    (Gemini, batched) — only if GEMINI_API_KEY is set
  3. Persist combined classification JSON

Without a Gemini key the system runs rules-only and is still useful.
"""
from .llm import is_llm_available, classify_with_llm  # noqa: F401
from .pipeline import classify_backlog  # noqa: F401

__all__ = ["classify_backlog", "is_llm_available", "classify_with_llm"]
