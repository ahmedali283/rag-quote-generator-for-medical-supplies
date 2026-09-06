"""
Export a stratified sample of candidate pairs to CSV for hand labeling.

Sampling strategy:
  - ALL predicted-match pairs (auto_match + llm_match) — typically 20-40 rows
  - Top ~110 llm_no_match pairs by cosine_sim (hardest true negatives)
  - ~25 random auto_reject pairs (easy negatives, sanity check)

Target total: 150-180 rows. This gives full precision coverage (every match
prediction is labeled) plus enough negatives to estimate recall.

predicted_label reflects the final pipeline decision:
  match    = auto_match or llm_match
  no_match = auto_reject or llm_no_match
  ambiguous = pending_llm (should not appear after a complete run)

human_label column is left blank — fill in: match / no_match
"""

import csv
import logging
import os
import random

log = logging.getLogger(__name__)

# Stratified sample targets
SAMPLE_LLM_NO_MATCH = 110   # highest-cosine llm_no_match pairs
SAMPLE_AUTO_REJECT  = 25    # random auto_reject pairs
RANDOM_SEED         = 42


def _decision_to_label(decision: str) -> str:
    if decision in ("auto_match", "llm_match"):
        return "match"
    if decision in ("auto_reject", "llm_no_match"):
        return "no_match"
    return "ambiguous"


def _fetch_stratum(cur, decision_filter: str, order: str, limit: int | None) -> list:
    limit_clause = f"LIMIT {limit}" if limit else ""
    cur.execute(
        f"""
        SELECT
            cp.id              AS pair_id,
            cp.item_a_id, cp.item_b_id,
            si_a.source        AS source_a,
            si_b.source        AS source_b,
            si_a.raw_name      AS name_a,
            si_b.raw_name      AS name_b,
            si_a.source_url    AS url_a,
            si_b.source_url    AS url_b,
            si_a.raw_pack_info AS pack_a,
            si_b.raw_pack_info AS pack_b,
            cp.category_bucket,
            cp.cosine_sim,
            cp.computed_score,
            cp.manufacturer_bonus,
            cp.uom_conflict,
            cp.attribute_conflict,
            cp.decision,
            cp.llm_reason
        FROM candidate_pairs cp
        JOIN source_items si_a ON si_a.id = cp.item_a_id
        JOIN source_items si_b ON si_b.id = cp.item_b_id
        WHERE cp.decision = %s
        ORDER BY {order}
        {limit_clause}
        """,
        (decision_filter,),
    )
    return cur.fetchall()


def export_eval_csv(conn, output_path: str) -> int:
    """
    Write a stratified candidate-pair sample to output_path for hand labeling.
    Returns the number of rows written.
    """
    with conn.cursor() as cur:
        # All auto_match
        auto_match_rows = _fetch_stratum(cur, "auto_match", "cp.computed_score DESC, cp.id", None)
        # All llm_match
        llm_match_rows = _fetch_stratum(cur, "llm_match", "cp.computed_score DESC, cp.id", None)
        # Top llm_no_match by cosine_sim (hardest negatives — most likely to contain false negatives)
        llm_no_match_rows = _fetch_stratum(cur, "llm_no_match", "cp.cosine_sim DESC, cp.id", SAMPLE_LLM_NO_MATCH)
        # All auto_reject, then random sample
        all_auto_reject = _fetch_stratum(cur, "auto_reject", "cp.id", None)

    rng = random.Random(RANDOM_SEED)
    auto_reject_sample = rng.sample(all_auto_reject, min(SAMPLE_AUTO_REJECT, len(all_auto_reject)))

    rows = list(auto_match_rows) + list(llm_match_rows) + list(llm_no_match_rows) + auto_reject_sample
    # Sort final output by predicted_label (matches first) then cosine_sim desc
    rows.sort(key=lambda r: (0 if r["decision"] in ("auto_match", "llm_match") else 1, -float(r["cosine_sim"])))

    log.info(
        "build_eval_set: sample — auto_match=%d llm_match=%d llm_no_match=%d auto_reject=%d total=%d",
        len(auto_match_rows), len(llm_match_rows), len(llm_no_match_rows), len(auto_reject_sample), len(rows),
    )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fieldnames = [
        "pair_id",
        "item_a_id",
        "item_b_id",
        "source_a",
        "source_b",
        "name_a",
        "name_b",
        "url_a",
        "url_b",
        "pack_a",
        "pack_b",
        "category_bucket",
        "cosine_sim",
        "computed_score",
        "manufacturer_bonus",
        "uom_conflict",
        "attribute_conflict",
        "predicted_label",
        "llm_reason",
        "human_label",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "pair_id":             row["pair_id"],
                "item_a_id":           row["item_a_id"],
                "item_b_id":           row["item_b_id"],
                "source_a":            row["source_a"],
                "source_b":            row["source_b"],
                "name_a":              row["name_a"],
                "name_b":              row["name_b"],
                "url_a":               row["url_a"],
                "url_b":               row["url_b"],
                "pack_a":              row["pack_a"] or "",
                "pack_b":              row["pack_b"] or "",
                "category_bucket":     row["category_bucket"],
                "cosine_sim":          float(row["cosine_sim"]),
                "computed_score":      float(row["computed_score"]),
                "manufacturer_bonus":  row["manufacturer_bonus"],
                "uom_conflict":        row["uom_conflict"],
                "attribute_conflict":  row["attribute_conflict"],
                "predicted_label":     _decision_to_label(row["decision"]),
                "llm_reason":          row.get("llm_reason") or "",
                "human_label":         "",
            })

    log.info("build_eval_set: wrote %d rows to %s", len(rows), output_path)
    return len(rows)
