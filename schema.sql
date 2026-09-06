-- schema.sql — complete database schema for the medical/dental supply catalog pipeline
--
-- Reconstructed from the live Azure PostgreSQL database (2026-09-06).
-- Covers all three pipeline parts in dependency order.
--
-- Prerequisites:
--   CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector, needed for vector(1024) columns
--
-- Apply once against a fresh database:
--   psql $DATABASE_URL -f schema.sql
--
-- The matching pipeline has its own migration file (matching/migration.sql) that
-- adds Part 2 tables and alters normalized_items. This file supersedes it: applying
-- this file alone produces an identical schema to the live database.

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- Part 1 — Crawler
-- ============================================================================

-- crawl_runs
-- One row per crawler invocation. Counters are incremented in-flight.
CREATE TABLE IF NOT EXISTS crawl_runs (
    id                   SERIAL PRIMARY KEY,
    source               TEXT NOT NULL,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at          TIMESTAMPTZ,
    pages_fetched        INTEGER DEFAULT 0,
    records_upserted     INTEGER DEFAULT 0,
    records_new          INTEGER DEFAULT 0,
    records_changed      INTEGER DEFAULT 0,
    records_unchanged    INTEGER,
    records_disappeared  INTEGER DEFAULT 0,
    retries              INTEGER DEFAULT 0,
    extraction_failures  INTEGER DEFAULT 0,
    status               TEXT DEFAULT 'running'
);

-- crawl_queue
-- URL work queue for resumable HTML crawls.
CREATE TABLE IF NOT EXISTS crawl_queue (
    id                   SERIAL PRIMARY KEY,
    crawl_run_id         INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    url                  TEXT NOT NULL,
    url_type             TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    attempts             INTEGER DEFAULT 0,
    last_attempted_at    TIMESTAMPTZ,
    error_message        TEXT,
    UNIQUE (crawl_run_id, url)
);

CREATE INDEX IF NOT EXISTS idx_crawl_queue_run_status
    ON crawl_queue (crawl_run_id, status);

-- source_items
-- Raw scraped products. One row per (source, source_url). Updated in-place on
-- each incremental run when content_hash changes.
CREATE TABLE IF NOT EXISTS source_items (
    id                   SERIAL PRIMARY KEY,
    source               TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    source_sku           TEXT,
    raw_name             TEXT,
    raw_description      TEXT,
    raw_price            TEXT,
    raw_pack_info        TEXT,
    raw_category         TEXT,
    raw_manufacturer     TEXT,
    content_hash         TEXT NOT NULL,
    captured_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (source, source_url)
);

CREATE INDEX IF NOT EXISTS idx_source_items_source
    ON source_items (source);
CREATE INDEX IF NOT EXISTS idx_source_items_hash
    ON source_items (content_hash);

-- price_history
-- One row per observed price change. Written when raw_price differs from the
-- most recent price_history entry for that source_item.
CREATE TABLE IF NOT EXISTS price_history (
    id                   SERIAL PRIMARY KEY,
    source_item_id       INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    price                NUMERIC,
    currency             TEXT DEFAULT 'USD',
    observed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_item
    ON price_history (source_item_id);

-- ============================================================================
-- Part 2 — Matching / Deduplication
-- ============================================================================

-- normalized_items
-- One row per source_item. Populated by matching/normalize.py (pack fields,
-- name normalization) and matching/embed.py (embedding). Both steps are
-- idempotent and can run independently.
CREATE TABLE IF NOT EXISTS normalized_items (
    id               SERIAL PRIMARY KEY,
    source_item_id   INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,

    -- Normalized product fields (written by normalize.py)
    name             TEXT NOT NULL,
    description      TEXT,
    manufacturer     TEXT,
    category         TEXT,

    -- Pack normalization
    -- pack_size:     count of individual units in the purchasable item
    -- pack_unit:     what each sub-unit is ('each', 'box', etc.)
    -- units_per_pack: count of intermediate containers
    -- unit_of_measure: outermost container purchased ('box', 'case', etc.)
    pack_size        NUMERIC,
    pack_unit        TEXT,
    units_per_pack   NUMERIC,
    unit_of_measure  TEXT,

    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Added by Part 2
    embedding        vector(1024),                      -- Voyage AI voyage-2, NULL until embed.py runs
    parse_method     TEXT NOT NULL DEFAULT 'regex'
                     CHECK (parse_method IN ('regex', 'llm')),
    category_bucket  TEXT,                              -- pre-computed for blocking
    normalized_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (source_item_id)
);

CREATE INDEX IF NOT EXISTS idx_normalized_source_item
    ON normalized_items (source_item_id);
CREATE INDEX IF NOT EXISTS idx_normalized_bucket
    ON normalized_items (category_bucket);

-- candidate_pairs
-- Blocking output: cross-source pairs in the same category_bucket whose
-- cosine similarity exceeds the floor. item_a_id < item_b_id is enforced so
-- (A,B) and (B,A) are treated as the same pair.
CREATE TABLE IF NOT EXISTS candidate_pairs (
    id                   BIGSERIAL PRIMARY KEY,
    item_a_id            BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    item_b_id            BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,

    cosine_sim           NUMERIC(6,4) NOT NULL,
    computed_score       NUMERIC(6,4) NOT NULL,

    manufacturer_bonus   BOOLEAN NOT NULL DEFAULT FALSE,
    uom_conflict         BOOLEAN NOT NULL DEFAULT FALSE,
    attribute_conflict   BOOLEAN NOT NULL DEFAULT FALSE,

    category_bucket      TEXT NOT NULL,

    -- Decision lifecycle:
    --   auto_match   — cosine ≥ 0.85, no LLM needed
    --   auto_reject  — cosine ≤ 0.50, clearly not a match
    --   pending_llm  — 0.50 < cosine < 0.85, sent to Claude Haiku
    --   llm_match    — Claude decided: same product
    --   llm_no_match — Claude decided: different products
    decision             TEXT CHECK (decision IN (
                             'auto_match', 'auto_reject',
                             'pending_llm', 'llm_match', 'llm_no_match'
                         )),
    llm_reason           TEXT,

    blocked_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at           TIMESTAMPTZ,

    CONSTRAINT chk_pair_order   CHECK (item_a_id < item_b_id),
    CONSTRAINT uq_candidate_pair UNIQUE (item_a_id, item_b_id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_decision
    ON candidate_pairs (decision);
CREATE INDEX IF NOT EXISTS idx_candidate_score
    ON candidate_pairs (computed_score DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_item_a
    ON candidate_pairs (item_a_id);
CREATE INDEX IF NOT EXISTS idx_candidate_item_b
    ON candidate_pairs (item_b_id);

-- curated_products
-- One row per deduplicated canonical product (a cluster of source_items that
-- the matching pipeline determined are the same real-world product).
CREATE TABLE IF NOT EXISTS curated_products (
    id               SERIAL PRIMARY KEY,
    canonical_name   TEXT NOT NULL,
    category         TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- curated_product_members
-- Many-to-one: which source_items belong to each curated_product.
CREATE TABLE IF NOT EXISTS curated_product_members (
    id                   SERIAL PRIMARY KEY,
    curated_product_id   INTEGER NOT NULL REFERENCES curated_products(id) ON DELETE CASCADE,
    source_item_id       INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    confidence_score     NUMERIC NOT NULL,
    UNIQUE (curated_product_id, source_item_id)
);

CREATE INDEX IF NOT EXISTS idx_curated_members_product
    ON curated_product_members (curated_product_id);
CREATE INDEX IF NOT EXISTS idx_curated_members_item
    ON curated_product_members (source_item_id);

-- ============================================================================
-- Part 3 — Quote Generation (RAG pipeline)
-- ============================================================================

-- historical_quotes
-- 19 synthetic historical quotes used for RAG retrieval. Embedded as
-- natural-language summaries ("<segment> customer purchasing: <items>") and
-- stored as vector(1024) for pgvector cosine similarity search.
CREATE TABLE IF NOT EXISTS historical_quotes (
    id               SERIAL PRIMARY KEY,
    source_file      TEXT NOT NULL,           -- quote_id from historical_quotes.json (idempotency key)
    customer_name    TEXT,
    customer_segment TEXT,
    quote_date       DATE,
    full_text        TEXT NOT NULL,
    subtotal         NUMERIC,
    discount         NUMERIC,
    freight          NUMERIC,
    total            NUMERIC,
    notes            TEXT,
    embedding        vector(1024),            -- NULL until embed_quotes.py runs
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_historical_quotes_embedding
    ON historical_quotes USING ivfflat (embedding vector_cosine_ops);

-- historical_quote_line_items
-- Individual line items parsed from each historical quote.
CREATE TABLE IF NOT EXISTS historical_quote_line_items (
    id                   SERIAL PRIMARY KEY,
    historical_quote_id  INTEGER NOT NULL REFERENCES historical_quotes(id) ON DELETE CASCADE,
    curated_product_id   INTEGER REFERENCES curated_products(id),
    line_number          INTEGER,
    description          TEXT,
    quantity             NUMERIC,
    unit_of_measure      TEXT,
    unit_price           NUMERIC,
    extended_price       NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_historical_quote_line_items_quote
    ON historical_quote_line_items (historical_quote_id);
