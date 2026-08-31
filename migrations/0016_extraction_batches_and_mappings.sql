-- Add immutable schema mappings and exact-source extraction cohorts.
-- The migration runner has already frozen and verified 0015 before this file runs.
-- Legacy reconciliation below reads relational evidence only; it never opens originals,
-- invents spreadsheet mappings, or changes verified financial values.

-- ---------------------------------------------------------------------------
-- Exact source keys shared by mappings and batches
-- ---------------------------------------------------------------------------

ALTER TABLE source_intakes
    ADD CONSTRAINT source_intakes_id_source_identity_key
        UNIQUE (id, document_id, source_file_id, source_version, source_sha256);

-- ---------------------------------------------------------------------------
-- Immutable mapping contracts and complete source-bound mapping sets
-- ---------------------------------------------------------------------------

CREATE TABLE schema_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    table_locator TEXT NOT NULL
        CHECK (length(table_locator) BETWEEN 1 AND 1024),
    schema_fingerprint TEXT NOT NULL
        CHECK (schema_fingerprint ~ '^[0-9a-f]{64}$'),
    record_kind TEXT NOT NULL
        CHECK (record_kind IN ('financial', 'generic_document')),
    financial_subtype TEXT
        CHECK (financial_subtype IS NULL OR financial_subtype IN (
            'transaction', 'receipt', 'invoice', 'bill', 'recurring_bill',
            'quote', 'other_financial'
        )),
    field_rules JSONB NOT NULL
        CHECK (jsonb_typeof(field_rules) = 'object')
        CHECK (pg_column_size(field_rules) <= 262144),
    required_fields JSONB NOT NULL
        CHECK (jsonb_typeof(required_fields) = 'array')
        CHECK (jsonb_array_length(required_fields) <= 64),
    mapping_version INTEGER NOT NULL DEFAULT 1 CHECK (mapping_version > 0),
    mapping_digest TEXT NOT NULL UNIQUE
        CHECK (mapping_digest ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT schema_mappings_financial_subtype_shape
        CHECK (
            (record_kind = 'financial' AND financial_subtype IS NOT NULL)
            OR
            (record_kind = 'generic_document' AND financial_subtype IS NULL)
        ),
    CONSTRAINT schema_mappings_exact_contract_key
        UNIQUE (id, mapping_version, table_locator, schema_fingerprint)
);

CREATE INDEX schema_mappings_schema_idx
    ON schema_mappings (schema_fingerprint, created_at DESC);

CREATE TABLE mapping_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_intake_id UUID NOT NULL,
    document_id UUID NOT NULL,
    source_file_id UUID NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    structure_fingerprint TEXT NOT NULL
        CHECK (structure_fingerprint ~ '^[0-9a-f]{64}$'),
    set_digest TEXT NOT NULL CHECK (set_digest ~ '^[0-9a-f]{64}$'),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mapping_sets_source_intake_digest_key
        UNIQUE (source_intake_id, set_digest),
    CONSTRAINT mapping_sets_exact_identity_key
        UNIQUE (
            id, version, set_digest, source_intake_id, document_id,
            source_file_id, source_version, source_sha256
        ),
    CONSTRAINT mapping_sets_exact_source_fkey
        FOREIGN KEY (
            source_intake_id, document_id, source_file_id, source_version, source_sha256
        ) REFERENCES source_intakes (
            id, document_id, source_file_id, source_version, source_sha256
        ) ON DELETE RESTRICT
);

CREATE INDEX mapping_sets_source_created_idx
    ON mapping_sets (source_intake_id, created_at DESC);

CREATE TABLE mapping_set_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mapping_set_id UUID NOT NULL REFERENCES mapping_sets(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0 AND ordinal < 256),
    table_locator TEXT NOT NULL
        CHECK (length(table_locator) BETWEEN 1 AND 1024),
    schema_fingerprint TEXT NOT NULL
        CHECK (schema_fingerprint ~ '^[0-9a-f]{64}$'),
    mapping_id UUID,
    mapping_version INTEGER CHECK (mapping_version IS NULL OR mapping_version > 0),
    ignore_reason TEXT
        CHECK (ignore_reason IS NULL OR length(ignore_reason) BETWEEN 1 AND 2048),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mapping_set_entries_set_ordinal_key
        UNIQUE (mapping_set_id, ordinal),
    CONSTRAINT mapping_set_entries_set_locator_key
        UNIQUE (mapping_set_id, table_locator),
    CONSTRAINT mapping_set_entries_mapping_xor_ignore
        CHECK (
            (
                mapping_id IS NOT NULL
                AND mapping_version IS NOT NULL
                AND ignore_reason IS NULL
            )
            OR
            (
                mapping_id IS NULL
                AND mapping_version IS NULL
                AND ignore_reason IS NOT NULL
            )
        ),
    CONSTRAINT mapping_set_entries_exact_mapping_fkey
        FOREIGN KEY (mapping_id, mapping_version, table_locator, schema_fingerprint)
        REFERENCES schema_mappings (
            id, mapping_version, table_locator, schema_fingerprint
        ) ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------------
-- Exact-source immutable extraction cohorts
-- ---------------------------------------------------------------------------

CREATE TABLE extraction_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_intake_id UUID NOT NULL,
    document_id UUID NOT NULL,
    source_file_id UUID NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    normalized_sha256 TEXT NOT NULL CHECK (normalized_sha256 ~ '^[0-9a-f]{64}$'),
    structure_fingerprint TEXT NOT NULL
        CHECK (structure_fingerprint ~ '^[0-9a-f]{64}$'),
    mapping_set_id UUID,
    mapping_set_version INTEGER CHECK (
        mapping_set_version IS NULL OR mapping_set_version > 0
    ),
    mapping_set_digest TEXT CHECK (
        mapping_set_digest IS NULL OR mapping_set_digest ~ '^[0-9a-f]{64}$'
    ),
    producer TEXT NOT NULL CHECK (length(producer) BETWEEN 1 AND 255),
    producer_version TEXT NOT NULL
        CHECK (length(producer_version) BETWEEN 1 AND 255),
    origin TEXT NOT NULL CHECK (length(origin) BETWEEN 1 AND 64),
    intake_intent TEXT NOT NULL
        CHECK (intake_intent IN ('legacy_unspecified', 'generic_file', 'bill_scan')),
    lifecycle TEXT NOT NULL DEFAULT 'open'
        CHECK (lifecycle IN (
            'open', 'ready_to_activate', 'active', 'superseded', 'rejected'
        )),
    idempotency_key TEXT NOT NULL
        CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    producer_job_id UUID REFERENCES jobs(id) ON DELETE RESTRICT,
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    reconciliation_counts JSONB NOT NULL,
    reconciliation_digest TEXT NOT NULL
        CHECK (reconciliation_digest ~ '^[0-9a-f]{64}$'),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT extraction_batches_id_document_id_key
        UNIQUE (id, document_id),
    CONSTRAINT extraction_batches_candidate_source_key
        UNIQUE (id, document_id, source_file_id, source_version),
    CONSTRAINT extraction_batches_source_idempotency_key
        UNIQUE (source_intake_id, idempotency_key),
    CONSTRAINT extraction_batches_mapping_set_identity_complete
        CHECK (
            (
                mapping_set_id IS NULL
                AND mapping_set_version IS NULL
                AND mapping_set_digest IS NULL
            )
            OR
            (
                mapping_set_id IS NOT NULL
                AND mapping_set_version IS NOT NULL
                AND mapping_set_digest IS NOT NULL
            )
        ),
    CONSTRAINT extraction_batches_reconciliation_counts_shape
        CHECK (
            jsonb_typeof(reconciliation_counts) = 'object'
            AND reconciliation_counts ?&
                ARRAY[
                    'mapped_candidate', 'residual_generic_candidate',
                    'explicit_ignore', 'blank', 'parse_error'
                ]
            AND reconciliation_counts -
                ARRAY[
                    'mapped_candidate', 'residual_generic_candidate',
                    'explicit_ignore', 'blank', 'parse_error'
                ]::text[]
                = '{}'::jsonb
            AND CASE WHEN
                jsonb_typeof(reconciliation_counts -> 'mapped_candidate') = 'number'
                AND jsonb_typeof(
                    reconciliation_counts -> 'residual_generic_candidate'
                ) = 'number'
                AND jsonb_typeof(reconciliation_counts -> 'explicit_ignore') = 'number'
                AND jsonb_typeof(reconciliation_counts -> 'blank') = 'number'
                AND jsonb_typeof(reconciliation_counts -> 'parse_error') = 'number'
                AND reconciliation_counts ->> 'mapped_candidate' ~ '^[0-9]+$'
                AND reconciliation_counts ->> 'residual_generic_candidate' ~ '^[0-9]+$'
                AND reconciliation_counts ->> 'explicit_ignore' ~ '^[0-9]+$'
                AND reconciliation_counts ->> 'blank' ~ '^[0-9]+$'
                AND reconciliation_counts ->> 'parse_error' ~ '^[0-9]+$'
            THEN candidate_count =
                (reconciliation_counts ->> 'mapped_candidate')::integer
                + (reconciliation_counts ->> 'residual_generic_candidate')::integer
            ELSE false END
            AND pg_column_size(reconciliation_counts) <= 32768
        ),
    CONSTRAINT extraction_batches_exact_source_fkey
        FOREIGN KEY (
            source_intake_id, document_id, source_file_id, source_version, source_sha256
        ) REFERENCES source_intakes (
            id, document_id, source_file_id, source_version, source_sha256
        ) ON DELETE RESTRICT,
    CONSTRAINT extraction_batches_exact_mapping_set_fkey
        FOREIGN KEY (
            mapping_set_id, mapping_set_version, mapping_set_digest,
            source_intake_id, document_id, source_file_id, source_version, source_sha256
        ) REFERENCES mapping_sets (
            id, version, set_digest,
            source_intake_id, document_id, source_file_id, source_version, source_sha256
        ) ON DELETE RESTRICT
);

CREATE INDEX extraction_batches_source_created_idx
    ON extraction_batches (source_intake_id, created_at DESC);

CREATE UNIQUE INDEX extraction_batches_one_active_document_key
    ON extraction_batches (document_id)
    WHERE lifecycle = 'active';

-- Candidate and chunk columns stay nullable during the staged rollout so the
-- still-compatible singular writer can continue until the Phase 3 cutover.
ALTER TABLE extracted_records
    ADD COLUMN batch_id UUID,
    ADD COLUMN candidate_ordinal INTEGER,
    ADD COLUMN candidate_key TEXT,
    ADD COLUMN record_kind TEXT,
    ADD COLUMN financial_subtype TEXT,
    ADD COLUMN source_locator TEXT,
    ADD COLUMN row_fingerprint TEXT,
    ADD COLUMN validation_issues JSONB,
    ADD COLUMN evidence_group_keys JSONB;

ALTER TABLE chunks
    ADD COLUMN batch_id UUID,
    ADD COLUMN extraction_id UUID,
    ADD COLUMN record_kind TEXT,
    ADD COLUMN source_file_id UUID,
    ADD COLUMN source_version INTEGER,
    ADD COLUMN candidate_key TEXT;

-- ---------------------------------------------------------------------------
-- Deterministic legacy authority preflight and singleton backfill
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    ambiguous_approved_documents INTEGER;
    ambiguous_verified_authorities INTEGER;
    approved_without_verified INTEGER;
    ambiguous_current_pending INTEGER;
    missing_exact_intakes INTEGER;
BEGIN
    SELECT count(*) INTO ambiguous_approved_documents
      FROM (
          SELECT document_id
            FROM extracted_records
           WHERE status = 'approved'
           GROUP BY document_id
          HAVING count(*) > 1
      ) AS conflicts;

    SELECT count(*) INTO ambiguous_verified_authorities
      FROM (
          SELECT extraction.document_id
            FROM extracted_records AS extraction
            JOIN verified_records AS verified
              ON verified.extracted_id = extraction.id
             AND verified.document_id = extraction.document_id
           WHERE extraction.status = 'approved'
           GROUP BY extraction.document_id
          HAVING count(*) > 1
      ) AS conflicts;

    SELECT count(*) INTO approved_without_verified
      FROM extracted_records AS extraction
     WHERE extraction.status = 'approved'
       AND NOT EXISTS (
           SELECT 1
             FROM verified_records AS verified
            WHERE verified.extracted_id = extraction.id
              AND verified.document_id = extraction.document_id
       );

    WITH current_sources AS (
        SELECT DISTINCT ON (source.document_id)
            source.document_id,
            source.id AS source_file_id,
            source.version AS source_version
        FROM document_files AS source
        WHERE source.kind = 'original'
        ORDER BY source.document_id, source.version DESC, source.created_at DESC, source.id DESC
    )
    SELECT count(*) INTO ambiguous_current_pending
      FROM (
          SELECT extraction.document_id
            FROM extracted_records AS extraction
            JOIN current_sources AS source
              ON source.document_id = extraction.document_id
             AND source.source_file_id = extraction.source_file_id
             AND source.source_version = extraction.source_version
           WHERE extraction.status = 'pending_review'
           GROUP BY extraction.document_id
          HAVING count(*) > 1
      ) AS conflicts;

    SELECT count(*) INTO missing_exact_intakes
      FROM extracted_records AS extraction
     WHERE 1 <> (
         SELECT count(*)
           FROM source_intakes AS intake
          WHERE intake.document_id = extraction.document_id
            AND intake.source_file_id = extraction.source_file_id
            AND intake.source_version = extraction.source_version
            AND EXISTS (
                SELECT 1
                  FROM document_files AS source
                 WHERE source.id = extraction.source_file_id
                   AND source.document_id = extraction.document_id
                   AND source.version = extraction.source_version
                   AND source.sha256 = intake.source_sha256
                   AND source.kind = 'original'
            )
     );

    IF ambiguous_approved_documents > 0
       OR ambiguous_verified_authorities > 0
       OR approved_without_verified > 0
       OR ambiguous_current_pending > 0
       OR missing_exact_intakes > 0 THEN
        RAISE EXCEPTION
            'legacy authority reconciliation required '
            '(approved=%, verified=%, approved_without_verified=%, current_pending=%, exact_source=%)',
            ambiguous_approved_documents,
            ambiguous_verified_authorities,
            approved_without_verified,
            ambiguous_current_pending,
            missing_exact_intakes
            USING ERRCODE = '23514';
    END IF;
END;
$$;

WITH current_sources AS (
    SELECT DISTINCT ON (source.document_id)
        source.document_id,
        source.id AS source_file_id,
        source.version AS source_version
    FROM document_files AS source
    WHERE source.kind = 'original'
    ORDER BY source.document_id, source.version DESC, source.created_at DESC, source.id DESC
), legacy_batches AS (
    SELECT
        extraction.id,
        intake.id AS source_intake_id,
        extraction.document_id,
        extraction.source_file_id,
        extraction.source_version,
        intake.source_sha256,
        encode(digest(
            'legacy_unknown_normalized:' || extraction.id::text || ':' || intake.source_sha256,
            'sha256'
        ), 'hex') AS normalized_sha256,
        encode(digest(
            'legacy_unknown_structure:' || extraction.id::text || ':' || intake.source_sha256,
            'sha256'
        ), 'hex') AS structure_fingerprint,
        intake.intake_intent,
        CASE extraction.status
            WHEN 'approved' THEN 'active'
            WHEN 'rejected' THEN 'rejected'
            WHEN 'superseded' THEN 'superseded'
            WHEN 'pending_review' THEN CASE
                WHEN current_source.source_file_id = extraction.source_file_id
                 AND current_source.source_version = extraction.source_version
                THEN 'open'
                ELSE 'superseded'
            END
        END AS lifecycle,
        jsonb_build_object(
            'mapped_candidate', 1,
            'residual_generic_candidate', 0,
            'explicit_ignore', 0,
            'blank', 0,
            'parse_error', 0
        ) AS reconciliation_counts,
        extraction.created_at
    FROM extracted_records AS extraction
    JOIN source_intakes AS intake
      ON intake.document_id = extraction.document_id
     AND intake.source_file_id = extraction.source_file_id
     AND intake.source_version = extraction.source_version
    LEFT JOIN current_sources AS current_source
      ON current_source.document_id = extraction.document_id
)
INSERT INTO extraction_batches (
    id, source_intake_id, document_id, source_file_id, source_version, source_sha256,
    normalized_sha256, structure_fingerprint,
    mapping_set_id, mapping_set_version, mapping_set_digest,
    producer, producer_version, origin, intake_intent, lifecycle,
    idempotency_key, producer_job_id, candidate_count,
    reconciliation_counts, reconciliation_digest, version, created_at, updated_at
)
SELECT
    legacy.id,
    legacy.source_intake_id,
    legacy.document_id,
    legacy.source_file_id,
    legacy.source_version,
    legacy.source_sha256,
    legacy.normalized_sha256,
    legacy.structure_fingerprint,
    NULL,
    NULL,
    NULL,
    'legacy_migration',
    '0016',
    'legacy_singleton',
    legacy.intake_intent,
    legacy.lifecycle,
    'legacy-singleton:' || legacy.id::text,
    NULL,
    1,
    legacy.reconciliation_counts,
    encode(digest(legacy.reconciliation_counts::text, 'sha256'), 'hex'),
    1,
    legacy.created_at,
    legacy.created_at
FROM legacy_batches AS legacy;

WITH legacy_candidate_values AS (
    SELECT
        extraction.id,
        CASE document.document_class
            WHEN 'receipt' THEN 'receipt'
            WHEN 'invoice' THEN 'invoice'
            WHEN 'bill' THEN 'bill'
            WHEN 'recurring_bill' THEN 'recurring_bill'
            WHEN 'quote' THEN 'quote'
            ELSE 'other_financial'
        END AS financial_subtype,
        batch.source_sha256,
        encode(digest(
            'legacy_row:' || extraction.id::text || ':' || extraction.payload::text,
            'sha256'
        ), 'hex') AS row_fingerprint
    FROM extracted_records AS extraction
    JOIN extraction_batches AS batch ON batch.id = extraction.id
    JOIN documents AS document ON document.id = extraction.document_id
), legacy_candidate_keys AS (
    SELECT
        candidate.*,
        encode(digest(
            candidate.source_sha256 || '|legacy_unknown|1|' || candidate.row_fingerprint
            || '|financial|' || candidate.financial_subtype || '|legacy_unknown',
            'sha256'
        ), 'hex') AS candidate_key
    FROM legacy_candidate_values AS candidate
)
UPDATE extracted_records AS extraction
   SET batch_id = extraction.id,
       candidate_ordinal = 1,
       candidate_key = candidate.candidate_key,
       record_kind = 'financial',
       financial_subtype = candidate.financial_subtype,
       source_locator = 'legacy_unknown',
       row_fingerprint = candidate.row_fingerprint,
       validation_issues = '[]'::jsonb,
       evidence_group_keys = '[]'::jsonb
  FROM legacy_candidate_keys AS candidate
 WHERE candidate.id = extraction.id;

DO $$
DECLARE
    unbound_extractions INTEGER;
    mismatched_batches INTEGER;
    invalid_active_bindings INTEGER;
BEGIN
    SELECT count(*) INTO unbound_extractions
      FROM extracted_records
     WHERE batch_id IS NULL;

    SELECT count(*) INTO mismatched_batches
      FROM extraction_batches AS batch
     WHERE batch.candidate_count <> (
         SELECT count(*)
           FROM extracted_records AS extraction
          WHERE extraction.batch_id = batch.id
     );

    SELECT count(*) INTO invalid_active_bindings
      FROM extracted_records AS extraction
      JOIN extraction_batches AS batch ON batch.id = extraction.batch_id
     WHERE (extraction.status = 'approved') IS DISTINCT FROM (batch.lifecycle = 'active');

    IF unbound_extractions > 0
       OR mismatched_batches > 0
       OR invalid_active_bindings > 0 THEN
        RAISE EXCEPTION
            'legacy batch verification failed (unbound=%, counts=%, authority=%)',
            unbound_extractions, mismatched_batches, invalid_active_bindings
            USING ERRCODE = '23514';
    END IF;
END;
$$;

ALTER TABLE extracted_records
    ADD CONSTRAINT extracted_records_exact_batch_source_fkey
        FOREIGN KEY (batch_id, document_id, source_file_id, source_version)
        REFERENCES extraction_batches (id, document_id, source_file_id, source_version)
        ON DELETE RESTRICT
        NOT VALID,
    ADD CONSTRAINT extracted_records_chunk_lineage_key
        UNIQUE (
            id, batch_id, document_id, candidate_key, record_kind,
            source_file_id, source_version
        ),
    ADD CONSTRAINT extracted_records_candidate_ordinal_positive
        CHECK (candidate_ordinal IS NULL OR candidate_ordinal > 0)
        NOT VALID,
    ADD CONSTRAINT extracted_records_candidate_key_check
        CHECK (candidate_key IS NULL OR candidate_key ~ '^[0-9a-f]{64}$')
        NOT VALID,
    ADD CONSTRAINT extracted_records_record_kind_check
        CHECK (record_kind IS NULL OR record_kind IN ('financial', 'generic_document'))
        NOT VALID,
    ADD CONSTRAINT extracted_records_financial_subtype_check
        CHECK (financial_subtype IS NULL OR financial_subtype IN (
            'transaction', 'receipt', 'invoice', 'bill', 'recurring_bill',
            'quote', 'other_financial'
        ))
        NOT VALID,
    ADD CONSTRAINT extracted_records_financial_subtype_shape
        CHECK (
            batch_id IS NULL
            OR (record_kind = 'financial' AND financial_subtype IS NOT NULL)
            OR (record_kind = 'generic_document' AND financial_subtype IS NULL)
        )
        NOT VALID,
    ADD CONSTRAINT extracted_records_source_locator_length
        CHECK (source_locator IS NULL OR length(source_locator) BETWEEN 1 AND 2048)
        NOT VALID,
    ADD CONSTRAINT extracted_records_row_fingerprint_check
        CHECK (row_fingerprint IS NULL OR row_fingerprint ~ '^[0-9a-f]{64}$')
        NOT VALID,
    ADD CONSTRAINT extracted_records_validation_issues_array
        CHECK (
            validation_issues IS NULL
            OR (
                jsonb_typeof(validation_issues) = 'array'
                AND jsonb_array_length(validation_issues) <= 100
                AND pg_column_size(validation_issues) <= 65536
            )
        )
        NOT VALID,
    ADD CONSTRAINT extracted_records_evidence_group_keys_array
        CHECK (
            evidence_group_keys IS NULL
            OR (
                jsonb_typeof(evidence_group_keys) = 'array'
                AND jsonb_array_length(evidence_group_keys) <= 100
                AND pg_column_size(evidence_group_keys) <= 32768
            )
        )
        NOT VALID,
    ADD CONSTRAINT extracted_records_batch_lineage_complete
        CHECK (
            (
                batch_id IS NULL
                AND candidate_ordinal IS NULL
                AND candidate_key IS NULL
                AND record_kind IS NULL
                AND financial_subtype IS NULL
                AND source_locator IS NULL
                AND row_fingerprint IS NULL
                AND validation_issues IS NULL
                AND evidence_group_keys IS NULL
            )
            OR
            (
                batch_id IS NOT NULL
                AND candidate_ordinal IS NOT NULL
                AND candidate_key IS NOT NULL
                AND record_kind IS NOT NULL
                AND source_locator IS NOT NULL
                AND row_fingerprint IS NOT NULL
                AND validation_issues IS NOT NULL
                AND evidence_group_keys IS NOT NULL
            )
        )
        NOT VALID;

ALTER TABLE extracted_records
    VALIDATE CONSTRAINT extracted_records_exact_batch_source_fkey,
    VALIDATE CONSTRAINT extracted_records_candidate_ordinal_positive,
    VALIDATE CONSTRAINT extracted_records_candidate_key_check,
    VALIDATE CONSTRAINT extracted_records_record_kind_check,
    VALIDATE CONSTRAINT extracted_records_financial_subtype_check,
    VALIDATE CONSTRAINT extracted_records_financial_subtype_shape,
    VALIDATE CONSTRAINT extracted_records_source_locator_length,
    VALIDATE CONSTRAINT extracted_records_row_fingerprint_check,
    VALIDATE CONSTRAINT extracted_records_validation_issues_array,
    VALIDATE CONSTRAINT extracted_records_evidence_group_keys_array,
    VALIDATE CONSTRAINT extracted_records_batch_lineage_complete;

CREATE UNIQUE INDEX extracted_records_batch_ordinal_key
    ON extracted_records (batch_id, candidate_ordinal)
    WHERE batch_id IS NOT NULL;

CREATE UNIQUE INDEX extracted_records_batch_candidate_key
    ON extracted_records (batch_id, candidate_key)
    WHERE batch_id IS NOT NULL;

-- Existing chunks describe the previously visible singleton authority. Bind them
-- only when that authority is proven unique; leave all other lineage NULL.
WITH active_singletons AS (
    SELECT
        batch.document_id,
        batch.id AS batch_id,
        extraction.id AS extraction_id,
        extraction.record_kind,
        extraction.source_file_id,
        extraction.source_version,
        extraction.candidate_key,
        count(*) OVER (PARTITION BY batch.document_id) AS authority_count
    FROM extraction_batches AS batch
    JOIN extracted_records AS extraction
      ON extraction.batch_id = batch.id
     AND extraction.document_id = batch.document_id
    WHERE batch.lifecycle = 'active'
      AND extraction.status = 'approved'
)
UPDATE chunks AS chunk
   SET batch_id = authority.batch_id,
       extraction_id = authority.extraction_id,
       record_kind = authority.record_kind,
       source_file_id = authority.source_file_id,
       source_version = authority.source_version,
       candidate_key = authority.candidate_key
  FROM active_singletons AS authority
 WHERE authority.document_id = chunk.document_id
   AND authority.authority_count = 1;

ALTER TABLE chunks
    DROP CONSTRAINT chunks_document_id_seq_key,
    ADD CONSTRAINT chunks_exact_batch_source_fkey
        FOREIGN KEY (batch_id, document_id, source_file_id, source_version)
        REFERENCES extraction_batches (id, document_id, source_file_id, source_version)
        ON DELETE RESTRICT
        NOT VALID,
    ADD CONSTRAINT chunks_exact_candidate_lineage_fkey
        FOREIGN KEY (
            extraction_id, batch_id, document_id, candidate_key,
            record_kind, source_file_id, source_version
        ) REFERENCES extracted_records (
            id, batch_id, document_id, candidate_key,
            record_kind, source_file_id, source_version
        ) ON DELETE RESTRICT
        NOT VALID,
    ADD CONSTRAINT chunks_record_kind_check
        CHECK (record_kind IS NULL OR record_kind IN ('financial', 'generic_document'))
        NOT VALID,
    ADD CONSTRAINT chunks_source_version_positive
        CHECK (source_version IS NULL OR source_version > 0)
        NOT VALID,
    ADD CONSTRAINT chunks_candidate_key_check
        CHECK (candidate_key IS NULL OR candidate_key ~ '^[0-9a-f]{64}$')
        NOT VALID,
    ADD CONSTRAINT chunks_candidate_lineage_complete
        CHECK (
            (
                batch_id IS NULL
                AND extraction_id IS NULL
                AND record_kind IS NULL
                AND source_file_id IS NULL
                AND source_version IS NULL
                AND candidate_key IS NULL
            )
            OR
            (
                batch_id IS NOT NULL
                AND extraction_id IS NOT NULL
                AND record_kind IS NOT NULL
                AND source_file_id IS NOT NULL
                AND source_version IS NOT NULL
                AND candidate_key IS NOT NULL
            )
        )
        NOT VALID;

ALTER TABLE chunks
    VALIDATE CONSTRAINT chunks_exact_batch_source_fkey,
    VALIDATE CONSTRAINT chunks_exact_candidate_lineage_fkey,
    VALIDATE CONSTRAINT chunks_record_kind_check,
    VALIDATE CONSTRAINT chunks_source_version_positive,
    VALIDATE CONSTRAINT chunks_candidate_key_check,
    VALIDATE CONSTRAINT chunks_candidate_lineage_complete;

CREATE UNIQUE INDEX chunks_legacy_document_seq_key
    ON chunks (document_id, seq)
    WHERE batch_id IS NULL;

CREATE UNIQUE INDEX chunks_batch_candidate_seq_key
    ON chunks (batch_id, extraction_id, seq)
    WHERE batch_id IS NOT NULL;

-- spreadsheet_rows is intentionally untouched. Its legacy literal staging is
-- retained as evidence and is never reclassified into candidates or mappings here.

-- ---------------------------------------------------------------------------
-- Immutability, cohort completeness, and auditable lifecycle guards
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION reject_immutable_mapping_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable; create a new version instead', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER schema_mappings_immutable
    BEFORE UPDATE OR DELETE ON schema_mappings
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_mapping_evidence();

CREATE TRIGGER mapping_sets_immutable
    BEFORE UPDATE OR DELETE ON mapping_sets
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_mapping_evidence();

CREATE TRIGGER mapping_set_entries_immutable
    BEFORE UPDATE OR DELETE ON mapping_set_entries
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_mapping_evidence();

CREATE OR REPLACE FUNCTION audit_immutable_mapping_evidence_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    evidence_digest TEXT;
BEGIN
    -- Audit only a digest. Mapping literals, ignore reasons, and source locators
    -- can contain private user values and do not belong in the audit payload.
    evidence_digest := encode(digest(
        (
            to_jsonb(NEW)
            - ARRAY['field_rules', 'required_fields', 'ignore_reason', 'table_locator']::text[]
        )::text,
        'sha256'
    ), 'hex');
    PERFORM public.write_audit_event(
        TG_TABLE_NAME, NEW.id::text, 'INSERT', 'evidence_digest',
        'null'::jsonb, public.audit_json(evidence_digest)
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER schema_mappings_insert_audit
    AFTER INSERT ON schema_mappings
    FOR EACH ROW EXECUTE FUNCTION audit_immutable_mapping_evidence_insert();

CREATE TRIGGER mapping_sets_insert_audit
    AFTER INSERT ON mapping_sets
    FOR EACH ROW EXECUTE FUNCTION audit_immutable_mapping_evidence_insert();

CREATE TRIGGER mapping_set_entries_insert_audit
    AFTER INSERT ON mapping_set_entries
    FOR EACH ROW EXECUTE FUNCTION audit_immutable_mapping_evidence_insert();

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

    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'extraction batch mutation requires the next optimistic version'
            USING ERRCODE = '40001';
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER extraction_batches_mutation_guard
    BEFORE UPDATE OR DELETE ON extraction_batches
    FOR EACH ROW EXECUTE FUNCTION enforce_extraction_batch_mutation();

CREATE OR REPLACE FUNCTION enforce_extraction_batch_candidate_count()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_batch_id UUID;
    expected_count INTEGER;
    actual_count INTEGER;
BEGIN
    IF TG_TABLE_NAME = 'extraction_batches' THEN
        target_batch_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target_batch_id := OLD.batch_id;
    ELSE
        target_batch_id := NEW.batch_id;
    END IF;
    IF target_batch_id IS NOT NULL THEN
        SELECT candidate_count INTO expected_count
          FROM extraction_batches
         WHERE id = target_batch_id;
        IF FOUND THEN
            SELECT count(*) INTO actual_count
              FROM extracted_records
             WHERE batch_id = target_batch_id;
            IF actual_count <> expected_count THEN
                RAISE EXCEPTION 'extraction batch candidate count does not reconcile'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
    END IF;

    IF TG_TABLE_NAME = 'extracted_records' AND TG_OP = 'UPDATE' THEN
        IF OLD.batch_id IS NOT NULL AND OLD.batch_id IS DISTINCT FROM NEW.batch_id THEN
            SELECT candidate_count INTO expected_count
              FROM extraction_batches
             WHERE id = OLD.batch_id;
            IF FOUND THEN
                SELECT count(*) INTO actual_count
                  FROM extracted_records
                 WHERE batch_id = OLD.batch_id;
                IF actual_count <> expected_count THEN
                    RAISE EXCEPTION 'prior extraction batch candidate count does not reconcile'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER extraction_batches_candidate_count_guard
    AFTER INSERT OR UPDATE ON extraction_batches
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_extraction_batch_candidate_count();

CREATE CONSTRAINT TRIGGER extracted_records_candidate_count_guard
    AFTER INSERT OR UPDATE OR DELETE ON extracted_records
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_extraction_batch_candidate_count();

CREATE OR REPLACE FUNCTION reject_bound_extraction_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.batch_id IS NOT NULL THEN
        RAISE EXCEPTION 'batch candidate membership is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER extracted_records_batch_membership_delete_guard
    BEFORE DELETE ON extracted_records
    FOR EACH ROW EXECUTE FUNCTION reject_bound_extraction_delete();

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
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'extraction content is immutable; create a new extraction instead'
            USING ERRCODE = '55000';
    END IF;
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
    RETURN NEW;
END;
$$;

CREATE TRIGGER extraction_batches_lifecycle_audit
    AFTER INSERT OR UPDATE ON extraction_batches
    FOR EACH ROW EXECUTE FUNCTION audit_extraction_batch_lifecycle();
