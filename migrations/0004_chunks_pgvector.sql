-- Clerk-san migration 0004: pgvector chunks pinned by the embedding selection artifact.
-- Source: eval/results/embedding-decision.json
-- Selected tag: nomic-embed-text:v1.5
-- Resolved local digest: 0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f
-- Dimension: 768
-- A model change requires a new migration and a full re-embedding run.  Do not alter
-- this DDL or change the settings pin in place once user chunks exist.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL CHECK (seq >= 0),
    heading_path TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL CHECK (btrim(text) <> ''),
    embedding vector(768) NOT NULL,
    embed_model TEXT NOT NULL CHECK (embed_model = 'nomic-embed-text:v1.5'),
    embed_model_digest TEXT NOT NULL CHECK (
        embed_model_digest = '0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f'
    ),
    token_count INTEGER NOT NULL CHECK (token_count > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, seq)
);

CREATE INDEX IF NOT EXISTS chunks_document_seq_idx ON chunks (document_id, seq);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
