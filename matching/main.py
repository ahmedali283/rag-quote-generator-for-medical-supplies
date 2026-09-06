"""
Part 2 matching pipeline — CLI entry point.

Usage
-----
    # Apply DB schema (run once)
    psql $DATABASE_URL -f matching/migration.sql

    # Run full pipeline
    python -m matching.main --step all

    # Individual steps
    python -m matching.main --step normalize
    python -m matching.main --step embed
    python -m matching.main --step match
    python -m matching.main --step build-eval --eval-csv eval/candidate_pairs.csv

    # After hand-labeling the CSV
    python -m matching.score_eval --csv eval/candidate_pairs.csv

    # Dry run (no DB writes, no API calls)
    python -m matching.main --step all --dry-run

Steps
-----
normalize   Parse raw_pack_info → normalized_items (regex-first, LLM fallback).
embed       Generate Voyage AI embeddings for all normalized_items rows.
match       Block → score → LLM-arbitrate → write curated clusters.
build-eval  Export candidate pairs CSV for hand labeling.
all         normalize → embed → match → build-eval (in order).

Environment variables required
-------------------------------
DATABASE_URL       PostgreSQL connection string
ANTHROPIC_API_KEY  Required for normalize (LLM fallback) and match (LLM arbitration)
VOYAGE_API_KEY     Required for embed step

The .env file in the project root is loaded automatically.
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Load .env before anything reads env vars.
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# Add parent to sys.path so `from crawler.db import get_db` works when
# running as `python -m matching.main` from the project root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from crawler.db import get_db
from matching import normalize, embed, matcher, build_eval_set

log = logging.getLogger(__name__)


def setup_logging(level: str, log_file: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(fmt)
    root.addHandler(console)

    Path("logs").mkdir(exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Part 2 medical supply matching pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--step",
        choices=["normalize", "embed", "match", "build-eval", "all"],
        default="all",
        help="Which step to run (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would happen without writing to the DB or calling APIs",
    )
    parser.add_argument(
        "--eval-csv",
        default="eval/candidate_pairs.csv",
        help="Path for the eval set CSV (default: eval/candidate_pairs.csv)",
    )
    parser.add_argument(
        "--decision-log",
        default=None,
        help="Path for the match decision log (default: logs/match_decisions_<ts>.jsonl)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING"],
        default="INFO",
    )
    parser.add_argument(
        "--cosine-floor",
        type=float,
        default=matcher.COSINE_FLOOR,
        help=f"Cosine similarity floor for blocking (default: {matcher.COSINE_FLOOR})",
    )
    return parser.parse_args()


def step_normalize(conn, dry_run: bool) -> None:
    log.info("=== STEP: normalize ===")
    summary = normalize.run_normalize(conn, dry_run=dry_run)
    log.info("normalize: %s", summary)


def step_embed(conn, dry_run: bool) -> None:
    log.info("=== STEP: embed ===")
    summary = embed.run_embed(conn, dry_run=dry_run)
    log.info("embed: %s", summary)


def step_match(conn, decision_log: str, cosine_floor: float, dry_run: bool) -> None:
    log.info("=== STEP: match ===")

    # 1. Blocking
    n_pairs = matcher.run_blocking(conn, cosine_floor=cosine_floor, dry_run=dry_run)
    log.info("blocking: %d pairs inserted/found", n_pairs)

    # 2. Scoring
    score_summary = matcher.run_scoring(conn, dry_run=dry_run)
    log.info("scoring: %s", score_summary)

    if not dry_run:
        # Log all auto decisions before LLM arbitration so the log is complete.
        matcher._log_auto_decisions(conn, decision_log)

    # 3. LLM arbitration
    arb_summary = matcher.run_llm_arbitration(conn, decision_log, dry_run=dry_run)
    log.info("llm_arbitration: %s", arb_summary)

    # 4. Build curated clusters
    cluster_summary = matcher.build_curated_clusters(conn, dry_run=dry_run)
    log.info("curated_clusters: %s", cluster_summary)


def step_build_eval(conn, eval_csv: str) -> None:
    log.info("=== STEP: build-eval ===")
    n = build_eval_set.export_eval_csv(conn, eval_csv)
    log.info("build_eval: wrote %d rows to %s", n, eval_csv)


def main() -> None:
    args = parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"logs/matching_{ts}.log"
    setup_logging(args.log_level, log_file)

    decision_log = args.decision_log or f"logs/match_decisions_{ts}.jsonl"

    if args.dry_run:
        log.warning("DRY RUN mode — no DB writes, no API calls")

    steps = (
        ["normalize", "embed", "match", "build-eval"]
        if args.step == "all"
        else [args.step]
    )

    try:
        with get_db() as conn:
            for step in steps:
                if step == "normalize":
                    step_normalize(conn, args.dry_run)
                elif step == "embed":
                    step_embed(conn, args.dry_run)
                elif step == "match":
                    step_match(conn, decision_log, args.cosine_floor, args.dry_run)
                elif step == "build-eval":
                    step_build_eval(conn, args.eval_csv)
    except Exception:
        log.exception("Pipeline failed")
        sys.exit(1)

    log.info("Pipeline complete. Log: %s", log_file)
    if not args.dry_run and "match" in steps:
        log.info("Decision log: %s", decision_log)


if __name__ == "__main__":
    main()
