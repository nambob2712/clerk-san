-- Retain a reviewer-supplied rejection reason as immutable lifecycle evidence.
-- This follows the append-only extraction payload rule: a reason can be attached only
-- while the record moves from pending_review to rejected, and the trigger-owned audit
-- log records that one field change exactly once.

ALTER TABLE extracted_records
    ADD COLUMN IF NOT EXISTS rejection_reason TEXT;

-- Extend the trigger-owned lifecycle audit before backfilling legacy rows so the
-- provenance marker below has one explicit audit event per changed field.
CREATE OR REPLACE FUNCTION audit_extracted_record_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM public.write_audit_event(
            'extracted_records', NEW.id::text, 'UPDATE', 'status',
            public.audit_json(OLD.status), public.audit_json(NEW.status)
        );
    END IF;

    IF NEW.version IS DISTINCT FROM OLD.version THEN
        PERFORM public.write_audit_event(
            'extracted_records', NEW.id::text, 'UPDATE', 'version',
            public.audit_json(OLD.version), public.audit_json(NEW.version)
        );
    END IF;

    IF NEW.reviewer IS DISTINCT FROM OLD.reviewer THEN
        PERFORM public.write_audit_event(
            'extracted_records', NEW.id::text, 'UPDATE', 'reviewer',
            public.audit_json(OLD.reviewer), public.audit_json(NEW.reviewer)
        );
    END IF;

    IF NEW.rejection_reason IS DISTINCT FROM OLD.rejection_reason THEN
        PERFORM public.write_audit_event(
            'extracted_records', NEW.id::text, 'UPDATE', 'rejection_reason',
            public.audit_json(OLD.rejection_reason), public.audit_json(NEW.rejection_reason)
        );
    END IF;

    IF NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at THEN
        PERFORM public.write_audit_event(
            'extracted_records', NEW.id::text, 'UPDATE', 'reviewed_at',
            public.audit_json(OLD.reviewed_at), public.audit_json(NEW.reviewed_at)
        );
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS extracted_records_lifecycle_audit ON extracted_records;
CREATE TRIGGER extracted_records_lifecycle_audit
    AFTER UPDATE OF status, version, reviewer, rejection_reason, reviewed_at ON extracted_records
    FOR EACH ROW
    EXECUTE FUNCTION audit_extracted_record_lifecycle();

-- Earlier rejected rows predate this field. Preserve the fact that a reason was not captured
-- rather than fabricating reviewer intent; the marker is immutable with the rest of history.
UPDATE extracted_records
   SET rejection_reason = 'Legacy rejection reason unavailable at migration'
 WHERE status = 'rejected'
   AND rejection_reason IS NULL;

ALTER TABLE extracted_records
    ADD CONSTRAINT extracted_records_rejection_reason_nonblank
    CHECK (rejection_reason IS NULL OR btrim(rejection_reason) <> '');

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
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
       OR (
           OLD.rejection_reason IS DISTINCT FROM NEW.rejection_reason
           AND NOT (
               OLD.rejection_reason IS NULL
               AND NEW.rejection_reason IS NOT NULL
               AND OLD.status = 'pending_review'
               AND NEW.status = 'rejected'
           )
       ) THEN
        RAISE EXCEPTION 'extraction content is immutable; create a new extraction instead'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_extraction_rejection_reason()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'rejected' AND COALESCE(btrim(NEW.rejection_reason), '') = '' THEN
        RAISE EXCEPTION 'rejected extractions require a nonblank rejection reason'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS extracted_records_rejection_reason_guard ON extracted_records;
CREATE TRIGGER extracted_records_rejection_reason_guard
    BEFORE INSERT OR UPDATE ON extracted_records
    FOR EACH ROW
    EXECUTE FUNCTION enforce_extraction_rejection_reason();
