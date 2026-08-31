-- Add one-off bills and a coarse verified expense projection without rewriting evidence.

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_document_class_check;

ALTER TABLE documents
    ADD CONSTRAINT documents_document_class_check
    CHECK (document_class IN ('receipt', 'invoice', 'bill', 'recurring_bill', 'quote', 'other'));

ALTER TABLE verified_records
    ADD COLUMN IF NOT EXISTS expense_kind TEXT,
    ADD COLUMN IF NOT EXISTS due_date DATE,
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE verified_records
    DROP CONSTRAINT IF EXISTS verified_records_expense_kind_check;

ALTER TABLE verified_records
    ADD CONSTRAINT verified_records_expense_kind_check
    CHECK (
        expense_kind IS NULL OR expense_kind IN (
            'retail', 'electricity', 'water', 'gas', 'telecom', 'tax',
            'insurance', 'rent', 'subscription', 'other'
        )
    );

ALTER TABLE verified_records
    DROP CONSTRAINT IF EXISTS verified_records_version_positive;

ALTER TABLE verified_records
    ADD CONSTRAINT verified_records_version_positive CHECK (version > 0);

CREATE INDEX IF NOT EXISTS verified_records_expense_kind_date_idx
    ON verified_records (expense_kind, transaction_date);

CREATE INDEX IF NOT EXISTS verified_records_due_date_idx
    ON verified_records (due_date)
    WHERE due_date IS NOT NULL;

-- Only relationships already reviewed as recurring provide safe legacy labels.
WITH deterministic_bill_values AS (
    SELECT
        v.id AS verified_record_id,
        CASE i.kind
            WHEN 'electric' THEN 'electricity'
            WHEN 'water' THEN 'water'
            WHEN 'gas' THEN 'gas'
            WHEN 'tax' THEN 'tax'
            WHEN 'nhi' THEN 'insurance'
            ELSE NULL
        END AS expense_kind,
        rb.due_date
    FROM verified_records AS v
    JOIN recurring_bills AS rb ON rb.verified_record_id = v.id
    JOIN issuers AS i ON i.id = rb.issuer_id
    JOIN extracted_records AS e
      ON e.id = v.extracted_id
     AND e.document_id = v.document_id
    WHERE rb.superseded_at IS NULL
      AND e.status = 'approved'
)
UPDATE verified_records AS target
   SET expense_kind = COALESCE(target.expense_kind, source.expense_kind),
       due_date = COALESCE(target.due_date, source.due_date)
  FROM deterministic_bill_values AS source
 WHERE target.id = source.verified_record_id
   AND (
       (target.expense_kind IS NULL AND source.expense_kind IS NOT NULL)
       OR (target.due_date IS NULL AND source.due_date IS NOT NULL)
   );

-- The existing generic UPDATE trigger audits these fields automatically. This insert
-- trigger records review-time corrections against immutable extraction values.
CREATE OR REPLACE FUNCTION audit_expense_document_verified_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_payload JSONB;
    source_expense_kind JSONB;
    source_due_date JSONB;
BEGIN
    SELECT payload
      INTO source_payload
      FROM public.extracted_records
     WHERE id = NEW.extracted_id
       AND document_id = NEW.document_id;

    source_expense_kind := public.canonical_extraction_text_value(
        public.extraction_payload_value(source_payload, 'expense_kind')
    );
    source_due_date := public.canonical_extraction_date_value(
        public.extraction_payload_value(source_payload, 'due_date')
    );

    IF public.audit_json(NEW.expense_kind) IS DISTINCT FROM source_expense_kind THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'expense_kind',
            source_expense_kind, public.audit_json(NEW.expense_kind)
        );
    END IF;

    IF public.audit_json(NEW.due_date) IS DISTINCT FROM source_due_date THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'due_date',
            source_due_date, public.audit_json(NEW.due_date)
        );
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS verified_records_expense_document_correction_audit
    ON verified_records;
CREATE TRIGGER verified_records_expense_document_correction_audit
    AFTER INSERT ON verified_records
    FOR EACH ROW
    EXECUTE FUNCTION audit_expense_document_verified_insert();
