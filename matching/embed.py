"""
Voyage AI embedding generation for normalized_items.

For each source_items row, we build a plain-text embedding input from:
    "{raw_name} | {raw_category} | {raw_manufacturer}"
Empty fields are omitted (not included as empty strings) to avoid
polluting the embedding space with repeated padding tokens.

Model: voyage-2 (1024 dimensions) — matches historical_quotes.embedding
column type for consistency with the rest of the schema.

Batch size: Voyage allows up to 128 inputs per API call. We process in
batches of 128 and commit after each batch so a crash mid-run is resumable
without re-embedding already-stored rows.

VOYAGE_API_KEY must be set in the environment (or .env file).
"""

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

VOYAGE_MODEL = "voyage-2"
VOYAGE_DIMS = 1024
VOYAGE_BATCH_SIZE = 128


def build_embedding_input(
    raw_name: str,
    raw_category: Optional[str],
    raw_manufacturer: Optional[str],
) -> str:
    """
    Construct the string to embed for a single item.

    Uses " | " as the field separator so the embedding captures the
    combined semantic signal of name, category, and manufacturer without
    treating them as a single run-on phrase. Fields with no content are
    omitted entirely.
    """
    parts = [raw_name.strip()]
    if raw_category and raw_category.strip():
        parts.append(raw_category.strip())
    if raw_manufacturer and raw_manufacturer.strip():
        parts.append(raw_manufacturer.strip())
    return " | ".join(parts)


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Call the Voyage AI API and return a list of 1024-dim float vectors.
    Raises on API error — callers handle retry/skip.
    """
    import voyageai

    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY environment variable not set")

    client = voyageai.Client(api_key=api_key)
    result = client.embed(texts, model=VOYAGE_MODEL)
    vectors = result.embeddings
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"Voyage returned {len(vectors)} embeddings for {len(texts)} inputs"
        )
    return vectors


def run_embed(conn, dry_run: bool = False) -> dict:
    """
    Embed all normalized_items rows whose embedding column is NULL.
    Processes in batches of VOYAGE_BATCH_SIZE; commits after each batch.
    Idempotent: rows with an existing embedding are skipped.

    Returns a summary dict: {total, embedded, skipped, errors}.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ni.id AS ni_id,
                   si.raw_name, si.raw_category, si.raw_manufacturer
            FROM normalized_items ni
            JOIN source_items si ON si.id = ni.source_item_id
            WHERE ni.embedding IS NULL
            ORDER BY ni.id
            """
        )
        rows = cur.fetchall()

    log.info("embed: %d rows need embeddings", len(rows))

    total = embedded = errors = 0

    for batch_start in range(0, len(rows), VOYAGE_BATCH_SIZE):
        batch = rows[batch_start : batch_start + VOYAGE_BATCH_SIZE]
        texts = [
            build_embedding_input(
                r["raw_name"], r["raw_category"], r["raw_manufacturer"]
            )
            for r in batch
        ]

        if dry_run:
            log.info(
                "dry_run: would embed batch %d-%d (%d items)",
                batch_start, batch_start + len(batch) - 1, len(batch),
            )
            total += len(batch)
            embedded += len(batch)
            continue

        try:
            vectors = _embed_batch(texts)
        except Exception as exc:
            log.error(
                "Embedding batch %d-%d failed: %s",
                batch_start, batch_start + len(batch) - 1, exc,
            )
            errors += len(batch)
            total += len(batch)
            continue

        with conn.cursor() as cur:
            for row, vec in zip(batch, vectors):
                # pgvector accepts a Python list of floats directly when using
                # psycopg2 with the vector extension registered.
                cur.execute(
                    "UPDATE normalized_items SET embedding = %s WHERE id = %s",
                    (vec, row["ni_id"]),
                )

        conn.commit()
        embedded += len(batch)
        total += len(batch)
        log.info(
            "embed: batch %d/%d complete (%d embedded so far)",
            batch_start // VOYAGE_BATCH_SIZE + 1,
            (len(rows) + VOYAGE_BATCH_SIZE - 1) // VOYAGE_BATCH_SIZE,
            embedded,
        )

    summary = {
        "total": total,
        "embedded": embedded,
        "errors": errors,
        "dry_run": dry_run,
    }
    log.info("embed complete: %s", summary)
    return summary
