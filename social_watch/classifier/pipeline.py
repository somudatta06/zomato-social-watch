"""Classifier pipeline: rules-first, then LLM upgrade if available.

The pipeline is the entry point for all classification work:
  1. Rules layer — runs preclassifier on every NULL-classified post
  2. LLM layer  — if Gemini key is set, batches posts and overlays the
                  richer LLM output onto the rules-only baseline

The result merges both layers: rules wins on tripwires (safety can't be
LLM-downgraded); LLM wins on topic detail, reasoning, and confidence
calibration. Persisted to posts.classification as a single JSON blob with
both layers visible for audit.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from loguru import logger

from .. import config, handles as handles_mod
from ..preclassifier import preclassify_all
from ..priority import compute_priority, get_author_multiplier
from .llm import LLMClassification, classify_with_llm, is_llm_available


# Tripwires must NEVER be downgraded by the LLM. These rules-side fields
# always win over LLM output:
_TRIPWIRE_LOCKED_FIELDS = (
    "tripwires_fired",
    "needs_human_review",  # tripwires force this true regardless of LLM
)


def _merge(rules: dict[str, Any], llm: LLMClassification | None) -> dict[str, Any]:
    """Merge rules + LLM into the unified classification dict.

    LLM wins for: primary_topic, secondary_topics, sub_claims, urgency*,
                  sentiment, tone_flags, audience, author_role, geography,
                  format, confidence, reasoning.
    Rules wins for: tripwires_fired, side (rules can fire 'neither' for
                    explicit out-of-scope), and forces needs_human_review
                    to true if any tripwire fired regardless of LLM.
    """
    if llm is None:
        # Rules-only — return the rules dict unchanged but mark layer
        return {**rules, "method": "rules-only"}

    merged: dict[str, Any] = {**rules}
    merged.update({
        "method": "rules+llm",
        "primary_topic": llm.primary_topic,
        "secondary_topics": llm.secondary_topics,
        "sub_claims": [sc.model_dump() for sc in llm.sub_claims],
        "sentiment": llm.sentiment,
        "tone_flags": list(set(rules.get("tone_flags", []) + llm.tone_flags)),
        "urgency": llm.urgency,
        "urgency_score": llm.urgency_score,
        "audience": list(set(rules.get("audience", []) + llm.audience)),
        "author_role": llm.author_role,
        "geography_llm": llm.geography,
        "format": llm.format,
        "confidence": llm.confidence,
        "reasoning": llm.reasoning,
    })

    # Tripwires lock — even if LLM said urgency=low, a tripwire fire forces critical
    if rules.get("tripwires_fired"):
        merged["urgency"] = "critical"
        merged["urgency_score"] = max(merged.get("urgency_score", 0.0), 0.95)
        merged["needs_human_review"] = True

    # Auto-action gate: derived rule, not LLM-decided
    merged["auto_action_safe"] = _compute_auto_action_safe(merged)
    merged["classified_at"] = datetime.now(timezone.utc).isoformat()
    return merged


def _compute_auto_action_safe(c: dict[str, Any]) -> bool:
    """Pure rule. The LLM cannot override this."""
    if c.get("tripwires_fired"):
        return False
    if c.get("needs_human_review"):
        return False
    if c.get("confidence", 0) < 0.7:
        return False
    if c.get("sentiment") in ("abusive", "sarcastic"):
        return False
    sensitive = {"religious", "caste", "gender", "threat", "political"}
    if any(t in sensitive for t in c.get("tone_flags", [])):
        return False
    return True


async def classify_backlog(*, force: bool = False, llm_enabled: bool = True) -> dict[str, int]:
    """Classify all unclassified posts (or all posts if force=True).

    Steps:
      1. Run preclassifier (rules) on backlog — populates baseline
      2. If LLM available + llm_enabled: batch-classify same posts via Gemini
      3. Merge LLM output onto rules baseline; persist combined JSON

    Returns counts dict.
    """
    counts = {
        "rules_processed": 0,
        "llm_processed": 0,
        "llm_skipped_no_key": 0,
        "tripwires_fired": 0,
        "auto_action_safe": 0,
        "needs_review": 0,
    }

    # Step 1: rules layer
    rules_result = await preclassify_all(force=force)
    counts["rules_processed"] = rules_result["processed"]
    counts["tripwires_fired"] = rules_result["tripwires_fired"]

    if not llm_enabled or not is_llm_available():
        if not is_llm_available():
            counts["llm_skipped_no_key"] = counts["rules_processed"]
            logger.info(
                "LLM stage skipped (GEMINI_API_KEY missing). "
                "Set it in .env to enable richer classification."
            )
        # Still compute auto_action_safe + needs_review from rules
        await _post_process_rules_only(counts)
        return counts

    # Step 2: gather posts to LLM-classify
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        where = "" if force else "WHERE classification NOT LIKE '%\"method\":\"rules+llm\"%'"
        cur = await db.execute(
            f"SELECT id, source, native_id, author, content, url, created_at, classification "
            f"FROM posts {where} ORDER BY created_at DESC"
        )
        rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        return counts

    # Build LLM input list with rules hint
    post_inputs: list[dict[str, Any]] = []
    rules_by_id: dict[str, dict[str, Any]] = {}
    for r in rows:
        rules = json.loads(r["classification"]) if r["classification"] else {}
        rules_by_id[r["id"]] = rules
        hint_parts: list[str] = []
        if rules.get("side"):
            hint_parts.append(f"side={rules['side']}")
        if rules.get("tripwires_fired"):
            hint_parts.append(f"tripwires={rules['tripwires_fired']}")
        if rules.get("sentiment"):
            hint_parts.append(f"sentiment={rules['sentiment']}")
        post_inputs.append({
            "id": r["id"],
            "source": r["source"],
            "author": r["author"],
            "content": r["content"],
            "created_at": r["created_at"],
            "rules_hint": "; ".join(hint_parts),
        })

    # Step 3: LLM
    llm_results = await classify_with_llm(post_inputs)
    llm_by_id = {c.post_id: c for c in llm_results}
    counts["llm_processed"] = len(llm_results)

    # Step 4: merge + persist (classification + priority)
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        for post_id, rules in rules_by_id.items():
            llm = llm_by_id.get(post_id)
            merged = _merge(rules, llm)
            if merged.get("auto_action_safe"):
                counts["auto_action_safe"] += 1
            if merged.get("needs_human_review"):
                counts["needs_review"] += 1

            # Need post row to feed compute_priority (metadata, source, created_at)
            post_row = await (await db.execute(
                "SELECT source, author, content, url, created_at, metadata FROM posts WHERE id = ?",
                (post_id,),
            )).fetchone()
            if post_row is None:
                continue
            prior = await _count_prior_complaints(db, post_row["author"], post_id)

            # Phase γ: classify the author and read their reach multiplier.
            # get_or_compute_handle is idempotent — re-running won't dup rows.
            try:
                metadata_dict = json.loads(post_row["metadata"]) if post_row["metadata"] else {}
            except Exception:
                metadata_dict = {}
            await handles_mod.get_or_compute_handle(
                db,
                handle=post_row["author"],
                source=post_row["source"],
                metadata=metadata_dict,
                bio=None,                                # bio scraping is v2
                posted_at=post_row["created_at"],
            )
            multiplier = await get_author_multiplier(
                post_row["author"], post_row["source"], conn=db
            )
            priority = compute_priority(
                dict(post_row),
                merged,
                prior_complaints=prior,
                author_multiplier=multiplier,
            )

            await db.execute(
                """
                UPDATE posts SET
                    category = ?,
                    score = ?,
                    classification = ?,
                    classified_at = ?,
                    priority_score = ?,
                    priority_band = ?,
                    priority_breakdown = ?
                WHERE id = ?
                """,
                (
                    merged.get("primary_topic") or merged.get("side"),
                    merged.get("urgency_score") or 0,
                    json.dumps(merged, default=str),
                    merged.get("classified_at") or datetime.now(timezone.utc).isoformat(),
                    priority["score"],
                    priority["band"],
                    json.dumps(priority, default=str),
                    post_id,
                ),
            )
        await db.commit()

    logger.info(
        f"classify_backlog done: rules={counts['rules_processed']} "
        f"llm={counts['llm_processed']} tripwires={counts['tripwires_fired']} "
        f"auto_safe={counts['auto_action_safe']} needs_review={counts['needs_review']}"
    )
    return counts


async def _count_prior_complaints(
    db: aiosqlite.Connection, author: str | None, exclude_post_id: str
) -> int:
    """Count negative-sentiment posts from the same handle in last 7 days,
    excluding the post we're scoring."""
    if not author:
        return 0
    cur = await db.execute(
        """
        SELECT COUNT(*) FROM posts
        WHERE author = ?
          AND id != ?
          AND created_at >= datetime('now', '-7 days')
          AND classification IS NOT NULL
          AND json_extract(classification, '$.sentiment') IN ('negative', 'abusive')
        """,
        (author, exclude_post_id),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def backfill_priorities() -> dict[str, int]:
    """Compute priority for every classified post in DB. Idempotent —
    safe to re-run. Used for one-time backfill after the priority columns
    are added, and any time weights change."""
    counts = {"processed": 0, "by_band": {"P0": 0, "P1": 0, "P2": 0, "P3": 0}}
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Seed watchlists once before the loop so press/politician handles
        # already in the DB get their multiplier on this pass.
        await handles_mod.seed_watchlists(db)
        cur = await db.execute(
            "SELECT id, source, author, content, url, created_at, metadata, "
            "classification FROM posts WHERE classification IS NOT NULL"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            try:
                cls = json.loads(r["classification"])
            except Exception:
                continue
            prior = await _count_prior_complaints(db, r["author"], r["id"])

            # Phase γ: refresh the author's handles row + look up multiplier
            try:
                metadata_dict = json.loads(r["metadata"]) if r["metadata"] else {}
            except Exception:
                metadata_dict = {}
            await handles_mod.get_or_compute_handle(
                db,
                handle=r["author"],
                source=r["source"],
                metadata=metadata_dict,
                bio=None,
                posted_at=r["created_at"],
            )
            multiplier = await get_author_multiplier(
                r["author"], r["source"], conn=db
            )
            priority = compute_priority(
                r, cls, prior_complaints=prior, author_multiplier=multiplier
            )
            await db.execute(
                "UPDATE posts SET priority_score = ?, priority_band = ?, "
                "priority_breakdown = ? WHERE id = ?",
                (
                    priority["score"],
                    priority["band"],
                    json.dumps(priority, default=str),
                    r["id"],
                ),
            )
            counts["processed"] += 1
            counts["by_band"][priority["band"]] = counts["by_band"].get(priority["band"], 0) + 1
        await db.commit()
    logger.info(f"Priority backfill: {counts}")
    return counts


async def _post_process_rules_only(counts: dict[str, int]) -> None:
    """When LLM is unavailable, walk rules-only classifications, set
    needs_review / auto_action_safe, AND compute priority. Without this
    the priority columns stay null on rules-only-classified posts."""
    async with aiosqlite.connect(str(config.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Phase γ: seed watchlists once so seed handles get tier on first pass
        await handles_mod.seed_watchlists(db)
        cur = await db.execute(
            "SELECT id, source, author, content, url, created_at, metadata, "
            "classification FROM posts WHERE classification IS NOT NULL"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            try:
                c = json.loads(r["classification"])
            except Exception:
                continue
            c["auto_action_safe"] = _compute_auto_action_safe(c)
            if c.get("needs_human_review"):
                counts["needs_review"] += 1
            if c.get("auto_action_safe"):
                counts["auto_action_safe"] += 1
            prior = await _count_prior_complaints(db, r["author"], r["id"])

            # Phase γ: classify author + look up multiplier
            try:
                metadata_dict = json.loads(r["metadata"]) if r["metadata"] else {}
            except Exception:
                metadata_dict = {}
            await handles_mod.get_or_compute_handle(
                db,
                handle=r["author"],
                source=r["source"],
                metadata=metadata_dict,
                bio=None,
                posted_at=r["created_at"],
            )
            multiplier = await get_author_multiplier(
                r["author"], r["source"], conn=db
            )
            priority = compute_priority(
                r, c, prior_complaints=prior, author_multiplier=multiplier
            )
            await db.execute(
                "UPDATE posts SET classification = ?, priority_score = ?, "
                "priority_band = ?, priority_breakdown = ? WHERE id = ?",
                (
                    json.dumps(c, default=str),
                    priority["score"],
                    priority["band"],
                    json.dumps(priority, default=str),
                    r["id"],
                ),
            )
        await db.commit()
