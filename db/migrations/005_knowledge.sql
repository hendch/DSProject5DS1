-- Knowledge base for the RCA agent (replaces Chroma): documents + embeddings in pgvector.
-- Requires the `vector` extension: pgvector/pgvector Docker image, Neon, or pgserver.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS knowledge;

CREATE TABLE IF NOT EXISTS knowledge.documents (
    collection TEXT NOT NULL DEFAULT 'devops_kb',
    id         TEXT NOT NULL,
    source     TEXT NOT NULL,                -- label shown and cited by the RCA agent
    content    TEXT NOT NULL,
    metadata   JSONB NOT NULL DEFAULT '{}',
    embedding  vector(384) NOT NULL,         -- BAAI/bge-small-en-v1.5 (shared/knowledge.py)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS idx_documents_embedding
    ON knowledge.documents USING hnsw (embedding vector_cosine_ops);
