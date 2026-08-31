-- Preserve every replacement source as an immutable document-file version.

ALTER TABLE document_files
    ADD COLUMN IF NOT EXISTS source_filename TEXT;

ALTER TABLE document_files DISABLE TRIGGER document_files_append_only;

UPDATE document_files AS file
   SET source_filename = document.source_filename
  FROM documents AS document
 WHERE file.document_id = document.id
   AND file.source_filename IS NULL;

ALTER TABLE document_files ENABLE TRIGGER document_files_append_only;

ALTER TABLE document_files
    ALTER COLUMN source_filename SET NOT NULL;

ALTER TABLE document_files
    ADD CONSTRAINT document_files_source_filename_nonblank
    CHECK (btrim(source_filename) <> '');

DROP INDEX IF EXISTS document_files_one_original_per_document_idx;

CREATE INDEX IF NOT EXISTS document_files_latest_original_idx
    ON document_files (document_id, version DESC)
    WHERE kind = 'original';

CREATE OR REPLACE FUNCTION audit_document_file_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM public.write_audit_event(
        'document_files', NEW.id::text, 'INSERT', 'version', 'null'::jsonb, public.audit_json(NEW.version)
    );
    PERFORM public.write_audit_event(
        'document_files', NEW.id::text, 'INSERT', 'kind', 'null'::jsonb, public.audit_json(NEW.kind)
    );
    PERFORM public.write_audit_event(
        'document_files', NEW.id::text, 'INSERT', 'source_filename',
        'null'::jsonb, public.audit_json(NEW.source_filename)
    );
    PERFORM public.write_audit_event(
        'document_files', NEW.id::text, 'INSERT', 'content_path',
        'null'::jsonb, public.audit_json(NEW.content_path)
    );
    PERFORM public.write_audit_event(
        'document_files', NEW.id::text, 'INSERT', 'sha256', 'null'::jsonb, public.audit_json(NEW.sha256)
    );
    PERFORM public.write_audit_event(
        'document_files', NEW.id::text, 'INSERT', 'mime', 'null'::jsonb, public.audit_json(NEW.mime)
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS document_files_insert_audit ON document_files;
CREATE TRIGGER document_files_insert_audit
    AFTER INSERT ON document_files
    FOR EACH ROW
    EXECUTE FUNCTION audit_document_file_insert();

CREATE OR REPLACE FUNCTION audit_document_source_filename_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.source_filename IS DISTINCT FROM OLD.source_filename THEN
        PERFORM public.write_audit_event(
            'documents', NEW.id::text, 'UPDATE', 'source_filename',
            public.audit_json(OLD.source_filename), public.audit_json(NEW.source_filename)
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS documents_source_filename_audit ON documents;
CREATE TRIGGER documents_source_filename_audit
    AFTER UPDATE OF source_filename ON documents
    FOR EACH ROW
    EXECUTE FUNCTION audit_document_source_filename_update();
