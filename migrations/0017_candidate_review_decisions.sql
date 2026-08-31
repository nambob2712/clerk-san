-- Add append-only record-scoped review decisions and activation evidence.
-- Decision staging is deliberately orthogonal to extracted-record lifecycle:
-- no statement in this migration changes an extraction status or creates a verified row.

-- ---------------------------------------------------------------------------
-- Complete, immutable evidence for one cohort activation
-- ---------------------------------------------------------------------------

ALTER TABLE extraction_batches
    ADD COLUMN activation_vector_sha256 TEXT,
    ADD COLUMN activated_by TEXT,
    ADD COLUMN activated_at TIMESTAMPTZ,
    ADD COLUMN activation_included_count INTEGER,
    ADD COLUMN activation_excluded_count INTEGER,
    ADD COLUMN accepted_exclusions BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN accepted_empty BOOLEAN NOT NULL DEFAULT false,
    ADD CONSTRAINT extraction_batches_activation_vector_check
        CHECK (
            activation_vector_sha256 IS NULL
            OR activation_vector_sha256 ~ '^[0-9a-f]{64}$'
        ),
    ADD CONSTRAINT extraction_batches_activated_by_length
        CHECK (
            activated_by IS NULL
            OR length(btrim(activated_by)) BETWEEN 1 AND 255
        ),
    ADD CONSTRAINT extraction_batches_activation_metadata_complete
        CHECK (
            (
                activation_vector_sha256 IS NULL
                AND activated_by IS NULL
                AND activated_at IS NULL
                AND activation_included_count IS NULL
                AND activation_excluded_count IS NULL
                AND accepted_exclusions = false
                AND accepted_empty = false
            )
            OR
            (
                activation_vector_sha256 IS NOT NULL
                AND activated_by IS NOT NULL
                AND activated_at IS NOT NULL
                AND activation_included_count IS NOT NULL
                AND activation_excluded_count IS NOT NULL
            )
        ),
    ADD CONSTRAINT extraction_batches_activation_included_nonnegative
        CHECK (
            activation_included_count IS NULL
            OR activation_included_count >= 0
        ),
    ADD CONSTRAINT extraction_batches_activation_excluded_nonnegative
        CHECK (
            activation_excluded_count IS NULL
            OR activation_excluded_count >= 0
        ),
    ADD CONSTRAINT extraction_batches_activation_counts_reconcile
        CHECK (
            activation_vector_sha256 IS NULL
            OR activation_included_count + activation_excluded_count = candidate_count
        ),
    ADD CONSTRAINT extraction_batches_empty_activation_consent
        CHECK (
            activation_vector_sha256 IS NULL
            OR (
                (candidate_count = 0 AND accepted_empty = true)
                OR (candidate_count > 0 AND accepted_empty = false)
            )
        ),
    ADD CONSTRAINT extraction_batches_exclusion_activation_consent
        CHECK (
            activation_vector_sha256 IS NULL
            OR (
                (activation_excluded_count > 0 AND accepted_exclusions = true)
                OR (activation_excluded_count = 0 AND accepted_exclusions = false)
            )
        );

CREATE INDEX extraction_batches_activated_at_idx
    ON extraction_batches (activated_at DESC)
    WHERE activation_vector_sha256 IS NOT NULL;

CREATE OR REPLACE FUNCTION enforce_extraction_batch_activation_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.lifecycle = 'active' AND NEW.activation_vector_sha256 IS NULL THEN
        RAISE EXCEPTION 'new active batches require complete activation evidence'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.lifecycle <> 'active' AND NEW.activation_vector_sha256 IS NOT NULL THEN
        RAISE EXCEPTION 'activation evidence may only be inserted with an active batch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER extraction_batches_activation_insert_guard
    BEFORE INSERT ON extraction_batches
    FOR EACH ROW EXECUTE FUNCTION enforce_extraction_batch_activation_insert();

-- ---------------------------------------------------------------------------
-- One append-only, exact-version decision chain per batch candidate
-- ---------------------------------------------------------------------------

ALTER TABLE extracted_records
    ADD CONSTRAINT extracted_records_id_batch_id_key UNIQUE (id, batch_id);

CREATE TABLE candidate_review_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL
        REFERENCES extraction_batches(id) ON DELETE RESTRICT,
    extraction_id UUID NOT NULL,
    decision_revision INTEGER NOT NULL CHECK (decision_revision > 0),
    expected_extraction_version INTEGER NOT NULL
        CHECK (expected_extraction_version > 0),
    supersedes_decision_id UUID,
    action TEXT NOT NULL CHECK (action IN ('include', 'exclude')),
    corrected_payload JSONB
        CHECK (
            corrected_payload IS NULL
            OR (
                jsonb_typeof(corrected_payload) = 'object'
                AND pg_column_size(corrected_payload) <= 65536
            )
        ),
    corrected_financial_subtype TEXT
        CHECK (
            corrected_financial_subtype IS NULL
            OR corrected_financial_subtype IN (
                'transaction', 'receipt', 'invoice', 'bill', 'recurring_bill',
                'quote', 'other_financial'
            )
        ),
    exclusion_reason TEXT
        CHECK (
            exclusion_reason IS NULL
            OR length(btrim(exclusion_reason)) BETWEEN 1 AND 2048
        ),
    actor TEXT NOT NULL CHECK (length(btrim(actor)) BETWEEN 1 AND 255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT candidate_review_decisions_exact_identity_key
        UNIQUE (id, batch_id, extraction_id),
    CONSTRAINT candidate_review_decisions_linear_revision_key
        UNIQUE (batch_id, extraction_id, decision_revision),
    CONSTRAINT candidate_review_decisions_one_successor_key
        UNIQUE (supersedes_decision_id),
    CONSTRAINT candidate_review_decisions_exact_candidate_fkey
        FOREIGN KEY (extraction_id, batch_id)
        REFERENCES extracted_records (id, batch_id)
        ON DELETE RESTRICT,
    CONSTRAINT candidate_review_decisions_exact_predecessor_fkey
        FOREIGN KEY (supersedes_decision_id, batch_id, extraction_id)
        REFERENCES candidate_review_decisions (id, batch_id, extraction_id)
        ON DELETE RESTRICT,
    CONSTRAINT candidate_review_decisions_action_payload_shape
        CHECK (
            (action = 'include' AND exclusion_reason IS NULL)
            OR
            (
                action = 'exclude'
                AND corrected_payload IS NULL
                AND corrected_financial_subtype IS NULL
                AND exclusion_reason IS NOT NULL
            )
        ),
    CONSTRAINT candidate_review_decisions_predecessor_shape
        CHECK (
            (decision_revision = 1 AND supersedes_decision_id IS NULL)
            OR
            (decision_revision > 1 AND supersedes_decision_id IS NOT NULL)
        )
);

CREATE INDEX candidate_review_decisions_latest_idx
    ON candidate_review_decisions (
        batch_id, extraction_id, decision_revision DESC
    );

CREATE INDEX candidate_review_decisions_batch_created_idx
    ON candidate_review_decisions (batch_id, created_at, id);

CREATE OR REPLACE FUNCTION enforce_candidate_review_decision_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    candidate_version INTEGER;
    candidate_record_kind TEXT;
    candidate_status TEXT;
    batch_lifecycle TEXT;
    latest_decision_id UUID;
    latest_revision INTEGER;
BEGIN
    SELECT
        candidate.version,
        candidate.record_kind,
        candidate.status,
        batch.lifecycle
      INTO
        candidate_version,
        candidate_record_kind,
        candidate_status,
        batch_lifecycle
      FROM extracted_records AS candidate
      JOIN extraction_batches AS batch
        ON batch.id = candidate.batch_id
     WHERE candidate.id = NEW.extraction_id
       AND candidate.batch_id = NEW.batch_id
     FOR UPDATE OF candidate, batch;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'decision must bind one exact batch candidate'
            USING ERRCODE = '23503';
    END IF;

    IF candidate_version <> NEW.expected_extraction_version THEN
        RAISE EXCEPTION 'candidate extraction version is stale'
            USING ERRCODE = '40001';
    END IF;

    IF candidate_status <> 'pending_review' THEN
        RAISE EXCEPTION 'only pending candidates accept staged decisions'
            USING ERRCODE = '23514';
    END IF;

    IF batch_lifecycle NOT IN ('open', 'ready_to_activate') THEN
        RAISE EXCEPTION 'batch lifecycle does not accept staged decisions'
            USING ERRCODE = '23514';
    END IF;

    IF candidate_record_kind = 'generic_document'
       AND (
           NEW.corrected_payload IS NOT NULL
           OR NEW.corrected_financial_subtype IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'generic candidates cannot carry financial corrections'
            USING ERRCODE = '23514';
    END IF;

    SELECT decision.id, decision.decision_revision
      INTO latest_decision_id, latest_revision
      FROM candidate_review_decisions AS decision
     WHERE decision.batch_id = NEW.batch_id
       AND decision.extraction_id = NEW.extraction_id
     ORDER BY decision.decision_revision DESC
     LIMIT 1
     FOR UPDATE;

    IF latest_decision_id IS NULL THEN
        IF NEW.decision_revision <> 1 OR NEW.supersedes_decision_id IS NOT NULL THEN
            RAISE EXCEPTION 'first candidate decision must be revision one'
                USING ERRCODE = '40001';
        END IF;
    ELSIF NEW.decision_revision <> latest_revision + 1
       OR NEW.supersedes_decision_id IS DISTINCT FROM latest_decision_id THEN
        RAISE EXCEPTION 'candidate decision revision is stale or would fork history'
            USING ERRCODE = '40001';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER candidate_review_decisions_insert_guard
    BEFORE INSERT ON candidate_review_decisions
    FOR EACH ROW EXECUTE FUNCTION enforce_candidate_review_decision_insert();

CREATE OR REPLACE FUNCTION reject_candidate_review_decision_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'candidate review decisions are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER candidate_review_decisions_immutable
    BEFORE UPDATE OR DELETE ON candidate_review_decisions
    FOR EACH ROW EXECUTE FUNCTION reject_candidate_review_decision_mutation();

CREATE OR REPLACE FUNCTION audit_candidate_review_decision_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    correction_sha256 TEXT;
    reason_sha256 TEXT;
BEGIN
    correction_sha256 := CASE
        WHEN NEW.corrected_payload IS NULL THEN NULL
        ELSE encode(digest(NEW.corrected_payload::text, 'sha256'), 'hex')
    END;
    reason_sha256 := CASE
        WHEN NEW.exclusion_reason IS NULL THEN NULL
        ELSE encode(digest(NEW.exclusion_reason, 'sha256'), 'hex')
    END;

    PERFORM public.write_audit_event(
        'candidate_review_decisions', NEW.id::text, 'INSERT', 'decision_evidence',
        'null'::jsonb,
        jsonb_build_object(
            'batch_id', NEW.batch_id,
            'extraction_id', NEW.extraction_id,
            'decision_revision', NEW.decision_revision,
            'expected_extraction_version', NEW.expected_extraction_version,
            'supersedes_decision_id', NEW.supersedes_decision_id,
            'action', NEW.action,
            'corrected_financial_subtype', NEW.corrected_financial_subtype,
            'correction_sha256', correction_sha256,
            'reason_sha256', reason_sha256,
            'actor', NEW.actor
        )
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER candidate_review_decisions_insert_audit
    AFTER INSERT ON candidate_review_decisions
    FOR EACH ROW EXECUTE FUNCTION audit_candidate_review_decision_insert();

-- A verified financial row may be written in any order inside activation, but
-- the complete transaction must end with its exact candidate approved and its
-- cohort active. The deferred guard therefore permits atomic multi-step writes
-- without permitting a staged or non-active candidate to become authoritative.
CREATE OR REPLACE FUNCTION enforce_verified_record_active_candidate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    candidate_status TEXT;
    candidate_record_kind TEXT;
    batch_lifecycle TEXT;
BEGIN
    SELECT candidate.status, candidate.record_kind, batch.lifecycle
      INTO candidate_status, candidate_record_kind, batch_lifecycle
      FROM extracted_records AS candidate
      JOIN extraction_batches AS batch ON batch.id = candidate.batch_id
     WHERE candidate.id = NEW.extracted_id
       AND candidate.document_id = NEW.document_id;

    IF NOT FOUND
       OR candidate_status <> 'approved'
       OR candidate_record_kind <> 'financial'
       OR batch_lifecycle <> 'active' THEN
        RAISE EXCEPTION 'verified records require one active approved financial candidate'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER verified_records_active_candidate_guard
    AFTER INSERT ON verified_records
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_verified_record_active_candidate();

-- ---------------------------------------------------------------------------
-- Exact source or exact candidate scope for non-destructive duplicate evidence
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS duplicate_flags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    suspected_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    score NUMERIC(6, 4) NOT NULL CHECK (score >= 0 AND score <= 1),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(evidence) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT duplicate_flags_distinct_documents
        CHECK (document_id <> suspected_document_id)
);

ALTER TABLE duplicate_flags
    DROP CONSTRAINT IF EXISTS duplicate_flags_document_suspect_key,
    ADD COLUMN source_file_id UUID,
    ADD COLUMN source_version INTEGER,
    ADD COLUMN batch_id UUID,
    ADD COLUMN extraction_id UUID,
    ADD COLUMN candidate_key TEXT,
    ADD COLUMN record_kind TEXT,
    ADD CONSTRAINT duplicate_flags_scope_shape
        CHECK (
            (
                source_file_id IS NULL
                AND source_version IS NULL
                AND batch_id IS NULL
                AND extraction_id IS NULL
                AND candidate_key IS NULL
                AND record_kind IS NULL
            )
            OR
            (
                source_file_id IS NOT NULL
                AND source_version IS NOT NULL
                AND batch_id IS NULL
                AND extraction_id IS NULL
                AND candidate_key IS NULL
                AND record_kind IS NULL
            )
            OR
            (
                source_file_id IS NOT NULL
                AND source_version IS NOT NULL
                AND batch_id IS NOT NULL
                AND extraction_id IS NOT NULL
                AND candidate_key IS NOT NULL
                AND record_kind IS NOT NULL
            )
        ),
    ADD CONSTRAINT duplicate_flags_source_version_positive
        CHECK (source_version IS NULL OR source_version > 0),
    ADD CONSTRAINT duplicate_flags_candidate_key_check
        CHECK (candidate_key IS NULL OR candidate_key ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT duplicate_flags_record_kind_check
        CHECK (record_kind IS NULL OR record_kind IN ('financial', 'generic_document')),
    ADD CONSTRAINT duplicate_flags_exact_source_fkey
        FOREIGN KEY (source_file_id, document_id, source_version)
        REFERENCES document_files (id, document_id, version)
        ON DELETE RESTRICT,
    ADD CONSTRAINT duplicate_flags_exact_candidate_fkey
        FOREIGN KEY (
            extraction_id, batch_id, document_id, candidate_key,
            record_kind, source_file_id, source_version
        ) REFERENCES extracted_records (
            id, batch_id, document_id, candidate_key,
            record_kind, source_file_id, source_version
        ) ON DELETE RESTRICT;

CREATE UNIQUE INDEX duplicate_flags_document_scope_key
    ON duplicate_flags (document_id, suspected_document_id)
    WHERE source_file_id IS NULL;

CREATE UNIQUE INDEX duplicate_flags_source_scope_key
    ON duplicate_flags (
        document_id, suspected_document_id, source_file_id, source_version
    )
    WHERE source_file_id IS NOT NULL AND batch_id IS NULL;

CREATE UNIQUE INDEX duplicate_flags_candidate_scope_key
    ON duplicate_flags (
        document_id, suspected_document_id, batch_id, extraction_id
    )
    WHERE batch_id IS NOT NULL;

CREATE INDEX duplicate_flags_candidate_lookup_idx
    ON duplicate_flags (batch_id, extraction_id, created_at DESC)
    WHERE batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS duplicate_flags_document_idx
    ON duplicate_flags (document_id, created_at DESC);

CREATE OR REPLACE FUNCTION enforce_duplicate_flag_scope_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.source_file_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
             FROM document_files AS source
            WHERE source.id = NEW.source_file_id
              AND source.document_id = NEW.document_id
              AND source.version = NEW.source_version
              AND source.kind = 'original'
       ) THEN
        RAISE EXCEPTION 'duplicate evidence must bind the exact original source'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER duplicate_flags_scope_insert_guard
    BEFORE INSERT ON duplicate_flags
    FOR EACH ROW EXECUTE FUNCTION enforce_duplicate_flag_scope_insert();

CREATE OR REPLACE FUNCTION enforce_duplicate_flag_scope_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        OLD.document_id, OLD.suspected_document_id,
        OLD.source_file_id, OLD.source_version,
        OLD.batch_id, OLD.extraction_id, OLD.candidate_key, OLD.record_kind
    ) IS DISTINCT FROM ROW(
        NEW.document_id, NEW.suspected_document_id,
        NEW.source_file_id, NEW.source_version,
        NEW.batch_id, NEW.extraction_id, NEW.candidate_key, NEW.record_kind
    ) THEN
        RAISE EXCEPTION 'duplicate evidence scope is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER duplicate_flags_scope_immutable
    BEFORE UPDATE ON duplicate_flags
    FOR EACH ROW EXECUTE FUNCTION enforce_duplicate_flag_scope_immutability();

CREATE OR REPLACE FUNCTION audit_duplicate_flag_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    evidence_sha256 TEXT;
    reason_sha256 TEXT;
BEGIN
    evidence_sha256 := encode(digest(NEW.evidence::text, 'sha256'), 'hex');
    reason_sha256 := encode(digest(NEW.reason, 'sha256'), 'hex');
    PERFORM public.write_audit_event(
        'duplicate_flags', NEW.id::text, 'INSERT', 'scope_evidence',
        'null'::jsonb,
        jsonb_build_object(
            'document_id', NEW.document_id,
            'suspected_document_id', NEW.suspected_document_id,
            'source_file_id', NEW.source_file_id,
            'source_version', NEW.source_version,
            'batch_id', NEW.batch_id,
            'extraction_id', NEW.extraction_id,
            'candidate_key', NEW.candidate_key,
            'record_kind', NEW.record_kind,
            'reason_sha256', reason_sha256,
            'score', NEW.score,
            'evidence_sha256', evidence_sha256
        )
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER duplicate_flags_insert_audit
    AFTER INSERT ON duplicate_flags
    FOR EACH ROW EXECUTE FUNCTION audit_duplicate_flag_insert();

-- ---------------------------------------------------------------------------
-- Batch lifecycle and activation evidence remain one optimistic mutation
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_extraction_batch_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'extraction batches are retained authority evidence'
            USING ERRCODE = '55000';
    END IF;

    IF ROW(
        OLD.source_intake_id, OLD.document_id, OLD.source_file_id, OLD.source_version,
        OLD.source_sha256, OLD.normalized_sha256, OLD.structure_fingerprint,
        OLD.mapping_set_id, OLD.mapping_set_version, OLD.mapping_set_digest,
        OLD.producer, OLD.producer_version, OLD.origin, OLD.intake_intent,
        OLD.idempotency_key, OLD.producer_job_id, OLD.candidate_count,
        OLD.reconciliation_counts, OLD.reconciliation_digest, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.source_intake_id, NEW.document_id, NEW.source_file_id, NEW.source_version,
        NEW.source_sha256, NEW.normalized_sha256, NEW.structure_fingerprint,
        NEW.mapping_set_id, NEW.mapping_set_version, NEW.mapping_set_digest,
        NEW.producer, NEW.producer_version, NEW.origin, NEW.intake_intent,
        NEW.idempotency_key, NEW.producer_job_id, NEW.candidate_count,
        NEW.reconciliation_counts, NEW.reconciliation_digest, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'extraction batch identity and membership are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.activation_vector_sha256 IS NOT NULL
       AND ROW(
           OLD.activation_vector_sha256, OLD.activated_by, OLD.activated_at,
           OLD.activation_included_count, OLD.activation_excluded_count,
           OLD.accepted_exclusions, OLD.accepted_empty
       ) IS DISTINCT FROM ROW(
           NEW.activation_vector_sha256, NEW.activated_by, NEW.activated_at,
           NEW.activation_included_count, NEW.activation_excluded_count,
           NEW.accepted_exclusions, NEW.accepted_empty
       ) THEN
        RAISE EXCEPTION 'activation evidence is immutable once recorded'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.activation_vector_sha256 IS NULL
       AND NEW.activation_vector_sha256 IS NOT NULL
       AND NOT (OLD.lifecycle <> 'active' AND NEW.lifecycle = 'active') THEN
        RAISE EXCEPTION 'activation evidence may only be recorded during activation'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
       AND NOT (
           (OLD.lifecycle = 'open' AND NEW.lifecycle IN (
               'ready_to_activate', 'active', 'superseded', 'rejected'
           ))
           OR
           (OLD.lifecycle = 'ready_to_activate' AND NEW.lifecycle IN (
               'open', 'active', 'superseded', 'rejected'
           ))
           OR
           (OLD.lifecycle = 'active' AND NEW.lifecycle = 'superseded')
       ) THEN
        RAISE EXCEPTION 'invalid extraction batch lifecycle transition'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.lifecycle <> 'active'
       AND NEW.lifecycle = 'active'
       AND NEW.activation_vector_sha256 IS NULL THEN
        RAISE EXCEPTION 'new active batches require complete activation evidence'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'extraction batch mutation requires the next optimistic version'
            USING ERRCODE = '40001';
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION audit_extraction_batch_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM public.write_audit_event(
            'extraction_batches', NEW.id::text, 'INSERT', 'source_identity',
            'null'::jsonb,
            jsonb_build_object(
                'source_intake_id', NEW.source_intake_id,
                'source_file_id', NEW.source_file_id,
                'source_version', NEW.source_version,
                'source_sha256', NEW.source_sha256,
                'candidate_count', NEW.candidate_count,
                'lifecycle', NEW.lifecycle
            )
        );
        RETURN NEW;
    END IF;

    IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle THEN
        PERFORM public.write_audit_event(
            'extraction_batches', NEW.id::text, 'UPDATE', 'lifecycle',
            public.audit_json(OLD.lifecycle), public.audit_json(NEW.lifecycle)
        );
    END IF;
    IF NEW.version IS DISTINCT FROM OLD.version THEN
        PERFORM public.write_audit_event(
            'extraction_batches', NEW.id::text, 'UPDATE', 'version',
            public.audit_json(OLD.version), public.audit_json(NEW.version)
        );
    END IF;
    IF NEW.activation_vector_sha256 IS DISTINCT FROM OLD.activation_vector_sha256 THEN
        PERFORM public.write_audit_event(
            'extraction_batches', NEW.id::text, 'UPDATE', 'activation_evidence',
            'null'::jsonb,
            jsonb_build_object(
                'activation_vector_sha256', NEW.activation_vector_sha256,
                'activated_by', NEW.activated_by,
                'activated_at', NEW.activated_at,
                'included_count', NEW.activation_included_count,
                'excluded_count', NEW.activation_excluded_count,
                'accepted_exclusions', NEW.accepted_exclusions,
                'accepted_empty', NEW.accepted_empty
            )
        );
    END IF;
    RETURN NEW;
END;
$$;
