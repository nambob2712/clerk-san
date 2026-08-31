-- Preserve recurring-bill history while allowing a reviewed reprocess to replace its active period.

ALTER TABLE recurring_bills
    ADD COLUMN IF NOT EXISTS review_corrections JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(review_corrections) = 'object');

ALTER TABLE recurring_bills
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;

ALTER TABLE recurring_bills
    DROP CONSTRAINT IF EXISTS recurring_bills_issuer_period_key;

CREATE UNIQUE INDEX IF NOT EXISTS recurring_bills_active_issuer_period_key
    ON recurring_bills (issuer_id, billing_period)
    WHERE superseded_at IS NULL;

CREATE OR REPLACE FUNCTION reject_recurring_bill_projection_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.issuer_id IS DISTINCT FROM NEW.issuer_id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.verified_record_id IS DISTINCT FROM NEW.verified_record_id
       OR OLD.billing_period IS DISTINCT FROM NEW.billing_period
       OR OLD.amount IS DISTINCT FROM NEW.amount
       OR OLD.due_date IS DISTINCT FROM NEW.due_date
       OR OLD.consumption_value IS DISTINCT FROM NEW.consumption_value
       OR OLD.consumption_unit IS DISTINCT FROM NEW.consumption_unit
       OR OLD.review_corrections IS DISTINCT FROM NEW.review_corrections THEN
        RAISE EXCEPTION 'recurring bill projection is immutable; create a replacement row instead'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS recurring_bills_projection_immutable ON recurring_bills;
CREATE TRIGGER recurring_bills_projection_immutable
    BEFORE UPDATE ON recurring_bills
    FOR EACH ROW
    EXECUTE FUNCTION reject_recurring_bill_projection_mutation();

CREATE OR REPLACE FUNCTION audit_recurring_bill_review_corrections()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_payload JSONB;
    field_name TEXT;
    old_value JSONB;
    new_value JSONB;
BEGIN
    IF NEW.review_corrections = '{}'::jsonb THEN
        RETURN NEW;
    END IF;

    SELECT extracted.payload
      INTO source_payload
      FROM verified_records verified
      JOIN extracted_records extracted
        ON extracted.id = verified.extracted_id
       AND extracted.document_id = verified.document_id
     WHERE verified.id = NEW.verified_record_id;

    FOR field_name IN SELECT jsonb_object_keys(NEW.review_corrections) LOOP
        old_value := public.extraction_payload_value(source_payload, field_name);
        CASE field_name
            WHEN 'issuer_name' THEN
                SELECT public.audit_json(name) INTO new_value FROM issuers WHERE id = NEW.issuer_id;
            WHEN 'issuer_kind' THEN
                SELECT public.audit_json(kind) INTO new_value FROM issuers WHERE id = NEW.issuer_id;
            WHEN 'billing_period' THEN new_value := public.audit_json(NEW.billing_period);
            WHEN 'due_date' THEN new_value := public.audit_json(NEW.due_date);
            WHEN 'consumption_value' THEN new_value := public.audit_json(NEW.consumption_value);
            WHEN 'consumption_unit' THEN new_value := public.audit_json(NEW.consumption_unit);
            ELSE
                RAISE EXCEPTION 'unsupported recurring-bill review correction: %', field_name
                    USING ERRCODE = '22023';
        END CASE;
        IF old_value IS DISTINCT FROM new_value THEN
            PERFORM public.write_audit_event(
                'recurring_bills', NEW.id::text, 'INSERT', field_name, old_value, new_value
            );
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS recurring_bills_review_correction_audit ON recurring_bills;
CREATE TRIGGER recurring_bills_review_correction_audit
    AFTER INSERT ON recurring_bills
    FOR EACH ROW
    EXECUTE FUNCTION audit_recurring_bill_review_corrections();

CREATE OR REPLACE FUNCTION audit_recurring_bill_payment_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.payment_status IS DISTINCT FROM OLD.payment_status THEN
        PERFORM public.write_audit_event(
            'recurring_bills', NEW.id::text, 'UPDATE', 'payment_status',
            public.audit_json(OLD.payment_status), public.audit_json(NEW.payment_status)
        );
    END IF;
    IF NEW.paid_at IS DISTINCT FROM OLD.paid_at THEN
        PERFORM public.write_audit_event(
            'recurring_bills', NEW.id::text, 'UPDATE', 'paid_at',
            public.audit_json(OLD.paid_at), public.audit_json(NEW.paid_at)
        );
    END IF;
    IF NEW.reviewer IS DISTINCT FROM OLD.reviewer THEN
        PERFORM public.write_audit_event(
            'recurring_bills', NEW.id::text, 'UPDATE', 'reviewer',
            public.audit_json(OLD.reviewer), public.audit_json(NEW.reviewer)
        );
    END IF;
    IF NEW.superseded_at IS DISTINCT FROM OLD.superseded_at THEN
        PERFORM public.write_audit_event(
            'recurring_bills', NEW.id::text, 'UPDATE', 'superseded_at',
            public.audit_json(OLD.superseded_at), public.audit_json(NEW.superseded_at)
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS recurring_bills_payment_audit ON recurring_bills;
CREATE TRIGGER recurring_bills_payment_audit
    AFTER UPDATE OF payment_status, paid_at, reviewer, superseded_at ON recurring_bills
    FOR EACH ROW
    EXECUTE FUNCTION audit_recurring_bill_payment_update();
