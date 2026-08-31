"""Focused model and SQLite-upgrade coverage for extraction cohorts."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from clerksan.db.models import (
    Base,
    BatchLifecycle,
    FinancialSubtype,
    RecordKind,
)
from clerksan.db.sqlite_schema import SQLiteSchemaUpgradeError, upgrade_sqlite_demo_schema


def test_persisted_candidate_enums_are_canonical_and_do_not_extend_review_status() -> None:
    assert [kind.value for kind in RecordKind] == ["financial", "generic_document"]
    assert [subtype.value for subtype in FinancialSubtype] == [
        "transaction",
        "receipt",
        "invoice",
        "bill",
        "recurring_bill",
        "quote",
        "other_financial",
    ]
    assert [lifecycle.value for lifecycle in BatchLifecycle] == [
        "open",
        "ready_to_activate",
        "active",
        "superseded",
        "rejected",
    ]


def test_models_expose_exact_source_mapping_batch_and_chunk_lineage() -> None:
    assert {"schema_mappings", "mapping_sets", "mapping_set_entries", "extraction_batches"} <= (
        Base.metadata.tables.keys()
    )
    assert {
        "batch_id",
        "candidate_ordinal",
        "candidate_key",
        "record_kind",
        "financial_subtype",
        "source_locator",
        "row_fingerprint",
        "validation_issues",
        "evidence_group_keys",
    } <= set(Base.metadata.tables["extracted_records"].columns.keys())
    assert {
        "batch_id",
        "extraction_id",
        "record_kind",
        "source_file_id",
        "source_version",
        "candidate_key",
    } <= set(Base.metadata.tables["chunks"].columns.keys())

    extracted_constraints = {
        constraint.name for constraint in Base.metadata.tables["extracted_records"].constraints
    }
    chunk_constraints = {
        constraint.name for constraint in Base.metadata.tables["chunks"].constraints
    }
    assert "extracted_records_exact_batch_source_fkey" in extracted_constraints
    assert "extracted_records_batch_lineage_complete" in extracted_constraints
    assert "chunks_exact_candidate_lineage_fkey" in chunk_constraints
    assert "chunks_candidate_lineage_complete" in chunk_constraints


async def _seed_legacy_candidate(
    connection: AsyncConnection,
    *,
    document_id: str,
    source_file_id: str,
    intake_id: str,
    extraction_id: str,
    verified_id: str,
    status: str = "approved",
    source_sha256: str = "a" * 64,
) -> None:
    await connection.execute(
        text(
            "INSERT INTO documents (id, document_class, status, source_filename) "
            "VALUES (:id, 'receipt', 'verified', 'legacy.png')"
        ),
        {"id": document_id},
    )
    await connection.execute(
        text(
            "INSERT INTO document_files ("
            "id, document_id, version, kind, content_path, sha256, mime, source_filename"
            ") VALUES ("
            ":id, :document_id, 1, 'original', 'originals/legacy.png', :sha256, "
            "'image/png', 'legacy.png')"
        ),
        {"id": source_file_id, "document_id": document_id, "sha256": source_sha256},
    )
    await connection.execute(
        text(
            "INSERT INTO source_intakes ("
            "id, document_id, source_file_id, source_version, source_sha256, canonical_mime, "
            "policy_version, intake_intent, state, execution_profile, sandbox_verified"
            ") VALUES ("
            ":id, :document_id, :source_file_id, 1, :sha256, 'image/png', "
            "'legacy-test', 'bill_scan', 'processed', 'legacy_compat', false)"
        ),
        {
            "id": intake_id,
            "document_id": document_id,
            "source_file_id": source_file_id,
            "sha256": source_sha256,
        },
    )
    await _insert_legacy_extraction(
        connection,
        document_id=document_id,
        source_file_id=source_file_id,
        extraction_id=extraction_id,
        verified_id=verified_id,
        status=status,
    )


async def _insert_legacy_extraction(
    connection: AsyncConnection,
    *,
    document_id: str,
    source_file_id: str,
    extraction_id: str,
    verified_id: str,
    status: str,
) -> None:
    payload = json.dumps(
        {
            "transaction_date": {"value": "2026-08-22", "confidence": 0.9},
            "total_amount": {"value": 1200, "confidence": 0.9},
            "counterparty": {"value": "Legacy Shop", "confidence": 0.9},
        }
    )
    await connection.execute(
        text(
            "INSERT INTO extracted_records ("
            "id, document_id, source_file_id, source_version, payload, field_confidences, "
            "source_spans, model_name, prompt_version, status, version"
            ") VALUES ("
            ":id, :document_id, :source_file_id, 1, :payload, '{}', '{}', "
            "'legacy-model', 'legacy-prompt', :status, 1)"
        ),
        {
            "id": extraction_id,
            "document_id": document_id,
            "source_file_id": source_file_id,
            "payload": payload,
            "status": status,
        },
    )
    if status == "approved":
        await connection.execute(
            text(
                "INSERT INTO verified_records ("
                "id, document_id, extracted_id, transaction_date, total_amount, counterparty, "
                "reviewer, version"
                ") VALUES ("
                ":id, :document_id, :extracted_id, '2026-08-22', 1200, "
                "'Legacy Shop', 'reviewer', 1)"
            ),
            {
                "id": verified_id,
                "document_id": document_id,
                "extracted_id": extraction_id,
            },
        )


async def _replace_chunks_with_legacy_layout(connection: AsyncConnection) -> None:
    await connection.execute(text("DROP TABLE chunks"))
    await connection.execute(
        text(
            "CREATE TABLE chunks ("
            "id CHAR(32) NOT NULL PRIMARY KEY, "
            "document_id CHAR(32) NOT NULL REFERENCES documents(id) ON DELETE RESTRICT, "
            "seq INTEGER NOT NULL CHECK (seq >= 0), "
            "heading_path TEXT NOT NULL DEFAULT '', "
            "text TEXT NOT NULL, embedding JSON NOT NULL, embed_model TEXT NOT NULL, "
            "embed_model_digest TEXT NOT NULL, token_count INTEGER NOT NULL, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "UNIQUE (document_id, seq))"
        )
    )


@pytest.mark.asyncio
async def test_sqlite_upgrade_creates_one_deterministic_singleton_and_binds_active_chunks() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    document_id = uuid4().hex
    source_file_id = uuid4().hex
    extraction_id = uuid4().hex
    spreadsheet_id = uuid4().hex
    chunk_id = uuid4().hex
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys = ON"))
            await connection.run_sync(Base.metadata.create_all)
            await _replace_chunks_with_legacy_layout(connection)
            await _seed_legacy_candidate(
                connection,
                document_id=document_id,
                source_file_id=source_file_id,
                intake_id=uuid4().hex,
                extraction_id=extraction_id,
                verified_id=uuid4().hex,
            )
            await connection.execute(
                text(
                    "INSERT INTO spreadsheet_rows ("
                    'id, document_id, source_version, source_location, row_index, "values", '
                    "value_types) VALUES ("
                    ":id, :document_id, 1, 'sheet:legacy', 1, :values, :types)"
                ),
                {
                    "id": spreadsheet_id,
                    "document_id": document_id,
                    "values": '{"literal":"=1+1"}',
                    "types": '{"literal":"string"}',
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO chunks ("
                    "id, document_id, seq, heading_path, text, embedding, embed_model, "
                    "embed_model_digest, token_count"
                    ") VALUES ("
                    ":id, :document_id, 0, '', 'legacy chunk', :embedding, "
                    "'nomic-embed-text:v1.5', :digest, 2)"
                ),
                {
                    "id": chunk_id,
                    "document_id": document_id,
                    "embedding": json.dumps([0.0] * 768),
                    "digest": ("0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"),
                },
            )

            await upgrade_sqlite_demo_schema(connection)
            await upgrade_sqlite_demo_schema(connection)

            batch = (
                await connection.execute(
                    text(
                        "SELECT id, origin, lifecycle, intake_intent, candidate_count "
                        "FROM extraction_batches"
                    )
                )
            ).one()
            candidate = (
                await connection.execute(
                    text(
                        "SELECT batch_id, candidate_ordinal, record_kind, financial_subtype, "
                        "source_locator FROM extracted_records"
                    )
                )
            ).one()
            chunk = (
                await connection.execute(
                    text("SELECT batch_id, extraction_id, record_kind FROM chunks")
                )
            ).one()
            spreadsheet = (
                await connection.execute(
                    text('SELECT id, "values", value_types FROM spreadsheet_rows')
                )
            ).one()

        assert batch == (extraction_id, "legacy_singleton", "active", "bill_scan", 1)
        assert candidate == (extraction_id, 1, "financial", "receipt", "legacy_unknown")
        assert chunk == (extraction_id, extraction_id, "financial")
        assert spreadsheet == (
            spreadsheet_id,
            '{"literal":"=1+1"}',
            '{"literal":"string"}',
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_upgrade_aborts_ambiguous_approved_authority_before_batch_writes() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    document_id = uuid4().hex
    source_file_id = uuid4().hex
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys = ON"))
            await connection.run_sync(Base.metadata.create_all)
            await _seed_legacy_candidate(
                connection,
                document_id=document_id,
                source_file_id=source_file_id,
                intake_id=uuid4().hex,
                extraction_id=uuid4().hex,
                verified_id=uuid4().hex,
            )
            await _insert_legacy_extraction(
                connection,
                document_id=document_id,
                source_file_id=source_file_id,
                extraction_id=uuid4().hex,
                verified_id=uuid4().hex,
                status="approved",
            )

            with pytest.raises(SQLiteSchemaUpgradeError, match="owner reconciliation"):
                await upgrade_sqlite_demo_schema(connection)
            assert (await connection.scalar(text("SELECT count(*) FROM extraction_batches"))) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_restart_backfills_a_legacy_candidate_written_after_prior_upgrade() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    extraction_id = uuid4().hex
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys = ON"))
            await connection.run_sync(Base.metadata.create_all)
            await upgrade_sqlite_demo_schema(connection)
            await _seed_legacy_candidate(
                connection,
                document_id=uuid4().hex,
                source_file_id=uuid4().hex,
                intake_id=uuid4().hex,
                extraction_id=extraction_id,
                verified_id=uuid4().hex,
                status="pending_review",
            )

            await upgrade_sqlite_demo_schema(connection)
            result = (
                await connection.execute(
                    text(
                        "SELECT batch.id, batch.lifecycle, extraction.candidate_ordinal "
                        "FROM extraction_batches AS batch "
                        "JOIN extracted_records AS extraction ON extraction.batch_id = batch.id"
                    )
                )
            ).one()

        assert result == (extraction_id, "open", 1)
    finally:
        await engine.dispose()
