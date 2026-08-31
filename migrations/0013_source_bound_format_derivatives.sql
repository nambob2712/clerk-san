-- Bind mutable OOXML-derived rows to the immutable raw-source version that produced them.
-- Before this migration, format projections did not retain that provenance. They cannot be
-- truthfully attributed to the latest original on an upgraded database, so discard them and
-- let the normal processing job rebuild them from the preserved raw source.

ALTER TABLE embedded_media
    ADD COLUMN source_version INTEGER;

CREATE TEMP TABLE format_derivative_rebuild_documents
ON COMMIT DROP
AS
SELECT document_id FROM embedded_media
UNION
SELECT document_id FROM spreadsheet_rows;

DELETE FROM embedded_media;

ALTER TABLE embedded_media
    ALTER COLUMN source_version SET NOT NULL;

ALTER TABLE embedded_media
    ADD CONSTRAINT embedded_media_source_version_positive
    CHECK (source_version > 0);

ALTER TABLE embedded_media
    DROP CONSTRAINT embedded_media_document_sha256_key;

ALTER TABLE embedded_media
    ADD CONSTRAINT embedded_media_document_source_sha256_key
    UNIQUE (document_id, source_version, sha256);

CREATE INDEX IF NOT EXISTS embedded_media_document_source_idx
    ON embedded_media (document_id, source_version, created_at);

ALTER TABLE spreadsheet_rows
    ADD COLUMN source_version INTEGER;

DELETE FROM spreadsheet_rows;

ALTER TABLE spreadsheet_rows
    ALTER COLUMN source_version SET NOT NULL;

ALTER TABLE spreadsheet_rows
    ADD CONSTRAINT spreadsheet_rows_source_version_positive
    CHECK (source_version > 0);

ALTER TABLE spreadsheet_rows
    DROP CONSTRAINT spreadsheet_rows_document_source_row_key;

ALTER TABLE spreadsheet_rows
    ADD CONSTRAINT spreadsheet_rows_document_source_version_row_key
    UNIQUE (document_id, source_version, source_location, row_index);

CREATE INDEX IF NOT EXISTS spreadsheet_rows_document_version_source_idx
    ON spreadsheet_rows (document_id, source_version, source_location);

-- Rebuild only the purged mutable projections. Do not enqueue a full document reprocess:
-- that would append a new extraction and incorrectly return a previously reviewed record
-- to the review queue merely because this schema gained source provenance.
INSERT INTO jobs (document_id, job_type, payload, idempotency_key)
SELECT affected.document_id,
       'rebuild_format_derivatives',
       jsonb_build_object(
           'document_id', affected.document_id::text,
           'source_version', source.version,
           'migration', '0013_source_bound_format_derivatives'
       ),
       'format-derivatives:0013:' || source.version::text
  FROM format_derivative_rebuild_documents AS affected
 CROSS JOIN LATERAL (
     SELECT version
       FROM document_files
      WHERE document_id = affected.document_id
        AND kind = 'original'
      ORDER BY version DESC, id DESC
      LIMIT 1
 ) AS source
ON CONFLICT (document_id, idempotency_key) DO NOTHING;
