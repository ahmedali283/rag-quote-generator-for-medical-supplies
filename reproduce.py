"""
Full pipeline reproduction from cache.

Steps:
  1. Verify schema (table-existence check against DATABASE_URL)
  2. Crawl all three sources with --no-network (no outbound HTTP to source sites)
  3. Matching pipeline: normalize → embed → match
     (embed and match call Voyage AI and Anthropic APIs — not cached)
  4. Embed historical quotes (calls Voyage AI — not cached)
  5. Generate quotes for both committed test emails (calls Anthropic API)
  6. Print summary

Network access:
  - Source websites: NONE (--no-network enforces cache-only for the crawler)
  - Voyage AI (embeddings): REQUIRED for steps 3 and 4 if any items/quotes
    lack embeddings. Already-embedded rows are skipped.
  - Anthropic API: REQUIRED for step 3 (LLM arbitration on undecided pairs)
    and step 5 (quote generation). Already-decided pairs are skipped.

Usage:
  python reproduce.py
"""

import io
import os
import subprocess
import sys
import textwrap

# Force UTF-8 output on Windows consoles that default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd, *, check=True):
    """Run a command list, streaming output. Raises on non-zero exit if check=True."""
    print(f"\n>>> {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, check=False)
    if check and result.returncode != 0:
        print(f"\nERROR: command exited with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)
    return result

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ── Step 1: Schema check ──────────────────────────────────────────────────────

section("Step 1 — Schema check")

# Load .env before importing anything that reads DATABASE_URL
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv may not be installed yet; DATABASE_URL should be in env

required_tables = [
    "source_items", "crawl_runs", "crawl_queue", "price_history",
    "normalized_items", "candidate_pairs", "curated_products",
    "curated_product_members", "historical_quotes", "historical_quote_line_items",
]

try:
    import urllib.parse
    import psycopg2
    import psycopg2.extras

    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        print("ERROR: DATABASE_URL is not set. Copy .env.example to .env and fill it in.",
              file=sys.stderr)
        sys.exit(1)

    parsed = urllib.parse.urlparse(raw)
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=urllib.parse.unquote(parsed.password or ""),
        **({k: v for k, v in urllib.parse.parse_qsl(parsed.query)
            if k in {"sslmode", "sslcert", "sslkey", "sslrootcert",
                     "connect_timeout", "application_name"}}),
    )
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()

    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    existing = {r["table_name"] for r in cur.fetchall()}
    missing = [t for t in required_tables if t not in existing]

    if missing:
        print(textwrap.dedent(f"""
            ERROR: The following required tables are missing from the database:
              {', '.join(missing)}

            Apply the schema files in order before running this script:
              psql $DATABASE_URL -f schema.sql
              psql $DATABASE_URL -f matching/migration.sql

            If psql is not available, run both files through the pgAdmin Query Tool
            against your database. See README.md for details.
        """), file=sys.stderr)
        sys.exit(1)

    print(f"Schema OK — all {len(required_tables)} required tables present.")
    conn.close()

except psycopg2.OperationalError as e:
    print(f"ERROR: Could not connect to database: {e}", file=sys.stderr)
    sys.exit(1)

# ── Step 2: Crawl (cache-only) ────────────────────────────────────────────────

section("Step 2 — Crawl all sources (--no-network, cache only)")

for source in ("medidepot", "skydental", "pocketnurse"):
    run([sys.executable, "-m", "crawler.main", "--source", source, "--no-network"])

# ── Step 3: Matching pipeline ─────────────────────────────────────────────────

section("Step 3 — Matching pipeline (normalize → embed → match)")
print("Note: embed calls Voyage AI; match calls Anthropic API for undecided pairs.")
print("Already-embedded rows and already-decided pairs are skipped.")

run([sys.executable, "-m", "matching.main", "--step", "all"])

# ── Step 4: Embed historical quotes ──────────────────────────────────────────

section("Step 4 — Embed historical quotes (Voyage AI)")
print("Note: already-embedded quotes are skipped (idempotent).")

run([sys.executable, "quotes/embed_quotes.py"])

# ── Step 5: Generate quotes ───────────────────────────────────────────────────

section("Step 5 — Generate quotes (Anthropic API)")

os.makedirs("quotes/output", exist_ok=True)

run([sys.executable, "quotes/generate_quote.py",
     "--email", "quotes/tests/test_email_clean.txt",
     "--out",   "quotes/output/quote_clean.txt"])

# Brief pause between quote runs — Voyage AI free tier enforces 3 RPM.
# Each generate_quote.py call makes 1-2 embedding requests; 25s gap keeps
# the second run from hitting the rate limit immediately after the first.
import time
print("Waiting 25s between quote runs (Voyage AI rate limit precaution)...")
time.sleep(25)

run([sys.executable, "quotes/generate_quote.py",
     "--email", "quotes/tests/test_email_messy.txt",
     "--out",   "quotes/output/quote_messy.txt"])

# ── Step 6: Summary ───────────────────────────────────────────────────────────

section("Step 6 — Summary")

try:
    raw = os.environ.get("DATABASE_URL", "")
    parsed = urllib.parse.urlparse(raw)
    conn = psycopg2.connect(
        host=parsed.hostname, port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"), user=parsed.username,
        password=urllib.parse.unquote(parsed.password or ""),
        **({k: v for k, v in urllib.parse.parse_qsl(parsed.query)
            if k in {"sslmode", "sslcert", "sslkey", "sslrootcert",
                     "connect_timeout", "application_name"}}),
    )
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()

    cur.execute("""
        SELECT source, COUNT(*) AS n
        FROM source_items WHERE is_active
        GROUP BY source ORDER BY source
    """)
    rows = cur.fetchall()
    total = sum(r["n"] for r in rows)
    print(f"\nSource items (active):")
    for r in rows:
        print(f"  {r['source']:15s} {r['n']:4d}")
    print(f"  {'TOTAL':15s} {total:4d}")

    cur.execute("SELECT COUNT(*) AS n FROM curated_products")
    cp = cur.fetchone()["n"]
    print(f"\nCurated products (deduplicated clusters): {cp}")

    cur.execute("SELECT COUNT(*) AS n FROM normalized_items WHERE embedding IS NOT NULL")
    emb = cur.fetchone()["n"]
    print(f"Normalized items with embeddings: {emb}")

    conn.close()

except Exception as e:
    print(f"Warning: could not fetch summary stats: {e}")

quote_clean = "quotes/output/quote_clean.txt"
quote_messy = "quotes/output/quote_messy.txt"
clean_ok = os.path.exists(quote_clean) and os.path.getsize(quote_clean) > 0
messy_ok = os.path.exists(quote_messy) and os.path.getsize(quote_messy) > 0

print(f"\nQuote output files:")
print(f"  {quote_clean:45s} {'OK' if clean_ok else 'MISSING'}")
print(f"  {quote_messy:45s} {'OK' if messy_ok else 'MISSING'}")

if clean_ok and messy_ok:
    print("\nReproduction complete.")
else:
    print("\nWARNING: one or more quote files not generated.", file=sys.stderr)
    sys.exit(1)
