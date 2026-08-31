-- Normalize extraction scalars to the verified-record column types before the
-- insert audit trigger decides whether a reviewer corrected a value.

CREATE OR REPLACE FUNCTION canonical_extraction_date_value(p_value JSONB)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    candidate TEXT;
BEGIN
    IF p_value IS NULL OR p_value = 'null'::jsonb THEN
        RETURN 'null'::jsonb;
    END IF;

    IF jsonb_typeof(p_value) <> 'string' THEN
        RETURN p_value;
    END IF;

    candidate := p_value #>> '{}';
    IF candidate IS NULL OR candidate <> btrim(candidate) THEN
        RETURN p_value;
    END IF;

    BEGIN
        RETURN public.audit_json(candidate::date);
    EXCEPTION
        WHEN invalid_datetime_format OR datetime_field_overflow THEN
            RETURN p_value;
    END;
END;
$$;

CREATE OR REPLACE FUNCTION canonical_extraction_numeric_value(p_value JSONB)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    candidate TEXT;
BEGIN
    IF p_value IS NULL OR p_value = 'null'::jsonb THEN
        RETURN 'null'::jsonb;
    END IF;

    IF jsonb_typeof(p_value) = 'number' THEN
        RETURN p_value;
    END IF;
    IF jsonb_typeof(p_value) <> 'string' THEN
        RETURN p_value;
    END IF;

    candidate := btrim(p_value #>> '{}');
    IF candidate = '' THEN
        RETURN p_value;
    END IF;

    BEGIN
        RETURN public.audit_json(candidate::numeric);
    EXCEPTION
        WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN p_value;
    END;
END;
$$;

CREATE OR REPLACE FUNCTION canonical_extraction_text_value(p_value JSONB)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    candidate TEXT;
BEGIN
    IF p_value IS NULL OR p_value = 'null'::jsonb THEN
        RETURN 'null'::jsonb;
    END IF;

    IF jsonb_typeof(p_value) <> 'string' THEN
        RETURN p_value;
    END IF;

    candidate := btrim(p_value #>> '{}');
    IF candidate = '' THEN
        RETURN 'null'::jsonb;
    END IF;
    RETURN public.audit_json(candidate);
END;
$$;

CREATE OR REPLACE FUNCTION audit_verified_record_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_payload JSONB;
    source_transaction_date JSONB;
    source_total_amount JSONB;
    source_counterparty JSONB;
    source_currency JSONB;
    source_category JSONB;
    source_registration_number JSONB;
    source_tax_8_amount JSONB;
    source_tax_10_amount JSONB;
BEGIN
    SELECT payload
      INTO source_payload
      FROM public.extracted_records
     WHERE id = NEW.extracted_id
       AND document_id = NEW.document_id;

    source_transaction_date := public.canonical_extraction_date_value(
        public.extraction_payload_value(source_payload, 'transaction_date')
    );
    source_total_amount := public.canonical_extraction_numeric_value(
        public.extraction_payload_value(source_payload, 'total_amount')
    );
    source_counterparty := public.canonical_extraction_text_value(
        public.extraction_payload_value(source_payload, 'counterparty')
    );
    source_currency := public.canonical_extraction_text_value(
        public.extraction_payload_value(source_payload, 'currency')
    );
    source_category := public.canonical_extraction_text_value(
        public.extraction_payload_value(source_payload, 'expense_category')
    );
    source_registration_number := public.canonical_extraction_text_value(
        public.extraction_payload_value(source_payload, 'registration_number')
    );
    source_tax_8_amount := public.canonical_extraction_numeric_value(
        public.extraction_payload_value(source_payload, 'tax_8_amount')
    );
    source_tax_10_amount := public.canonical_extraction_numeric_value(
        public.extraction_payload_value(source_payload, 'tax_10_amount')
    );

    IF public.audit_json(NEW.transaction_date) IS DISTINCT FROM source_transaction_date THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'transaction_date',
            source_transaction_date, public.audit_json(NEW.transaction_date)
        );
    END IF;

    IF public.audit_json(NEW.total_amount) IS DISTINCT FROM source_total_amount THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'total_amount',
            source_total_amount, public.audit_json(NEW.total_amount)
        );
    END IF;

    IF public.audit_json(NEW.counterparty) IS DISTINCT FROM source_counterparty THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'counterparty',
            source_counterparty, public.audit_json(NEW.counterparty)
        );
    END IF;

    IF public.audit_json(NEW.currency) IS DISTINCT FROM source_currency THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'currency',
            source_currency, public.audit_json(NEW.currency)
        );
    END IF;

    IF public.audit_json(NEW.category) IS DISTINCT FROM source_category THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'category',
            source_category, public.audit_json(NEW.category)
        );
    END IF;

    IF public.audit_json(NEW.registration_number) IS DISTINCT FROM source_registration_number THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'registration_number',
            source_registration_number, public.audit_json(NEW.registration_number)
        );
    END IF;

    IF public.audit_json(NEW.tax_8_amount) IS DISTINCT FROM source_tax_8_amount THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'tax_8_amount',
            source_tax_8_amount, public.audit_json(NEW.tax_8_amount)
        );
    END IF;

    IF public.audit_json(NEW.tax_10_amount) IS DISTINCT FROM source_tax_10_amount THEN
        PERFORM public.write_audit_event(
            'verified_records', NEW.id::text, 'INSERT', 'tax_10_amount',
            source_tax_10_amount, public.audit_json(NEW.tax_10_amount)
        );
    END IF;

    RETURN NEW;
END;
$$;
