-- Clerk-san migration 0002: append-only audit log (訂正削除履歴).
-- Triggers are the sole writer: they compute field differences and use the transaction-
-- local actor set by the application. Direct database changes fall back to db:<role>.
-- Audit rows must outlive source rows, so audit_log has no foreign keys.

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
    table_name TEXT NOT NULL CHECK (btrim(table_name) <> ''),
    row_pk TEXT NOT NULL CHECK (btrim(row_pk) <> ''),
    action TEXT NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    field TEXT NOT NULL CHECK (btrim(field) <> ''),
    old_value TEXT,
    new_value TEXT,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_table_row_at_idx
    ON audit_log (table_name, row_pk, at DESC);

CREATE OR REPLACE FUNCTION current_audit_actor()
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(
        NULLIF(current_setting('clerksan.actor', true), ''),
        'db:' || session_user
    );
$$;

CREATE OR REPLACE FUNCTION audit_json(value anyelement)
RETURNS JSONB
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(to_jsonb(value), 'null'::jsonb);
$$;

CREATE OR REPLACE FUNCTION extraction_payload_value(payload JSONB, field_name TEXT)
RETURNS JSONB
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT COALESCE(payload #> ARRAY[field_name, 'value'], 'null'::jsonb);
$$;

CREATE OR REPLACE FUNCTION write_audit_event(
    p_table_name TEXT,
    p_row_pk TEXT,
    p_action TEXT,
    p_field TEXT,
    p_old_value JSONB,
    p_new_value JSONB
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    INSERT INTO public.audit_log (
        actor,
        table_name,
        row_pk,
        action,
        field,
        old_value,
        new_value
    )
    VALUES (
        public.current_audit_actor(),
        p_table_name,
        p_row_pk,
        p_action,
        p_field,
        COALESCE(p_old_value, 'null'::jsonb)::text,
        COALESCE(p_new_value, 'null'::jsonb)::text
    );
END;
$$;

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
    AFTER UPDATE OF status, version, reviewer, reviewed_at ON extracted_records
    FOR EACH ROW
    EXECUTE FUNCTION audit_extracted_record_lifecycle();

CREATE OR REPLACE FUNCTION audit_verified_record_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_payload JSONB;
BEGIN
    SELECT payload
      INTO source_payload
      FROM public.extracted_records
     WHERE id = NEW.extracted_id
       AND document_id = NEW.document_id;

    IF public.audit_json(NEW.transaction_date) IS DISTINCT FROM public.extraction_payload_value(source_payload, 'transaction_date') THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'transaction_date',
            public.extraction_payload_value(source_payload, 'transaction_date'),
            public.audit_json(NEW.transaction_date)
        );
    END IF;

    IF public.audit_json(NEW.total_amount) IS DISTINCT FROM public.extraction_payload_value(source_payload, 'total_amount') THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'total_amount',
            public.extraction_payload_value(source_payload, 'total_amount'),
            public.audit_json(NEW.total_amount)
        );
    END IF;

    IF public.audit_json(NEW.counterparty) IS DISTINCT FROM public.extraction_payload_value(source_payload, 'counterparty') THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'counterparty',
            public.extraction_payload_value(source_payload, 'counterparty'),
            public.audit_json(NEW.counterparty)
        );
    END IF;

    IF public.audit_json(NEW.currency) IS DISTINCT FROM public.extraction_payload_value(source_payload, 'currency') THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'currency',
            public.extraction_payload_value(source_payload, 'currency'),
            public.audit_json(NEW.currency)
        );
    END IF;

    IF public.audit_json(NEW.category) IS DISTINCT FROM public.extraction_payload_value(source_payload, 'expense_category') THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'category',
            public.extraction_payload_value(source_payload, 'expense_category'),
            public.audit_json(NEW.category)
        );
    END IF;

    IF public.audit_json(NEW.registration_number) IS DISTINCT FROM public.extraction_payload_value(source_payload, 'registration_number') THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'registration_number',
            public.extraction_payload_value(source_payload, 'registration_number'),
            public.audit_json(NEW.registration_number)
        );
    END IF;

    IF public.audit_json(NEW.tax_8_amount) IS DISTINCT FROM public.extraction_payload_value(source_payload, 'tax_8_amount') THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'tax_8_amount',
            public.extraction_payload_value(source_payload, 'tax_8_amount'),
            public.audit_json(NEW.tax_8_amount)
        );
    END IF;

    IF public.audit_json(NEW.tax_10_amount) IS DISTINCT FROM public.extraction_payload_value(source_payload, 'tax_10_amount') THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'tax_10_amount',
            public.extraction_payload_value(source_payload, 'tax_10_amount'),
            public.audit_json(NEW.tax_10_amount)
        );
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS verified_records_correction_audit ON verified_records;
CREATE TRIGGER verified_records_correction_audit
    AFTER INSERT ON verified_records
    FOR EACH ROW
    EXECUTE FUNCTION audit_verified_record_insert();

CREATE OR REPLACE FUNCTION audit_verified_record_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_row JSONB := to_jsonb(OLD) - 'id';
    new_row JSONB := to_jsonb(NEW) - 'id';
    field_name TEXT;
    old_field_value JSONB;
    new_field_value JSONB;
BEGIN
    FOR field_name IN
        SELECT key
          FROM (
              SELECT jsonb_object_keys(old_row) AS key
              UNION
              SELECT jsonb_object_keys(new_row) AS key
          ) AS changed_fields
         ORDER BY key
    LOOP
        old_field_value := COALESCE(old_row -> field_name, 'null'::jsonb);
        new_field_value := COALESCE(new_row -> field_name, 'null'::jsonb);

        IF old_field_value IS DISTINCT FROM new_field_value THEN
            PERFORM public.write_audit_event(
                'verified_records', NEW.id::text, 'UPDATE', field_name,
                old_field_value, new_field_value
            );
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS verified_records_update_audit ON verified_records;
CREATE TRIGGER verified_records_update_audit
    AFTER UPDATE ON verified_records
    FOR EACH ROW
    EXECUTE FUNCTION audit_verified_record_update();

CREATE OR REPLACE FUNCTION audit_verified_record_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_row JSONB := to_jsonb(OLD) - 'id';
    field_name TEXT;
BEGIN
    FOR field_name IN
        SELECT jsonb_object_keys(old_row)
         ORDER BY 1
    LOOP
        PERFORM public.write_audit_event(
            'verified_records', OLD.id::text, 'DELETE', field_name,
            COALESCE(old_row -> field_name, 'null'::jsonb), 'null'::jsonb
        );
    END LOOP;

    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS verified_records_delete_audit ON verified_records;
CREATE TRIGGER verified_records_delete_audit
    AFTER DELETE ON verified_records
    FOR EACH ROW
    EXECUTE FUNCTION audit_verified_record_delete();

CREATE OR REPLACE FUNCTION reject_direct_audit_log_write()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Nested writes originate in a source-table audit trigger. Direct writes do not.
    IF TG_OP = 'INSERT' AND pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'audit_log may only be written by audit triggers and is append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_write_guard ON audit_log;
CREATE TRIGGER audit_log_write_guard
    BEFORE INSERT OR UPDATE OR DELETE ON audit_log
    FOR EACH ROW
    EXECUTE FUNCTION reject_direct_audit_log_write();

REVOKE INSERT, UPDATE, DELETE ON audit_log FROM PUBLIC;
