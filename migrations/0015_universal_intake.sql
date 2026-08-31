-- Add the dark universal-intake persistence contract without activating new formats.
-- Every legacy backfill below uses relational evidence already stored in PostgreSQL;
-- it never opens an original, guesses a format from private bytes, or changes review authority.

-- ---------------------------------------------------------------------------
-- Exact-source derivative lineage
-- ---------------------------------------------------------------------------

ALTER TABLE document_files
    ADD COLUMN source_file_id UUID,
    ADD COLUMN source_version INTEGER,
    ADD COLUMN page_number INTEGER;

ALTER TABLE document_files
    DROP CONSTRAINT document_files_document_id_sha256_key;

ALTER TABLE document_files
    ADD CONSTRAINT document_files_id_document_id_version_key
        UNIQUE (id, document_id, version),
    ADD CONSTRAINT document_files_exact_source_identity_key
        UNIQUE (id, document_id, version, sha256);

-- A derivative can be attributed safely only when the document has exactly one
-- retained original. Multiple-original histories remain immutable and unbound.
ALTER TABLE document_files DISABLE TRIGGER document_files_append_only;

WITH sole_originals AS (
    SELECT
        document_id,
        min(id::text)::uuid AS source_file_id,
        min(version) AS source_version
    FROM document_files
    WHERE kind = 'original'
    GROUP BY document_id
    HAVING count(*) = 1
)
UPDATE document_files AS derivative
   SET source_file_id = source.source_file_id,
       source_version = source.source_version
  FROM sole_originals AS source
 WHERE derivative.document_id = source.document_id
   AND derivative.kind <> 'original'
   AND derivative.source_file_id IS NULL
   AND derivative.source_version IS NULL;

ALTER TABLE document_files ENABLE TRIGGER document_files_append_only;

ALTER TABLE document_files
    ADD CONSTRAINT document_files_source_identity_fkey
        FOREIGN KEY (source_file_id, document_id, source_version)
        REFERENCES document_files (id, document_id, version)
        ON DELETE RESTRICT
        NOT VALID,
    ADD CONSTRAINT document_files_source_version_positive
        CHECK (source_version IS NULL OR source_version > 0)
        NOT VALID,
    ADD CONSTRAINT document_files_source_identity_complete
        CHECK ((source_file_id IS NULL) = (source_version IS NULL))
        NOT VALID,
    ADD CONSTRAINT document_files_page_number_shape
        CHECK (page_number IS NULL OR (kind = 'page_render' AND page_number > 0))
        NOT VALID;

CREATE UNIQUE INDEX document_files_original_sha256_key
    ON document_files (document_id, sha256)
    WHERE kind = 'original';

CREATE UNIQUE INDEX document_files_page_render_slot_key
    ON document_files (source_file_id, source_version, page_number)
    WHERE kind = 'page_render' AND source_file_id IS NOT NULL;

CREATE UNIQUE INDEX document_files_preview_manifest_key
    ON document_files (source_file_id, source_version)
    WHERE mime = 'application/vnd.clerksan.preview-manifest+json'
      AND source_file_id IS NOT NULL;

CREATE OR REPLACE FUNCTION enforce_document_file_source_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    exact_source_kind TEXT;
BEGIN
    IF NEW.kind = 'original' THEN
        IF NEW.source_file_id IS NOT NULL
           OR NEW.source_version IS NOT NULL
           OR NEW.page_number IS NOT NULL THEN
            RAISE EXCEPTION 'original document files cannot carry derivative lineage'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.source_file_id IS NULL OR NEW.source_version IS NULL THEN
        RAISE EXCEPTION 'new derivatives require exact original source lineage'
            USING ERRCODE = '23514';
    END IF;

    SELECT kind
      INTO exact_source_kind
      FROM document_files
     WHERE id = NEW.source_file_id
       AND document_id = NEW.document_id
       AND version = NEW.source_version;

    IF exact_source_kind IS DISTINCT FROM 'original' THEN
        RAISE EXCEPTION 'derivative source must be the exact same-document original version'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.kind = 'page_render' THEN
        IF NEW.page_number IS NULL OR NEW.page_number <= 0 THEN
            RAISE EXCEPTION 'page-render derivatives require a positive page slot'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.page_number IS NOT NULL THEN
        RAISE EXCEPTION 'only page-render derivatives may carry a page slot'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.mime = 'application/vnd.clerksan.preview-manifest+json'
       AND NEW.kind <> 'normalized' THEN
        RAISE EXCEPTION 'preview manifests must be normalized derivatives'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS document_files_source_lineage_guard ON document_files;
CREATE TRIGGER document_files_source_lineage_guard
    BEFORE INSERT ON document_files
    FOR EACH ROW
    EXECUTE FUNCTION enforce_document_file_source_lineage();

-- ---------------------------------------------------------------------------
-- Parser execution evidence and expiring worker capability parity
-- ---------------------------------------------------------------------------

ALTER TABLE jobs
    ADD COLUMN execution_profile TEXT NOT NULL DEFAULT 'legacy_compat',
    ADD COLUMN sandbox_verified BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN registry_digest TEXT,
    ADD COLUMN capabilities_digest TEXT,
    ADD COLUMN requirements_digest TEXT NOT NULL
        DEFAULT '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
    ADD COLUMN required_components JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN intake_intent TEXT NOT NULL DEFAULT 'legacy_unspecified';

ALTER TABLE jobs
    ADD CONSTRAINT jobs_execution_profile_check
        CHECK (execution_profile IN ('legacy_compat', 'universal_sandboxed')),
    ADD CONSTRAINT jobs_execution_sandbox_consistent
        CHECK (
            (execution_profile = 'legacy_compat' AND sandbox_verified = false)
            OR
            (execution_profile = 'universal_sandboxed' AND sandbox_verified = true)
        ),
    ADD CONSTRAINT jobs_registry_digest_check
        CHECK (registry_digest IS NULL OR registry_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT jobs_capabilities_digest_check
        CHECK (capabilities_digest IS NULL OR capabilities_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT jobs_requirements_digest_check
        CHECK (requirements_digest IS NULL OR requirements_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT jobs_required_components_array
        CHECK (jsonb_typeof(required_components) = 'array'),
    ADD CONSTRAINT jobs_intake_intent_check
        CHECK (intake_intent IN ('legacy_unspecified', 'generic_file', 'bill_scan'));

CREATE OR REPLACE FUNCTION enforce_job_execution_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.execution_profile = 'universal_sandboxed'
       AND (NEW.registry_digest IS NULL OR NEW.capabilities_digest IS NULL) THEN
        RAISE EXCEPTION 'universal sandbox jobs require registry and capability digests'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'UPDATE'
       AND (
           OLD.execution_profile IS DISTINCT FROM NEW.execution_profile
           OR OLD.sandbox_verified IS DISTINCT FROM NEW.sandbox_verified
           OR OLD.registry_digest IS DISTINCT FROM NEW.registry_digest
           OR OLD.capabilities_digest IS DISTINCT FROM NEW.capabilities_digest
           OR OLD.requirements_digest IS DISTINCT FROM NEW.requirements_digest
           OR OLD.required_components IS DISTINCT FROM NEW.required_components
           OR OLD.intake_intent IS DISTINCT FROM NEW.intake_intent
       ) THEN
        RAISE EXCEPTION 'job execution evidence is immutable after enqueue'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS jobs_execution_evidence_guard ON jobs;
CREATE TRIGGER jobs_execution_evidence_guard
    BEFORE INSERT OR UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION enforce_job_execution_evidence();

CREATE TABLE worker_capability_leases (
    worker_id TEXT PRIMARY KEY CHECK (btrim(worker_id) <> ''),
    registry_digest TEXT NOT NULL CHECK (registry_digest ~ '^[0-9a-f]{64}$'),
    capabilities_digest TEXT NOT NULL CHECK (capabilities_digest ~ '^[0-9a-f]{64}$'),
    sandbox_verified BOOLEAN NOT NULL DEFAULT false,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT worker_capability_leases_expiry_after_heartbeat
        CHECK (expires_at > heartbeat_at)
);

CREATE INDEX worker_capability_leases_expires_idx
    ON worker_capability_leases (expires_at);

-- ---------------------------------------------------------------------------
-- One audited source intake per exact immutable original
-- ---------------------------------------------------------------------------

CREATE TABLE source_intakes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    source_file_id UUID NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    duplicate_of_document_id UUID REFERENCES documents(id) ON DELETE RESTRICT,
    detected_family TEXT,
    detected_format TEXT,
    canonical_mime TEXT,
    detection_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(detection_evidence) = 'array'),
    policy_version TEXT NOT NULL CHECK (btrim(policy_version) <> ''),
    registry_digest TEXT CHECK (registry_digest IS NULL OR registry_digest ~ '^[0-9a-f]{64}$'),
    capabilities_digest TEXT
        CHECK (capabilities_digest IS NULL OR capabilities_digest ~ '^[0-9a-f]{64}$'),
    requirements_digest TEXT NOT NULL
        DEFAULT '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
        CHECK (requirements_digest ~ '^[0-9a-f]{64}$'),
    required_components JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(required_components) = 'array'),
    intake_intent TEXT NOT NULL DEFAULT 'legacy_unspecified'
        CHECK (intake_intent IN ('legacy_unspecified', 'generic_file', 'bill_scan')),
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN (
            'queued', 'processing', 'processed', 'needs_mapping',
            'stored_unprocessed', 'failed'
        )),
    reason_code TEXT CHECK (reason_code IS NULL OR btrim(reason_code) <> ''),
    retryable BOOLEAN NOT NULL DEFAULT false,
    failure_phase TEXT CHECK (failure_phase IS NULL OR btrim(failure_phase) <> ''),
    execution_profile TEXT NOT NULL DEFAULT 'legacy_compat'
        CHECK (execution_profile IN ('legacy_compat', 'universal_sandboxed')),
    sandbox_verified BOOLEAN NOT NULL DEFAULT false,
    upload_idempotency_key UUID UNIQUE,
    intent_digest TEXT CHECK (intent_digest IS NULL OR intent_digest ~ '^[0-9a-f]{64}$'),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT source_intakes_source_identity_key
        UNIQUE (document_id, source_file_id, source_version, source_sha256),
    CONSTRAINT source_intakes_exact_source_fkey
        FOREIGN KEY (source_file_id, document_id, source_version, source_sha256)
        REFERENCES document_files (id, document_id, version, sha256)
        ON DELETE RESTRICT,
    CONSTRAINT source_intakes_idempotency_binding_complete
        CHECK (
            (upload_idempotency_key IS NULL AND intent_digest IS NULL)
            OR
            (upload_idempotency_key IS NOT NULL AND intent_digest IS NOT NULL)
        ),
    CONSTRAINT source_intakes_execution_sandbox_consistent
        CHECK (
            (execution_profile = 'legacy_compat' AND sandbox_verified = false)
            OR
            (execution_profile = 'universal_sandboxed' AND sandbox_verified = true)
        ),
    CONSTRAINT source_intakes_duplicate_not_self
        CHECK (duplicate_of_document_id IS NULL OR duplicate_of_document_id <> document_id)
);

CREATE INDEX source_intakes_document_created_idx
    ON source_intakes (document_id, created_at DESC);

CREATE INDEX source_intakes_state_updated_idx
    ON source_intakes (state, updated_at DESC);

CREATE TABLE upload_idempotency_reservations (
    upload_idempotency_key UUID PRIMARY KEY,
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    intent_digest TEXT NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{64}$'),
    source_intake_id UUID UNIQUE REFERENCES source_intakes(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION enforce_upload_idempotency_reservation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    bound_key UUID;
    bound_sha256 TEXT;
    bound_intent TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'live upload idempotency reservations cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.upload_idempotency_key IS DISTINCT FROM NEW.upload_idempotency_key
           OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256
           OR OLD.intent_digest IS DISTINCT FROM NEW.intent_digest
           OR OLD.created_at IS DISTINCT FROM NEW.created_at
           OR OLD.source_intake_id IS NOT NULL THEN
            RAISE EXCEPTION 'upload idempotency reservation bindings are immutable'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    IF NEW.source_intake_id IS NOT NULL THEN
        SELECT upload_idempotency_key, source_sha256, intent_digest
          INTO bound_key, bound_sha256, bound_intent
          FROM source_intakes
         WHERE id = NEW.source_intake_id;
        IF bound_key IS DISTINCT FROM NEW.upload_idempotency_key
           OR bound_sha256 IS DISTINCT FROM NEW.source_sha256
           OR bound_intent IS DISTINCT FROM NEW.intent_digest THEN
            RAISE EXCEPTION 'reservation must bind to the exact matching source intake'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS upload_idempotency_reservations_guard
    ON upload_idempotency_reservations;
CREATE TRIGGER upload_idempotency_reservations_guard
    BEFORE INSERT OR UPDATE OR DELETE ON upload_idempotency_reservations
    FOR EACH ROW
    EXECUTE FUNCTION enforce_upload_idempotency_reservation();

CREATE OR REPLACE FUNCTION enforce_source_intake_write()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    exact_source_kind TEXT;
    transition_allowed BOOLEAN;
BEGIN
    SELECT kind
      INTO exact_source_kind
      FROM document_files
     WHERE id = NEW.source_file_id
       AND document_id = NEW.document_id
       AND version = NEW.source_version
       AND sha256 = NEW.source_sha256;

    IF exact_source_kind IS DISTINCT FROM 'original' THEN
        RAISE EXCEPTION 'source intake must reference the exact immutable original identity'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT'
       AND NEW.upload_idempotency_key IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
             FROM upload_idempotency_reservations AS reservation
            WHERE reservation.upload_idempotency_key = NEW.upload_idempotency_key
              AND reservation.source_sha256 = NEW.source_sha256
              AND reservation.intent_digest = NEW.intent_digest
              AND reservation.source_intake_id IS NULL
       ) THEN
        RAISE EXCEPTION 'source intake upload binding requires its matching reservation'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 THEN
            RAISE EXCEPTION 'new source intake version must be one'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF ROW(
        OLD.document_id,
        OLD.source_file_id,
        OLD.source_version,
        OLD.source_sha256,
        OLD.duplicate_of_document_id,
        OLD.upload_idempotency_key,
        OLD.intent_digest,
        OLD.intake_intent,
        OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.document_id,
        NEW.source_file_id,
        NEW.source_version,
        NEW.source_sha256,
        NEW.duplicate_of_document_id,
        NEW.upload_idempotency_key,
        NEW.intent_digest,
        NEW.intake_intent,
        NEW.created_at
    ) THEN
        RAISE EXCEPTION 'source intake identity and upload binding are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.execution_profile = 'universal_sandboxed'
       AND NEW.execution_profile = 'legacy_compat' THEN
        RAISE EXCEPTION 'a sandboxed intake cannot fall back to legacy execution'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'source intake transitions require the next optimistic version'
            USING ERRCODE = '40001';
    END IF;

    transition_allowed := CASE OLD.state
        WHEN 'queued' THEN NEW.state IN (
            'processing', 'processed', 'needs_mapping', 'stored_unprocessed', 'failed'
        )
        WHEN 'processing' THEN NEW.state IN (
            'queued', 'processed', 'needs_mapping', 'stored_unprocessed', 'failed'
        )
        WHEN 'processed' THEN NEW.state = 'queued'
        WHEN 'needs_mapping' THEN NEW.state IN ('queued', 'processing', 'failed')
        WHEN 'stored_unprocessed' THEN NEW.state IN ('queued', 'processing', 'failed')
        WHEN 'failed' THEN NEW.state = 'queued'
        ELSE false
    END;

    IF NOT transition_allowed THEN
        RAISE EXCEPTION 'invalid source intake state transition: % -> %', OLD.state, NEW.state
            USING ERRCODE = '23514';
    END IF;

    IF NEW.state = 'failed' AND COALESCE(btrim(NEW.reason_code), '') = '' THEN
        RAISE EXCEPTION 'failed source intake requires a stable reason code'
            USING ERRCODE = '23514';
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS source_intakes_write_guard ON source_intakes;
CREATE TRIGGER source_intakes_write_guard
    BEFORE INSERT OR UPDATE ON source_intakes
    FOR EACH ROW
    EXECUTE FUNCTION enforce_source_intake_write();

CREATE OR REPLACE FUNCTION reject_source_intake_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'source intake evidence is append-retained and cannot be deleted'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS source_intakes_delete_guard ON source_intakes;
CREATE TRIGGER source_intakes_delete_guard
    BEFORE DELETE ON source_intakes
    FOR EACH ROW
    EXECUTE FUNCTION reject_source_intake_delete();

CREATE OR REPLACE FUNCTION audit_source_intake_write()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'INSERT', 'source_identity',
            'null'::jsonb,
            jsonb_build_object(
                'document_id', NEW.document_id,
                'source_file_id', NEW.source_file_id,
                'source_version', NEW.source_version,
                'source_sha256', NEW.source_sha256
            )
        );
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'INSERT', 'state',
            'null'::jsonb, public.audit_json(NEW.state)
        );
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'INSERT', 'intake_intent',
            'null'::jsonb, public.audit_json(NEW.intake_intent)
        );
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'INSERT', 'execution_profile',
            'null'::jsonb, public.audit_json(NEW.execution_profile)
        );
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'INSERT', 'sandbox_verified',
            'null'::jsonb, public.audit_json(NEW.sandbox_verified)
        );
        RETURN NEW;
    END IF;

    IF NEW.state IS DISTINCT FROM OLD.state THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'state',
            public.audit_json(OLD.state), public.audit_json(NEW.state)
        );
    END IF;
    IF NEW.reason_code IS DISTINCT FROM OLD.reason_code THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'reason_code',
            public.audit_json(OLD.reason_code), public.audit_json(NEW.reason_code)
        );
    END IF;
    IF NEW.retryable IS DISTINCT FROM OLD.retryable THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'retryable',
            public.audit_json(OLD.retryable), public.audit_json(NEW.retryable)
        );
    END IF;
    IF NEW.failure_phase IS DISTINCT FROM OLD.failure_phase THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'failure_phase',
            public.audit_json(OLD.failure_phase), public.audit_json(NEW.failure_phase)
        );
    END IF;
    IF NEW.execution_profile IS DISTINCT FROM OLD.execution_profile THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'execution_profile',
            public.audit_json(OLD.execution_profile), public.audit_json(NEW.execution_profile)
        );
    END IF;
    IF NEW.sandbox_verified IS DISTINCT FROM OLD.sandbox_verified THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'sandbox_verified',
            public.audit_json(OLD.sandbox_verified), public.audit_json(NEW.sandbox_verified)
        );
    END IF;
    IF NEW.registry_digest IS DISTINCT FROM OLD.registry_digest THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'registry_digest',
            public.audit_json(OLD.registry_digest), public.audit_json(NEW.registry_digest)
        );
    END IF;
    IF NEW.capabilities_digest IS DISTINCT FROM OLD.capabilities_digest THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'capabilities_digest',
            public.audit_json(OLD.capabilities_digest), public.audit_json(NEW.capabilities_digest)
        );
    END IF;
    IF NEW.requirements_digest IS DISTINCT FROM OLD.requirements_digest THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'requirements_digest',
            public.audit_json(OLD.requirements_digest), public.audit_json(NEW.requirements_digest)
        );
    END IF;
    IF NEW.required_components IS DISTINCT FROM OLD.required_components THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'required_components',
            OLD.required_components, NEW.required_components
        );
    END IF;
    IF NEW.version IS DISTINCT FROM OLD.version THEN
        PERFORM public.write_audit_event(
            'source_intakes', NEW.id::text, 'UPDATE', 'version',
            public.audit_json(OLD.version), public.audit_json(NEW.version)
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS source_intakes_audit ON source_intakes;
CREATE TRIGGER source_intakes_audit
    AFTER INSERT OR UPDATE ON source_intakes
    FOR EACH ROW
    EXECUTE FUNCTION audit_source_intake_write();

WITH exact_source_evidence AS (
    SELECT
        source.id AS source_file_id,
        source.document_id,
        source.version AS source_version,
        source.sha256 AS source_sha256,
        source.mime AS canonical_mime,
        source.created_at,
        EXISTS (
            SELECT 1
              FROM extracted_records AS extraction
             WHERE extraction.document_id = source.document_id
               AND extraction.source_file_id = source.id
               AND extraction.source_version = source.version
        ) OR EXISTS (
            SELECT 1
              FROM document_files AS derivative
             WHERE derivative.document_id = source.document_id
               AND derivative.kind <> 'original'
               AND derivative.source_file_id = source.id
               AND derivative.source_version = source.version
        ) AS processed_evidence,
        EXISTS (
            SELECT 1
              FROM jobs AS job
             WHERE job.document_id = source.document_id
               AND job.job_type = 'process_document'
               AND job.status IN ('failed', 'dead')
               AND (job.payload ->> 'source_version') ~ '^[1-9][0-9]*$'
               AND (job.payload ->> 'source_version')::integer = source.version
        ) AS failed_evidence
    FROM document_files AS source
    WHERE source.kind = 'original'
)
INSERT INTO source_intakes (
    document_id,
    source_file_id,
    source_version,
    source_sha256,
    canonical_mime,
    detection_evidence,
    policy_version,
    requirements_digest,
    required_components,
    intake_intent,
    state,
    reason_code,
    retryable,
    failure_phase,
    execution_profile,
    sandbox_verified,
    version,
    created_at,
    updated_at
)
SELECT
    evidence.document_id,
    evidence.source_file_id,
    evidence.source_version,
    evidence.source_sha256,
    evidence.canonical_mime,
    '[]'::jsonb,
    'legacy-pre-0015',
    encode(digest('[]', 'sha256'), 'hex'),
    '[]'::jsonb,
    'legacy_unspecified',
    CASE
        WHEN evidence.processed_evidence THEN 'processed'
        WHEN evidence.failed_evidence THEN 'failed'
        ELSE 'stored_unprocessed'
    END,
    CASE
        WHEN evidence.processed_evidence THEN NULL
        WHEN evidence.failed_evidence THEN 'processing_failed'
        ELSE 'legacy_outcome_unavailable'
    END,
    CASE WHEN evidence.failed_evidence AND NOT evidence.processed_evidence THEN true ELSE false END,
    CASE
        WHEN evidence.failed_evidence AND NOT evidence.processed_evidence THEN 'legacy_job'
        ELSE NULL
    END,
    'legacy_compat',
    false,
    1,
    evidence.created_at,
    evidence.created_at
FROM exact_source_evidence AS evidence
ON CONFLICT (document_id, source_file_id, source_version, source_sha256) DO NOTHING;
