# Part 1 — Crawler: Design Notes

## What Was Built

A production-quality asynchronous Python crawler for three medical supply
sites: MediDepot (Shopify JSON API), Sky Dental Supply (HTML), and Anatomy
Warehouse (HTML). The crawler populates a pre-existing PostgreSQL schema
(`source_items`, `price_history`, `crawl_runs`, `crawl_queue`) and is
designed to survive hard kills, resume mid-crawl, and operate fully offline
against a local cache.

### File layout

```
crawler/
├── cache.py                      Filesystem cache (atomic writes)
├── db.py                         All PostgreSQL operations (psycopg2)
├── robots.py                     Per-host robots.txt enforcement (stdlib)
├── fetchers/
│   ├── base.py                   RateLimiter, fetch_with_retry, session builder
│   ├── medidepot.py              Shopify /products.json paginator
│   ├── skydental.py              HTML listing + product fetcher
│   └── anatomywarehouse.py       HTML listing + product fetcher
├── extractors/
│   ├── shopify.py                Shopify JSON → normalized item
│   ├── html_claude.py            Claude API schema discovery + cache
│   ├── html_parser.py            BeautifulSoup extractor using discovered schema
│   └── playwright_fallback.py    Per-page JS rendering fallback
└── main.py                       CLI entry point, orchestration, resumability
```

---

## Key Design Decisions

### 1. Rate limiting via asyncio.Lock held across the sleep

The `RateLimiter.acquire()` lock is held for the full duration of the
inter-request sleep, not just for the timestamp read-and-update. This ensures
that two coroutines targeting the same host cannot both observe "no wait
needed" simultaneously and fire together. The result is strictly serial
access within a host while different hosts proceed concurrently via
`asyncio.gather`.

### 2. Cache-first, network-second ordering in fetch_with_retry

Every fetch checks the filesystem cache before checking robots.txt or opening
a socket. This means `--no-network` mode is trivially correct (no network
code runs at all), and robots.txt is not consulted for URLs we already have
— both correct behaviours for a cached crawl.

### 3. Resumability without graceful shutdown handlers

Crash-safe resumability is achieved by making every state transition an
immediate, committed DB write:

- `crawl_queue.status` is set to `'fetching'` *before* the HTTP request
  begins and `'fetched'` *after* the response is cached to disk.
- If the process is killed between those two writes, the row stays
  `'fetching'`. On the next startup, `reset_fetching_to_pending()` moves
  those rows back to `'pending'`, and the cache serves the content without
  a network re-fetch.
- `crawl_runs.status` is left as `'running'` on a crash.
  `find_resumable_run()` treats `'running'` and `'interrupted'` identically
  — both trigger resume. This means no graceful-shutdown handler is needed
  for correctness (though one is registered for SIGTERM/SIGINT to set
  `status='interrupted'` which is informative, not structural).

### 4. Content hash over text fields, not raw HTML

`content_hash` is computed as `sha256(name\x00description\x00price\x00...)`.
Using normalized text fields (not raw HTML) means a site updating a CSS class
or adding a tracking pixel does not register as a product change and does not
insert a spurious `price_history` row.

The NUL (`\x00`) separator between fields is important: without it,
`("ab", "c")` and `("a", "bc")` would hash identically.

### 5. Claude schema discovery: once per site, not per page

Claude is called once per source per schema version. The result is cached at
`cache/<source>/__schema__.json` using the same atomic-write path as all
other cache entries. The synthetic URL key `"__schema__"` produces a stable
filename via sha256 without adding a new code path.

The threshold for automatic re-discovery is 30% of pages failing extraction,
with a minimum of 10 pages before the rate is meaningful. This prevents
false positives in the first few pages of a cold run.

Claude is not called for MediDepot at all — the Shopify JSON API provides
structured data directly.

### 6. Shopify API over HTML parsing for MediDepot

MediDepot runs on Shopify. Shopify's public `/products.json` endpoint returns
clean, structured JSON including title, SKU, price, vendor, and variant data.
Parsing this is more reliable, faster, and less fragile than parsing HTML —
and explicitly permitted by Shopify's platform design (the endpoint is
intentionally public).

The pagination sentinel is an empty `products` list (not a 404 or a
`Link` header). We stop paginating when we see it.

### 7. Playwright fallback is per-page, not per-source

The fallback to Playwright is triggered only when `needs_playwright_fallback()`
returns True — that is, when the plain-HTTP response is suspiciously short,
contains bot-detection markers (Cloudflare), or has neither the name nor price
selector matching. Every trigger is logged at WARNING level because frequent
triggers are a meaningful signal: they indicate the site depends on JavaScript
rendering and the plain-HTTP strategy is insufficient for that source.

### 8. Separate DB connections per source

`asyncio.gather` runs all three source coroutines concurrently in the same
event loop thread. psycopg2 is synchronous and not designed for connection
sharing across coroutines that `await` in the middle of transactions. Each
`run_source()` coroutine opens its own connection. Three connections is
minimal and correct.

### 9. Whitelisted counter columns in `increment_crawl_run_counter`

Column names for the counter increment function are checked against a frozen
set before being interpolated into SQL. This prevents SQL injection if the
function is called with an untrusted field name — a belt-and-suspenders
measure given the function is internal, but cheap to add.

### 10. `FOR UPDATE SKIP LOCKED` on the queue

`get_pending_queue_items` uses `FOR UPDATE SKIP LOCKED`. This costs nothing
in the current single-process setup and makes the system safe for a future
multi-worker configuration without any schema or logic changes.

---

## Assumptions

1. **Database schema is pre-applied exactly as described.** Column names in
   `db.py` are inferred from the table names and standard conventions. If the
   actual schema uses different column names (e.g., `item_url` instead of
   `source_url`), update the constants in `db.py`.

2. **`crawl_queue` has a unique constraint on `(crawl_run_id, url)`** to make
   the `ON CONFLICT DO NOTHING` in `enqueue_urls` work. If this constraint
   does not exist, duplicate rows may accumulate on resume.

3. **`source_items` has a unique constraint on `(source, source_url)`** for
   the upsert conflict target.

4. **The `xmax = 0` trick works as expected.** In standard PostgreSQL MVCC,
   `xmax = 0` on a RETURNING clause reliably identifies a fresh INSERT.
   This is a well-known pattern used by SQLAlchemy and other ORMs internally,
   but is a PostgreSQL implementation detail. If it behaves unexpectedly
   (e.g., with non-standard storage engines), replace with a pre-upsert
   SELECT.

5. **MediDepot's Shopify store is publicly accessible** without authentication.
   Shopify's `/products.json` endpoint is designed to be public on storefront
   plans. If MediDepot has restricted it, fetches will return 403 and those
   URLs will be marked failed in the queue.

6. **`ANTHROPIC_API_KEY` is set in the environment** for HTML sources. If not
   set, Claude discovery falls back to `DEFAULT_SCHEMA` (all selectors None)
   and the heuristic extractor in `html_parser.py` takes over. The crawl
   continues but extraction quality may be lower.

7. **Playwright is optional.** Install with `pip install ".[playwright]"` and
   `playwright install chromium`. If not installed, the fallback logs an error
   and returns None; the plain-HTTP HTML is used regardless.

8. **Volume target is 150–250 products per source** (not 250–500). This is a
   deliberate scope reduction for the assessment timeline. The
   `COLLECTION_PAGE_LIMIT` and `LISTING_PAGE_LIMIT` constants in each fetcher
   module control this cap and can be raised for production use.

9. **`is_active` column exists on `source_items`** for the end-of-run
   deactivation sweep in `mark_inactive_unseen`.

---

## Known Limitations / Future Work

- **Multi-variant Shopify products**: Only the first variant's price and SKU
  are captured. A product sold in 10 sizes at 10 prices has only one row.
  Full variant support is a Part 2 concern.

- **Sky Dental pagination**: Category pages use `?page=N` but the last-page
  sentinel is "no new URLs found", not an empty list. This means one extra
  listing page fetch per category at end-of-pagination.

- **Anatomy Warehouse JS rendering**: If Playwright triggers frequently, the
  entire source should be migrated to a Playwright-first strategy. The
  per-page fallback is intentionally a signal for this, not a permanent
  solution.

- **Schema rediscovery resets failure counter**: After rediscovery, the
  failure counter resets to 0. If the new schema is also wrong, failures
  accumulate again and rediscovery fires a second time. This could loop if
  Claude consistently produces poor selectors for a given site. The minimum
  of 10 pages before triggering limits the rate.
