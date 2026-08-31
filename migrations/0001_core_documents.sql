-- Core raw -> extracted -> verified schema.
-- Reprocessing appends a new extraction; immutable raw artifacts and extraction payloads
-- are protected by database triggers. Verified records are indexed for date, amount, and
-- counterparty search.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_class TEXT NOT NULL DEFAULT 'other'
        CHECK (document_class IN ('receipt', 'invoice', 'recurring_bill', 'quote', 'other')),
    status TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (status IN (
            'uploaded', 'normalized', 'extracted', 'in_review', 'verified',
            'needs_reprocess', 'failed'
        )),
    source_filename TEXT NOT NULL CHECK (btrim(source_filename) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_class_status_idx
    ON documents (document_class, status);

CREATE TABLE IF NOT EXISTS document_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL CHECK (version > 0),
    kind TEXT NOT NULL CHECK (kind IN ('original', 'page_render', 'normalized')),
    content_path TEXT NOT NULL CHECK (btrim(content_path) <> ''),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    mime TEXT NOT NULL CHECK (btrim(mime) <> ''),
    ocr_text TEXT,
    text_provenance TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version),
    UNIQUE (document_id, sha256)
);

CREATE UNIQUE INDEX IF NOT EXISTS document_files_one_original_per_document_idx
    ON document_files (document_id)
    WHERE kind = 'original';

CREATE INDEX IF NOT EXISTS document_files_document_created_idx
    ON document_files (document_id, created_at);

CREATE OR REPLACE FUNCTION reject_document_file_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'document_files rows are append-only; add a new artifact version instead'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS document_files_append_only ON document_files;
CREATE TRIGGER document_files_append_only
    BEFORE UPDATE OR DELETE ON document_files
    FOR EACH ROW
    EXECUTE FUNCTION reject_document_file_mutation();

CREATE TABLE IF NOT EXISTS extracted_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    field_confidences JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(field_confidences) = 'object'),
    source_spans JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(source_spans) = 'object'),
    model_name TEXT NOT NULL CHECK (btrim(model_name) <> ''),
    prompt_version TEXT NOT NULL CHECK (btrim(prompt_version) <> ''),
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review', 'approved', 'rejected', 'superseded')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    reviewer TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, document_id)
);

CREATE INDEX IF NOT EXISTS extracted_records_document_created_idx
    ON extracted_records (document_id, created_at DESC);

CREATE INDEX IF NOT EXISTS extracted_records_pending_review_idx
    ON extracted_records (created_at)
    WHERE status = 'pending_review';

CREATE OR REPLACE FUNCTION reject_extraction_content_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.payload IS DISTINCT FROM NEW.payload
       OR OLD.field_confidences IS DISTINCT FROM NEW.field_confidences
       OR OLD.source_spans IS DISTINCT FROM NEW.source_spans
       OR OLD.model_name IS DISTINCT FROM NEW.model_name
       OR OLD.prompt_version IS DISTINCT FROM NEW.prompt_version
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'extraction content is immutable; create a new extraction instead'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS extracted_records_content_immutable ON extracted_records;
CREATE TRIGGER extracted_records_content_immutable
    BEFORE UPDATE ON extracted_records
    FOR EACH ROW
    EXECUTE FUNCTION reject_extraction_content_mutation();

CREATE TABLE IF NOT EXISTS verified_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    extracted_id UUID NOT NULL,
    transaction_date DATE NOT NULL,
    total_amount NUMERIC(18, 2) NOT NULL,
    counterparty TEXT NOT NULL CHECK (btrim(counterparty) <> ''),
    currency TEXT,
    category TEXT,
    registration_number TEXT,
    tax_8_amount NUMERIC(18, 2),
    tax_10_amount NUMERIC(18, 2),
    reviewer TEXT NOT NULL CHECK (btrim(reviewer) <> ''),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (extracted_id, document_id)
        REFERENCES extracted_records (id, document_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS verified_records_transaction_date_idx
    ON verified_records (transaction_date);

CREATE INDEX IF NOT EXISTS verified_records_total_amount_idx
    ON verified_records (total_amount);

CREATE INDEX IF NOT EXISTS verified_records_counterparty_idx
    ON verified_records (counterparty);

-- Counterparty equality plus date/amount ranges is the common combined search path.
CREATE INDEX IF NOT EXISTS verified_records_combined_search_idx
    ON verified_records (counterparty, transaction_date, total_amount);

CREATE INDEX IF NOT EXISTS verified_records_document_idx
    ON verified_records (document_id);
