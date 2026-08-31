-- Keep rejection explanations immutable after the extraction-batch migration
-- extended the content guard with candidate-lineage fields.

CREATE OR REPLACE FUNCTION reject_extraction_content_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.source_file_id IS DISTINCT FROM NEW.source_file_id
       OR OLD.source_version IS DISTINCT FROM NEW.source_version
       OR OLD.batch_id IS DISTINCT FROM NEW.batch_id
       OR OLD.candidate_ordinal IS DISTINCT FROM NEW.candidate_ordinal
       OR OLD.candidate_key IS DISTINCT FROM NEW.candidate_key
       OR OLD.record_kind IS DISTINCT FROM NEW.record_kind
       OR OLD.financial_subtype IS DISTINCT FROM NEW.financial_subtype
       OR OLD.source_locator IS DISTINCT FROM NEW.source_locator
       OR OLD.row_fingerprint IS DISTINCT FROM NEW.row_fingerprint
       OR OLD.validation_issues IS DISTINCT FROM NEW.validation_issues
       OR OLD.evidence_group_keys IS DISTINCT FROM NEW.evidence_group_keys
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
