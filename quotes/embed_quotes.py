"""
Step 1: Embed historical quotes and insert into historical_quotes + historical_quote_line_items.

For each quote in historical_quotes.json, builds a natural-language embedding input from:
  "{customer_segment} customer purchasing: {item1 description}, {item2 description}, ..."
then calls Voyage AI voyage-2 to get a 1024-dim embedding, and upserts the quote into DB.

Idempotent: skips quotes already present (matched by quote_id stored in source_file).
"""

import json
import logging
import os
import sys
import urllib.parse
from datetime import date
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import voyageai
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

QUOTES_FILE = os.path.join(os.path.dirname(__file__), "historical_quotes.json")
VOYAGE_MODEL = "voyage-2"


def _build_embedding_text(quote: dict) -> str:
    """Natural-language summary of what a quote represents — the embedding input."""
    segment = quote["customer"]["segment"].replace("_", " ")
    items = ", ".join(li["product"] for li in quote["line_items"])
    return f"{segment} customer purchasing: {items}"


def _build_full_text(quote: dict) -> str:
    """Human-readable full text of the quote for storage and LLM context."""
    lines = [
        f"Quote {quote['quote_id']} dated {quote['date']}",
        f"Customer: {quote['customer']['name']} ({quote['customer']['segment'].replace('_', ' ')})",
        f"Account: {quote['customer']['account_id']} | Type: {quote['customer']['customer_type']} | Prior orders (12mo): {quote['customer']['prior_orders_12mo']}",
        f"Rep: {quote['rep']}",
        f"Notes: {quote['notes']}",
        "",
        "Line items:",
    ]
    for li in quote["line_items"]:
        pref = " [preferred pricing]" if li.get("preferred_pricing") else ""
        lines.append(
            f"  {li['line']}. {li['product']} — qty {li['qty']} @ ${li['unit_price']:.2f} = ${li['extended']:.2f}{pref}"
        )
    lines += [
        "",
        f"Subtotal: ${quote['subtotal']:.2f}",
        f"Volume discount ({quote['volume_discount_pct']}%): -${quote['volume_discount_amt']:.2f}",
        f"Freight: ${quote['freight']:.2f} ({quote['freight_note']})",
        f"TOTAL: ${quote['total']:.2f}",
    ]
    return "\n".join(lines)


def run(dry_run: bool = False) -> dict:
    with open(QUOTES_FILE, encoding="utf-8") as f:
        quotes = json.load(f)

    p = urlparse(os.environ["DATABASE_URL"])
    conn = psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        dbname=p.path.lstrip("/"), user=p.username,
        password=urllib.parse.unquote(p.password or ""),
    )
    conn.autocommit = False
    psycopg2.extras.register_default_jsonb(conn)

    vc = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])

    # Find already-inserted quote IDs
    with conn.cursor() as cur:
        cur.execute("SELECT source_file FROM historical_quotes")
        existing = {r[0] for r in cur.fetchall()}

    to_insert = [q for q in quotes if q["quote_id"] not in existing]
    log.info("%d quotes total, %d already in DB, %d to insert", len(quotes), len(existing), len(to_insert))

    if not to_insert:
        conn.close()
        return {"total": len(quotes), "inserted": 0, "skipped": len(existing)}

    # Build embedding texts
    texts = [_build_embedding_text(q) for q in to_insert]
    log.info("Embedding %d quotes via Voyage AI...", len(texts))

    if dry_run:
        log.info("[dry_run] skipping API call and DB writes")
        conn.close()
        return {"total": len(quotes), "inserted": 0, "skipped": len(existing), "dry_run": True}

    result = vc.embed(texts, model=VOYAGE_MODEL, input_type="document")
    embeddings = result.embeddings

    inserted = 0
    with conn.cursor() as cur:
        for quote, emb_text, embedding in zip(to_insert, texts, embeddings):
            full_text = _build_full_text(quote)
            emb_str = "[" + ",".join(str(x) for x in embedding) + "]"

            cur.execute(
                """
                INSERT INTO historical_quotes
                    (source_file, customer_name, customer_segment, quote_date,
                     full_text, subtotal, discount, freight, total, notes, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                RETURNING id
                """,
                (
                    quote["quote_id"],
                    quote["customer"]["name"],
                    quote["customer"]["segment"],
                    quote["date"],
                    full_text,
                    quote["subtotal"],
                    quote["volume_discount_amt"],
                    quote["freight"],
                    quote["total"],
                    quote["notes"],
                    emb_str,
                ),
            )
            hq_id = cur.fetchone()[0]

            for li in quote["line_items"]:
                cur.execute(
                    """
                    INSERT INTO historical_quote_line_items
                        (historical_quote_id, line_number, description,
                         quantity, unit_price, extended_price)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (hq_id, li["line"], li["product"], li["qty"],
                     li["unit_price"], li["extended"]),
                )

            log.info("  inserted %s (hq_id=%d)", quote["quote_id"], hq_id)
            inserted += 1

    conn.commit()
    conn.close()
    log.info("Done: %d inserted", inserted)
    return {"total": len(quotes), "inserted": inserted, "skipped": len(existing)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = run(dry_run=args.dry_run)
    print(result)
