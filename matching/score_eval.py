"""
Evaluation scoring script — run AFTER hand-labeling the CSV.

Reads the completed eval CSV (with human_label filled in for ≥ 150 rows)
and computes:
  - Precision, Recall, F1 (binary: match vs no_match)
  - Confusion matrix (TN / FP / FN / TP)
  - Per-tier breakdown: how the pipeline performed within each decision tier
    (auto_match, auto_reject, ambiguous/LLM-arbitrated)
  - Coverage: fraction of pairs that received a definitive prediction

Usage:
    python -m matching.score_eval --csv eval/candidate_pairs.csv

human_label values recognized: "match", "no_match" (case-insensitive).
Rows with blank or unrecognized human_label are skipped.
"""

import argparse
import csv
import os
import sys
from collections import defaultdict


def _load_labeled(csv_path: str) -> tuple[list[dict], int]:
    """
    Return (labeled_rows, total_rows).
    labeled_rows are those with a valid human_label ('match' or 'no_match').
    """
    labeled = []
    total = 0
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            total += 1
            label = row.get("human_label", "").strip().lower()
            if label in ("match", "no_match"):
                labeled.append(row)
    return labeled, total


def _to_binary(label_str: str) -> int:
    """1 = match, 0 = no_match/ambiguous."""
    return 1 if label_str.strip().lower() == "match" else 0


def run_scoring(csv_path: str) -> None:
    labeled, total = _load_labeled(csv_path)

    if not labeled:
        print(f"No labeled rows found in {csv_path!r}.")
        print("Fill in the 'human_label' column (match/no_match) for at least 150 rows.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"Eval set: {csv_path}")
    print(f"Total rows: {total}  |  Labeled: {len(labeled)}  |  Skipped: {total - len(labeled)}")
    print(f"{'='*60}\n")

    y_true = [_to_binary(r["human_label"]) for r in labeled]
    y_pred = [_to_binary(r["predicted_label"]) for r in labeled]

    # ── Overall metrics ───────────────────────────────────────────────────────
    tp = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 1)
    fp = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 0)
    tn = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 0)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0.0)

    print("── Overall metrics ─────────────────────────────────────────")
    print(f"  Precision : {precision:.3f}  ({tp} true matches / {tp+fp} predicted matches)")
    print(f"  Recall    : {recall:.3f}  ({tp} true matches / {tp+fn} actual matches)")
    print(f"  F1 score  : {f1:.3f}")
    print()
    print("── Confusion matrix ────────────────────────────────────────")
    print(f"                 Predicted no_match  Predicted match")
    print(f"  Actual no_match      {tn:>6}              {fp:>6}")
    print(f"  Actual match         {fn:>6}              {tp:>6}")
    print()

    # ── Per-tier breakdown ────────────────────────────────────────────────────
    tiers: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in labeled:
        pred = row["predicted_label"].strip().lower()
        true = row["human_label"].strip().lower()
        tiers[pred]["total"] += 1
        if true == "match" and pred == "match":
            tiers[pred]["tp"] += 1
        elif true == "no_match" and pred == "match":
            tiers[pred]["fp"] += 1
        elif true == "match" and pred != "match":
            tiers[pred]["fn"] += 1
        else:
            tiers[pred]["tn"] += 1

    print("── Per-tier breakdown ──────────────────────────────────────")
    print(f"  {'Tier':<14} {'Count':>6}  {'TP':>5}  {'FP':>5}  {'FN':>5}  {'TN':>5}  {'Acc':>6}")
    print(f"  {'-'*60}")
    for tier in ("match", "no_match", "ambiguous"):
        t = tiers.get(tier, {})
        n = t.get("total", 0)
        if n == 0:
            continue
        tp_ = t.get("tp", 0)
        fp_ = t.get("fp", 0)
        fn_ = t.get("fn", 0)
        tn_ = t.get("tn", 0)
        acc = (tp_ + tn_) / n if n > 0 else 0.0
        print(f"  {tier:<14} {n:>6}  {tp_:>5}  {fp_:>5}  {fn_:>5}  {tn_:>5}  {acc:>6.3f}")
    print()

    # ── Score distribution ────────────────────────────────────────────────────
    try:
        scores = [float(r["computed_score"]) for r in labeled]
        match_scores = [float(r["computed_score"]) for r in labeled
                        if r["human_label"].strip().lower() == "match"]
        nomatch_scores = [float(r["computed_score"]) for r in labeled
                          if r["human_label"].strip().lower() == "no_match"]

        def _stats(vals):
            if not vals:
                return "n/a"
            return f"min={min(vals):.3f} avg={sum(vals)/len(vals):.3f} max={max(vals):.3f}"

        print("── Score distribution ──────────────────────────────────────")
        print(f"  All labeled  : {_stats(scores)}")
        print(f"  True matches : {_stats(match_scores)}")
        print(f"  True no-match: {_stats(nomatch_scores)}")
        print()
    except (ValueError, KeyError):
        pass

    # ── Scikit-learn for cross-check (optional) ───────────────────────────────
    try:
        from sklearn.metrics import (
            classification_report,
            confusion_matrix,
        )
        print("── sklearn classification_report ────────────────────────────")
        print(classification_report(
            y_true, y_pred,
            target_names=["no_match", "match"],
            zero_division=0,
        ))
    except ImportError:
        pass  # sklearn optional; metrics above are sufficient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score the hand-labeled eval CSV against pipeline predictions."
    )
    parser.add_argument("--csv", required=True, help="Path to labeled eval CSV")
    args = parser.parse_args()
    if not os.path.exists(args.csv):
        print(f"Error: CSV file not found: {args.csv!r}")
        print("Run 'python -m matching.main --step build-eval' first, then fill in human_label.")
        sys.exit(1)
    run_scoring(args.csv)


if __name__ == "__main__":
    main()
