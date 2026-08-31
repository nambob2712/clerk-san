-- Recurring bill time series linked to reviewed source records.

CREATE TABLE IF NOT EXISTS issuers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    kind TEXT NOT NULL DEFAULT 'other'
        CHECK (kind IN ('electric', 'gas', 'water', 'nhi', 'tax', 'other')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT issuers_name_key UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS recurring_bills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    issuer_id UUID NOT NULL REFERENCES issuers(id) ON DELETE RESTRICT,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    verified_record_id UUID NOT NULL REFERENCES verified_records(id) ON DELETE RESTRICT,
    billing_period DATE NOT NULL
        CHECK (billing_period = date_trunc('month', billing_period)::date),
    amount NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
    due_date DATE,
    payment_status TEXT NOT NULL DEFAULT 'unpaid'
        CHECK (payment_status IN ('paid', 'unpaid', 'overdue')),
    consumption_value NUMERIC(18, 4),
    consumption_unit TEXT,
    paid_at TIMESTAMPTZ,
    reviewer TEXT CHECK (reviewer IS NULL OR btrim(reviewer) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recurring_bills_verified_record_id_key UNIQUE (verified_record_id),
    CONSTRAINT recurring_bills_issuer_period_key UNIQUE (issuer_id, billing_period),
    CONSTRAINT recurring_bills_consumption_pair_check CHECK (
        (consumption_value IS NULL AND consumption_unit IS NULL)
        OR (
            consumption_value > 0
            AND consumption_unit IS NOT NULL
            AND btrim(consumption_unit) <> ''
        )
    )
);

CREATE INDEX IF NOT EXISTS recurring_bills_issuer_period_idx
    ON recurring_bills (issuer_id, billing_period);

CREATE INDEX IF NOT EXISTS recurring_bills_payment_due_idx
    ON recurring_bills (payment_status, due_date)
    WHERE payment_status <> 'paid' AND due_date IS NOT NULL;

CREATE OR REPLACE FUNCTION recurring_bills_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS issuers_touch_updated_at ON issuers;
CREATE TRIGGER issuers_touch_updated_at
    BEFORE UPDATE ON issuers
    FOR EACH ROW
    EXECUTE FUNCTION recurring_bills_touch_updated_at();

DROP TRIGGER IF EXISTS recurring_bills_touch_updated_at ON recurring_bills;
CREATE TRIGGER recurring_bills_touch_updated_at
    BEFORE UPDATE ON recurring_bills
    FOR EACH ROW
    EXECUTE FUNCTION recurring_bills_touch_updated_at();

CREATE OR REPLACE FUNCTION enforce_recurring_bill_source_document()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_document_id UUID;
BEGIN
    SELECT document_id
      INTO source_document_id
      FROM verified_records
     WHERE id = NEW.verified_record_id;

    IF source_document_id IS NULL OR source_document_id <> NEW.document_id THEN
        RAISE EXCEPTION 'recurring bill source document must match its verified record'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS recurring_bills_source_document_guard ON recurring_bills;
CREATE TRIGGER recurring_bills_source_document_guard
    BEFORE INSERT OR UPDATE OF document_id, verified_record_id ON recurring_bills
    FOR EACH ROW
    EXECUTE FUNCTION enforce_recurring_bill_source_document();

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
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS recurring_bills_payment_audit ON recurring_bills;
CREATE TRIGGER recurring_bills_payment_audit
    AFTER UPDATE OF payment_status, paid_at, reviewer ON recurring_bills
    FOR EACH ROW
    EXECUTE FUNCTION audit_recurring_bill_payment_update();
