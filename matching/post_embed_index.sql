-- Run this AFTER matching/embed.py has populated at least one embedding row.
--
-- IVFFlat requires the table to contain at least `lists` rows before it can
-- be trained. Running this against an empty (or nearly empty) embedding column
-- will fail with: "table must have at least lists (10) rows".
--
-- Usage:
--     psql $DATABASE_URL -f matching/post_embed_index.sql

CREATE INDEX IF NOT EXISTS idx_normalized_embedding
    ON normalized_items USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);
