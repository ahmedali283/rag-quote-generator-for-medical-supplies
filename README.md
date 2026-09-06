# Medical/Dental Supply Catalog Pipeline

Three-part pipeline: crawl three medical and dental supply websites, deduplicate the resulting 478-product catalog using hybrid embedding and LLM arbitration, then generate grounded sales quotations from customer emails using RAG over 19 historical quotes. Details on every design decision are in the notes files listed at the end of this document.

---

## Environment

- **Python**: 3.10 (other 3.x versions may work but have not been tested)
- **OS**: Windows 11 — all paths in this document use Windows conventions
- **Database**: PostgreSQL with the `pgvector` extension enabled

---

## Setup from zero

**1. Clone and create a virtual environment**

```
git clone <repo-url>
cd technical-assessment
python -m venv venv
venv\Scripts\activate
```

**2. Install dependencies**

```
pip install -r requirements.txt
```

**3. Create `.env`**

Copy `.env.example` to `.env` and fill in the three required variables:

```
ANTHROPIC_API_KEY=...
VOYAGE_API_KEY=...
DATABASE_URL=postgresql://user:password@host:5432/dbname?sslmode=require
```

- `ANTHROPIC_API_KEY` — used by the matching pipeline (LLM arbitration on ambiguous pairs), the quote parser (email parsing), and the quote generator (final quote text).
- `VOYAGE_API_KEY` — used to embed catalog items (`matching/embed.py`) and historical quotes (`quotes/embed_quotes.py`). Free tier is 3 RPM; the reproduction script adds a pause between calls to stay within this.
- `DATABASE_URL` — PostgreSQL connection string. The database must have the `pgvector` extension enabled (`CREATE EXTENSION IF NOT EXISTS vector;`). If connecting to Azure or another cloud Postgres, include `?sslmode=require` at the end.

**4. Apply the schema**

Two SQL files must be applied in order against your database, once:

```
psql %DATABASE_URL% -f schema.sql
psql %DATABASE_URL% -f matching/migration.sql
```

If `psql` is not installed, open both files in the pgAdmin Query Tool and run them against your database in the same order. `schema.sql` covers all tables for all three parts. `matching/migration.sql` is idempotent and safe to re-apply.

---

## Reproducing results from cache

```
python reproduce.py
```

What this does, in order:

1. Checks that all required tables exist in the database — fails with a clear message if the schema hasn't been applied yet.
2. Runs the crawler for all three sources with `--no-network`, serving pages from the local `cache/` directory. **No outbound HTTP to source websites.**
3. Runs the full matching pipeline (`normalize → embed → match`).
4. Embeds the 19 historical quotes.
5. Generates quotes for both committed test emails, writing output to `quotes/output/`.
6. Prints a summary: record counts per source, curated product count, confirmation that both quote files were written.

**What still requires network access:**

Steps 3 and 4 call the Voyage AI API to embed catalog items and historical quotes. Step 3 (matching) also calls the Anthropic API in two places: the normalize sub-step calls Haiku for the 26 items whose pack-size text couldn't be parsed by regex (these are re-processed on every run, not cached); the match sub-step calls Haiku to arbitrate undecided candidate pairs (already-decided pairs are skipped). The embed sub-step skips already-embedded rows. On a second run the embed and match sub-steps complete in seconds; normalize still makes ~26 Haiku calls. The crawler (step 2) makes no external calls at all.

---

## Running each part individually

### Part 1 — Crawler

```
python -m crawler.main --source medidepot --no-network
python -m crawler.main --source skydental --no-network
python -m crawler.main --source pocketnurse --no-network
```

`--no-network` serves all pages from `cache/` and raises an error if a required URL is not cached. Omit the flag to crawl live (respects robots.txt and rate limiting). `--source all` runs all three sources in sequence.

Logs go to `logs/crawl_<source>_<timestamp>.log`. The crawler is resumable: killing and re-running continues the same `crawl_run` record.

### Part 2 — Matching pipeline

```
python -m matching.main --step all
```

Individual steps:

```
python -m matching.main --step normalize   # pack-size parsing, name normalization
python -m matching.main --step embed       # Voyage AI embeddings (skips already-embedded rows)
python -m matching.main --step match       # blocking, scoring, LLM arbitration
```

To export the eval CSV after matching:

```
python -m matching.build_eval_set
```

To score a labeled CSV:

```
python -m matching.score_eval --csv matching/eval/candidate_pairs_v3.csv
```

Logs go to `logs/matching_<timestamp>.log`. Per-pair LLM decisions are recorded in `logs/match_decisions_<timestamp>.jsonl`.

### Part 3 — Quote generation

First embed the historical quotes (idempotent, already-embedded quotes are skipped):

```
python quotes/embed_quotes.py
```

Generate a quote from a customer email:

```
python quotes/generate_quote.py --email quotes/tests/test_email_clean.txt --out quotes/output/quote_clean.txt
python quotes/generate_quote.py --email quotes/tests/test_email_messy.txt --out quotes/output/quote_messy.txt
```

**Extension scripts** (beyond the core assessment scope — see `quotes/README_CUSTOMER_EMAIL.md`):

Generate a customer-facing reply email (resolved items stated plainly, unresolved items turned into clarifying questions):

```
python quotes/generate_customer_email.py --email quotes/tests/test_email_live.txt --out quotes/output/email_live.txt
```

Process the customer's clarifying reply and produce an updated quote:

```
python quotes/process_customer_reply.py \
    --original-email quotes/tests/test_email_live.txt \
    --reply-email    quotes/tests/test_email_live_reply.txt \
    --out            quotes/output/quote_live_updated.txt \
    --email-out      quotes/output/email_live_updated.txt
```

---

## Web UI (demo)

A browser interface for the quote generation pipeline. Not part of the graded assessment — built as an interview-facing demo.

**Backend** (FastAPI, port 8001) — from the project root with venv active:

```
uvicorn webapp.backend.main:app --reload --port 8001
```

**Frontend** (Next.js, port 3000):

```
cd webapp\frontend
npm install
npm run dev
```

Open `http://localhost:3000`. Paste a customer email subject and body, click **Generate Quote**. The pipeline runs server-side and returns two panels:

- **Left** — the outbound reply email to the customer (confirmed prices + clarifying questions for anything unresolved)
- **Right** — the formal quotation document, downloadable as PDF via the **Download Quotation PDF** button

See `webapp/README.md` for full setup details.

---

## Where things are

| File | What's in it |
|------|-------------|
| `crawler/PART1_NOTES.md` | Crawler design: cache architecture, rate limiting, schema discovery, resumability, why each source was chosen |
| `matching/PART2_NOTES.md` | Matching pipeline design: blocking strategy, scoring formula, LLM arbitration, eval results, known precision problem and its cause |
| `quotes/PART3_NOTES.md` | RAG pipeline design: embedding strategy, retrieval paths, resolution thresholds, test case walkthroughs, known limitations |
| `webapp/README.md` | Setup and run instructions for the demo web UI |
| `SCRAPE_LEDGER.md` | Per-source crawl decisions: what was tried, what was blocked, what was scoped out and why |
| `WRITEUP.md` | Short written answers to the assessment questions: source selection, agent vs. hand-code tradeoffs, matching strategy and eval numbers, hallucination prevention, production monitoring |
| `dev_scripts/` | One-off diagnostic scripts used during development (schema checks, resolution debugging, decision restoration) — not part of the main pipeline |

---

## Known limitations

- **Matching precision is low (6.5%)**: 2 correct matches out of 31 predicted, against 166 hand-labeled pairs. The cause is the manufacturer bonus pulling same-brand-but-different-tier products together. A tier-keyword conflict detector would fix most of it; it was scoped but not built. See `matching/PART2_NOTES.md`.
- **Price anomaly detection has a gap**: a catalog price with no historical quote to compare against passes through undetected. The `$17.00` SimJunior case in test 2 was caught by an absolute floor check, not by historical comparison. See `quotes/PART3_NOTES.md`.
- **Category-level false positives in resolution**: high embedding similarity can score a functionally wrong product very high when two products share domain vocabulary. The confirmed case: a TCCC tourniquet-trainer resolving to a moulage/makeup kit (both "casualty simulation" products). The confidence threshold and ambiguity gap both passed because there was one confident wrong answer with no close competitor. Category-aware blocking at the resolution stage would fix this. See `quotes/PART3_NOTES.md`.

---

## API costs incurred during development

- **Voyage AI embeddings**: under $1 total across all embedding runs for both catalog items and historical quotes.
- **Claude API, Part 2 (LLM arbitration)**: the majority of cost. Approximately 6,983 candidate pairs sent to Claude Haiku for arbitration at roughly $7–8 total. Haiku was chosen specifically because of this volume. Already-decided pairs are skipped on re-runs, so cost is not incurred twice.
- **Claude API, Part 3**: small. Email parsing (Haiku) and quote generation (Sonnet) together cost a few cents per email.
