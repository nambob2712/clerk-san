-- Tie each immutable extraction to the exact immutable original that produced it.

ALTER TABLE document_files
    ADD CONSTRAINT document_files_id_document_id_key UNIQUE (id, document_id);

ALTER TABLE extracted_records
    ADD COLUMN source_file_id UUID,
    ADD COLUMN source_version INTEGER;

UPDATE extracted_records AS extraction
   SET source_file_id = source.id,
       source_version = source.version
  FROM document_files AS source
 WHERE extraction.source_file_id IS NULL
   AND source.id = (
       SELECT candidate.id
         FROM document_files AS candidate
        WHERE candidate.document_id = extraction.document_id
          AND candidate.kind = 'original'
        ORDER BY
            (candidate.created_at <= extraction.created_at) DESC,
            candidate.version DESC,
            candidate.id DESC
        LIMIT 1
   );

ALTER TABLE extracted_records
    ALTER COLUMN source_file_id SET NOT NULL,
    ALTER COLUMN source_version SET NOT NULL;

ALTER TABLE extracted_records
    ADD CONSTRAINT extracted_records_source_version_positive CHECK (source_version > 0),
    ADD CONSTRAINT extracted_records_source_file_document_fkey
        FOREIGN KEY (source_file_id, document_id)
        REFERENCES document_files (id, document_id)
        ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION enforce_extracted_record_source_file()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_kind TEXT;
    actual_version INTEGER;
BEGIN
    SELECT kind, version
      INTO source_kind, actual_version
      FROM document_files
     WHERE id = NEW.source_file_id
       AND document_id = NEW.document_id;

    IF source_kind IS DISTINCT FROM 'original'
       OR actual_version IS DISTINCT FROM NEW.source_version THEN
        RAISE EXCEPTION 'extraction source must be the matching immutable original version'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS extracted_records_source_file_guard ON extracted_records;
CREATE TRIGGER extracted_records_source_file_guard
    BEFORE INSERT OR UPDATE OF source_file_id, source_version, document_id ON extracted_records
    FOR EACH ROW
    EXECUTE FUNCTION enforce_extracted_record_source_file();

CREATE OR REPLACE FUNCTION reject_extraction_content_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.source_file_id IS DISTINCT FROM NEW.source_file_id
       OR OLD.source_version IS DISTINCT FROM NEW.source_version
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
