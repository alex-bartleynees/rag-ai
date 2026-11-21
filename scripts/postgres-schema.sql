BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id          SERIAL PRIMARY KEY,
    source_file TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    heading     TEXT,
    content     TEXT NOT NULL,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    embedding   vector(768),
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (source_file, chunk_index)
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS heading TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS documents_embedding_idx ON documents
    USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS ingested_files (
    id           SERIAL PRIMARY KEY,
    source_file  TEXT        NOT NULL UNIQUE,
    content_hash TEXT        NOT NULL,
    chunk_count  INTEGER     NOT NULL DEFAULT 0,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memories (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    embedding   vector(768),
    topics      TEXT[]      DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memories_embedding_idx ON memories
    USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS documents_source_file_idx ON documents (source_file);
CREATE INDEX IF NOT EXISTS ingested_files_source_file_idx ON ingested_files (source_file);

COMMIT;
