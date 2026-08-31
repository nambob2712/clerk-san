"""Backward-compatible schema upgrades for the local SQLite demo.

PostgreSQL uses the ordered SQL migrations under ``migrations/``.  The local
demo instead creates SQLAlchemy metadata at startup, which cannot alter an
existing SQLite table.  Keep the intentionally small set of additive demo
upgrades here so an older local demo remains usable without resetting it.
SQLite mirrors the supported decision, activation-evidence, and candidate-lineage
shape for the local demo, but does not constitute proof of PostgreSQL deferred
constraints, trigger-owned audit, row locking, or concurrent activation enforcement.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from clerksan.db.models import (
    Base,
    CandidateReviewDecision,
    ExtractionBatch,
    MappingSet,
    MappingSetEntry,
    SchemaMapping,
    SourceIntake,
    UploadIdempotencyReservation,
    WorkerCapabilityLease,
)


class SQLiteSchemaUpgradeError(RuntimeError):
    """Raised when a local SQLite schema cannot be safely upgraded."""


_VERIFIED_RECORD_COLUMNS = {
    "expense_kind": "VARCHAR(32)",
    "due_date": "DATE",
    "version": "INTEGER NOT NULL DEFAULT 1",
}
_VERIFIED_RECORD_INDEXES = (
    "CREATE INDEX IF NOT EXISTS verified_records_expense_kind_date_idx "
    "ON verified_records (expense_kind, transaction_date)",
    "CREATE INDEX IF NOT EXISTS verified_records_due_date_idx ON verified_records (due_date)",
)
_SQLITE_TYPE_FAMILIES = {
    "expense_kind": ("VARCHAR", "TEXT"),
    "due_date": ("DATE",),
    "version": ("INTEGER", "INT"),
}
_DOCUMENT_FILE_COLUMNS = {
    "source_file_id": "CHAR(32)",
    "source_version": "INTEGER",
    "page_number": "INTEGER",
}
_JOB_EVIDENCE_COLUMNS = {
    "execution_profile": "VARCHAR(19) NOT NULL DEFAULT 'legacy_compat'",
    "sandbox_verified": "BOOLEAN NOT NULL DEFAULT false",
    "registry_digest": "VARCHAR(64)",
    "capabilities_digest": "VARCHAR(64)",
    "requirements_digest": (
        "VARCHAR(64) NOT NULL DEFAULT "
        "'4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'"
    ),
    "required_components": "JSON NOT NULL DEFAULT '[]'",
    "intake_intent": "VARCHAR(18) NOT NULL DEFAULT 'legacy_unspecified'",
}
_SOURCE_INTAKE_EVIDENCE_COLUMNS = {
    "intake_intent": "VARCHAR(18) NOT NULL DEFAULT 'legacy_unspecified'",
}
_INTAKE_INTENT_VALUES = "'legacy_unspecified', 'generic_file', 'bill_scan'"
_REQUIREMENTS_EMPTY_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
_PREVIEW_MANIFEST_MIME = "application/vnd.clerksan.preview-manifest+json"
_DOCUMENT_FILE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS document_files_document_created_idx "
    "ON document_files (document_id, created_at)",
    "CREATE INDEX IF NOT EXISTS document_files_latest_original_idx "
    "ON document_files (document_id, version) WHERE kind = 'original'",
    "CREATE UNIQUE INDEX IF NOT EXISTS document_files_original_sha256_key "
    "ON document_files (document_id, sha256) WHERE kind = 'original'",
    "CREATE UNIQUE INDEX IF NOT EXISTS document_files_page_render_slot_key "
    "ON document_files (source_file_id, source_version, page_number) "
    "WHERE kind = 'page_render' AND source_file_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS document_files_preview_manifest_key "
    "ON document_files (source_file_id, source_version) "
    f"WHERE mime = '{_PREVIEW_MANIFEST_MIME}' AND source_file_id IS NOT NULL",
)
_EXTRACTED_RECORD_LINEAGE_COLUMNS = {
    "batch_id": "CHAR(32)",
    "candidate_ordinal": "INTEGER",
    "candidate_key": "VARCHAR(64)",
    "record_kind": "VARCHAR(16)",
    "financial_subtype": "VARCHAR(17)",
    "source_locator": "TEXT",
    "row_fingerprint": "VARCHAR(64)",
    "validation_issues": "JSON",
    "evidence_group_keys": "JSON",
}
_CHUNK_LINEAGE_COLUMNS = {
    "batch_id": "CHAR(32)",
    "extraction_id": "CHAR(32)",
    "record_kind": "VARCHAR(16)",
    "source_file_id": "CHAR(32)",
    "source_version": "INTEGER",
    "candidate_key": "VARCHAR(64)",
}
_PHASE3_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS source_intakes_id_source_identity_idx "
    "ON source_intakes (id, document_id, source_file_id, source_version, source_sha256)",
    "CREATE UNIQUE INDEX IF NOT EXISTS extracted_records_chunk_lineage_idx "
    "ON extracted_records (id, batch_id, document_id, candidate_key, record_kind, "
    "source_file_id, source_version)",
    "CREATE UNIQUE INDEX IF NOT EXISTS extracted_records_batch_ordinal_key "
    "ON extracted_records (batch_id, candidate_ordinal) WHERE batch_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS extracted_records_batch_candidate_key "
    "ON extracted_records (batch_id, candidate_key) WHERE batch_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS chunks_document_seq_idx ON chunks (document_id, seq)",
    "CREATE UNIQUE INDEX IF NOT EXISTS chunks_legacy_document_seq_key "
    "ON chunks (document_id, seq) WHERE batch_id IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS chunks_batch_candidate_seq_key "
    "ON chunks (batch_id, extraction_id, seq) WHERE batch_id IS NOT NULL",
)
_EXTRACTION_BATCH_ACTIVATION_COLUMNS = {
    "activation_vector_sha256": "VARCHAR(64)",
    "activated_by": "TEXT",
    "activated_at": "DATETIME",
    "activation_included_count": "INTEGER",
    "activation_excluded_count": "INTEGER",
    "accepted_exclusions": "BOOLEAN NOT NULL DEFAULT false",
    "accepted_empty": "BOOLEAN NOT NULL DEFAULT false",
}
_EXTRACTED_RECORD_DECISION_KEY_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS extracted_records_id_batch_id_key "
    "ON extracted_records (id, batch_id)"
)
_DUPLICATE_SCOPE_COLUMNS = {
    "source_file_id": "CHAR(32)",
    "source_version": "INTEGER",
    "batch_id": "CHAR(32)",
    "extraction_id": "CHAR(32)",
    "candidate_key": "VARCHAR(64)",
    "record_kind": "VARCHAR(16)",
}
_PHASE4_INDEXES = (
    "CREATE INDEX IF NOT EXISTS extraction_batches_activated_at_idx "
    "ON extraction_batches (activated_at DESC) WHERE activation_vector_sha256 IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS duplicate_flags_document_idx "
    "ON duplicate_flags (document_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS duplicate_flags_document_scope_key "
    "ON duplicate_flags (document_id, suspected_document_id) WHERE source_file_id IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS duplicate_flags_source_scope_key "
    "ON duplicate_flags (document_id, suspected_document_id, source_file_id, source_version) "
    "WHERE source_file_id IS NOT NULL AND batch_id IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS duplicate_flags_candidate_scope_key "
    "ON duplicate_flags (document_id, suspected_document_id, batch_id, extraction_id) "
    "WHERE batch_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS duplicate_flags_candidate_lookup_idx "
    "ON duplicate_flags (batch_id, extraction_id, created_at DESC) WHERE batch_id IS NOT NULL",
)


async def upgrade_sqlite_demo_schema(connection: AsyncConnection) -> None:
    """Apply the additive compatibility upgrades needed by old local demos.

    The upgrades are deliberately explicit rather than a generic model-to-DDL
    synchronizer: only additions that are safe for an existing demo are
    applied.  New schema changes must add their own reviewed upgrade here.
    """

    try:
        await _upgrade_verified_records(connection)
        # ``Base.metadata.create_all`` can add Phase 4 child tables around an older
        # persisted parent table. Install the additive parent columns and unique
        # keys before any earlier table rebuild runs a global foreign-key check.
        await _upgrade_extracted_record_lineage(connection)
        await connection.execute(text(_EXTRACTED_RECORD_DECISION_KEY_INDEX))
        await _upgrade_document_files(connection)
        await _upgrade_job_evidence(connection)
        await connection.run_sync(_create_universal_intake_tables)
        await _upgrade_source_intake_evidence(connection)
        await _backfill_source_intakes(connection)
        await _install_phase3_exact_source_index(connection)
        await connection.run_sync(_create_phase3_tables)
        await _upgrade_chunk_lineage(connection)
        await _upgrade_extraction_batch_activation(connection)
        await _upgrade_duplicate_flag_scope(connection)
        await connection.run_sync(_create_phase4_tables)
        await _drop_phase3_backfill_conflicting_triggers(connection)
        await _backfill_legacy_extraction_batches(connection)
        await _bind_legacy_chunks(connection)
        await _install_universal_intake_triggers(connection)
        await _install_phase3_triggers(connection)
        await _install_phase4_triggers(connection)
        await _assert_sqlite_schema_matches_models(connection)
    except SQLiteSchemaUpgradeError:
        raise
    except Exception as error:
        raise SQLiteSchemaUpgradeError("could not upgrade the local SQLite demo schema") from error


async def _upgrade_verified_records(connection: AsyncConnection) -> None:
    columns = await _sqlite_table_columns(connection, "verified_records")
    if not columns:
        raise SQLiteSchemaUpgradeError("verified_records is unavailable")
    for name, definition in _VERIFIED_RECORD_COLUMNS.items():
        if name not in columns:
            await connection.execute(
                text(f"ALTER TABLE verified_records ADD COLUMN {name} {definition}")
            )
    for statement in _VERIFIED_RECORD_INDEXES:
        await connection.execute(text(statement))


async def _upgrade_document_files(connection: AsyncConnection) -> None:
    columns = await _sqlite_table_columns(connection, "document_files")
    if not columns:
        raise SQLiteSchemaUpgradeError("document_files is unavailable")
    required_legacy = {
        "id",
        "document_id",
        "version",
        "kind",
        "content_path",
        "sha256",
        "mime",
        "source_filename",
        "ocr_text",
        "text_provenance",
        "created_at",
    }
    missing_legacy = sorted(required_legacy - columns.keys())
    if missing_legacy:
        raise SQLiteSchemaUpgradeError(
            "document_files has an unsupported legacy layout: " + ", ".join(missing_legacy)
        )

    if await _has_document_wide_sha_uniqueness(connection):
        await _rebuild_document_files(connection, columns)
    else:
        for name, definition in _DOCUMENT_FILE_COLUMNS.items():
            if name not in columns:
                await connection.execute(
                    text(f"ALTER TABLE document_files ADD COLUMN {name} {definition}")
                )

    # Bind only derivatives whose document has one and only one retained original.
    await connection.execute(
        text(
            "UPDATE document_files AS derivative "
            "SET source_file_id = ("
            "    SELECT source.id FROM document_files AS source "
            "    WHERE source.document_id = derivative.document_id "
            "      AND source.kind = 'original'"
            "), source_version = ("
            "    SELECT source.version FROM document_files AS source "
            "    WHERE source.document_id = derivative.document_id "
            "      AND source.kind = 'original'"
            ") "
            "WHERE derivative.kind <> 'original' "
            "  AND derivative.source_file_id IS NULL "
            "  AND derivative.source_version IS NULL "
            "  AND (SELECT count(*) FROM document_files AS source "
            "       WHERE source.document_id = derivative.document_id "
            "         AND source.kind = 'original') = 1"
        )
    )
    for statement in _DOCUMENT_FILE_INDEXES:
        await connection.execute(text(statement))


async def _has_document_wide_sha_uniqueness(connection: AsyncConnection) -> bool:
    indexes = await connection.execute(text("PRAGMA index_list(document_files)"))
    for row in indexes:
        if not bool(row[2]) or bool(row[4]):
            continue
        name = str(row[1]).replace('"', '""')
        info = await connection.execute(text(f'PRAGMA index_info("{name}")'))
        columns = [str(index_row[2]) for index_row in info]
        if columns == ["document_id", "sha256"]:
            return True
    return False


async def _rebuild_document_files(
    connection: AsyncConnection, columns: dict[str, tuple[object, ...]]
) -> None:
    """Remove the legacy table-wide SHA unique constraint without losing rows.

    SQLite cannot drop the auto-index backing a table UNIQUE constraint. The
    rebuild runs inside the caller's transaction, defers incoming foreign keys,
    verifies the completed graph, and only then clears the deferred state.
    """

    await connection.execute(text("PRAGMA defer_foreign_keys = ON"))
    await connection.execute(text("DROP TABLE IF EXISTS document_files__universal_upgrade"))
    await connection.execute(
        text(
            "CREATE TABLE document_files__universal_upgrade ("
            "id CHAR(32) NOT NULL PRIMARY KEY, "
            "document_id CHAR(32) NOT NULL REFERENCES documents(id) ON DELETE RESTRICT, "
            "version INTEGER NOT NULL CHECK (version > 0), "
            "kind VARCHAR(11) NOT NULL "
            "    CHECK (kind IN ('original', 'page_render', 'normalized')), "
            "source_file_id CHAR(32), "
            "source_version INTEGER CHECK (source_version IS NULL OR source_version > 0), "
            "page_number INTEGER "
            "    CHECK (page_number IS NULL OR (kind = 'page_render' AND page_number > 0)), "
            "content_path TEXT NOT NULL CHECK (trim(content_path) <> ''), "
            "sha256 VARCHAR(64) NOT NULL "
            "    CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'), "
            "mime TEXT NOT NULL CHECK (trim(mime) <> ''), "
            "source_filename TEXT NOT NULL CHECK (trim(source_filename) <> ''), "
            "ocr_text TEXT, "
            "text_provenance TEXT, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "CONSTRAINT document_files_document_id_version_key "
            "    UNIQUE (document_id, version), "
            "CONSTRAINT document_files_id_document_id_key UNIQUE (id, document_id), "
            "CONSTRAINT document_files_id_document_id_version_key "
            "    UNIQUE (id, document_id, version), "
            "CONSTRAINT document_files_exact_source_identity_key "
            "    UNIQUE (id, document_id, version, sha256), "
            "CONSTRAINT document_files_source_identity_complete "
            "    CHECK ((source_file_id IS NULL) = (source_version IS NULL)), "
            "CONSTRAINT document_files_source_identity_fkey "
            "    FOREIGN KEY (source_file_id, document_id, source_version) "
            "    REFERENCES document_files__universal_upgrade (id, document_id, version) "
            "    ON DELETE RESTRICT"
            ")"
        )
    )
    source_file = "source_file_id" if "source_file_id" in columns else "NULL"
    source_version = "source_version" if "source_version" in columns else "NULL"
    page_number = "page_number" if "page_number" in columns else "NULL"
    await connection.execute(
        text(
            "INSERT INTO document_files__universal_upgrade ("
            "id, document_id, version, kind, source_file_id, source_version, page_number, "
            "content_path, sha256, mime, source_filename, ocr_text, text_provenance, created_at"
            ") SELECT id, document_id, version, kind, "
            f"{source_file}, {source_version}, {page_number}, "
            "content_path, sha256, mime, source_filename, ocr_text, text_provenance, created_at "
            "FROM document_files "
            "ORDER BY CASE WHEN kind = 'original' THEN 0 ELSE 1 END, version, id"
        )
    )
    await connection.execute(text("DROP TABLE document_files"))
    await connection.execute(
        text("ALTER TABLE document_files__universal_upgrade RENAME TO document_files")
    )
    violations = [tuple(row) for row in await connection.execute(text("PRAGMA foreign_key_check"))]
    if violations:
        raise SQLiteSchemaUpgradeError("document_files rebuild would leave invalid foreign keys")
    # Clear DROP TABLE's deferred violation only after the rebuilt graph proves valid.
    await connection.execute(text("PRAGMA defer_foreign_keys = OFF"))


async def _upgrade_job_evidence(connection: AsyncConnection) -> None:
    columns = await _sqlite_table_columns(connection, "jobs")
    if not columns:
        raise SQLiteSchemaUpgradeError("jobs is unavailable")
    for name, definition in _JOB_EVIDENCE_COLUMNS.items():
        if name not in columns:
            await connection.execute(text(f"ALTER TABLE jobs ADD COLUMN {name} {definition}"))


async def _upgrade_source_intake_evidence(connection: AsyncConnection) -> None:
    columns = await _sqlite_table_columns(connection, "source_intakes")
    if not columns:
        raise SQLiteSchemaUpgradeError("source_intakes is unavailable")
    for name, definition in _SOURCE_INTAKE_EVIDENCE_COLUMNS.items():
        if name not in columns:
            await connection.execute(
                text(f"ALTER TABLE source_intakes ADD COLUMN {name} {definition}")
            )


def _create_universal_intake_tables(sync_connection: object) -> None:
    WorkerCapabilityLease.__table__.create(sync_connection, checkfirst=True)
    SourceIntake.__table__.create(sync_connection, checkfirst=True)
    UploadIdempotencyReservation.__table__.create(sync_connection, checkfirst=True)


def _create_phase3_tables(sync_connection: object) -> None:
    """Create the additive SQLite shape without claiming PostgreSQL-level proof."""

    SchemaMapping.__table__.create(sync_connection, checkfirst=True)
    MappingSet.__table__.create(sync_connection, checkfirst=True)
    MappingSetEntry.__table__.create(sync_connection, checkfirst=True)
    ExtractionBatch.__table__.create(sync_connection, checkfirst=True)


def _create_phase4_tables(sync_connection: object) -> None:
    """Create the local decision shape without claiming PostgreSQL audit or locks."""

    CandidateReviewDecision.__table__.create(sync_connection, checkfirst=True)


async def _upgrade_extraction_batch_activation(connection: AsyncConnection) -> None:
    columns = await _sqlite_table_columns(connection, "extraction_batches")
    if not columns:
        raise SQLiteSchemaUpgradeError("extraction_batches is unavailable")
    for name, definition in _EXTRACTION_BATCH_ACTIVATION_COLUMNS.items():
        if name not in columns:
            await connection.execute(
                text(f"ALTER TABLE extraction_batches ADD COLUMN {name} {definition}")
            )
    await connection.execute(text(_EXTRACTED_RECORD_DECISION_KEY_INDEX))
    await connection.execute(text(_PHASE4_INDEXES[0]))


async def _upgrade_duplicate_flag_scope(connection: AsyncConnection) -> None:
    columns = await _sqlite_table_columns(connection, "duplicate_flags")
    if not columns:
        raise SQLiteSchemaUpgradeError("duplicate_flags is unavailable")

    if await _has_document_wide_duplicate_uniqueness(connection):
        await _rebuild_duplicate_flags(connection, columns)
    else:
        for name, definition in _DUPLICATE_SCOPE_COLUMNS.items():
            if name not in columns:
                await connection.execute(
                    text(f"ALTER TABLE duplicate_flags ADD COLUMN {name} {definition}")
                )

    for statement in _PHASE4_INDEXES[1:]:
        await connection.execute(text(statement))


async def _has_document_wide_duplicate_uniqueness(connection: AsyncConnection) -> bool:
    indexes = await connection.execute(text("PRAGMA index_list(duplicate_flags)"))
    for row in indexes:
        if not bool(row[2]) or bool(row[4]):
            continue
        name = str(row[1]).replace('"', '""')
        info = await connection.execute(text(f'PRAGMA index_info("{name}")'))
        if [str(index_row[2]) for index_row in info] == [
            "document_id",
            "suspected_document_id",
        ]:
            return True
    return False


async def _rebuild_duplicate_flags(
    connection: AsyncConnection, columns: dict[str, tuple[object, ...]]
) -> None:
    """Replace the old document-wide unique key with explicit scope indexes."""

    await connection.execute(text("PRAGMA defer_foreign_keys = ON"))
    await connection.execute(text("DROP TABLE IF EXISTS duplicate_flags__scope_upgrade"))
    await connection.execute(
        text(
            "CREATE TABLE duplicate_flags__scope_upgrade ("
            "id CHAR(32) NOT NULL PRIMARY KEY, "
            "document_id CHAR(32) NOT NULL REFERENCES documents(id) ON DELETE RESTRICT, "
            "suspected_document_id CHAR(32) NOT NULL "
            "    REFERENCES documents(id) ON DELETE RESTRICT, "
            "source_file_id CHAR(32), source_version INTEGER, "
            "batch_id CHAR(32), extraction_id CHAR(32), "
            "candidate_key VARCHAR(64), record_kind VARCHAR(16), "
            "reason TEXT NOT NULL, score NUMERIC(6, 4) NOT NULL, "
            "evidence JSON NOT NULL DEFAULT '{}', "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "CONSTRAINT duplicate_flags_distinct_documents "
            "    CHECK (document_id <> suspected_document_id), "
            "CONSTRAINT duplicate_flags_score_range CHECK (score >= 0 AND score <= 1), "
            "CONSTRAINT duplicate_flags_scope_shape CHECK ("
            "    (source_file_id IS NULL AND source_version IS NULL "
            "     AND batch_id IS NULL AND extraction_id IS NULL "
            "     AND candidate_key IS NULL AND record_kind IS NULL) OR "
            "    (source_file_id IS NOT NULL AND source_version IS NOT NULL "
            "     AND batch_id IS NULL AND extraction_id IS NULL "
            "     AND candidate_key IS NULL AND record_kind IS NULL) OR "
            "    (source_file_id IS NOT NULL AND source_version IS NOT NULL "
            "     AND batch_id IS NOT NULL AND extraction_id IS NOT NULL "
            "     AND candidate_key IS NOT NULL AND record_kind IS NOT NULL)), "
            "CONSTRAINT duplicate_flags_source_version_positive "
            "    CHECK (source_version IS NULL OR source_version > 0), "
            "CONSTRAINT duplicate_flags_candidate_key_length "
            "    CHECK (candidate_key IS NULL OR length(candidate_key) = 64), "
            "CONSTRAINT duplicate_flags_record_kind_check "
            "    CHECK (record_kind IS NULL OR record_kind IN "
            "        ('financial', 'generic_document')), "
            "CONSTRAINT duplicate_flags_exact_source_fkey "
            "    FOREIGN KEY (source_file_id, document_id, source_version) "
            "    REFERENCES document_files (id, document_id, version) ON DELETE RESTRICT, "
            "CONSTRAINT duplicate_flags_exact_candidate_fkey "
            "    FOREIGN KEY ("
            "        extraction_id, batch_id, document_id, candidate_key, "
            "        record_kind, source_file_id, source_version"
            "    ) REFERENCES extracted_records ("
            "        id, batch_id, document_id, candidate_key, "
            "        record_kind, source_file_id, source_version"
            "    ) ON DELETE RESTRICT"
            ")"
        )
    )
    values = {name: name if name in columns else "NULL" for name in _DUPLICATE_SCOPE_COLUMNS}
    await connection.execute(
        text(
            "INSERT INTO duplicate_flags__scope_upgrade ("
            "id, document_id, suspected_document_id, source_file_id, source_version, "
            "batch_id, extraction_id, candidate_key, record_kind, reason, score, evidence, "
            "created_at, updated_at"
            ") SELECT id, document_id, suspected_document_id, "
            f"{values['source_file_id']}, {values['source_version']}, "
            f"{values['batch_id']}, {values['extraction_id']}, "
            f"{values['candidate_key']}, {values['record_kind']}, "
            "reason, score, evidence, created_at, updated_at FROM duplicate_flags"
        )
    )
    await connection.execute(text("DROP TABLE duplicate_flags"))
    await connection.execute(
        text("ALTER TABLE duplicate_flags__scope_upgrade RENAME TO duplicate_flags")
    )
    violations = [tuple(row) for row in await connection.execute(text("PRAGMA foreign_key_check"))]
    if violations:
        raise SQLiteSchemaUpgradeError("duplicate_flags rebuild left invalid foreign keys")
    await connection.execute(text("PRAGMA defer_foreign_keys = OFF"))


async def _backfill_source_intakes(connection: AsyncConnection) -> None:
    await connection.execute(
        text(
            "INSERT OR IGNORE INTO source_intakes ("
            "id, document_id, source_file_id, source_version, source_sha256, "
            "canonical_mime, detection_evidence, policy_version, "
            "requirements_digest, required_components, state, reason_code, retryable, "
            "failure_phase, execution_profile, sandbox_verified, intake_intent, version, "
            "created_at, updated_at"
            ") "
            "SELECT lower(hex(randomblob(16))), source.document_id, source.id, source.version, "
            "source.sha256, source.mime, '[]', 'legacy-pre-0015', :requirements_digest, '[]', "
            "CASE "
            "  WHEN EXISTS ("
            "    SELECT 1 FROM extracted_records AS extraction "
            "    WHERE extraction.document_id = source.document_id "
            "      AND extraction.source_file_id = source.id "
            "      AND extraction.source_version = source.version"
            "  ) OR EXISTS ("
            "    SELECT 1 FROM document_files AS derivative "
            "    WHERE derivative.document_id = source.document_id "
            "      AND derivative.kind <> 'original' "
            "      AND derivative.source_file_id = source.id "
            "      AND derivative.source_version = source.version"
            "  ) THEN 'processed' "
            "  WHEN EXISTS ("
            "    SELECT 1 FROM jobs AS job "
            "    WHERE job.document_id = source.document_id "
            "      AND job.job_type = 'process_document' "
            "      AND job.status IN ('failed', 'dead') "
            "      AND json_valid(job.payload) "
            "      AND json_type(job.payload, '$.source_version') = 'integer' "
            "      AND CAST(json_extract(job.payload, '$.source_version') AS INTEGER) "
            "          = source.version"
            "  ) THEN 'failed' "
            "  ELSE 'stored_unprocessed' END, "
            "CASE "
            "  WHEN EXISTS ("
            "    SELECT 1 FROM extracted_records AS extraction "
            "    WHERE extraction.document_id = source.document_id "
            "      AND extraction.source_file_id = source.id "
            "      AND extraction.source_version = source.version"
            "  ) OR EXISTS ("
            "    SELECT 1 FROM document_files AS derivative "
            "    WHERE derivative.document_id = source.document_id "
            "      AND derivative.kind <> 'original' "
            "      AND derivative.source_file_id = source.id "
            "      AND derivative.source_version = source.version"
            "  ) THEN NULL "
            "  WHEN EXISTS ("
            "    SELECT 1 FROM jobs AS job "
            "    WHERE job.document_id = source.document_id "
            "      AND job.job_type = 'process_document' "
            "      AND job.status IN ('failed', 'dead') "
            "      AND json_valid(job.payload) "
            "      AND json_type(job.payload, '$.source_version') = 'integer' "
            "      AND CAST(json_extract(job.payload, '$.source_version') AS INTEGER) "
            "          = source.version"
            "  ) THEN 'processing_failed' "
            "  ELSE 'legacy_outcome_unavailable' END, "
            "CASE WHEN EXISTS ("
            "  SELECT 1 FROM jobs AS job "
            "  WHERE job.document_id = source.document_id "
            "    AND job.job_type = 'process_document' "
            "    AND job.status IN ('failed', 'dead') "
            "    AND json_valid(job.payload) "
            "    AND json_type(job.payload, '$.source_version') = 'integer' "
            "    AND CAST(json_extract(job.payload, '$.source_version') AS INTEGER) "
            "        = source.version"
            ") THEN true ELSE false END, "
            "CASE WHEN EXISTS ("
            "  SELECT 1 FROM jobs AS job "
            "  WHERE job.document_id = source.document_id "
            "    AND job.job_type = 'process_document' "
            "    AND job.status IN ('failed', 'dead') "
            "    AND json_valid(job.payload) "
            "    AND json_type(job.payload, '$.source_version') = 'integer' "
            "    AND CAST(json_extract(job.payload, '$.source_version') AS INTEGER) "
            "        = source.version"
            ") THEN 'legacy_job' ELSE NULL END, "
            "'legacy_compat', false, 'legacy_unspecified', 1, source.created_at, source.created_at "
            "FROM document_files AS source WHERE source.kind = 'original'"
        ),
        {"requirements_digest": _REQUIREMENTS_EMPTY_DIGEST},
    )


async def _install_phase3_exact_source_index(connection: AsyncConnection) -> None:
    await connection.execute(text(_PHASE3_INDEXES[0]))


async def _upgrade_extracted_record_lineage(connection: AsyncConnection) -> None:
    columns = await _sqlite_table_columns(connection, "extracted_records")
    if not columns:
        raise SQLiteSchemaUpgradeError("extracted_records is unavailable")
    for name, definition in _EXTRACTED_RECORD_LINEAGE_COLUMNS.items():
        if name not in columns:
            await connection.execute(
                text(f"ALTER TABLE extracted_records ADD COLUMN {name} {definition}")
            )
    for statement in _PHASE3_INDEXES[1:4]:
        await connection.execute(text(statement))


async def _upgrade_chunk_lineage(connection: AsyncConnection) -> None:
    columns = await _sqlite_table_columns(connection, "chunks")
    if not columns:
        raise SQLiteSchemaUpgradeError("chunks is unavailable")
    required_legacy = {
        "id",
        "document_id",
        "seq",
        "heading_path",
        "text",
        "embedding",
        "embed_model",
        "embed_model_digest",
        "token_count",
        "created_at",
    }
    missing_legacy = sorted(required_legacy - columns.keys())
    if missing_legacy:
        raise SQLiteSchemaUpgradeError(
            "chunks has an unsupported legacy layout: " + ", ".join(missing_legacy)
        )

    if await _has_unscoped_chunk_uniqueness(connection):
        await _rebuild_chunks(connection, columns)
    else:
        for name, definition in _CHUNK_LINEAGE_COLUMNS.items():
            if name not in columns:
                await connection.execute(text(f"ALTER TABLE chunks ADD COLUMN {name} {definition}"))

    for statement in _PHASE3_INDEXES[4:]:
        await connection.execute(text(statement))


async def _has_unscoped_chunk_uniqueness(connection: AsyncConnection) -> bool:
    indexes = await connection.execute(text("PRAGMA index_list(chunks)"))
    for row in indexes:
        if not bool(row[2]) or bool(row[4]):
            continue
        name = str(row[1]).replace('"', '""')
        info = await connection.execute(text(f'PRAGMA index_info("{name}")'))
        if [str(index_row[2]) for index_row in info] == ["document_id", "seq"]:
            return True
    return False


async def _rebuild_chunks(
    connection: AsyncConnection, columns: dict[str, tuple[object, ...]]
) -> None:
    """Remove legacy document-wide sequence uniqueness while retaining embeddings."""

    await connection.execute(text("PRAGMA defer_foreign_keys = ON"))
    await connection.execute(text("DROP TABLE IF EXISTS chunks__batch_upgrade"))
    await connection.execute(
        text(
            "CREATE TABLE chunks__batch_upgrade ("
            "id CHAR(32) NOT NULL PRIMARY KEY, "
            "document_id CHAR(32) NOT NULL REFERENCES documents(id) ON DELETE RESTRICT, "
            "batch_id CHAR(32), "
            "extraction_id CHAR(32), "
            "record_kind VARCHAR(16) "
            "    CHECK (record_kind IS NULL OR record_kind IN ('financial', 'generic_document')), "
            "source_file_id CHAR(32), "
            "source_version INTEGER CHECK (source_version IS NULL OR source_version > 0), "
            "candidate_key VARCHAR(64) "
            "    CHECK (candidate_key IS NULL OR length(candidate_key) = 64), "
            "seq INTEGER NOT NULL CHECK (seq >= 0), "
            "heading_path TEXT NOT NULL DEFAULT '', "
            "text TEXT NOT NULL, "
            "embedding JSON NOT NULL, "
            "embed_model TEXT NOT NULL, "
            "embed_model_digest TEXT NOT NULL, "
            "token_count INTEGER NOT NULL CHECK (token_count > 0), "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "CONSTRAINT chunks_candidate_lineage_complete CHECK ("
            "    (batch_id IS NULL AND extraction_id IS NULL AND record_kind IS NULL "
            "     AND source_file_id IS NULL AND source_version IS NULL AND candidate_key IS NULL) "
            "    OR "
            "    (batch_id IS NOT NULL AND extraction_id IS NOT NULL AND record_kind IS NOT NULL "
            "     AND source_file_id IS NOT NULL AND source_version IS NOT NULL "
            "     AND candidate_key IS NOT NULL)"
            "))"
        )
    )
    lineage_select = [name if name in columns else "NULL" for name in _CHUNK_LINEAGE_COLUMNS]
    await connection.execute(
        text(
            "INSERT INTO chunks__batch_upgrade ("
            "id, document_id, batch_id, extraction_id, record_kind, source_file_id, "
            "source_version, candidate_key, seq, heading_path, text, embedding, embed_model, "
            "embed_model_digest, token_count, created_at"
            ") SELECT id, document_id, "
            + ", ".join(lineage_select)
            + ", seq, heading_path, text, embedding, embed_model, embed_model_digest, "
            "token_count, created_at FROM chunks"
        )
    )
    await connection.execute(text("DROP TABLE chunks"))
    await connection.execute(text("ALTER TABLE chunks__batch_upgrade RENAME TO chunks"))
    violations = [tuple(row) for row in await connection.execute(text("PRAGMA foreign_key_check"))]
    if violations:
        raise SQLiteSchemaUpgradeError("chunks rebuild would leave invalid foreign keys")
    await connection.execute(text("PRAGMA defer_foreign_keys = OFF"))


def _sha256_parts(*parts: str) -> str:
    payload = "|".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_financial_subtype(document_class: str) -> str:
    return {
        "receipt": "receipt",
        "invoice": "invoice",
        "bill": "bill",
        "recurring_bill": "recurring_bill",
        "quote": "quote",
    }.get(document_class, "other_financial")


async def _backfill_legacy_extraction_batches(connection: AsyncConnection) -> None:
    """Create one deterministic singleton per legacy extraction or fail closed."""

    rows = (
        (
            await connection.execute(
                text(
                    "SELECT extraction.id, extraction.document_id, extraction.source_file_id, "
                    "extraction.source_version, extraction.payload, extraction.status, "
                    "extraction.created_at, document.document_class "
                    "FROM extracted_records AS extraction "
                    "JOIN documents AS document ON document.id = extraction.document_id "
                    "WHERE extraction.batch_id IS NULL "
                    "ORDER BY extraction.document_id, extraction.created_at, extraction.id"
                )
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return

    approved_by_document: dict[str, int] = {}
    current_pending_by_document: dict[str, int] = {}
    current_sources = {
        str(row.document_id): (str(row.source_file_id), int(row.source_version))
        for row in (
            await connection.execute(
                text(
                    "SELECT source.document_id, source.id AS source_file_id, "
                    "source.version AS source_version "
                    "FROM document_files AS source "
                    "WHERE source.kind = 'original' AND source.version = ("
                    "  SELECT max(candidate.version) FROM document_files AS candidate "
                    "  WHERE candidate.document_id = source.document_id "
                    "    AND candidate.kind = 'original'"
                    ")"
                )
            )
        ).mappings()
    }
    for row in rows:
        document_id = str(row.document_id)
        if row.status == "approved":
            approved_by_document[document_id] = approved_by_document.get(document_id, 0) + 1
        if row.status == "pending_review" and current_sources.get(document_id) == (
            str(row.source_file_id),
            int(row.source_version),
        ):
            current_pending_by_document[document_id] = (
                current_pending_by_document.get(document_id, 0) + 1
            )
    ambiguous_approved = sum(count > 1 for count in approved_by_document.values())
    ambiguous_pending = sum(count > 1 for count in current_pending_by_document.values())

    approved_without_verified = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM extracted_records AS extraction "
                "WHERE extraction.batch_id IS NULL AND extraction.status = 'approved' "
                "AND NOT EXISTS (SELECT 1 FROM verified_records AS verified "
                "WHERE verified.extracted_id = extraction.id "
                "AND verified.document_id = extraction.document_id)"
            )
        )
        or 0
    )
    verified_conflicts = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM (SELECT extraction.document_id "
                "FROM extracted_records AS extraction "
                "JOIN verified_records AS verified ON verified.extracted_id = extraction.id "
                "AND verified.document_id = extraction.document_id "
                "WHERE extraction.batch_id IS NULL AND extraction.status = 'approved' "
                "GROUP BY extraction.document_id HAVING count(*) > 1)"
            )
        )
        or 0
    )
    if ambiguous_approved or ambiguous_pending or approved_without_verified or verified_conflicts:
        raise SQLiteSchemaUpgradeError(
            "legacy extraction authority requires owner reconciliation "
            f"(approved={ambiguous_approved}, verified={verified_conflicts}, "
            f"approved_without_verified={approved_without_verified}, "
            f"current_pending={ambiguous_pending})"
        )

    for row in rows:
        source_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT intake.id, intake.source_sha256, intake.intake_intent "
                        "FROM source_intakes AS intake "
                        "JOIN document_files AS source ON source.id = intake.source_file_id "
                        "AND source.document_id = intake.document_id "
                        "AND source.version = intake.source_version "
                        "AND source.sha256 = intake.source_sha256 "
                        "WHERE intake.document_id = :document_id "
                        "AND intake.source_file_id = :source_file_id "
                        "AND intake.source_version = :source_version "
                        "AND source.kind = 'original'"
                    ),
                    {
                        "document_id": row.document_id,
                        "source_file_id": row.source_file_id,
                        "source_version": row.source_version,
                    },
                )
            )
            .mappings()
            .all()
        )
        if len(source_rows) != 1:
            raise SQLiteSchemaUpgradeError(
                "legacy extraction lacks one exact source intake; owner reconciliation is required"
            )
        intake = source_rows[0]
        extraction_id = str(row.id)
        source_sha256 = str(intake.source_sha256)
        financial_subtype = _legacy_financial_subtype(str(row.document_class))
        row_fingerprint = _sha256_parts("legacy_row", extraction_id, _canonical_json(row.payload))
        candidate_key = _sha256_parts(
            source_sha256,
            "legacy_unknown",
            "1",
            row_fingerprint,
            "financial",
            financial_subtype,
            "legacy_unknown",
        )
        reconciliation = _canonical_json(
            {
                "mapped_candidate": 1,
                "residual_generic_candidate": 0,
                "explicit_ignore": 0,
                "blank": 0,
                "parse_error": 0,
            }
        )
        current_source = current_sources.get(str(row.document_id))
        lifecycle = {
            "approved": "active",
            "rejected": "rejected",
            "superseded": "superseded",
        }.get(str(row.status))
        if lifecycle is None:
            lifecycle = (
                "open"
                if current_source == (str(row.source_file_id), int(row.source_version))
                else "superseded"
            )

        await connection.execute(
            text(
                "INSERT INTO extraction_batches ("
                "id, source_intake_id, document_id, source_file_id, source_version, "
                "source_sha256, normalized_sha256, structure_fingerprint, producer, "
                "producer_version, origin, intake_intent, lifecycle, idempotency_key, "
                "candidate_count, reconciliation_counts, reconciliation_digest, version, "
                "created_at, updated_at"
                ") VALUES ("
                ":id, :source_intake_id, :document_id, :source_file_id, :source_version, "
                ":source_sha256, :normalized_sha256, :structure_fingerprint, "
                "'legacy_migration', '0016', 'legacy_singleton', :intake_intent, :lifecycle, "
                ":idempotency_key, 1, :reconciliation_counts, :reconciliation_digest, 1, "
                ":created_at, :created_at)"
            ),
            {
                "id": row.id,
                "source_intake_id": intake.id,
                "document_id": row.document_id,
                "source_file_id": row.source_file_id,
                "source_version": row.source_version,
                "source_sha256": source_sha256,
                "normalized_sha256": _sha256_parts(
                    "legacy_unknown_normalized", extraction_id, source_sha256
                ),
                "structure_fingerprint": _sha256_parts(
                    "legacy_unknown_structure", extraction_id, source_sha256
                ),
                "intake_intent": intake.intake_intent,
                "lifecycle": lifecycle,
                "idempotency_key": f"legacy-singleton:{extraction_id}",
                "reconciliation_counts": reconciliation,
                "reconciliation_digest": hashlib.sha256(reconciliation.encode("utf-8")).hexdigest(),
                "created_at": row.created_at,
            },
        )
        await connection.execute(
            text(
                "UPDATE extracted_records SET batch_id = :batch_id, candidate_ordinal = 1, "
                "candidate_key = :candidate_key, record_kind = 'financial', "
                "financial_subtype = :financial_subtype, source_locator = 'legacy_unknown', "
                "row_fingerprint = :row_fingerprint, validation_issues = '[]', "
                "evidence_group_keys = '[]' WHERE id = :id AND batch_id IS NULL"
            ),
            {
                "batch_id": row.id,
                "candidate_key": candidate_key,
                "financial_subtype": financial_subtype,
                "row_fingerprint": row_fingerprint,
                "id": row.id,
            },
        )

    unbound = int(
        await connection.scalar(
            text("SELECT count(*) FROM extracted_records WHERE batch_id IS NULL")
        )
        or 0
    )
    mismatched = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM extraction_batches AS batch "
                "WHERE batch.candidate_count <> (SELECT count(*) FROM extracted_records "
                "WHERE batch_id = batch.id)"
            )
        )
        or 0
    )
    if unbound or mismatched:
        raise SQLiteSchemaUpgradeError(
            f"legacy extraction batch verification failed (unbound={unbound}, counts={mismatched})"
        )


async def _drop_phase3_backfill_conflicting_triggers(connection: AsyncConnection) -> None:
    """Temporarily allow the reviewed startup backfill inside its transaction."""

    for name in (
        "extracted_records_batch_lineage_guard_sqlite",
        "chunks_batch_lineage_guard_sqlite",
        "extraction_batches_activation_insert_guard_sqlite",
    ):
        await connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))


async def _bind_legacy_chunks(connection: AsyncConnection) -> None:
    await connection.execute(
        text(
            "UPDATE chunks SET "
            "batch_id = (SELECT batch.id FROM extraction_batches AS batch "
            "WHERE batch.document_id = chunks.document_id AND batch.lifecycle = 'active'), "
            "extraction_id = (SELECT extraction.id FROM extracted_records AS extraction "
            "JOIN extraction_batches AS batch ON batch.id = extraction.batch_id "
            "WHERE batch.document_id = chunks.document_id AND batch.lifecycle = 'active' "
            "AND extraction.status = 'approved'), "
            "record_kind = (SELECT extraction.record_kind FROM extracted_records AS extraction "
            "JOIN extraction_batches AS batch ON batch.id = extraction.batch_id "
            "WHERE batch.document_id = chunks.document_id AND batch.lifecycle = 'active' "
            "AND extraction.status = 'approved'), "
            "source_file_id = (SELECT extraction.source_file_id "
            "FROM extracted_records AS extraction JOIN extraction_batches AS batch "
            "ON batch.id = extraction.batch_id WHERE batch.document_id = chunks.document_id "
            "AND batch.lifecycle = 'active' AND extraction.status = 'approved'), "
            "source_version = (SELECT extraction.source_version "
            "FROM extracted_records AS extraction JOIN extraction_batches AS batch "
            "ON batch.id = extraction.batch_id WHERE batch.document_id = chunks.document_id "
            "AND batch.lifecycle = 'active' AND extraction.status = 'approved'), "
            "candidate_key = (SELECT extraction.candidate_key "
            "FROM extracted_records AS extraction JOIN extraction_batches AS batch "
            "ON batch.id = extraction.batch_id WHERE batch.document_id = chunks.document_id "
            "AND batch.lifecycle = 'active' AND extraction.status = 'approved') "
            "WHERE chunks.batch_id IS NULL AND 1 = ("
            "SELECT count(*) FROM extracted_records AS extraction "
            "JOIN extraction_batches AS batch ON batch.id = extraction.batch_id "
            "WHERE batch.document_id = chunks.document_id AND batch.lifecycle = 'active' "
            "AND extraction.status = 'approved')"
        )
    )


async def _install_universal_intake_triggers(connection: AsyncConnection) -> None:
    statements = (
        "DROP TRIGGER IF EXISTS document_files_future_lineage_guard",
        "CREATE TRIGGER document_files_future_lineage_guard "
        "BEFORE INSERT ON document_files BEGIN "
        "SELECT CASE WHEN NEW.kind = 'original' AND ("
        "NEW.source_file_id IS NOT NULL OR NEW.source_version IS NOT NULL "
        "OR NEW.page_number IS NOT NULL) "
        "THEN RAISE(ABORT, 'original document files cannot carry derivative lineage') END; "
        "SELECT CASE WHEN NEW.kind <> 'original' AND ("
        "NEW.source_file_id IS NULL OR NEW.source_version IS NULL) "
        "THEN RAISE(ABORT, 'new derivatives require exact original source lineage') END; "
        "SELECT CASE WHEN NEW.kind <> 'original' AND NOT EXISTS ("
        "SELECT 1 FROM document_files AS source "
        "WHERE source.id = NEW.source_file_id "
        "AND source.document_id = NEW.document_id "
        "AND source.version = NEW.source_version AND source.kind = 'original') "
        "THEN RAISE(ABORT, 'derivative source must be the exact original') END; "
        "SELECT CASE WHEN NEW.kind = 'page_render' "
        "AND (NEW.page_number IS NULL OR NEW.page_number <= 0) "
        "THEN RAISE(ABORT, 'page-render derivatives require a positive page slot') END; "
        "SELECT CASE WHEN NEW.kind <> 'page_render' AND NEW.page_number IS NOT NULL "
        "THEN RAISE(ABORT, 'only page-render derivatives may carry a page slot') END; "
        f"SELECT CASE WHEN NEW.mime = '{_PREVIEW_MANIFEST_MIME}' "
        "AND NEW.kind <> 'normalized' "
        "THEN RAISE(ABORT, 'preview manifests must be normalized derivatives') END; "
        "END",
        "DROP TRIGGER IF EXISTS document_files_append_only_sqlite_update",
        "CREATE TRIGGER document_files_append_only_sqlite_update "
        "BEFORE UPDATE ON document_files BEGIN "
        "SELECT RAISE(ABORT, 'document_files rows are append-only'); END",
        "DROP TRIGGER IF EXISTS document_files_append_only_sqlite_delete",
        "CREATE TRIGGER document_files_append_only_sqlite_delete "
        "BEFORE DELETE ON document_files BEGIN "
        "SELECT RAISE(ABORT, 'document_files rows are append-only'); END",
        "DROP TRIGGER IF EXISTS jobs_execution_evidence_guard_insert",
        "CREATE TRIGGER jobs_execution_evidence_guard_insert BEFORE INSERT ON jobs BEGIN "
        f"SELECT CASE WHEN NEW.intake_intent NOT IN ({_INTAKE_INTENT_VALUES}) "
        "THEN RAISE(ABORT, 'unsupported job intake intent') END; "
        "SELECT CASE WHEN NOT ((NEW.execution_profile = 'legacy_compat' "
        "AND NEW.sandbox_verified = false) OR ("
        "NEW.execution_profile = 'universal_sandboxed' AND NEW.sandbox_verified = true)) "
        "THEN RAISE(ABORT, 'job execution profile and sandbox evidence disagree') END; "
        "SELECT CASE WHEN NEW.execution_profile = 'universal_sandboxed' "
        "AND (NEW.registry_digest IS NULL OR NEW.capabilities_digest IS NULL) "
        "THEN RAISE(ABORT, 'universal jobs require capability digests') END; "
        "SELECT CASE WHEN NOT json_valid(NEW.required_components) "
        "OR json_type(NEW.required_components) <> 'array' "
        "THEN RAISE(ABORT, 'required_components must be a JSON array') END; END",
        "DROP TRIGGER IF EXISTS jobs_execution_evidence_guard_update",
        "CREATE TRIGGER jobs_execution_evidence_guard_update BEFORE UPDATE OF "
        "execution_profile, sandbox_verified, registry_digest, capabilities_digest, "
        "requirements_digest, required_components, intake_intent ON jobs BEGIN "
        "SELECT RAISE(ABORT, 'job execution evidence is immutable after enqueue'); END",
        "DROP TRIGGER IF EXISTS source_intakes_write_guard_insert",
        "CREATE TRIGGER source_intakes_write_guard_insert BEFORE INSERT ON source_intakes BEGIN "
        f"SELECT CASE WHEN NEW.intake_intent NOT IN ({_INTAKE_INTENT_VALUES}) "
        "THEN RAISE(ABORT, 'unsupported source intake intent') END; "
        "SELECT CASE WHEN NEW.state NOT IN ("
        "'queued', 'processing', 'processed', 'needs_mapping', 'stored_unprocessed', 'failed') "
        "THEN RAISE(ABORT, 'unsupported source intake state') END; "
        "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM document_files AS source "
        "WHERE source.id = NEW.source_file_id AND source.document_id = NEW.document_id "
        "AND source.version = NEW.source_version AND source.sha256 = NEW.source_sha256 "
        "AND source.kind = 'original') "
        "THEN RAISE(ABORT, 'source intake must reference the exact original') END; "
        "SELECT CASE WHEN NEW.version <> 1 "
        "THEN RAISE(ABORT, 'new source intake version must be one') END; "
        "SELECT CASE WHEN NEW.upload_idempotency_key IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM upload_idempotency_reservations AS reservation "
        "WHERE reservation.upload_idempotency_key = NEW.upload_idempotency_key "
        "AND reservation.source_sha256 = NEW.source_sha256 "
        "AND reservation.intent_digest = NEW.intent_digest "
        "AND reservation.source_intake_id IS NULL) "
        "THEN RAISE(ABORT, 'source intake requires its upload reservation') END; "
        "SELECT CASE WHEN NOT ((NEW.execution_profile = 'legacy_compat' "
        "AND NEW.sandbox_verified = false) OR ("
        "NEW.execution_profile = 'universal_sandboxed' AND NEW.sandbox_verified = true)) "
        "THEN RAISE(ABORT, 'source intake execution evidence disagrees') END; END",
        "DROP TRIGGER IF EXISTS source_intakes_write_guard_update",
        "CREATE TRIGGER source_intakes_write_guard_update BEFORE UPDATE ON source_intakes BEGIN "
        "SELECT CASE WHEN OLD.document_id IS NOT NEW.document_id "
        "OR OLD.source_file_id IS NOT NEW.source_file_id "
        "OR OLD.source_version IS NOT NEW.source_version "
        "OR OLD.source_sha256 IS NOT NEW.source_sha256 "
        "OR OLD.duplicate_of_document_id IS NOT NEW.duplicate_of_document_id "
        "OR OLD.upload_idempotency_key IS NOT NEW.upload_idempotency_key "
        "OR OLD.intent_digest IS NOT NEW.intent_digest "
        "OR OLD.intake_intent IS NOT NEW.intake_intent "
        "OR OLD.created_at IS NOT NEW.created_at "
        "THEN RAISE(ABORT, 'source intake identity is immutable') END; "
        "SELECT CASE WHEN NEW.version <> OLD.version + 1 "
        "THEN RAISE(ABORT, 'source intake optimistic version is stale') END; "
        "SELECT CASE WHEN NOT ("
        "(OLD.state = 'queued' AND NEW.state IN ("
        "'processing', 'processed', 'needs_mapping', 'stored_unprocessed', 'failed')) OR "
        "(OLD.state = 'processing' AND NEW.state IN ("
        "'queued', 'processed', 'needs_mapping', 'stored_unprocessed', 'failed')) OR "
        "(OLD.state = 'processed' AND NEW.state = 'queued') OR "
        "(OLD.state IN ('needs_mapping', 'stored_unprocessed') "
        "AND NEW.state IN ('queued', 'processing', 'failed')) OR "
        "(OLD.state = 'failed' AND NEW.state = 'queued')) "
        "THEN RAISE(ABORT, 'invalid source intake state transition') END; "
        "SELECT CASE WHEN OLD.execution_profile = 'universal_sandboxed' "
        "AND NEW.execution_profile = 'legacy_compat' "
        "THEN RAISE(ABORT, 'sandboxed intake cannot fall back to legacy') END; "
        "SELECT CASE WHEN NEW.state = 'failed' AND trim(COALESCE(NEW.reason_code, '')) = '' "
        "THEN RAISE(ABORT, 'failed intake requires a reason code') END; END",
        "DROP TRIGGER IF EXISTS source_intakes_delete_guard",
        "CREATE TRIGGER source_intakes_delete_guard BEFORE DELETE ON source_intakes BEGIN "
        "SELECT RAISE(ABORT, 'source intake evidence cannot be deleted'); END",
        "DROP TRIGGER IF EXISTS upload_idempotency_reservations_guard_insert",
        "CREATE TRIGGER upload_idempotency_reservations_guard_insert "
        "BEFORE INSERT ON upload_idempotency_reservations BEGIN "
        "SELECT CASE WHEN NEW.source_intake_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM source_intakes AS intake WHERE intake.id = NEW.source_intake_id "
        "AND intake.upload_idempotency_key = NEW.upload_idempotency_key "
        "AND intake.source_sha256 = NEW.source_sha256 "
        "AND intake.intent_digest = NEW.intent_digest) "
        "THEN RAISE(ABORT, 'reservation must match its source intake') END; END",
        "DROP TRIGGER IF EXISTS upload_idempotency_reservations_guard_update",
        "CREATE TRIGGER upload_idempotency_reservations_guard_update "
        "BEFORE UPDATE ON upload_idempotency_reservations BEGIN "
        "SELECT CASE WHEN OLD.upload_idempotency_key IS NOT NEW.upload_idempotency_key "
        "OR OLD.source_sha256 IS NOT NEW.source_sha256 "
        "OR OLD.intent_digest IS NOT NEW.intent_digest "
        "OR OLD.created_at IS NOT NEW.created_at OR OLD.source_intake_id IS NOT NULL "
        "THEN RAISE(ABORT, 'upload reservation binding is immutable') END; "
        "SELECT CASE WHEN NEW.source_intake_id IS NULL OR NOT EXISTS ("
        "SELECT 1 FROM source_intakes AS intake WHERE intake.id = NEW.source_intake_id "
        "AND intake.upload_idempotency_key = NEW.upload_idempotency_key "
        "AND intake.source_sha256 = NEW.source_sha256 "
        "AND intake.intent_digest = NEW.intent_digest) "
        "THEN RAISE(ABORT, 'reservation must match its source intake') END; END",
        "DROP TRIGGER IF EXISTS upload_idempotency_reservations_guard_delete",
        "CREATE TRIGGER upload_idempotency_reservations_guard_delete "
        "BEFORE DELETE ON upload_idempotency_reservations BEGIN "
        "SELECT RAISE(ABORT, 'upload reservations cannot be deleted'); END",
    )
    for statement in statements:
        await connection.execute(text(statement))


async def _install_phase3_triggers(connection: AsyncConnection) -> None:
    """Mirror safety guards, without claiming deferred/concurrency parity with PostgreSQL."""

    statements = (
        "DROP TRIGGER IF EXISTS schema_mappings_immutable_sqlite_update",
        "CREATE TRIGGER schema_mappings_immutable_sqlite_update BEFORE UPDATE ON schema_mappings "
        "BEGIN SELECT RAISE(ABORT, 'schema mappings are immutable'); END",
        "DROP TRIGGER IF EXISTS schema_mappings_immutable_sqlite_delete",
        "CREATE TRIGGER schema_mappings_immutable_sqlite_delete BEFORE DELETE ON schema_mappings "
        "BEGIN SELECT RAISE(ABORT, 'schema mappings are immutable'); END",
        "DROP TRIGGER IF EXISTS mapping_sets_immutable_sqlite_update",
        "CREATE TRIGGER mapping_sets_immutable_sqlite_update BEFORE UPDATE ON mapping_sets "
        "BEGIN SELECT RAISE(ABORT, 'mapping sets are immutable'); END",
        "DROP TRIGGER IF EXISTS mapping_sets_immutable_sqlite_delete",
        "CREATE TRIGGER mapping_sets_immutable_sqlite_delete BEFORE DELETE ON mapping_sets "
        "BEGIN SELECT RAISE(ABORT, 'mapping sets are immutable'); END",
        "DROP TRIGGER IF EXISTS mapping_set_entries_immutable_sqlite_update",
        "CREATE TRIGGER mapping_set_entries_immutable_sqlite_update "
        "BEFORE UPDATE ON mapping_set_entries "
        "BEGIN SELECT RAISE(ABORT, 'mapping set entries are immutable'); END",
        "DROP TRIGGER IF EXISTS mapping_set_entries_immutable_sqlite_delete",
        "CREATE TRIGGER mapping_set_entries_immutable_sqlite_delete "
        "BEFORE DELETE ON mapping_set_entries "
        "BEGIN SELECT RAISE(ABORT, 'mapping set entries are immutable'); END",
        "DROP TRIGGER IF EXISTS mapping_sets_exact_source_guard",
        "CREATE TRIGGER mapping_sets_exact_source_guard BEFORE INSERT ON mapping_sets BEGIN "
        "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM source_intakes AS intake "
        "WHERE intake.id = NEW.source_intake_id AND intake.document_id = NEW.document_id "
        "AND intake.source_file_id = NEW.source_file_id "
        "AND intake.source_version = NEW.source_version "
        "AND intake.source_sha256 = NEW.source_sha256) "
        "THEN RAISE(ABORT, 'mapping set must bind the exact source intake') END; END",
        "DROP TRIGGER IF EXISTS extraction_batches_exact_source_guard",
        "CREATE TRIGGER extraction_batches_exact_source_guard "
        "BEFORE INSERT ON extraction_batches BEGIN "
        "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM source_intakes AS intake "
        "WHERE intake.id = NEW.source_intake_id AND intake.document_id = NEW.document_id "
        "AND intake.source_file_id = NEW.source_file_id "
        "AND intake.source_version = NEW.source_version "
        "AND intake.source_sha256 = NEW.source_sha256) "
        "THEN RAISE(ABORT, 'extraction batch must bind the exact source intake') END; "
        "SELECT CASE WHEN NEW.mapping_set_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM mapping_sets AS mapping_set WHERE mapping_set.id = NEW.mapping_set_id "
        "AND mapping_set.version = NEW.mapping_set_version "
        "AND mapping_set.set_digest = NEW.mapping_set_digest "
        "AND mapping_set.source_intake_id = NEW.source_intake_id "
        "AND mapping_set.document_id = NEW.document_id "
        "AND mapping_set.source_file_id = NEW.source_file_id "
        "AND mapping_set.source_version = NEW.source_version "
        "AND mapping_set.source_sha256 = NEW.source_sha256) "
        "THEN RAISE(ABORT, 'extraction batch mapping set crosses source identity') END; END",
        "DROP TRIGGER IF EXISTS extraction_batches_reconciliation_guard",
        "CREATE TRIGGER extraction_batches_reconciliation_guard "
        "BEFORE INSERT ON extraction_batches BEGIN "
        "SELECT CASE WHEN NOT json_valid(NEW.reconciliation_counts) "
        "OR json_type(NEW.reconciliation_counts) <> 'object' "
        "OR (SELECT count(*) FROM json_each(NEW.reconciliation_counts)) <> 5 "
        "OR json_type(NEW.reconciliation_counts, '$.mapped_candidate') <> 'integer' "
        "OR json_type(NEW.reconciliation_counts, '$.residual_generic_candidate') <> 'integer' "
        "OR json_type(NEW.reconciliation_counts, '$.explicit_ignore') <> 'integer' "
        "OR json_type(NEW.reconciliation_counts, '$.blank') <> 'integer' "
        "OR json_type(NEW.reconciliation_counts, '$.parse_error') <> 'integer' "
        "OR json_extract(NEW.reconciliation_counts, '$.mapped_candidate') < 0 "
        "OR json_extract(NEW.reconciliation_counts, '$.residual_generic_candidate') < 0 "
        "OR json_extract(NEW.reconciliation_counts, '$.explicit_ignore') < 0 "
        "OR json_extract(NEW.reconciliation_counts, '$.blank') < 0 "
        "OR json_extract(NEW.reconciliation_counts, '$.parse_error') < 0 "
        "OR NEW.candidate_count <> "
        "json_extract(NEW.reconciliation_counts, '$.mapped_candidate') + "
        "json_extract(NEW.reconciliation_counts, '$.residual_generic_candidate') "
        "THEN RAISE(ABORT, 'extraction batch reconciliation counts are invalid') END; END",
        "DROP TRIGGER IF EXISTS extraction_batches_mutation_guard_sqlite",
        "CREATE TRIGGER extraction_batches_mutation_guard_sqlite "
        "BEFORE UPDATE ON extraction_batches BEGIN "
        "SELECT CASE WHEN OLD.source_intake_id IS NOT NEW.source_intake_id "
        "OR OLD.document_id IS NOT NEW.document_id "
        "OR OLD.source_file_id IS NOT NEW.source_file_id "
        "OR OLD.source_version IS NOT NEW.source_version "
        "OR OLD.source_sha256 IS NOT NEW.source_sha256 "
        "OR OLD.normalized_sha256 IS NOT NEW.normalized_sha256 "
        "OR OLD.structure_fingerprint IS NOT NEW.structure_fingerprint "
        "OR OLD.mapping_set_id IS NOT NEW.mapping_set_id "
        "OR OLD.mapping_set_version IS NOT NEW.mapping_set_version "
        "OR OLD.mapping_set_digest IS NOT NEW.mapping_set_digest "
        "OR OLD.producer IS NOT NEW.producer OR OLD.producer_version IS NOT NEW.producer_version "
        "OR OLD.origin IS NOT NEW.origin OR OLD.intake_intent IS NOT NEW.intake_intent "
        "OR OLD.idempotency_key IS NOT NEW.idempotency_key "
        "OR OLD.producer_job_id IS NOT NEW.producer_job_id "
        "OR OLD.candidate_count IS NOT NEW.candidate_count "
        "OR OLD.reconciliation_counts IS NOT NEW.reconciliation_counts "
        "OR OLD.reconciliation_digest IS NOT NEW.reconciliation_digest "
        "OR OLD.created_at IS NOT NEW.created_at "
        "THEN RAISE(ABORT, 'extraction batch identity and membership are immutable') END; "
        "SELECT CASE WHEN NEW.version <> OLD.version + 1 "
        "THEN RAISE(ABORT, 'extraction batch optimistic version is stale') END; END",
        "DROP TRIGGER IF EXISTS extraction_batches_delete_guard_sqlite",
        "CREATE TRIGGER extraction_batches_delete_guard_sqlite BEFORE DELETE ON extraction_batches "
        "BEGIN SELECT RAISE(ABORT, 'extraction batches cannot be deleted'); END",
        "DROP TRIGGER IF EXISTS extracted_records_batch_lineage_guard_sqlite",
        "CREATE TRIGGER extracted_records_batch_lineage_guard_sqlite BEFORE UPDATE OF "
        "batch_id, candidate_ordinal, candidate_key, record_kind, financial_subtype, "
        "source_locator, row_fingerprint, validation_issues, evidence_group_keys "
        "ON extracted_records BEGIN "
        "SELECT RAISE(ABORT, 'candidate lineage is immutable'); END",
        "DROP TRIGGER IF EXISTS extracted_records_batch_insert_guard_sqlite",
        "CREATE TRIGGER extracted_records_batch_insert_guard_sqlite "
        "BEFORE INSERT ON extracted_records BEGIN "
        "SELECT CASE WHEN NEW.batch_id IS NULL AND (NEW.candidate_ordinal IS NOT NULL "
        "OR NEW.candidate_key IS NOT NULL OR NEW.record_kind IS NOT NULL "
        "OR NEW.financial_subtype IS NOT NULL OR NEW.source_locator IS NOT NULL "
        "OR NEW.row_fingerprint IS NOT NULL OR NEW.validation_issues IS NOT NULL "
        "OR NEW.evidence_group_keys IS NOT NULL) "
        "THEN RAISE(ABORT, 'unbound extraction cannot carry candidate lineage') END; "
        "SELECT CASE WHEN NEW.batch_id IS NOT NULL AND (NEW.candidate_ordinal IS NULL "
        "OR NEW.candidate_ordinal <= 0 OR NEW.candidate_key IS NULL "
        "OR NEW.record_kind IS NULL OR NEW.source_locator IS NULL "
        "OR NEW.row_fingerprint IS NULL OR NEW.validation_issues IS NULL "
        "OR NEW.evidence_group_keys IS NULL) "
        "THEN RAISE(ABORT, 'batch candidate lineage must be complete') END; "
        "SELECT CASE WHEN NEW.batch_id IS NOT NULL AND NOT ("
        "(NEW.record_kind = 'financial' AND NEW.financial_subtype IS NOT NULL) OR "
        "(NEW.record_kind = 'generic_document' AND NEW.financial_subtype IS NULL)) "
        "THEN RAISE(ABORT, 'candidate record kind and subtype disagree') END; "
        "SELECT CASE WHEN NEW.batch_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM extraction_batches AS batch WHERE batch.id = NEW.batch_id "
        "AND batch.document_id = NEW.document_id AND batch.source_file_id = NEW.source_file_id "
        "AND batch.source_version = NEW.source_version) "
        "THEN RAISE(ABORT, 'candidate must bind the exact batch source') END; END",
        "DROP TRIGGER IF EXISTS extracted_records_batch_delete_guard_sqlite",
        "CREATE TRIGGER extracted_records_batch_delete_guard_sqlite "
        "BEFORE DELETE ON extracted_records WHEN OLD.batch_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'batch candidates cannot be deleted'); END",
        "DROP TRIGGER IF EXISTS chunks_batch_lineage_guard_sqlite",
        "CREATE TRIGGER chunks_batch_lineage_guard_sqlite BEFORE UPDATE OF "
        "batch_id, extraction_id, record_kind, source_file_id, source_version, candidate_key "
        "ON chunks BEGIN SELECT RAISE(ABORT, 'chunk lineage is immutable'); END",
        "DROP TRIGGER IF EXISTS chunks_batch_insert_guard_sqlite",
        "CREATE TRIGGER chunks_batch_insert_guard_sqlite BEFORE INSERT ON chunks BEGIN "
        "SELECT CASE WHEN NEW.batch_id IS NULL AND (NEW.extraction_id IS NOT NULL "
        "OR NEW.record_kind IS NOT NULL OR NEW.source_file_id IS NOT NULL "
        "OR NEW.source_version IS NOT NULL OR NEW.candidate_key IS NOT NULL) "
        "THEN RAISE(ABORT, 'unbound chunk cannot carry candidate lineage') END; "
        "SELECT CASE WHEN NEW.batch_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM extracted_records AS extraction "
        "WHERE extraction.id = NEW.extraction_id AND extraction.batch_id = NEW.batch_id "
        "AND extraction.document_id = NEW.document_id "
        "AND extraction.record_kind = NEW.record_kind "
        "AND extraction.source_file_id = NEW.source_file_id "
        "AND extraction.source_version = NEW.source_version "
        "AND extraction.candidate_key = NEW.candidate_key) "
        "THEN RAISE(ABORT, 'chunk must bind exact candidate lineage') END; END",
    )
    for statement in statements:
        await connection.execute(text(statement))


async def _install_phase4_triggers(connection: AsyncConnection) -> None:
    """Mirror single-writer guards; PostgreSQL still owns audit and lock guarantees."""

    statements = (
        "DROP TRIGGER IF EXISTS candidate_review_decisions_insert_guard_sqlite",
        "CREATE TRIGGER candidate_review_decisions_insert_guard_sqlite "
        "BEFORE INSERT ON candidate_review_decisions BEGIN "
        "SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM extracted_records AS candidate "
        "JOIN extraction_batches AS batch ON batch.id = candidate.batch_id "
        "WHERE candidate.id = NEW.extraction_id AND candidate.batch_id = NEW.batch_id) "
        "THEN RAISE(ABORT, 'decision must bind one exact batch candidate') END; "
        "SELECT CASE WHEN NEW.expected_extraction_version <> ("
        "SELECT candidate.version FROM extracted_records AS candidate "
        "WHERE candidate.id = NEW.extraction_id AND candidate.batch_id = NEW.batch_id) "
        "THEN RAISE(ABORT, 'candidate extraction version is stale') END; "
        "SELECT CASE WHEN (SELECT candidate.status FROM extracted_records AS candidate "
        "WHERE candidate.id = NEW.extraction_id) <> 'pending_review' "
        "THEN RAISE(ABORT, 'only pending candidates accept staged decisions') END; "
        "SELECT CASE WHEN (SELECT batch.lifecycle FROM extraction_batches AS batch "
        "WHERE batch.id = NEW.batch_id) NOT IN ('open', 'ready_to_activate') "
        "THEN RAISE(ABORT, 'batch lifecycle does not accept staged decisions') END; "
        "SELECT CASE WHEN (SELECT candidate.record_kind FROM extracted_records AS candidate "
        "WHERE candidate.id = NEW.extraction_id) = 'generic_document' "
        "AND (NEW.corrected_payload IS NOT NULL "
        "OR NEW.corrected_financial_subtype IS NOT NULL) "
        "THEN RAISE(ABORT, 'generic candidates cannot carry financial corrections') END; "
        "SELECT CASE WHEN NEW.corrected_payload IS NOT NULL AND ("
        "NOT json_valid(NEW.corrected_payload) "
        "OR json_type(NEW.corrected_payload) <> 'object' "
        "OR length(CAST(NEW.corrected_payload AS TEXT)) > 65536) "
        "THEN RAISE(ABORT, 'corrected payload must be one bounded object') END; "
        "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM candidate_review_decisions AS prior "
        "WHERE prior.batch_id = NEW.batch_id AND prior.extraction_id = NEW.extraction_id) "
        "AND (NEW.decision_revision <> 1 OR NEW.supersedes_decision_id IS NOT NULL) "
        "THEN RAISE(ABORT, 'first candidate decision must be revision one') END; "
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM candidate_review_decisions AS prior "
        "WHERE prior.batch_id = NEW.batch_id AND prior.extraction_id = NEW.extraction_id) "
        "AND (NEW.decision_revision <> (SELECT max(prior.decision_revision) + 1 "
        "FROM candidate_review_decisions AS prior WHERE prior.batch_id = NEW.batch_id "
        "AND prior.extraction_id = NEW.extraction_id) "
        "OR NEW.supersedes_decision_id IS NOT (SELECT prior.id "
        "FROM candidate_review_decisions AS prior WHERE prior.batch_id = NEW.batch_id "
        "AND prior.extraction_id = NEW.extraction_id "
        "ORDER BY prior.decision_revision DESC LIMIT 1)) "
        "THEN RAISE(ABORT, 'candidate decision revision is stale or would fork history') END; END",
        "DROP TRIGGER IF EXISTS candidate_review_decisions_immutable_sqlite_update",
        "CREATE TRIGGER candidate_review_decisions_immutable_sqlite_update "
        "BEFORE UPDATE ON candidate_review_decisions "
        "BEGIN SELECT RAISE(ABORT, 'candidate review decisions are append-only'); END",
        "DROP TRIGGER IF EXISTS candidate_review_decisions_immutable_sqlite_delete",
        "CREATE TRIGGER candidate_review_decisions_immutable_sqlite_delete "
        "BEFORE DELETE ON candidate_review_decisions "
        "BEGIN SELECT RAISE(ABORT, 'candidate review decisions are append-only'); END",
        "DROP TRIGGER IF EXISTS verified_records_active_candidate_guard_sqlite",
        "CREATE TRIGGER verified_records_active_candidate_guard_sqlite "
        "BEFORE INSERT ON verified_records BEGIN "
        "SELECT CASE WHEN EXISTS ("
        "SELECT 1 FROM extracted_records AS candidate "
        "JOIN extraction_batches AS batch ON batch.id = candidate.batch_id "
        "WHERE candidate.id = NEW.extracted_id AND candidate.document_id = NEW.document_id "
        "AND batch.origin <> 'legacy_singleton' "
        "AND (candidate.status <> 'approved' OR candidate.record_kind <> 'financial' "
        "OR batch.lifecycle <> 'active')) "
        "THEN RAISE(ABORT, "
        "'verified records require one active approved financial candidate') END; END",
        "DROP TRIGGER IF EXISTS extraction_batches_activation_insert_guard_sqlite",
        "CREATE TRIGGER extraction_batches_activation_insert_guard_sqlite "
        "BEFORE INSERT ON extraction_batches BEGIN "
        "SELECT CASE WHEN NOT ((NEW.activation_vector_sha256 IS NULL "
        "AND NEW.activated_by IS NULL AND NEW.activated_at IS NULL "
        "AND NEW.activation_included_count IS NULL "
        "AND NEW.activation_excluded_count IS NULL "
        "AND NEW.accepted_exclusions = false AND NEW.accepted_empty = false) OR "
        "(NEW.activation_vector_sha256 IS NOT NULL "
        "AND length(NEW.activation_vector_sha256) = 64 "
        "AND NEW.activation_vector_sha256 NOT GLOB '*[^0-9a-f]*' "
        "AND NEW.activated_by IS NOT NULL "
        "AND length(trim(NEW.activated_by)) BETWEEN 1 AND 255 "
        "AND NEW.activated_at IS NOT NULL "
        "AND NEW.activation_included_count >= 0 "
        "AND NEW.activation_excluded_count >= 0 "
        "AND NEW.activation_included_count + NEW.activation_excluded_count "
        "    = NEW.candidate_count "
        "AND ((NEW.candidate_count = 0 AND NEW.accepted_empty = true) "
        "     OR (NEW.candidate_count > 0 AND NEW.accepted_empty = false)) "
        "AND ((NEW.activation_excluded_count > 0 AND NEW.accepted_exclusions = true) "
        "     OR (NEW.activation_excluded_count = 0 "
        "         AND NEW.accepted_exclusions = false)))) "
        "THEN RAISE(ABORT, 'activation metadata is incomplete or inconsistent') END; "
        "SELECT CASE WHEN NEW.lifecycle = 'active' "
        "AND NEW.origin <> 'legacy_singleton' AND NEW.activation_vector_sha256 IS NULL "
        "THEN RAISE(ABORT, 'new active batches require activation evidence') END; END",
        "DROP TRIGGER IF EXISTS extraction_batches_activation_update_guard_sqlite",
        "CREATE TRIGGER extraction_batches_activation_update_guard_sqlite "
        "BEFORE UPDATE ON extraction_batches BEGIN "
        "SELECT CASE WHEN OLD.activation_vector_sha256 IS NOT NULL AND ("
        "OLD.activation_vector_sha256 IS NOT NEW.activation_vector_sha256 "
        "OR OLD.activated_by IS NOT NEW.activated_by "
        "OR OLD.activated_at IS NOT NEW.activated_at "
        "OR OLD.activation_included_count IS NOT NEW.activation_included_count "
        "OR OLD.activation_excluded_count IS NOT NEW.activation_excluded_count "
        "OR OLD.accepted_exclusions IS NOT NEW.accepted_exclusions "
        "OR OLD.accepted_empty IS NOT NEW.accepted_empty) "
        "THEN RAISE(ABORT, 'activation evidence is immutable once recorded') END; "
        "SELECT CASE WHEN OLD.activation_vector_sha256 IS NULL "
        "AND NEW.activation_vector_sha256 IS NOT NULL "
        "AND NOT (OLD.lifecycle <> 'active' AND NEW.lifecycle = 'active') "
        "THEN RAISE(ABORT, 'activation evidence may only be recorded during activation') END; "
        "SELECT CASE WHEN NEW.lifecycle IS NOT OLD.lifecycle AND NOT ("
        "(OLD.lifecycle = 'open' AND NEW.lifecycle IN "
        "    ('ready_to_activate', 'active', 'superseded', 'rejected')) OR "
        "(OLD.lifecycle = 'ready_to_activate' AND NEW.lifecycle IN "
        "    ('open', 'active', 'superseded', 'rejected')) OR "
        "(OLD.lifecycle = 'active' AND NEW.lifecycle = 'superseded')) "
        "THEN RAISE(ABORT, 'invalid extraction batch lifecycle transition') END; "
        "SELECT CASE WHEN OLD.lifecycle <> 'active' AND NEW.lifecycle = 'active' "
        "AND NEW.activation_vector_sha256 IS NULL "
        "THEN RAISE(ABORT, 'new active batches require activation evidence') END; "
        "SELECT CASE WHEN NOT ((NEW.activation_vector_sha256 IS NULL "
        "AND NEW.activated_by IS NULL AND NEW.activated_at IS NULL "
        "AND NEW.activation_included_count IS NULL "
        "AND NEW.activation_excluded_count IS NULL "
        "AND NEW.accepted_exclusions = false AND NEW.accepted_empty = false) OR "
        "(NEW.activation_vector_sha256 IS NOT NULL "
        "AND length(NEW.activation_vector_sha256) = 64 "
        "AND NEW.activation_vector_sha256 NOT GLOB '*[^0-9a-f]*' "
        "AND NEW.activated_by IS NOT NULL "
        "AND length(trim(NEW.activated_by)) BETWEEN 1 AND 255 "
        "AND NEW.activated_at IS NOT NULL "
        "AND NEW.activation_included_count >= 0 "
        "AND NEW.activation_excluded_count >= 0 "
        "AND NEW.activation_included_count + NEW.activation_excluded_count "
        "    = NEW.candidate_count "
        "AND ((NEW.candidate_count = 0 AND NEW.accepted_empty = true) "
        "     OR (NEW.candidate_count > 0 AND NEW.accepted_empty = false)) "
        "AND ((NEW.activation_excluded_count > 0 AND NEW.accepted_exclusions = true) "
        "     OR (NEW.activation_excluded_count = 0 "
        "         AND NEW.accepted_exclusions = false)))) "
        "THEN RAISE(ABORT, 'activation metadata is incomplete or inconsistent') END; END",
        "DROP TRIGGER IF EXISTS duplicate_flags_scope_guard_sqlite_insert",
        "CREATE TRIGGER duplicate_flags_scope_guard_sqlite_insert "
        "BEFORE INSERT ON duplicate_flags BEGIN "
        "SELECT CASE WHEN NOT ((NEW.source_file_id IS NULL AND NEW.source_version IS NULL "
        "AND NEW.batch_id IS NULL AND NEW.extraction_id IS NULL "
        "AND NEW.candidate_key IS NULL AND NEW.record_kind IS NULL) OR "
        "(NEW.source_file_id IS NOT NULL AND NEW.source_version IS NOT NULL "
        "AND NEW.batch_id IS NULL AND NEW.extraction_id IS NULL "
        "AND NEW.candidate_key IS NULL AND NEW.record_kind IS NULL) OR "
        "(NEW.source_file_id IS NOT NULL AND NEW.source_version IS NOT NULL "
        "AND NEW.batch_id IS NOT NULL AND NEW.extraction_id IS NOT NULL "
        "AND NEW.candidate_key IS NOT NULL AND NEW.record_kind IS NOT NULL)) "
        "THEN RAISE(ABORT, 'duplicate evidence scope is incomplete') END; "
        "SELECT CASE WHEN NEW.source_file_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM document_files AS source WHERE source.id = NEW.source_file_id "
        "AND source.document_id = NEW.document_id AND source.version = NEW.source_version "
        "AND source.kind = 'original') "
        "THEN RAISE(ABORT, 'duplicate evidence must bind the exact original source') END; "
        "SELECT CASE WHEN NEW.batch_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM extracted_records AS candidate "
        "WHERE candidate.id = NEW.extraction_id AND candidate.batch_id = NEW.batch_id "
        "AND candidate.document_id = NEW.document_id "
        "AND candidate.candidate_key = NEW.candidate_key "
        "AND candidate.record_kind = NEW.record_kind "
        "AND candidate.source_file_id = NEW.source_file_id "
        "AND candidate.source_version = NEW.source_version) "
        "THEN RAISE(ABORT, 'duplicate evidence must bind exact candidate lineage') END; END",
        "DROP TRIGGER IF EXISTS duplicate_flags_scope_immutable_sqlite_update",
        "CREATE TRIGGER duplicate_flags_scope_immutable_sqlite_update "
        "BEFORE UPDATE ON duplicate_flags BEGIN "
        "SELECT CASE WHEN OLD.document_id IS NOT NEW.document_id "
        "OR OLD.suspected_document_id IS NOT NEW.suspected_document_id "
        "OR OLD.source_file_id IS NOT NEW.source_file_id "
        "OR OLD.source_version IS NOT NEW.source_version "
        "OR OLD.batch_id IS NOT NEW.batch_id "
        "OR OLD.extraction_id IS NOT NEW.extraction_id "
        "OR OLD.candidate_key IS NOT NEW.candidate_key "
        "OR OLD.record_kind IS NOT NEW.record_kind "
        "THEN RAISE(ABORT, 'duplicate evidence scope is immutable') END; END",
    )
    for statement in statements:
        await connection.execute(text(statement))


async def _assert_sqlite_schema_matches_models(connection: AsyncConnection) -> None:
    problems: list[str] = []
    for table in Base.metadata.sorted_tables:
        result = await connection.execute(text(f"PRAGMA table_info({table.name})"))
        actual = {str(row[1]): tuple(row) for row in result}
        missing = sorted(column.name for column in table.columns if column.name not in actual)
        if missing:
            problems.append(f"{table.name}: {', '.join(missing)}")
    verified_columns = await _sqlite_table_columns(connection, "verified_records")
    malformed = _malformed_verified_record_columns(verified_columns)
    if malformed:
        problems.append("verified_records: " + ", ".join(malformed))

    document_file_columns = await _sqlite_table_columns(connection, "document_files")
    for name in ("source_file_id", "source_version", "page_number"):
        definition = document_file_columns.get(name)
        if definition is not None and bool(definition[3]):
            problems.append(f"document_files: {name} must allow legacy NULL values")
    if await _has_document_wide_sha_uniqueness(connection):
        problems.append("document_files: legacy document-wide SHA uniqueness remains")

    job_columns = await _sqlite_table_columns(connection, "jobs")
    problems.extend(_malformed_job_evidence_columns(job_columns))
    if "intake_intent" not in job_columns:
        problems.append("jobs: intake_intent is unavailable")

    intake_columns = await _sqlite_table_columns(connection, "source_intakes")
    if "intake_intent" not in intake_columns:
        problems.append("source_intakes: intake_intent is unavailable")

    source_indexes = {
        str(row[1]) for row in await connection.execute(text("PRAGMA index_list(source_intakes)"))
    }
    if "source_intakes_id_source_identity_idx" not in source_indexes:
        problems.append("source_intakes: exact id/source key is unavailable")

    original_count = await connection.scalar(
        text("SELECT count(*) FROM document_files WHERE kind = 'original'")
    )
    intake_count = await connection.scalar(text("SELECT count(*) FROM source_intakes"))
    if int(original_count or 0) != int(intake_count or 0):
        problems.append("source_intakes: every original must have exactly one intake")

    unbound_extractions = await connection.scalar(
        text("SELECT count(*) FROM extracted_records WHERE batch_id IS NULL")
    )
    if int(unbound_extractions or 0) != 0:
        problems.append("extracted_records: legacy candidates remain unbound")
    mismatched_batches = await connection.scalar(
        text(
            "SELECT count(*) FROM extraction_batches AS batch "
            "WHERE batch.candidate_count <> (SELECT count(*) FROM extracted_records "
            "WHERE batch_id = batch.id)"
        )
    )
    if int(mismatched_batches or 0) != 0:
        problems.append("extraction_batches: candidate counts do not reconcile")

    batch_columns = await _sqlite_table_columns(connection, "extraction_batches")
    for name in _EXTRACTION_BATCH_ACTIVATION_COLUMNS:
        if name not in batch_columns:
            problems.append(f"extraction_batches: activation column {name} is unavailable")

    duplicate_columns = await _sqlite_table_columns(connection, "duplicate_flags")
    for name in _DUPLICATE_SCOPE_COLUMNS:
        if name not in duplicate_columns:
            problems.append(f"duplicate_flags: scope column {name} is unavailable")
    if await _has_document_wide_duplicate_uniqueness(connection):
        problems.append("duplicate_flags: legacy document-wide uniqueness remains")

    foreign_key_violations = [
        tuple(row) for row in await connection.execute(text("PRAGMA foreign_key_check"))
    ]
    if foreign_key_violations:
        problems.append("foreign keys: upgraded schema contains invalid references")

    indexes = {
        str(row[1]) for row in await connection.execute(text("PRAGMA index_list(document_files)"))
    }
    for name in (
        "document_files_original_sha256_key",
        "document_files_page_render_slot_key",
        "document_files_preview_manifest_key",
    ):
        if name not in indexes:
            problems.append(f"document_files: missing index {name}")

    chunk_indexes = {
        str(row[1]) for row in await connection.execute(text("PRAGMA index_list(chunks)"))
    }
    for name in (
        "chunks_legacy_document_seq_key",
        "chunks_batch_candidate_seq_key",
    ):
        if name not in chunk_indexes:
            problems.append(f"chunks: missing index {name}")
    if await _has_unscoped_chunk_uniqueness(connection):
        problems.append("chunks: legacy document-wide sequence uniqueness remains")

    phase4_indexes = {
        str(row[1])
        for table_name in (
            "extraction_batches",
            "extracted_records",
            "candidate_review_decisions",
            "duplicate_flags",
        )
        for row in await connection.execute(text(f"PRAGMA index_list({table_name})"))
    }
    for name in (
        "extraction_batches_activated_at_idx",
        "extracted_records_id_batch_id_key",
        "candidate_review_decisions_latest_idx",
        "candidate_review_decisions_batch_created_idx",
        "duplicate_flags_document_scope_key",
        "duplicate_flags_source_scope_key",
        "duplicate_flags_candidate_scope_key",
        "duplicate_flags_candidate_lookup_idx",
    ):
        if name not in phase4_indexes:
            problems.append(f"SQLite index missing: {name}")

    triggers = {
        str(row[0])
        for row in await connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        )
    }
    for name in (
        "document_files_future_lineage_guard",
        "document_files_append_only_sqlite_update",
        "document_files_append_only_sqlite_delete",
        "jobs_execution_evidence_guard_insert",
        "jobs_execution_evidence_guard_update",
        "source_intakes_write_guard_insert",
        "source_intakes_write_guard_update",
        "source_intakes_delete_guard",
        "upload_idempotency_reservations_guard_insert",
        "upload_idempotency_reservations_guard_update",
        "upload_idempotency_reservations_guard_delete",
        "schema_mappings_immutable_sqlite_update",
        "schema_mappings_immutable_sqlite_delete",
        "mapping_sets_immutable_sqlite_update",
        "mapping_sets_immutable_sqlite_delete",
        "mapping_set_entries_immutable_sqlite_update",
        "mapping_set_entries_immutable_sqlite_delete",
        "mapping_sets_exact_source_guard",
        "extraction_batches_exact_source_guard",
        "extraction_batches_reconciliation_guard",
        "extraction_batches_mutation_guard_sqlite",
        "extraction_batches_delete_guard_sqlite",
        "extracted_records_batch_lineage_guard_sqlite",
        "extracted_records_batch_insert_guard_sqlite",
        "extracted_records_batch_delete_guard_sqlite",
        "chunks_batch_lineage_guard_sqlite",
        "chunks_batch_insert_guard_sqlite",
        "candidate_review_decisions_insert_guard_sqlite",
        "candidate_review_decisions_immutable_sqlite_update",
        "candidate_review_decisions_immutable_sqlite_delete",
        "verified_records_active_candidate_guard_sqlite",
        "extraction_batches_activation_insert_guard_sqlite",
        "extraction_batches_activation_update_guard_sqlite",
        "duplicate_flags_scope_guard_sqlite_insert",
        "duplicate_flags_scope_immutable_sqlite_update",
    ):
        if name not in triggers:
            problems.append(f"SQLite trigger missing: {name}")

    if problems:
        raise SQLiteSchemaUpgradeError(
            "local SQLite schema has an unsupported layout: " + "; ".join(problems)
        )


async def _sqlite_table_columns(
    connection: AsyncConnection, table: str
) -> dict[str, tuple[object, ...]]:
    result = await connection.execute(text(f"PRAGMA table_info({table})"))
    return {str(row[1]): tuple(row) for row in result}


def _malformed_verified_record_columns(columns: dict[str, tuple[object, ...]]) -> list[str]:
    malformed: list[str] = []
    for name, prefixes in _SQLITE_TYPE_FAMILIES.items():
        definition = columns.get(name)
        if definition is None:
            continue
        type_name = str(definition[2]).upper()
        if not type_name.startswith(prefixes):
            malformed.append(f"{name} has incompatible type {type_name!r}")
    version = columns.get("version")
    if version is not None:
        if not bool(version[3]):
            malformed.append("version must be NOT NULL")
        default = version[4]
        if default is not None and str(default).strip("()'") != "1":
            malformed.append("version has an incompatible default")
    for name in ("expense_kind", "due_date"):
        definition = columns.get(name)
        if definition is not None and bool(definition[3]):
            malformed.append(f"{name} must allow NULL")
    return malformed


def _malformed_job_evidence_columns(
    columns: dict[str, tuple[object, ...]],
) -> list[str]:
    malformed: list[str] = []
    required = {
        "execution_profile": (("VARCHAR", "TEXT"), True, {"legacy_compat"}),
        "sandbox_verified": (("BOOLEAN", "INTEGER", "INT"), True, {"false", "0"}),
        "requirements_digest": (
            ("VARCHAR", "TEXT"),
            True,
            {_REQUIREMENTS_EMPTY_DIGEST},
        ),
        "required_components": (("JSON", "TEXT"), True, {"[]"}),
    }
    for name, (prefixes, must_not_null, defaults) in required.items():
        definition = columns.get(name)
        if definition is None:
            continue
        type_name = str(definition[2]).upper()
        if not type_name.startswith(prefixes):
            malformed.append(f"jobs: {name} has incompatible type {type_name!r}")
        if must_not_null and not bool(definition[3]):
            malformed.append(f"jobs: {name} must be NOT NULL")
        default = definition[4]
        normalized_default = "" if default is None else str(default).strip("()'").lower()
        if normalized_default not in defaults:
            malformed.append(f"jobs: {name} has an incompatible default")
    for name in ("registry_digest", "capabilities_digest"):
        definition = columns.get(name)
        if definition is not None and bool(definition[3]):
            malformed.append(f"jobs: {name} must allow NULL legacy evidence")
    return malformed
