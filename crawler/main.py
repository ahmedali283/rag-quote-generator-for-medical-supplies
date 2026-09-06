"""
MedCatalogBot — Part 1 product catalog crawler.

Usage
-----
    python -m crawler.main [--source SOURCE] [--no-network] [--log-level LEVEL]
                           [--log-file PATH] [--force-rediscover]

    --source          One of: medidepot, skydental, pocketnurse, all (default: all)
    --no-network      Cache-only mode. Errors loudly if a URL is not cached.
    --log-level       DEBUG | INFO | WARNING (default: INFO)
    --log-file        Path for per-run log file (default: logs/crawl_<ts>.log)
    --force-rediscover  Delete cached Claude schema and re-invoke for HTML sources.

Architecture overview
---------------------
Each source runs as an asyncio coroutine. When --source=all, all three run
concurrently via asyncio.gather — they have independent rate limiters and
separate DB connections so there is no shared state between them.

Resumability
------------
State lives entirely in the DB. A hard kill -9 leaves crawl_runs.status as
'running'. On the next startup, find_resumable_run() detects this and resumes
from crawl_queue, re-processing only 'pending' rows (and any 'fetching' rows
that were in-flight at crash time, reset to 'pending' on startup). Cached
responses mean re-fetched URLs do not hit the network again.

DB connection policy
--------------------
Each run_source() coroutine opens its own psycopg2 connection. psycopg2 is
synchronous; sharing one connection across concurrent coroutines that await
between DB calls would interleave transactions unpredictably. Three connections
(one per source) is the clean solution.

Signal handling
---------------
SIGTERM and SIGINT set a module-level flag. The queue loop checks it at the
top of each iteration and exits gracefully, marking the run as 'interrupted'
so it can be resumed. SIGKILL cannot be intercepted — those runs resume from
'running' status on next startup.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Load .env before any other code reads environment variables.
# dotenv_path is resolved relative to this file so the correct .env is found
# regardless of which directory the process is launched from.
# find_dotenv(usecwd=False) walks up from the source file, but an explicit
# path is more predictable and survives package restructuring.
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

import httpx

from crawler import db
from crawler import robots
from crawler.fetchers.base import build_session, get_rate_limiter, NetworkDisabledError
from crawler.fetchers import medidepot as medidepot_fetcher
from crawler.fetchers import skydental as skydental_fetcher
from crawler.fetchers import pocketnurse as pocketnurse_fetcher
from crawler.extractors import shopify as shopify_extractor
from crawler.extractors import html_parser
from crawler.extractors import html_claude
from crawler.extractors.playwright_fallback import fetch_with_playwright, needs_playwright_fallback

log = logging.getLogger(__name__)

# ── Globals ───────────────────────────────────────────────────────────────────

_shutdown_requested = False

ALL_SOURCES = ["medidepot", "skydental", "pocketnurse"]

SOURCE_HOSTS = {
    "medidepot": "medidepot.com",
    "skydental": "www.skydentalsupply.com",
    "pocketnurse": "www.pocketnurse.com",
}

SOURCE_CATEGORIES = {
    "medidepot": {slug: slug for slug in medidepot_fetcher.COLLECTIONS},
    "skydental": {path: path.strip("/") for path in skydental_fetcher.LISTING_PATHS},
    "pocketnurse": {path: path.strip("/") for path in pocketnurse_fetcher.LISTING_PATHS},
}


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MedCatalogBot — product catalog crawler (Part 1)",
    )
    parser.add_argument(
        "--source",
        choices=ALL_SOURCES + ["all"],
        default="all",
        help="Which source to crawl (default: all). Sources: medidepot, skydental, pocketnurse",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help=(
            "Cache-only mode: use only locally cached responses. "
            "Errors loudly if a required URL is not cached."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING"],
        default="INFO",
        help="Console log verbosity (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Log file path. Defaults to logs/crawl_<source>_<timestamp>.log",
    )
    parser.add_argument(
        "--force-rediscover",
        action="store_true",
        help="Delete cached Claude schema and re-invoke structure discovery",
    )
    return parser.parse_args()


def setup_logging(log_level: str, log_file: str, source: str) -> None:
    """Attach console and file handlers to the root logger."""
    Path("logs").mkdir(exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console: respects --log-level
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, log_level))
    console.setFormatter(fmt)
    root.addHandler(console)

    # File: always DEBUG so every detail is captured for the write-up.
    if log_file is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = f"logs/crawl_{source}_{ts}.log"

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    log.info("Logging to %s at DEBUG level", log_file)


# ── Signal handling ───────────────────────────────────────────────────────────

def _handle_shutdown(signum, frame):
    global _shutdown_requested
    log.warning("Received signal %d — requesting graceful shutdown", signum)
    _shutdown_requested = True


# ── Upsert helper ─────────────────────────────────────────────────────────────

def _upsert_and_track(conn, run_id: int, item: dict) -> str:
    """
    Upsert one normalized item and record a price_history row if the price
    changed. Returns 'new', 'changed', or 'unchanged'.
    Commits are the caller's responsibility.
    """
    outcome, source_item_id = db.upsert_source_item(conn, item)
    db.increment_crawl_run_counter(conn, run_id, f"records_{outcome}")
    db.increment_crawl_run_counter(conn, run_id, "records_upserted")

    if outcome in ("new", "changed"):
        latest_price = db.get_latest_price(conn, source_item_id)
        raw_price = item.get("raw_price", "")
        try:
            price_float = float(raw_price.replace(",", "").replace("$", "")) if raw_price else None
        except (ValueError, AttributeError):
            price_float = None

        if price_float is not None and price_float > 0:
            price_changed = (
                latest_price is None
                or abs(latest_price - price_float) > 0.001
            )
            if price_changed:
                db.insert_price_history(conn, source_item_id, price_float)
                log.debug(
                    "Price history: item_id=%d old=%s new=%.4f",
                    source_item_id, latest_price, price_float,
                )

    return outcome


# ── Per-source orchestrators ──────────────────────────────────────────────────

async def run_medidepot(
    conn,
    session: httpx.AsyncClient,
    no_network: bool,
    run_id: int,
) -> None:
    """
    Crawl MediDepot using Shopify's /products.json API.

    Unlike HTML sources, MediDepot uses the structured JSON API directly —
    no product-page HTML fetching, no Claude schema discovery. Products are
    extracted inline as each JSON page is received.
    """
    rate_limiter = get_rate_limiter(SOURCE_HOSTS["medidepot"])
    # Commit every COMMIT_BATCH products rather than per-product to reduce
    # round-trips against a remote DB (Azure adds ~200-500ms per commit).
    COMMIT_BATCH = 10
    dirty = 0

    async for slug, raw_product in medidepot_fetcher.iter_all_products(
        session, rate_limiter, no_network, conn=conn, run_id=run_id
    ):
        if _shutdown_requested:
            break

        db.increment_crawl_run_counter(conn, run_id, "pages_fetched")
        item = shopify_extractor.extract_product(raw_product, slug)

        if item is None:
            db.increment_crawl_run_counter(conn, run_id, "extraction_failures")
            dirty += 1
        else:
            outcome = _upsert_and_track(conn, run_id, item)
            dirty += 1
            log.debug("MediDepot [%s] %s: %s", slug, outcome, item["raw_name"])

        if dirty >= COMMIT_BATCH:
            conn.commit()
            dirty = 0

    if dirty:
        conn.commit()


async def run_html_source(
    source: str,
    conn,
    session: httpx.AsyncClient,
    no_network: bool,
    run_id: int,
    force_rediscover: bool,
) -> None:
    """
    Crawl an HTML source (Sky Dental or Anatomy Warehouse).

    Flow:
    1. Select fetcher and listing paths for this source.
    2. Fetch schema (cache or Claude API).
    3. For each product page: extract, Playwright-fallback if needed, upsert.
    4. Track extraction failures; re-discover schema if threshold exceeded.
    """
    if source == "skydental":
        host = SOURCE_HOSTS["skydental"]
        iter_products = skydental_fetcher.iter_all_products
        listing_paths = skydental_fetcher.LISTING_PATHS
    else:
        host = SOURCE_HOSTS["pocketnurse"]
        iter_products = pocketnurse_fetcher.iter_all_products
        listing_paths = pocketnurse_fetcher.LISTING_PATHS

    rate_limiter = get_rate_limiter(host)

    # Schema discovery: fetch a sample page first if no cache exists.
    schema = await _get_or_discover_schema(
        source, session, rate_limiter, no_network, force_rediscover,
        listing_paths,
    )

    extraction_failures = 0
    extraction_total = 0

    async for product_url, html in iter_products(
        session, rate_limiter, schema, no_network, conn=conn, run_id=run_id
    ):
        if _shutdown_requested:
            break

        db.increment_crawl_run_counter(conn, run_id, "pages_fetched")
        extraction_total += 1

        # Playwright fallback if needed.
        if needs_playwright_fallback(html, schema):
            fallback_html = await fetch_with_playwright(product_url, source)
            if fallback_html:
                html = fallback_html

        category = _infer_category(product_url, listing_paths)
        item = html_parser.extract_product(html, product_url, source, schema, category)

        if item is None:
            extraction_failures += 1
            db.increment_crawl_run_counter(conn, run_id, "extraction_failures")
            log.warning("Extraction failure: %s", product_url)

            # Re-discover schema if failure rate exceeds threshold.
            if html_claude.should_rediscover(extraction_failures, extraction_total):
                log.warning(
                    "Extraction failure rate %.0f%% (%d/%d) exceeds threshold for %s "
                    "— re-discovering schema",
                    100 * extraction_failures / extraction_total,
                    extraction_failures, extraction_total, source,
                )
                schema = html_claude.discover_schema(source, html, force=True)
                extraction_failures = 0  # reset after rediscovery
            continue

        outcome = _upsert_and_track(conn, run_id, item)
        conn.commit()
        log.debug("[%s] %s: %s", source, outcome, item["raw_name"])


async def _get_or_discover_schema(
    source: str,
    session: httpx.AsyncClient,
    rate_limiter,
    no_network: bool,
    force_rediscover: bool,
    listing_paths: list[str],
) -> dict:
    """
    Return the extraction schema for an HTML source, discovering it via
    Claude if not already cached (or if force_rediscover is set).

    Fetches a real product page as the sample for Claude — a listing page
    alone does not show us the product field structure.
    """
    if not force_rediscover:
        cached = html_claude.load_schema_from_cache(source)
        if cached is not None:
            return cached

    # Need a sample product page. Fetch the first listing, extract one link.
    if source == "skydental":
        listing_url = skydental_fetcher.listing_url(listing_paths[0])
        fetch_fn = skydental_fetcher.fetch_product_page
        extract_urls = skydental_fetcher.extract_product_urls_from_listing
    else:
        listing_url = pocketnurse_fetcher.listing_url(listing_paths[0])
        fetch_fn = pocketnurse_fetcher.fetch_product_page
        extract_urls = pocketnurse_fetcher.extract_product_urls_from_listing

    listing_html, _ = await _safe_fetch(session, listing_url, source, rate_limiter, no_network)
    if listing_html is None:
        log.warning("Could not fetch sample listing for schema discovery — using defaults")
        return html_claude.DEFAULT_SCHEMA

    # Use default schema (all None) just to get URL list for schema discovery.
    product_urls = extract_urls(listing_html, html_claude.DEFAULT_SCHEMA)
    if not product_urls:
        log.warning("No product URLs found on listing — using default schema")
        return html_claude.DEFAULT_SCHEMA

    sample_html, _ = await fetch_fn(session, product_urls[0], rate_limiter, no_network)
    if sample_html is None:
        log.warning("Could not fetch sample product page — using default schema")
        return html_claude.DEFAULT_SCHEMA

    return html_claude.discover_schema(source, sample_html, force=force_rediscover)


async def _safe_fetch(session, url, source, rate_limiter, no_network):
    """Thin wrapper around fetch_with_retry that swallows NetworkDisabledError."""
    from crawler.fetchers.base import fetch_with_retry
    try:
        return await fetch_with_retry(
            session, url, source, rate_limiter, no_network=no_network
        )
    except NetworkDisabledError as exc:
        log.error("%s", exc)
        return None, "no_network"


def _infer_category(url: str, listing_paths: list[str]) -> str:
    """
    Infer the category label for a product URL by matching it against the
    listing path that it was discovered from.

    PocketNurse product pages live at /international/<sku-slug> — they don't
    carry the listing path segment (/products/manikins-simulators/) in their
    URL, so a substring match always fails. For pocketnurse.com we fall back
    to the deepest listing path segment as the category, since all three seed
    paths are simulator/manikin categories.
    """
    for path in listing_paths:
        if path.strip("/") in url:
            return path.strip("/").split("/")[-1]
    # PocketNurse fallback: all crawled products come from simulator listing paths.
    if "pocketnurse.com" in url:
        return "manikins-simulators"
    return ""


# ── Resumability ──────────────────────────────────────────────────────────────

def _get_or_create_run(conn, source: str) -> int:
    """
    Return the run_id to use for this crawl.

    If a previous run is still in-flight ('running' or 'interrupted'),
    resume it. A crashed process leaves status='running', so both states
    are treated as resumable. On resume, any 'fetching' rows are reset to
    'pending' (they were in-flight at crash time; the cache will serve them
    without network access).
    """
    existing = db.find_resumable_run(conn, source)
    if existing:
        run_id = existing["id"]
        reset_count = db.reset_fetching_to_pending(conn, run_id)
        db.update_crawl_run_status(conn, run_id, "running")
        conn.commit()
        log.info(
            "Resuming run %d for %s (reset %d in-flight items to pending)",
            run_id, source, reset_count,
        )
        return run_id

    run_id = db.create_crawl_run(conn, source)
    conn.commit()
    log.info("Started new crawl run %d for %s", run_id, source)
    return run_id


# ── Top-level source runner ───────────────────────────────────────────────────

async def run_source(
    source: str,
    session: httpx.AsyncClient,
    no_network: bool,
    force_rediscover: bool,
) -> dict:
    """
    Orchestrate a complete crawl of one source.

    Opens its own DB connection (psycopg2 is synchronous; sharing across
    concurrent coroutines would interleave transactions). Returns a summary
    dict for the final report.

    Any exception that escapes this function will be caught by asyncio.gather
    (return_exceptions=True) and logged at CRITICAL level in _main before the
    summary is printed. Nothing is silently swallowed.
    """
    conn = db.get_connection()
    run_id: Optional[int] = None
    try:
        # _get_or_create_run is outside the inner try/except so that a DB
        # connection failure surfaces immediately rather than being masked.
        run_id = _get_or_create_run(conn, source)

        # Track whether the crawl generator ran to natural completion.
        # Only a full, uninterrupted pass should deactivate unseen items —
        # an interrupted run has only visited some collections, so products
        # from unvisited collections must not be marked inactive.
        completed_fully = False

        try:
            if source == "medidepot":
                await run_medidepot(conn, session, no_network, run_id)
            else:
                await run_html_source(
                    source, conn, session, no_network, run_id, force_rediscover
                )

            # Only set this flag when the generator exhausted naturally and
            # shutdown was NOT requested mid-run.
            if not _shutdown_requested:
                completed_fully = True

        except NetworkDisabledError as exc:
            log.error("Aborting %s: %s", source, exc)
            db.update_crawl_run_status(conn, run_id, "failed", str(exc))
            conn.commit()
            return db.get_crawl_run(conn, run_id)

        except asyncio.CancelledError:
            db.update_crawl_run_status(conn, run_id, "interrupted")
            conn.commit()
            raise

        except Exception:
            # Log with full traceback so the operator sees exactly what failed.
            log.exception("Unhandled error in %s run %d", source, run_id)
            try:
                # Any DB error leaves the connection in a failed transaction.
                # Roll back first so subsequent queries on this connection work.
                conn.rollback()
                db.update_crawl_run_status(conn, run_id, "failed")
                conn.commit()
            except Exception:
                log.exception("Also failed to update crawl_run status to failed")
            return db.get_crawl_run(conn, run_id)

        finally:
            if _shutdown_requested and run_id is not None:
                try:
                    conn.rollback()
                    db.update_crawl_run_status(conn, run_id, "interrupted")
                    conn.commit()
                except Exception:
                    pass

        if completed_fully:
            # Safe to deactivate: every seed collection was visited in this run.
            deactivated = db.mark_inactive_unseen(conn, source, run_id)
            if deactivated:
                log.info("Marked %d products inactive for %s", deactivated, source)
            db.update_crawl_run_status(conn, run_id, "completed")
        else:
            # Interrupted by signal — mark as interrupted, never deactivate.
            log.info(
                "Run %d for %s was interrupted before completing all collections — "
                "skipping inactive sweep to avoid false deactivations.",
                run_id, source,
            )
            db.update_crawl_run_status(conn, run_id, "interrupted")

        conn.commit()

        return db.get_crawl_run(conn, run_id)

    finally:
        conn.close()


# ── Summary reporting ─────────────────────────────────────────────────────────

def _print_summary(results: list[dict]) -> None:
    """Print a formatted summary table of all crawl run results."""
    print()
    print("=" * 70)
    print("CRAWL SUMMARY")
    print("=" * 70)
    header = f"{'Source':<22} {'Status':<12} {'New':>6} {'Chg':>6} {'Same':>6} {'Fail':>6} {'Pages':>6}"
    print(header)
    print("-" * 70)
    for run in results:
        if not isinstance(run, dict):
            # Should not reach here — _main logs these before calling _print_summary.
            print(f"  {'(exception — see log)':<22} {'CRASHED':<12}")
            continue
        print(
            f"{run.get('source', '?'):<22} "
            f"{run.get('status', '?'):<12} "
            f"{run.get('records_new', 0):>6} "
            f"{run.get('records_changed', 0):>6} "
            f"{run.get('records_unchanged', 0):>6} "
            f"{run.get('extraction_failures', 0):>6} "
            f"{run.get('pages_fetched', 0):>6}"
        )
    print("=" * 70)
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main() -> int:
    args = parse_args()

    sources = ALL_SOURCES if args.source == "all" else [args.source]
    setup_logging(args.log_level, args.log_file, args.source)

    # Register signal handlers for graceful shutdown.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_shutdown)

    if args.no_network:
        log.warning(
            "--no-network mode active: skipping robots.txt fetch, "
            "will error on any uncached URL"
        )
    else:
        log.info("Fetching robots.txt for all target hosts...")
        for host in set(SOURCE_HOSTS[s] for s in sources):
            robots.fetch_robots(host)

    async with build_session() as session:
        tasks = [
            run_source(s, session, args.no_network, args.force_rediscover)
            for s in sources
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Log any exceptions that escaped run_source with full tracebacks.
    # These are always bugs — run_source is supposed to catch everything.
    any_exception = False
    for source_name, result in zip(sources, results):
        if isinstance(result, BaseException):
            any_exception = True
            log.critical(
                "run_source(%s) raised an unhandled exception — "
                "this is a bug in the crawler orchestration:",
                source_name,
                exc_info=result,
            )

    summary_rows = [r for r in results if isinstance(r, dict)]
    _print_summary(summary_rows)

    any_failed = any_exception or any(
        isinstance(r, dict) and r.get("status") == "failed"
        for r in results
    )
    return 1 if any_failed else 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
