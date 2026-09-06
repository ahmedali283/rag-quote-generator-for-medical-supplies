"""
All PostgreSQL operations for the crawler.

Uses psycopg2 (synchronous). Each source gets its own connection opened in
main.py — connections are never shared across coroutines or threads.

Real schema (inspected from the live DB — do not guess column names):

source_items:
  id, source, source_url, source_sku, raw_name, raw_description, raw_price,
  raw_pack_info, raw_category, raw_manufacturer,
  content_hash, captured_at, first_seen_at, last_seen_at, is_active

crawl_runs:
  id, source, started_at, finished_at, pages_fetched, records_upserted,
  records_new, records_changed, records_disappeared, retries,
  extraction_failures, status

crawl_queue:
  id, crawl_run_id, url, url_type, status, attempts, last_attempted_at,
  error_message

price_history:
  id, source_item_id, price, currency, observed_at
"""

import os
import logging
import urllib.parse
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg2
import psycopg2.extras
import psycopg2.extensions

log = logging.getLogger(__name__)

# Columns allowed in increment_crawl_run_counter — guards against SQL injection
# via the whitelisted column name interpolation in that function.
_ALLOWED_COUNTER_COLUMNS = frozenset({
    "pages_fetched",
    "records_upserted",
    "records_new",
    "records_changed",
    "records_unchanged",
    "records_disappeared",
    "retries",
    "extraction_failures",
})


# ── Connection ────────────────────────────────────────────────────────────────

def get_connection() -> psycopg2.extensions.connection:
    """
    Open a new connection using the DATABASE_URL environment variable.
    The caller is responsible for closing it. Prefer get_db() instead.

    DATABASE_URL passwords may contain characters that psycopg2's DSN parser
    rejects as malformed percent-encoding (e.g. a bare '%' not followed by
    two hex digits). We parse the URL with urllib and pass components as
    keyword arguments so psycopg2 never interprets the percent-encoding.
    """
    raw = os.environ["DATABASE_URL"]
    parsed = urllib.parse.urlparse(raw)
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=urllib.parse.unquote(parsed.password or ""),
        **_dsn_options(parsed.query),
    )
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def _dsn_options(query_string: str) -> dict:
    """Parse ?key=value pairs from a DATABASE_URL query string into psycopg2 kwargs."""
    supported = {"sslmode", "sslcert", "sslkey", "sslrootcert", "connect_timeout",
                 "application_name"}
    return {k: v for k, v in urllib.parse.parse_qsl(query_string) if k in supported}


@contextmanager
def get_db() -> Iterator[psycopg2.extensions.connection]:
    """
    Yield a connection. Commits on clean exit, rolls back on exception,
    always closes.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── crawl_runs ────────────────────────────────────────────────────────────────

def create_crawl_run(conn, source: str) -> int:
    """Insert a new crawl_runs row and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_runs (source, status, started_at)
            VALUES (%s, 'running', NOW())
            RETURNING id
            """,
            (source,),
        )
        return cur.fetchone()["id"]


def find_resumable_run(conn, source: str) -> Optional[dict]:
    """
    Return the most recent in-flight run for this source, or None.
    A crashed process leaves status='running'; both 'running' and
    'interrupted' are treated as resumable.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM crawl_runs
            WHERE source = %s AND status IN ('running', 'interrupted')
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (source,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_crawl_run_status(
    conn, run_id: int, status: str, error_message: Optional[str] = None
) -> None:
    """Set status and finished_at for terminal statuses."""
    terminal = status in ("completed", "failed", "interrupted")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_runs
            SET status = %s,
                finished_at = CASE WHEN %s THEN NOW() ELSE finished_at END
            WHERE id = %s
            """,
            (status, terminal, run_id),
        )


def increment_crawl_run_counter(conn, run_id: int, field: str, delta: int = 1) -> None:
    """
    Atomically increment one counter column.
    Field name is whitelisted before interpolation to prevent SQL injection.
    """
    if field not in _ALLOWED_COUNTER_COLUMNS:
        raise ValueError(f"Unknown counter column: {field!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE crawl_runs SET {field} = COALESCE({field}, 0) + %s WHERE id = %s",
            (delta, run_id),
        )


def get_crawl_run(conn, run_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM crawl_runs WHERE id = %s", (run_id,))
        return dict(cur.fetchone())


# ── crawl_queue ───────────────────────────────────────────────────────────────

def enqueue_urls(conn, run_id: int, urls: list[dict]) -> None:
    """
    Bulk-insert URL descriptors into crawl_queue.

    Each dict must have: url, url_type.
    ON CONFLICT (crawl_run_id, url) DO NOTHING makes re-enqueue on resume safe.
    """
    if not urls:
        return
    rows = [(run_id, u["url"], u["url_type"], "pending") for u in urls]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO crawl_queue (crawl_run_id, url, url_type, status)
            VALUES %s
            ON CONFLICT (crawl_run_id, url) DO NOTHING
            """,
            rows,
        )


def get_pending_queue_items(conn, run_id: int, limit: int = 50) -> list[dict]:
    """Fetch up to `limit` pending items, locking them with SKIP LOCKED."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM crawl_queue
            WHERE crawl_run_id = %s AND status = 'pending'
            ORDER BY id ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (run_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def reset_fetching_to_pending(conn, run_id: int) -> int:
    """
    On resume, rows stuck in 'fetching' (process crashed mid-fetch) are reset
    to 'pending'. The cache serves them without a network round-trip.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_queue
            SET status = 'pending'
            WHERE crawl_run_id = %s AND status = 'fetching'
            """,
            (run_id,),
        )
        return cur.rowcount


def update_queue_item_status(
    conn, item_id: int, status: str, error_message: Optional[str] = None
) -> None:
    """Update status immediately after each fetch attempt. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_queue
            SET status = %s,
                error_message = %s,
                last_attempted_at = NOW(),
                attempts = COALESCE(attempts, 0) + CASE WHEN %s = 'fetching' THEN 1 ELSE 0 END
            WHERE id = %s
            """,
            (status, error_message, status, item_id),
        )


def count_queue_by_status(conn, run_id: int) -> dict:
    """Return {status: count} for all statuses in a run."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM crawl_queue
            WHERE crawl_run_id = %s
            GROUP BY status
            """,
            (run_id,),
        )
        return {row["status"]: row["cnt"] for row in cur.fetchall()}


# ── source_items + price_history ──────────────────────────────────────────────

def upsert_source_item(conn, item: dict) -> tuple[str, int]:
    """
    Upsert a product into source_items using the real column names.

    Real columns: source, source_url, source_sku, raw_name, raw_description,
    raw_price, raw_pack_info, raw_category, raw_manufacturer,
    content_hash, first_seen_at, last_seen_at, is_active.

    Uses PostgreSQL's xmax=0 trick to detect INSERT vs UPDATE in one round-trip.
    Returns ('new'|'changed'|'unchanged', source_item_id).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_items
                (source, source_url, source_sku, raw_name, raw_description,
                 raw_price, raw_pack_info, raw_category, raw_manufacturer,
                 content_hash, captured_at, first_seen_at, last_seen_at, is_active)
            VALUES
                (%(source)s, %(source_url)s, %(source_sku)s, %(raw_name)s,
                 %(raw_description)s, %(raw_price)s, %(raw_pack_info)s,
                 %(raw_category)s, %(raw_manufacturer)s, %(content_hash)s,
                 NOW(), NOW(), NOW(), true)
            ON CONFLICT (source, source_url) DO UPDATE SET
                source_sku      = EXCLUDED.source_sku,
                raw_name        = EXCLUDED.raw_name,
                raw_description = EXCLUDED.raw_description,
                raw_price       = EXCLUDED.raw_price,
                raw_pack_info   = EXCLUDED.raw_pack_info,
                raw_category    = EXCLUDED.raw_category,
                raw_manufacturer= EXCLUDED.raw_manufacturer,
                content_hash    = EXCLUDED.content_hash,
                last_seen_at    = NOW(),
                is_active       = true
            RETURNING
                id,
                (xmax = 0)                                             AS is_insert,
                (xmax != 0 AND
                 source_items.content_hash != %(content_hash)s)       AS hash_changed
            """,
            item,
        )
        row = cur.fetchone()
        source_item_id = row["id"]
        if row["is_insert"]:
            outcome = "new"
        elif row["hash_changed"]:
            outcome = "changed"
        else:
            outcome = "unchanged"
        return outcome, source_item_id


def get_latest_price(conn, source_item_id: int) -> Optional[float]:
    """Return the most recently observed price for an item, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT price FROM price_history
            WHERE source_item_id = %s
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (source_item_id,),
        )
        row = cur.fetchone()
        return float(row["price"]) if row else None


def insert_price_history(
    conn, source_item_id: int, price: float, currency: str = "USD"
) -> None:
    """Append a price observation. Never updates existing rows."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO price_history (source_item_id, price, currency, observed_at)
            VALUES (%s, %s, %s, NOW())
            """,
            (source_item_id, price, currency),
        )


def mark_inactive_unseen(conn, source: str, run_id: int) -> int:
    """
    At end of a full crawl pass, mark source_items not touched in this run
    as is_active=false. 'Touched' means last_seen_at >= run's started_at.
    Returns count of rows deactivated.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE source_items si
            SET is_active = false
            FROM crawl_runs cr
            WHERE si.source = %s
              AND cr.id = %s
              AND si.last_seen_at < cr.started_at
            """,
            (source, run_id),
        )
        return cur.rowcount
