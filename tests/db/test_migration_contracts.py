"""Static migration-contract tests that do not require a PostgreSQL service."""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
COMPLIANCE_MATRIX = ROOT / "docs/compliance_denchoho.md"
EXPECTED_COMPLIANCE_REQUIREMENTS = {
    "Determine which preservation regime applies",
    "Preserve original and prove integrity",
    "Prevent undocumented correction/deletion",
    "Search by date, amount, and counterparty",
    "Range and combined-condition search",
    "Link source document to reviewed record",
    "See and export correction/deletion history",
    "Human review before a ledger/export handoff",
    "Accounting-export auditability",
    "Readable display / inspection on demand",
    "Scanner-preservation-specific controls",
    "Backup, restore, retention, and disaster recovery",
    "Operational documentation",
    "Electronic books / 優良な電子帳簿",
    "Product wording and professional sign-off",
}


def read_migration(filename: str) -> str:
    return (MIGRATIONS / filename).read_text(encoding="utf-8")


def normalized_sql(filename: str) -> str:
    return re.sub(r"\s+", " ", read_migration(filename)).lower()


def test_core_schema_preserves_three_tiers_and_artifact_versions() -> None:
    sql = normalized_sql("0001_core_documents.sql")

    assert "create extension if not exists pgcrypto" in sql
    for table_name in ("documents", "document_files", "extracted_records", "verified_records"):
        assert f"create table if not exists {table_name}" in sql

    assert "kind in ('original', 'page_render', 'normalized')" in sql
    assert "unique (document_id, version)" in sql
    assert "unique (document_id, sha256)" in sql
    assert "document_files_one_original_per_document_idx" in sql


def test_raw_files_and_extraction_content_have_database_guards() -> None:
    sql = normalized_sql("0001_core_documents.sql")

    assert "create trigger document_files_append_only" in sql
    assert "before update or delete on document_files" in sql
    assert "reject_document_file_mutation" in sql

    assert "create trigger extracted_records_content_immutable" in sql
    assert "before update on extracted_records" in sql
    for immutable_column in (
        "old.payload is distinct from new.payload",
        "old.field_confidences is distinct from new.field_confidences",
        "old.source_spans is distinct from new.source_spans",
        "old.model_name is distinct from new.model_name",
        "old.prompt_version is distinct from new.prompt_version",
    ):
        assert immutable_column in sql

    for lifecycle_column in ("status", "version", "reviewer", "reviewed_at"):
        assert lifecycle_column in sql


def test_verified_search_and_source_linkage_indexes_are_declared() -> None:
    sql = normalized_sql("0001_core_documents.sql")

    assert "foreign key (extracted_id, document_id)" in sql
    assert "references extracted_records (id, document_id)" in sql
    for index_name in (
        "verified_records_transaction_date_idx",
        "verified_records_total_amount_idx",
        "verified_records_counterparty_idx",
        "verified_records_combined_search_idx",
    ):
        assert index_name in sql
    assert "on verified_records (counterparty, transaction_date, total_amount)" in sql


def test_audit_is_trigger_owned_append_only_and_has_actor_fallback() -> None:
    sql = normalized_sql("0002_audit_log.sql")

    assert "create table if not exists audit_log" in sql
    assert "current_setting('clerksan.actor', true)" in sql
    assert "'db:' || session_user" in sql
    assert "security definer" in sql
    assert sql.count("insert into public.audit_log") == 1
    assert "create trigger audit_log_write_guard" in sql
    assert "before insert or update or delete on audit_log" in sql
    assert "pg_trigger_depth() > 1" in sql
    assert "revoke insert, update, delete on audit_log from public" in sql


def test_audit_triggers_cover_lifecycle_corrections_and_verified_history() -> None:
    sql = normalized_sql("0002_audit_log.sql")

    assert "create trigger extracted_records_lifecycle_audit" in sql
    assert "after update of status, version, reviewer, reviewed_at on extracted_records" in sql
    assert "create trigger verified_records_correction_audit" in sql
    assert "after insert on verified_records" in sql
    assert "extraction_payload_value(source_payload, 'expense_category')" in sql
    assert "create trigger verified_records_update_audit" in sql
    assert "after update on verified_records" in sql
    assert "create trigger verified_records_delete_audit" in sql
    assert "after delete on verified_records" in sql


def test_verified_insert_audit_canonicalizes_extraction_scalars_before_comparing() -> None:
    sql = normalized_sql("0009_audit_verified_record_canonicalization.sql")

    assert "create or replace function audit_verified_record_insert()" in sql
    assert "create or replace function canonical_extraction_date_value" in sql
    assert "create or replace function canonical_extraction_numeric_value" in sql
    assert "create or replace function canonical_extraction_text_value" in sql
    assert "candidate::date" in sql
    assert "candidate::numeric" in sql
    assert "source_transaction_date := public.canonical_extraction_date_value" in sql
    assert "source_total_amount := public.canonical_extraction_numeric_value" in sql
    assert "source_tax_8_amount := public.canonical_extraction_numeric_value" in sql
    assert "source_counterparty := public.canonical_extraction_text_value" in sql
    assert "public.audit_json(new.transaction_date) is distinct from source_transaction_date" in sql
    assert "public.audit_json(new.total_amount) is distinct from source_total_amount" in sql


def test_jobs_are_document_scoped_leased_and_have_a_queued_partial_index() -> None:
    sql = normalized_sql("0003_jobs.sql")

    assert "create table if not exists jobs" in sql
    assert "document_id uuid not null references documents(id)" in sql
    assert "unique (document_id, idempotency_key)" in sql
    for lease_column in (
        "available_at timestamptz",
        "lease_expires_at timestamptz",
        "lease_owner text",
    ):
        assert lease_column in sql
    assert "create index if not exists jobs_claim_queued_idx" in sql
    assert "where status = 'queued'" in sql
    assert "for update skip locked" in sql


def test_chunk_schema_uses_the_exact_d2_embedding_pin_and_hnsw() -> None:
    sql = normalized_sql("0004_chunks_pgvector.sql")

    assert "eval/results/embedding-decision.json" in read_migration("0004_chunks_pgvector.sql")
    assert "create extension if not exists vector" in sql
    assert "create table if not exists chunks" in sql
    assert "embedding vector(768) not null" in sql
    assert "nomic-embed-text:v1.5" in sql
    assert "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f" in sql
    assert "using hnsw (embedding vector_cosine_ops)" in sql


def test_format_staging_keeps_spreadsheet_rows_out_of_chunks_and_media_source_linked() -> None:
    sql = normalized_sql("0006_format_staging.sql")

    assert "create table if not exists embedded_media" in sql
    assert "unique (document_id, sha256)" in sql
    assert "create table if not exists spreadsheet_rows" in sql
    assert "unique (document_id, source_location, row_index)" in sql
    assert "values jsonb not null" in sql
    assert "value_types jsonb not null" in sql


def test_format_derivatives_are_bound_to_the_current_raw_source_version() -> None:
    sql = normalized_sql("0013_source_bound_format_derivatives.sql")

    assert "alter table embedded_media add column source_version integer" in sql
    assert "delete from embedded_media" in sql
    assert "update embedded_media as media" not in sql
    assert "embedded_media_document_source_sha256_key" in sql
    assert "unique (document_id, source_version, sha256)" in sql
    assert "embedded_media_document_source_idx" in sql
    assert "alter table spreadsheet_rows add column source_version integer" in sql
    assert "delete from spreadsheet_rows" in sql
    assert "update spreadsheet_rows as row" not in sql
    assert "spreadsheet_rows_document_source_version_row_key" in sql
    assert "unique (document_id, source_version, source_location, row_index)" in sql
    assert "spreadsheet_rows_document_version_source_idx" in sql
    assert "create temp table format_derivative_rebuild_documents" in sql
    assert "insert into jobs (document_id, job_type, payload, idempotency_key)" in sql
    assert "'rebuild_format_derivatives'" in sql
    assert "'format-derivatives:0013:'" in sql


def test_source_replacements_append_original_versions_and_audit_them() -> None:
    sql = normalized_sql("0008_document_source_versions.sql")

    assert "add column if not exists source_filename text" in sql
    assert "disable trigger document_files_append_only" in sql
    assert "enable trigger document_files_append_only" in sql
    assert "alter column source_filename set not null" in sql
    assert "drop index if exists document_files_one_original_per_document_idx" in sql
    assert "create index if not exists document_files_latest_original_idx" in sql
    assert "where kind = 'original'" in sql
    assert "create trigger document_files_insert_audit" in sql
    assert "create trigger documents_source_filename_audit" in sql


def test_extractions_keep_a_database_enforced_original_version_link() -> None:
    sql = normalized_sql("0010_extraction_source_versions.sql")

    assert "add column source_file_id uuid" in sql
    assert "add column source_version integer" in sql
    assert "foreign key (source_file_id, document_id)" in sql
    assert "references document_files (id, document_id)" in sql
    assert "candidate.kind = 'original'" in sql
    assert "create trigger extracted_records_source_file_guard" in sql
    assert "old.source_file_id is distinct from new.source_file_id" in sql
    assert "old.source_version is distinct from new.source_version" in sql


def test_verified_extraction_has_a_database_uniqueness_backstop() -> None:
    sql = normalized_sql("0011_review_lifecycle_guards.sql")

    assert "alter table verified_records" in sql
    assert "verified_records_extracted_id_key" in sql
    assert "unique (extracted_id)" in sql


def test_universal_intake_migration_follows_the_frozen_expense_foundation() -> None:
    migration_names = sorted(path.name for path in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))

    foundation_index = migration_names.index("0014_expense_document_foundation.sql")
    assert migration_names[foundation_index : foundation_index + 4] == [
        "0014_expense_document_foundation.sql",
        "0015_universal_intake.sql",
        "0016_extraction_batches_and_mappings.sql",
        "0017_candidate_review_decisions.sql",
    ]
    frozen = read_migration("0014_expense_document_foundation.sql").encode("utf-8")
    assert hashlib.sha256(frozen).hexdigest() == (
        "683e416fbc951e44baf853cfce5d090052c09a5ec2af89252524eae25675c2c3"
    )
    frozen_intake = read_migration("0015_universal_intake.sql").encode("utf-8")
    assert hashlib.sha256(frozen_intake).hexdigest() == (
        "5af1193382a45ab15f1bbfabd0267f26b341432afa322ec45db5afed10282baa"
    )
    frozen_batches = read_migration("0016_extraction_batches_and_mappings.sql").encode("utf-8")
    assert hashlib.sha256(frozen_batches).hexdigest() == (
        "1d06da60d60aeba506f9c716e1a553f98d9cd80548ff061afddbcf109023b522"
    )


def test_universal_intake_schema_binds_one_projection_to_one_exact_original() -> None:
    sql = normalized_sql("0015_universal_intake.sql")

    assert "create table source_intakes" in sql
    assert "unique (document_id, source_file_id, source_version, source_sha256)" in sql
    assert (
        "foreign key (source_file_id, document_id, source_version, source_sha256) "
        "references document_files (id, document_id, version, sha256)"
    ) in sql
    assert "upload_idempotency_key uuid unique" in sql
    assert "duplicate_of_document_id uuid references documents(id)" in sql
    assert "intent_digest text" in sql
    assert "version integer not null default 1" in sql
    assert "source_intakes_idempotency_binding_complete" in sql
    assert "create table upload_idempotency_reservations" in sql
    assert "source_intake_id uuid unique references source_intakes(id)" in sql
    assert "source_intakes_write_guard" in sql
    assert "source_intakes_delete_guard" in sql
    assert "source_intakes_audit" in sql
    assert "invalid source intake state transition" in sql


def test_post_batch_migration_restores_rejection_reason_immutability() -> None:
    filename = "0019_restore_extraction_rejection_reason_guard.sql"
    sql = normalized_sql(filename)

    assert hashlib.sha256(read_migration(filename).encode("utf-8")).hexdigest() == (
        "f88747c26747729a3d4bd65145b39501cbc70319e65c8ac1513cc1cdfa78d03b"
    )
    assert "create or replace function reject_extraction_content_mutation()" in sql
    for field in (
        "old.batch_id is distinct from new.batch_id",
        "old.candidate_key is distinct from new.candidate_key",
        "old.rejection_reason is distinct from new.rejection_reason",
    ):
        assert field in sql
    assert "old.rejection_reason is null" in sql
    assert "new.rejection_reason is not null" in sql
    assert "old.status = 'pending_review'" in sql
    assert "new.status = 'rejected'" in sql
    assert "using errcode = '55000'" in sql


def test_universal_intake_adds_exact_derivative_slots_and_legacy_safe_guards() -> None:
    sql = normalized_sql("0015_universal_intake.sql")

    assert "drop constraint document_files_document_id_sha256_key" in sql
    assert "document_files_original_sha256_key" in sql
    assert "on document_files (document_id, sha256) where kind = 'original'" in sql
    assert "document_files_page_render_slot_key" in sql
    assert "on document_files (source_file_id, source_version, page_number)" in sql
    assert "document_files_preview_manifest_key" in sql
    assert "document_files_source_identity_fkey" in sql
    assert "not valid" in sql
    assert "new derivatives require exact original source lineage" in sql
    assert "page-render derivatives require a positive page slot" in sql
    assert "having count(*) = 1" in sql


def test_universal_intake_records_legacy_execution_and_capability_evidence() -> None:
    sql = normalized_sql("0015_universal_intake.sql")

    for column in (
        "execution_profile text not null default 'legacy_compat'",
        "sandbox_verified boolean not null default false",
        "registry_digest text",
        "capabilities_digest text",
        "requirements_digest text not null default",
        "required_components jsonb not null default '[]'::jsonb",
    ):
        assert column in sql
    assert "jobs_execution_evidence_guard" in sql
    assert "job execution evidence is immutable after enqueue" in sql
    assert "create table worker_capability_leases" in sql
    assert "expires_at > heartbeat_at" in sql
    assert "'legacy_compat', false" in sql


def test_universal_intake_persists_immutable_source_and_job_intent() -> None:
    sql = normalized_sql("0015_universal_intake.sql")

    for table_column in (
        "add column intake_intent text not null default 'legacy_unspecified'",
        "intake_intent text not null default 'legacy_unspecified'",
        "jobs_intake_intent_check",
    ):
        assert table_column in sql
    assert "intake_intent in ('legacy_unspecified', 'generic_file', 'bill_scan')" in sql
    assert "old.intake_intent is distinct from new.intake_intent" in sql
    assert "new.intake_intent" in sql
    backfill = sql.split("with exact_source_evidence as", maxsplit=1)[1]
    assert "'legacy_unspecified'" in backfill


def test_universal_intake_backfill_uses_only_existing_relational_evidence() -> None:
    sql = normalized_sql("0015_universal_intake.sql")
    backfill = sql.split("with exact_source_evidence as", maxsplit=1)[1]

    assert "from extracted_records as extraction" in backfill
    assert "from document_files as derivative" in backfill
    assert "job.job_type = 'process_document'" in backfill
    assert "job.status in ('failed', 'dead')" in backfill
    assert "'stored_unprocessed'" in backfill
    assert "'legacy_outcome_unavailable'" in backfill
    for private_byte_reader in (
        "pg_read_file",
        "pg_read_binary_file",
        "lo_get",
        "content_path",
        "copy ",
    ):
        assert private_byte_reader not in backfill


def test_extraction_batch_migration_adds_exact_source_mapping_and_candidate_contracts() -> None:
    sql = normalized_sql("0016_extraction_batches_and_mappings.sql")

    for table_name in (
        "schema_mappings",
        "mapping_sets",
        "mapping_set_entries",
        "extraction_batches",
    ):
        assert f"create table {table_name}" in sql
    assert "source_intakes_id_source_identity_key" in sql
    assert (
        "foreign key ( source_intake_id, document_id, source_file_id, source_version, "
        "source_sha256 ) references source_intakes"
    ) in sql
    assert "mapping_set_entries_mapping_xor_ignore" in sql
    assert "mapping_set_entries_exact_mapping_fkey" in sql
    assert "extraction_batches_exact_mapping_set_fkey" in sql
    assert "extraction_batches_one_active_document_key" in sql
    assert "where lifecycle = 'active'" in sql
    assert "'open', 'ready_to_activate', 'active', 'superseded', 'rejected'" in sql
    assert "'mapped_candidate', 'residual_generic_candidate'" in sql
    assert "candidate_count = (reconciliation_counts ->> 'mapped_candidate')::integer" in sql


def test_extraction_batch_migration_requires_financial_subtypes_and_complete_lineage() -> None:
    sql = normalized_sql("0016_extraction_batches_and_mappings.sql")

    for subtype in (
        "transaction",
        "receipt",
        "invoice",
        "bill",
        "recurring_bill",
        "quote",
        "other_financial",
    ):
        assert f"'{subtype}'" in sql
    assert "record_kind in ('financial', 'generic_document')" in sql
    assert "record_kind = 'financial' and financial_subtype is not null" in sql
    assert "record_kind = 'generic_document' and financial_subtype is null" in sql
    for column in (
        "add column batch_id uuid",
        "add column candidate_ordinal integer",
        "add column candidate_key text",
        "add column record_kind text",
        "add column financial_subtype text",
        "add column source_locator text",
        "add column row_fingerprint text",
        "add column validation_issues jsonb",
        "add column evidence_group_keys jsonb",
    ):
        assert column in sql
    assert "extracted_records_batch_lineage_complete" in sql
    assert "chunks_candidate_lineage_complete" in sql
    assert "chunks_exact_candidate_lineage_fkey" in sql
    assert "chunks_batch_candidate_seq_key" in sql


def test_extraction_batch_legacy_backfill_fails_closed_and_never_infers_rows() -> None:
    sql = normalized_sql("0016_extraction_batches_and_mappings.sql")
    backfill = sql.split("do $$ declare ambiguous_approved_documents", maxsplit=1)[1]

    assert "legacy authority reconciliation required" in backfill
    assert "having count(*) > 1" in backfill
    assert "approved_without_verified" in backfill
    assert "current_pending" in backfill
    assert "missing_exact_intakes" in backfill
    assert "'legacy_singleton'" in backfill
    assert "set batch_id = extraction.id" in backfill
    assert "source_locator = 'legacy_unknown'" in backfill
    assert "when 'other' then 'other_financial'" not in backfill
    assert "else 'other_financial'" in backfill
    assert "where batch.lifecycle = 'active'" in backfill
    assert "authority.authority_count = 1" in backfill
    for private_byte_reader in (
        "pg_read_file",
        "pg_read_binary_file",
        "lo_get",
        "content_path",
        "copy ",
    ):
        assert private_byte_reader not in backfill
    for forbidden_spreadsheet_write in (
        "update spreadsheet_rows",
        "insert into spreadsheet_rows",
        "delete from spreadsheet_rows",
    ):
        assert forbidden_spreadsheet_write not in backfill


def test_extraction_batch_evidence_and_candidate_membership_are_immutable() -> None:
    sql = normalized_sql("0016_extraction_batches_and_mappings.sql")

    for trigger in (
        "schema_mappings_immutable",
        "mapping_sets_immutable",
        "mapping_set_entries_immutable",
        "schema_mappings_insert_audit",
        "mapping_sets_insert_audit",
        "mapping_set_entries_insert_audit",
        "extraction_batches_mutation_guard",
        "extraction_batches_candidate_count_guard",
        "extracted_records_candidate_count_guard",
        "extracted_records_batch_membership_delete_guard",
        "extraction_batches_lifecycle_audit",
    ):
        assert f"trigger {trigger}" in sql
    assert "field_rules', 'required_fields', 'ignore_reason', 'table_locator'" in sql
    assert "'evidence_digest'" in sql
    for immutable_column in (
        "old.batch_id is distinct from new.batch_id",
        "old.candidate_ordinal is distinct from new.candidate_ordinal",
        "old.candidate_key is distinct from new.candidate_key",
        "old.record_kind is distinct from new.record_kind",
        "old.financial_subtype is distinct from new.financial_subtype",
        "old.source_locator is distinct from new.source_locator",
        "old.row_fingerprint is distinct from new.row_fingerprint",
    ):
        assert immutable_column in sql


def test_compliance_matrix_has_the_complete_public_requirement_set() -> None:
    matrix = COMPLIANCE_MATRIX.read_text(encoding="utf-8")
    matrix_section = matrix.split("## Evidence matrix", maxsplit=1)[1].split(
        "## Required runtime and operating gate", maxsplit=1
    )[0]
    matrix_rows = [
        line
        for line in matrix_section.splitlines()
        if line.startswith("| ") and "Requirement area" not in line and not line.startswith("|---")
    ]

    requirements: set[str] = set()
    for row in matrix_rows:
        cells = [cell.strip() for cell in row.split("|")[1:-1]]
        assert len(cells) == 6, row
        requirement = cells[0]
        assert requirement not in requirements, row
        requirements.add(requirement)

    assert requirements == EXPECTED_COMPLIANCE_REQUIREMENTS
