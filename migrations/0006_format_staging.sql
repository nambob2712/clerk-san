-- Derived format data for bounded OOXML ingestion.  Source documents remain immutable.

CREATE TABLE IF NOT EXISTS embedded_media (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    content_path TEXT NOT NULL CHECK (btrim(content_path) <> ''),
    mime TEXT NOT NULL CHECK (btrim(mime) <> ''),
    source_location TEXT NOT NULL CHECK (btrim(source_location) <> ''),
    width INTEGER,
    height INTEGER,
    ocr_text TEXT,
    ocr_engine TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT embedded_media_document_sha256_key UNIQUE (document_id, sha256)
);

CREATE INDEX IF NOT EXISTS embedded_media_document_idx
    ON embedded_media (document_id, created_at);

CREATE TABLE IF NOT EXISTS spreadsheet_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    source_location TEXT NOT NULL CHECK (btrim(source_location) <> ''),
    row_index INTEGER NOT NULL CHECK (row_index > 0),
    values JSONB NOT NULL CHECK (jsonb_typeof(values) = 'object'),
    value_types JSONB NOT NULL CHECK (jsonb_typeof(value_types) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT spreadsheet_rows_document_source_row_key
        UNIQUE (document_id, source_location, row_index)
);

CREATE INDEX IF NOT EXISTS spreadsheet_rows_document_source_idx
    ON spreadsheet_rows (document_id, source_location);
