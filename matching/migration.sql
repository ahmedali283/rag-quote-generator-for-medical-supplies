-- Part 2 schema migration
-- Apply once against the live database before running any matching step.
-- Idempotent: all statements use IF NOT EXISTS / DO NOTHING.
--
-- Requires: pgvector extension (CREATE EXTENSION IF NOT EXISTS vector)
-- Run as: psql $DATABASE_URL -f matching/migration.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- normalized_items
-- One row per source_items row. Populated by matching/normalize.py (pack
-- fields) and matching/embed.py (embedding). Both steps are idempotent and
-- can run independently.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS normalized_items (
    id               BIGSERIAL PRIMARY KEY,
    source_item_id   BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,

    -- Pack normalization fields
    -- pack_size: count of individual units inside the purchasable item
    --   "box of 100"      → 100   (100 individual gloves in one box)
    --   "case of 10 boxes"→ 10    (10 boxes per case; boxes are the sub-unit)
    -- pack_unit: what each sub-unit is
    --   "box of 100"      → 'each'  (sub-unit is individual gloves)
    --   "case of 10 boxes"→ 'box'   (sub-unit is a box)
    -- units_per_pack: count of intermediate containers
    --   "box of 100"      → 1    (no intermediate layer)
    --   "case of 10 boxes"→ 10   (10 boxes)
    -- unit_of_measure: outermost container you actually purchase
    --   "box of 100"      → 'box'
    --   "case of 10 boxes"→ 'case'
    pack_size        INTEGER,
    pack_unit        TEXT,
    units_per_pack   INTEGER NOT NULL DEFAULT 1,
    unit_of_measure  TEXT,

    -- 'regex' when a regex pattern matched confidently;
    -- 'llm'   when the Claude API was used as fallback.
    -- Both values are intentional design choices — log them for auditability.
    parse_method     TEXT NOT NULL DEFAULT 'regex'
                     CHECK (parse_method IN ('regex', 'llm')),

    -- Pre-computed category bucket used for blocking (avoids recomputing per pair).
    category_bucket  TEXT,

    -- Voyage AI embedding (voyage-2, 1024 dims).
    -- NULL until embed.py runs for this row.
    embedding        vector(1024),

    normalized_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_normalized_source_item UNIQUE (source_item_id)
);

-- ---------------------------------------------------------------------------
-- ALTER TABLE normalized_items — add every Part 2 column that Part 1 omitted.
--
-- Part 1's live schema only has:
--   id, source_item_id, name, description, manufacturer, category,
--   pack_size, pack_unit, units_per_pack, unit_of_measure, updated_at
--
-- CREATE TABLE IF NOT EXISTS above is a complete no-op when the table already
-- exists, so every new column must be listed here explicitly.
-- ---------------------------------------------------------------------------

-- Embedding vector (NULL until embed.py runs for this row).
ALTER TABLE normalized_items
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- Parse method audit trail.
-- ADD COLUMN cannot attach a CHECK constraint on an idempotent re-run
-- (ALTER TABLE ... ADD CONSTRAINT fails if the constraint already exists,
--  and there is no ADD CONSTRAINT IF NOT EXISTS in Postgres < 17).
-- We add the column with its DEFAULT, then add the CHECK in a DO block
-- that skips gracefully if the constraint is already present.
ALTER TABLE normalized_items
    ADD COLUMN IF NOT EXISTS parse_method TEXT NOT NULL DEFAULT 'regex';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'normalized_items'::regclass
          AND conname   = 'normalized_items_parse_method_check'
    ) THEN
        ALTER TABLE normalized_items
            ADD CONSTRAINT normalized_items_parse_method_check
            CHECK (parse_method IN ('regex', 'llm'));
    END IF;
END;
$$;

-- Category bucket (pre-computed; avoids recomputing per blocking pair).
ALTER TABLE normalized_items
    ADD COLUMN IF NOT EXISTS category_bucket TEXT;

-- Normalization timestamp (Part 1 had updated_at; Part 2 adds normalized_at
-- as a separate column so the two concerns stay distinct).
ALTER TABLE normalized_items
    ADD COLUMN IF NOT EXISTS normalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Unique constraint on source_item_id (one normalized row per source item).
-- Uses the same DO-block pattern — ADD CONSTRAINT IF NOT EXISTS requires PG 17+.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'normalized_items'::regclass
          AND conname   = 'uq_normalized_source_item'
    ) THEN
        ALTER TABLE normalized_items
            ADD CONSTRAINT uq_normalized_source_item UNIQUE (source_item_id);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_normalized_source_item
    ON normalized_items (source_item_id);

CREATE INDEX IF NOT EXISTS idx_normalized_bucket
    ON normalized_items (category_bucket);

-- ---------------------------------------------------------------------------
-- candidate_pairs
-- Blocking output: cross-source pairs in the same category bucket whose
-- cosine similarity exceeds the floor. One row per unordered pair
-- (enforced by item_a_id < item_b_id constraint).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidate_pairs (
    id                   BIGSERIAL PRIMARY KEY,
    item_a_id            BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    item_b_id            BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,

    -- Raw cosine similarity from pgvector (before adjustments).
    cosine_sim           NUMERIC(6,4) NOT NULL,
    -- Final score after boost/penalty adjustments, clamped to [0,1].
    computed_score       NUMERIC(6,4) NOT NULL,

    -- Flags that explain why the score differs from cosine_sim.
    manufacturer_bonus   BOOLEAN NOT NULL DEFAULT FALSE,
    uom_conflict         BOOLEAN NOT NULL DEFAULT FALSE,
    attribute_conflict   BOOLEAN NOT NULL DEFAULT FALSE,

    category_bucket      TEXT NOT NULL,

    -- Decision lifecycle:
    --   auto_match   → confidence ≥ 0.85, no human review needed
    --   auto_reject  → confidence ≤ 0.50, clearly not a match
    --   pending_llm  → 0.50 < confidence < 0.85, sent to Claude
    --   llm_match    → Claude decided: match
    --   llm_no_match → Claude decided: no match
    decision             TEXT CHECK (decision IN (
                             'auto_match', 'auto_reject',
                             'pending_llm', 'llm_match', 'llm_no_match'
                         )),
    llm_reason           TEXT,   -- Claude's one-sentence reasoning (pending_llm only)

    blocked_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at           TIMESTAMPTZ,

    -- Canonical ordering: lower id always goes in item_a_id.
    -- This makes (A,B) and (B,A) the same pair and prevents duplicates.
    CONSTRAINT chk_pair_order CHECK (item_a_id < item_b_id),
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

-- ---------------------------------------------------------------------------
-- curated_products
-- Live schema (Part 1): id, canonical_name, category, created_at
-- CREATE TABLE is a no-op if table exists; ALTER adds any missing columns.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS curated_products (
    id                 BIGSERIAL PRIMARY KEY,
    canonical_name     TEXT NOT NULL,
    category           TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- curated_product_members
-- Live schema (Part 1): id, curated_product_id, source_item_id, confidence_score
-- CREATE TABLE is a no-op if table exists; ALTER adds any missing columns.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS curated_product_members (
    id                  BIGSERIAL PRIMARY KEY,
    curated_product_id  INTEGER NOT NULL
                        REFERENCES curated_products(id) ON DELETE CASCADE,
    source_item_id      INTEGER NOT NULL
                        REFERENCES source_items(id) ON DELETE CASCADE,
    confidence_score    NUMERIC(4,3) NOT NULL,

    CONSTRAINT uq_member UNIQUE (curated_product_id, source_item_id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'curated_product_members'::regclass
          AND conname   = 'uq_member'
    ) THEN
        ALTER TABLE curated_product_members
            ADD CONSTRAINT uq_member UNIQUE (curated_product_id, source_item_id);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_curated_members_product
    ON curated_product_members (curated_product_id);
CREATE INDEX IF NOT EXISTS idx_curated_members_item
    ON curated_product_members (source_item_id);
