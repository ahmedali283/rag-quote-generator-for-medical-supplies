"""
Candidate pair generation, scoring, LLM arbitration, and curated cluster building.

Pipeline
--------
1. run_blocking()        — SQL query finds cross-source pairs in the same
                           category bucket with cosine_sim ≥ COSINE_FLOOR.
                           Inserts into candidate_pairs (idempotent).

2. run_scoring()         — For each candidate pair, computes the final
                           confidence score: cosine_sim + adjustments.
                           Updates computed_score and decision in the DB.

3. run_llm_arbitration() — For all 'pending_llm' pairs (0.50 < score < 0.85),
                           calls the Claude API with both items' full raw data.
                           Claude returns {"decision": "match"|"no_match",
                           "reason": "one sentence"}. Updates decision and
                           llm_reason in the DB. Logs every decision to the
                           match_decisions log file.

4. build_curated_clusters() — Collects all matched pairs (auto_match +
                              llm_match), applies union-find to cluster
                              transitively connected items, and writes
                              curated_products + curated_product_members rows.

Scoring formula
---------------
final_score = clamp(cosine_sim + adjustments, 0.0, 1.0)

Adjustments:
  +0.10  manufacturer/brand token appears in BOTH items' names/manufacturer fields
         (rewards explicit brand alignment across sources)
  -0.30  UOM conflict: one item is pair/single, the other is a bulk box/case
         with pack_size > 1 (different purchasable unit — never the same SKU)
  -0.40  explicit attribute conflict: different size (S/M/L/XL), different
         material (latex/nitrile/vinyl), or sterile vs non-sterile mentioned
         in both names but incompatibly (always wrong to merge these)

Thresholds:
  ≥ 0.85  → auto_match   (high confidence, no human review)
  ≤ 0.50  → auto_reject  (clearly not a match, don't burden the review queue)
  (0.50, 0.85) → pending_llm → Claude arbitration → llm_match / llm_no_match

These thresholds are discussed in PART2_NOTES.md.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Blocking parameters ───────────────────────────────────────────────────────

COSINE_FLOOR = 0.50          # pairs below this are never candidates
COSINE_FLOOR_EVAL = 0.35     # lower floor used only when build_eval_set needs more rows

# ── Score thresholds ──────────────────────────────────────────────────────────

THRESHOLD_AUTO_MATCH = 0.85
THRESHOLD_AUTO_REJECT = 0.50

# ── Scoring adjustment magnitudes ────────────────────────────────────────────

BONUS_MANUFACTURER = +0.10
PENALTY_UOM_CONFLICT = -0.30
PENALTY_ATTRIBUTE_CONFLICT = -0.40

# ── Regex patterns for score adjustments ─────────────────────────────────────

# Known medical supply brand tokens to check for shared-brand boost.
# Only brands that appear in the actual source data are listed — expanding
# this list improves recall for the bonus without hurting precision.
_BRAND_RE = re.compile(
    r"\b(laerdal|nasco|life[\s/]?form|3b\s?scientific|gaumard|"
    r"simlabsolutions?|vata|medline|dynarex|cardinal|kimberly|"
    r"ansell|mckesson|halyard|bound\s*tree|dynarex|demo[\s-]?dos(?:er|e)|"
    r"simlabs?)\b",
    re.I,
)

# Glove/PPE size tokens — conflicting sizes mean items must not be merged.
# Anchored to word boundaries to avoid matching "xl" inside product codes.
_SIZE_RE = re.compile(
    r"\b(xs|x-?small|small|sm|medium|med|large|lg|x-?large|xl|xxl|2xl|3xl)\b",
    re.I,
)

# Material tokens — conflicting materials are never the same product.
_MATERIAL_RE = re.compile(
    r"\b(latex|nitrile|vinyl|neoprene|polyethylene|pe|"
    r"polychloroprene|chloroprene)\b",
    re.I,
)

# Sterility tokens — sterile vs non-sterile are different SKUs.
_STERILE_RE = re.compile(r"\b(non-?sterile|sterile)\b", re.I)

# UOM categories for the incompatibility check.
_PAIR_UOMS = {"pair", "pairs"}
_BULK_UOMS = {"box", "case", "pack", "bag", "carton"}  # bulk container types


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_pair(
    cosine_sim: float,
    item_a: dict,
    item_b: dict,
    ni_a: dict,
    ni_b: dict,
) -> tuple[float, dict]:
    """
    Compute the final confidence score for a candidate pair.

    item_a, item_b: source_items rows (dict)
    ni_a, ni_b:     normalized_items rows (dict)

    Returns (final_score, flags) where flags is a dict with boolean keys:
        manufacturer_bonus, uom_conflict, attribute_conflict
    """
    score = float(cosine_sim)
    flags: dict[str, bool] = {
        "manufacturer_bonus": False,
        "uom_conflict": False,
        "attribute_conflict": False,
    }

    # ── Manufacturer/brand bonus ──────────────────────────────────────────────
    # Collect brand tokens from name and (if present) manufacturer field.
    def brand_tokens(item: dict) -> set[str]:
        text = (item.get("raw_name") or "") + " " + (item.get("raw_manufacturer") or "")
        return {t.lower() for t in _BRAND_RE.findall(text)}

    brands_a = brand_tokens(item_a)
    brands_b = brand_tokens(item_b)
    if brands_a and brands_b and (brands_a & brands_b) and float(cosine_sim) >= 0.80:
        score += BONUS_MANUFACTURER
        flags["manufacturer_bonus"] = True

    # ── UOM conflict penalty ──────────────────────────────────────────────────
    uom_a = (ni_a.get("unit_of_measure") or "").lower()
    uom_b = (ni_b.get("unit_of_measure") or "").lower()
    ps_a = ni_a.get("pack_size") or 1
    ps_b = ni_b.get("pack_size") or 1

    uom_conflict = (
        (uom_a in _PAIR_UOMS and uom_b in _BULK_UOMS and ps_b > 1)
        or (uom_b in _PAIR_UOMS and uom_a in _BULK_UOMS and ps_a > 1)
    )
    if uom_conflict:
        score += PENALTY_UOM_CONFLICT
        flags["uom_conflict"] = True

    # ── Attribute conflict penalty ────────────────────────────────────────────
    # Conflict fires only when BOTH items mention the attribute but incompatibly.
    # If only one item mentions a size/material, we can't conclude a conflict.
    name_a = item_a.get("raw_name") or ""
    name_b = item_b.get("raw_name") or ""

    sizes_a = {t.lower() for t in _SIZE_RE.findall(name_a)}
    sizes_b = {t.lower() for t in _SIZE_RE.findall(name_b)}
    size_conflict = bool(sizes_a) and bool(sizes_b) and not (sizes_a & sizes_b)

    mats_a = {t.lower() for t in _MATERIAL_RE.findall(name_a)}
    mats_b = {t.lower() for t in _MATERIAL_RE.findall(name_b)}
    mat_conflict = bool(mats_a) and bool(mats_b) and not (mats_a & mats_b)

    sterile_a = {t.lower() for t in _STERILE_RE.findall(name_a)}
    sterile_b = {t.lower() for t in _STERILE_RE.findall(name_b)}
    sterile_conflict = bool(sterile_a) and bool(sterile_b) and not (sterile_a & sterile_b)

    attribute_conflict = size_conflict or mat_conflict or sterile_conflict
    if attribute_conflict:
        score += PENALTY_ATTRIBUTE_CONFLICT
        flags["attribute_conflict"] = True

    final = max(0.0, min(1.0, score))
    return final, flags


def _make_decision(score: float) -> str:
    if score >= THRESHOLD_AUTO_MATCH:
        return "auto_match"
    elif score <= THRESHOLD_AUTO_REJECT:
        return "auto_reject"
    return "pending_llm"


# ── Blocking ──────────────────────────────────────────────────────────────────

def run_blocking(conn, cosine_floor: float = COSINE_FLOOR, dry_run: bool = False) -> int:
    """
    Find cross-source candidate pairs in the same category bucket with
    cosine similarity ≥ cosine_floor via pgvector's <=> (cosine distance)
    operator.

    The embedding distance <=> gives cosine DISTANCE (0=identical, 1=orthogonal,
    2=opposite). Cosine SIMILARITY = 1 - distance. We filter on
    1 - (ni_a.embedding <=> ni_b.embedding) >= cosine_floor.

    Canonical ordering (item_a_id < item_b_id) ensures each pair is inserted
    once. ON CONFLICT DO NOTHING makes repeated runs idempotent.

    Returns the count of new pairs inserted.
    """
    log.info("blocking: floor=%.2f", cosine_floor)

    # We compute category buckets in Python during normalize (stored in
    # normalized_items.category_bucket), so the SQL join just matches on
    # the pre-computed bucket column.
    sql = """
        INSERT INTO candidate_pairs
            (item_a_id, item_b_id, cosine_sim, computed_score,
             category_bucket, manufacturer_bonus, uom_conflict,
             attribute_conflict, decision)
        SELECT
            LEAST(si_a.id, si_b.id)                                 AS item_a_id,
            GREATEST(si_a.id, si_b.id)                              AS item_b_id,
            ROUND((1 - (ni_a.embedding <=> ni_b.embedding))::NUMERIC, 4) AS cosine_sim,
            0.0                                                      AS computed_score,
            ni_a.category_bucket,
            FALSE, FALSE, FALSE,
            'pending_llm'
        FROM normalized_items ni_a
        JOIN source_items si_a ON si_a.id = ni_a.source_item_id
        JOIN normalized_items ni_b ON ni_b.id > ni_a.id
        JOIN source_items si_b ON si_b.id = ni_b.source_item_id
        WHERE si_a.source != si_b.source
          AND ni_a.category_bucket = ni_b.category_bucket
          AND ni_a.category_bucket != 'other'
          AND ni_a.embedding IS NOT NULL
          AND ni_b.embedding IS NOT NULL
          AND (1 - (ni_a.embedding <=> ni_b.embedding)) >= %s
          AND si_a.id < si_b.id
        ON CONFLICT (item_a_id, item_b_id) DO NOTHING
        RETURNING id
    """

    if dry_run:
        count_sql = """
            SELECT COUNT(*) AS cnt
            FROM normalized_items ni_a
            JOIN source_items si_a ON si_a.id = ni_a.source_item_id
            JOIN normalized_items ni_b ON ni_b.id > ni_a.id
            JOIN source_items si_b ON si_b.id = ni_b.source_item_id
            WHERE si_a.source != si_b.source
              AND ni_a.category_bucket = ni_b.category_bucket
              AND ni_a.category_bucket != 'other'
              AND ni_a.embedding IS NOT NULL
              AND ni_b.embedding IS NOT NULL
              AND (1 - (ni_a.embedding <=> ni_b.embedding)) >= %s
              AND si_a.id < si_b.id
        """
        with conn.cursor() as cur:
            cur.execute(count_sql, (cosine_floor,))
            n = cur.fetchone()["cnt"]
        log.info("dry_run: blocking would insert %d candidate pairs", n)
        return n

    with conn.cursor() as cur:
        cur.execute(sql, (cosine_floor,))
        inserted = cur.rowcount
    conn.commit()
    log.info("blocking: inserted %d new candidate pairs", inserted)
    return inserted


# ── Scoring pass ──────────────────────────────────────────────────────────────

def run_scoring(conn, dry_run: bool = False) -> dict:
    """
    Compute final scores and decisions for all candidate_pairs rows whose
    computed_score is 0.0 (i.e., freshly blocked, not yet scored).

    Fetches all unscored pairs with their source_items and normalized_items
    data, scores in Python, and updates the DB in batches of 100.

    Returns a summary dict.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cp.id AS pair_id,
                cp.cosine_sim,
                si_a.id AS id_a, si_a.raw_name AS name_a,
                si_a.raw_manufacturer AS mfr_a,
                si_a.raw_description AS desc_a,
                si_b.id AS id_b, si_b.raw_name AS name_b,
                si_b.raw_manufacturer AS mfr_b,
                si_b.raw_description AS desc_b,
                ni_a.pack_size AS ps_a, ni_a.unit_of_measure AS uom_a,
                ni_a.pack_unit AS pu_a, ni_a.units_per_pack AS upp_a,
                ni_b.pack_size AS ps_b, ni_b.unit_of_measure AS uom_b,
                ni_b.pack_unit AS pu_b, ni_b.units_per_pack AS upp_b
            FROM candidate_pairs cp
            JOIN source_items si_a ON si_a.id = cp.item_a_id
            JOIN source_items si_b ON si_b.id = cp.item_b_id
            JOIN normalized_items ni_a ON ni_a.source_item_id = cp.item_a_id
            JOIN normalized_items ni_b ON ni_b.source_item_id = cp.item_b_id
            WHERE cp.computed_score = 0.0
            ORDER BY cp.id
            """
        )
        rows = cur.fetchall()

    log.info("scoring: %d pairs to score", len(rows))
    counts = {"auto_match": 0, "auto_reject": 0, "pending_llm": 0}

    BATCH = 100
    updates = []

    for row in rows:
        item_a = {
            "raw_name": row["name_a"],
            "raw_manufacturer": row["mfr_a"],
            "raw_description": row["desc_a"],
        }
        item_b = {
            "raw_name": row["name_b"],
            "raw_manufacturer": row["mfr_b"],
            "raw_description": row["desc_b"],
        }
        ni_a = {
            "pack_size": row["ps_a"],
            "unit_of_measure": row["uom_a"],
            "pack_unit": row["pu_a"],
            "units_per_pack": row["upp_a"],
        }
        ni_b = {
            "pack_size": row["ps_b"],
            "unit_of_measure": row["uom_b"],
            "pack_unit": row["pu_b"],
            "units_per_pack": row["upp_b"],
        }

        final, flags = score_pair(float(row["cosine_sim"]), item_a, item_b, ni_a, ni_b)
        decision = _make_decision(final)
        counts[decision] = counts.get(decision, 0) + 1

        updates.append((
            round(final, 4),
            flags["manufacturer_bonus"],
            flags["uom_conflict"],
            flags["attribute_conflict"],
            decision,
            row["pair_id"],
        ))

        if len(updates) >= BATCH and not dry_run:
            _flush_score_updates(conn, updates)
            updates = []

    if updates and not dry_run:
        _flush_score_updates(conn, updates)

    log.info("scoring complete: %s", counts)
    return counts


def _flush_score_updates(conn, updates: list) -> None:
    import psycopg2.extras
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            UPDATE candidate_pairs AS cp SET
                computed_score       = u.score,
                manufacturer_bonus   = u.mfr_bonus,
                uom_conflict         = u.uom_conflict,
                attribute_conflict   = u.attr_conflict,
                decision             = u.decision,
                decided_at           = NOW()
            FROM (VALUES %s) AS u(score, mfr_bonus, uom_conflict, attr_conflict, decision, id)
            WHERE cp.id = u.id::bigint
            """,
            updates,
        )
    conn.commit()


# ── LLM arbitration ───────────────────────────────────────────────────────────

_ARBITRATION_PROMPT = """\
You are a medical supply catalog deduplication expert.
Your task: decide if these two products are the same physical product \
(same item, potentially sold under different catalog numbers or names by different distributors).

Product A:
  Name:            {name_a}
  Category:        {cat_a}
  Manufacturer:    {mfr_a}
  Raw pack info:   {pack_a}
  Normalized pack: pack_size={ps_a}, unit_of_measure={uom_a}, units_per_pack={upp_a}
  Description:     {desc_a}

Product B:
  Name:            {name_b}
  Category:        {cat_b}
  Manufacturer:    {mfr_b}
  Raw pack info:   {pack_b}
  Normalized pack: pack_size={ps_b}, unit_of_measure={uom_b}, units_per_pack={upp_b}
  Description:     {desc_b}

Pre-computed conflict signals (from rule-based scoring — treat these as hard constraints):
  UOM conflict detected:       {uom_conflict}
  Attribute conflict detected: {attribute_conflict}
  Shared brand/manufacturer:   {manufacturer_bonus}

Decision rules — apply in order, stopping at the first that matches:

1. HARD RULE — if UOM conflict is True: return no_match.
   A "pair" and a "box of 100" are never the same purchasable line item, even
   if the underlying product is related. Different packaging = different SKU.

2. HARD RULE — if attribute conflict is True: return no_match.
   Conflicting size (S vs L), material (latex vs nitrile), or sterility
   (sterile vs non-sterile) means these are categorically different products.
   Medical supplies with different specifications must never be merged.

3. SOFT RULE — same product, different pack counts (e.g. 50-count vs 100-count
   of the same item): return match. Different quantities of the same product
   are a match; buyers can compare prices per unit downstream.

4. SOFT RULE — same product sold by different distributors under different
   catalog numbers: return match.

5. If genuinely ambiguous after applying the above: prefer no_match to avoid
   false merges in a medical supply context.

Return ONLY valid JSON with no other text:
{{"decision": "match" or "no_match", "reason": "one concise sentence explaining which rule applied"}}"""


def run_llm_arbitration(
    conn,
    decision_log_path: str,
    dry_run: bool = False,
    concurrency: int = 8,
) -> dict:
    """
    For all candidate_pairs rows with decision='pending_llm', call the Claude
    API to decide match vs no_match.

    Uses asyncio with a semaphore (default 8 concurrent requests) so the
    full pending set completes in roughly pending/concurrency × avg_latency
    instead of pending × avg_latency.

    DB writes are batched in the main thread (psycopg2 is not thread-safe).
    Results are committed in batches of COMMIT_BATCH to bound memory while
    keeping round-trips low.

    Returns summary dict.
    """
    import asyncio
    import anthropic as _anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — cannot run LLM arbitration")
        return {"error": "no api key"}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cp.id AS pair_id, cp.cosine_sim, cp.computed_score,
                cp.manufacturer_bonus, cp.uom_conflict, cp.attribute_conflict,
                cp.category_bucket,
                si_a.raw_name AS name_a, si_a.raw_category AS cat_a,
                si_a.raw_manufacturer AS mfr_a, si_a.raw_pack_info AS pack_a,
                si_a.raw_description AS desc_a,
                ni_a.pack_size AS ps_a, ni_a.unit_of_measure AS uom_a,
                ni_a.units_per_pack AS upp_a,
                si_b.raw_name AS name_b, si_b.raw_category AS cat_b,
                si_b.raw_manufacturer AS mfr_b, si_b.raw_pack_info AS pack_b,
                si_b.raw_description AS desc_b,
                ni_b.pack_size AS ps_b, ni_b.unit_of_measure AS uom_b,
                ni_b.units_per_pack AS upp_b
            FROM candidate_pairs cp
            JOIN source_items si_a ON si_a.id = cp.item_a_id
            JOIN source_items si_b ON si_b.id = cp.item_b_id
            LEFT JOIN normalized_items ni_a ON ni_a.source_item_id = cp.item_a_id
            LEFT JOIN normalized_items ni_b ON ni_b.source_item_id = cp.item_b_id
            WHERE cp.decision = 'pending_llm'
            ORDER BY cp.computed_score DESC
            """
        )
        rows = cur.fetchall()

    log.info("llm_arbitration: %d pending pairs to arbitrate (concurrency=%d)",
             len(rows), concurrency)

    if dry_run:
        for row in rows:
            log.info("dry_run: would arbitrate pair %d (score=%.3f)",
                     row["pair_id"], float(row["computed_score"]))
        return {"pending": len(rows), "llm_match": 0, "llm_no_match": 0,
                "errors": 0, "dry_run": True}

    # ── async worker ──────────────────────────────────────────────────────────

    async def _arbitrate_all(rows: list) -> list[dict]:
        """Call Claude concurrently for all rows; return result dicts."""
        client = _anthropic.AsyncAnthropic(api_key=api_key)
        sem = asyncio.Semaphore(concurrency)
        results = [None] * len(rows)

        async def _one(idx: int, row: dict) -> None:
            prompt = _ARBITRATION_PROMPT.format(
                name_a=row["name_a"] or "", cat_a=row["cat_a"] or "",
                mfr_a=row["mfr_a"] or "unknown", pack_a=row["pack_a"] or "unknown",
                desc_a=(row["desc_a"] or "")[:300],
                ps_a=row["ps_a"] if row["ps_a"] is not None else "unknown",
                uom_a=row["uom_a"] or "unknown",
                upp_a=row["upp_a"] if row["upp_a"] is not None else 1,
                name_b=row["name_b"] or "", cat_b=row["cat_b"] or "",
                mfr_b=row["mfr_b"] or "unknown", pack_b=row["pack_b"] or "unknown",
                desc_b=(row["desc_b"] or "")[:300],
                ps_b=row["ps_b"] if row["ps_b"] is not None else "unknown",
                uom_b=row["uom_b"] or "unknown",
                upp_b=row["upp_b"] if row["upp_b"] is not None else 1,
                uom_conflict=row["uom_conflict"],
                attribute_conflict=row["attribute_conflict"],
                manufacturer_bonus=row["manufacturer_bonus"],
            )
            async with sem:
                try:
                    msg = await client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=256,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    raw = msg.content[0].text.strip()
                    if raw.startswith("```"):
                        raw = "\n".join(
                            ln for ln in raw.splitlines()
                            if not ln.startswith("```")
                        )
                    data = json.loads(raw)
                    decision_str = data.get("decision", "no_match").lower()
                    reason = data.get("reason", "")
                    error = False
                except Exception as exc:
                    log.error("LLM arbitration failed for pair %d: %s",
                              row["pair_id"], exc)
                    decision_str = "no_match"
                    reason = f"API error: {exc}"
                    error = True

            results[idx] = {
                "pair_id": row["pair_id"],
                "db_decision": "llm_match" if decision_str == "match" else "llm_no_match",
                "reason": reason,
                "error": error,
                "row": row,
            }

        tasks = [_one(i, r) for i, r in enumerate(rows)]
        # Log progress every 100 completions
        done = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % 100 == 0 or done == len(tasks):
                log.info("llm_arbitration: %d/%d complete", done, len(tasks))

        await client.close()
        return results

    # Run the async event loop
    results = asyncio.run(_arbitrate_all(rows))

    # ── Write results to DB + log (main thread, batched) ─────────────────────

    COMMIT_BATCH = 50
    llm_match = llm_no_match = errors = 0

    with open(decision_log_path, "a", encoding="utf-8") as log_f:
        for i, res in enumerate(results):
            if res is None:
                continue
            row = res["row"]
            db_decision = res["db_decision"]
            reason = res["reason"]

            if db_decision == "llm_match":
                llm_match += 1
            else:
                llm_no_match += 1
            if res["error"]:
                errors += 1

            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "pair_id": res["pair_id"],
                "tier": "llm",
                "cosine_sim": float(row["cosine_sim"]),
                "computed_score": float(row["computed_score"]),
                "manufacturer_bonus": row["manufacturer_bonus"],
                "uom_conflict": row["uom_conflict"],
                "attribute_conflict": row["attribute_conflict"],
                "category_bucket": row["category_bucket"],
                "name_a": row["name_a"],
                "name_b": row["name_b"],
                "decision": db_decision,
                "reason": reason,
            }
            log_f.write(json.dumps(entry) + "\n")

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE candidate_pairs
                    SET decision = %s, llm_reason = %s, decided_at = NOW()
                    WHERE id = %s
                    """,
                    (db_decision, reason, res["pair_id"]),
                )

            if (i + 1) % COMMIT_BATCH == 0:
                conn.commit()
                log_f.flush()

        conn.commit()
        log_f.flush()

    summary = {
        "pending": len(rows),
        "llm_match": llm_match,
        "llm_no_match": llm_no_match,
        "errors": errors,
        "dry_run": dry_run,
    }
    log.info("llm_arbitration complete: %s", summary)
    return summary


def _log_auto_decisions(conn, decision_log_path: str) -> None:
    """
    Append auto_match and auto_reject decisions to the decision log so every
    match decision has a traceable record, not just the LLM-arbitrated ones.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cp.id AS pair_id, cp.cosine_sim, cp.computed_score,
                   cp.decision, cp.manufacturer_bonus, cp.uom_conflict,
                   cp.attribute_conflict, cp.category_bucket,
                   si_a.raw_name AS name_a, si_b.raw_name AS name_b
            FROM candidate_pairs cp
            JOIN source_items si_a ON si_a.id = cp.item_a_id
            JOIN source_items si_b ON si_b.id = cp.item_b_id
            WHERE cp.decision IN ('auto_match', 'auto_reject')
            ORDER BY cp.id
            """
        )
        rows = cur.fetchall()

    if not rows:
        return

    with open(decision_log_path, "a", encoding="utf-8") as log_f:
        for row in rows:
            reason = (
                f"cosine_sim={float(row['cosine_sim']):.3f}, "
                f"computed_score={float(row['computed_score']):.3f}"
            )
            if row["manufacturer_bonus"]:
                reason += ", +brand_bonus"
            if row["uom_conflict"]:
                reason += ", -uom_conflict"
            if row["attribute_conflict"]:
                reason += ", -attribute_conflict"

            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "pair_id": row["pair_id"],
                "tier": "auto",
                "cosine_sim": float(row["cosine_sim"]),
                "computed_score": float(row["computed_score"]),
                "manufacturer_bonus": row["manufacturer_bonus"],
                "uom_conflict": row["uom_conflict"],
                "attribute_conflict": row["attribute_conflict"],
                "category_bucket": row["category_bucket"],
                "name_a": row["name_a"],
                "name_b": row["name_b"],
                "decision": row["decision"],
                "reason": reason,
            }
            log_f.write(json.dumps(entry) + "\n")


# ── Curated cluster builder ───────────────────────────────────────────────────

class _UnionFind:
    """Path-compressed weighted union-find for cluster building."""

    def __init__(self):
        self._parent: dict[int, int] = {}
        self._rank: dict[int, int] = {}

    def find(self, x: int) -> int:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1

    def clusters(self) -> dict[int, list[int]]:
        """Return {root_id: [member_ids]} for all clusters with >1 member."""
        from collections import defaultdict
        groups: dict[int, list[int]] = defaultdict(list)
        for node in self._parent:
            groups[self.find(node)].append(node)
        return {root: members for root, members in groups.items() if len(members) > 1}


def build_curated_clusters(conn, dry_run: bool = False) -> dict:
    """
    Collect all matched pairs (auto_match + llm_match), cluster them
    transitively using union-find, and write curated_products +
    curated_product_members.

    The canonical item per cluster is chosen by priority:
      1. medidepot (structured Shopify data, has manufacturer field)
      2. skydental
      3. pocketnurse
    Within the same source, prefer the item with the longest raw_name.

    Confidence stored in curated_product_members:
      - canonical member → 1.000
      - auto_match members → their computed_score
      - llm_match members → 0.800 (fixed: LLM decided, not purely numeric)

    Returns a summary dict.
    """
    # Fetch all matched pairs with their scores
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cp.item_a_id, cp.item_b_id, cp.computed_score, cp.decision
            FROM candidate_pairs cp
            WHERE cp.decision IN ('auto_match', 'llm_match')
            """
        )
        matched_pairs = cur.fetchall()

    if not matched_pairs:
        log.info("build_curated_clusters: no matched pairs found")
        return {"clusters": 0, "members": 0}

    # Build union-find
    uf = _UnionFind()
    pair_scores: dict[tuple[int, int], float] = {}
    pair_is_llm: dict[tuple[int, int], bool] = {}

    for row in matched_pairs:
        a, b = row["item_a_id"], row["item_b_id"]
        uf.union(a, b)
        pair_scores[(min(a, b), max(a, b))] = float(row["computed_score"])
        pair_is_llm[(min(a, b), max(a, b))] = row["decision"] == "llm_match"

    clusters = uf.clusters()
    log.info("build_curated_clusters: %d clusters from %d matched pairs",
             len(clusters), len(matched_pairs))

    if dry_run:
        for root, members in clusters.items():
            log.info("  cluster root=%d members=%s", root, members)
        return {"clusters": len(clusters), "members": sum(len(m) for m in clusters.values())}

    # Fetch source metadata for canonical selection
    all_item_ids = {iid for members in clusters.values() for iid in members}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source, raw_name, raw_category, raw_manufacturer
            FROM source_items
            WHERE id = ANY(%s)
            """,
            (list(all_item_ids),),
        )
        items_by_id: dict[int, dict] = {r["id"]: dict(r) for r in cur.fetchall()}

    _SOURCE_PRIORITY = {"medidepot": 0, "skydental": 1, "pocketnurse": 2}

    clusters_created = members_created = 0

    for root, member_ids in clusters.items():
        # Choose canonical item
        def sort_key(iid: int):
            item = items_by_id.get(iid, {})
            return (
                _SOURCE_PRIORITY.get(item.get("source", ""), 99),
                -len(item.get("raw_name") or ""),
            )

        canonical_id = min(member_ids, key=sort_key)
        canonical_item = items_by_id.get(canonical_id, {})

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_products (canonical_name, category)
                VALUES (%s, %s)
                RETURNING id
                """,
                (
                    canonical_item.get("raw_name", "Unknown"),
                    canonical_item.get("raw_category"),
                ),
            )
            cp_id = cur.fetchone()["id"]

        clusters_created += 1

        # Write members
        for iid in member_ids:
            if iid == canonical_id:
                confidence = 1.000
            else:
                key = (min(canonical_id, iid), max(canonical_id, iid))
                if key in pair_scores:
                    confidence = (
                        0.800 if pair_is_llm.get(key) else float(pair_scores[key])
                    )
                else:
                    # Transitively connected — use a moderate default
                    confidence = 0.750

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO curated_product_members
                        (curated_product_id, source_item_id, confidence_score)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (curated_product_id, source_item_id) DO NOTHING
                    """,
                    (cp_id, iid, round(confidence, 3)),
                )
            members_created += 1

        conn.commit()

    summary = {"clusters": clusters_created, "members": members_created}
    log.info("build_curated_clusters complete: %s", summary)
    return summary
