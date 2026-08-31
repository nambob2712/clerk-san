"""Focused decision, activation-evidence, and SQLite mirror contracts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from clerksan.db.models import (
    Base,
    CandidateDecisionAction,
    ExtractionStatus,
)
from clerksan.db.sqlite_schema import upgrade_sqlite_demo_schema

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"


def _normalized_migration(filename: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        (MIGRATIONS / filename).read_text(encoding="utf-8"),
    ).lower()


def test_migration_chain_through_decision_schema_remains_frozen() -> None:
    expected = {
        "0015_universal_intake.sql": (
            "5af1193382a45ab15f1bbfabd0267f26b341432afa322ec45db5afed10282baa"
        ),
        "0016_extraction_batches_and_mappings.sql": (
            "1d06da60d60aeba506f9c716e1a553f98d9cd80548ff061afddbcf109023b522"
        ),
        "0017_candidate_review_decisions.sql": (
            "4817826708f6e4456a737ad489e579beac05b7b2818c4098eb89870242e0025b"
        ),
    }

    for filename, checksum in expected.items():
        assert hashlib.sha256((MIGRATIONS / filename).read_bytes()).hexdigest() == checksum


def test_decision_migration_is_append_only_exact_versioned_and_non_authoritative() -> None:
    sql = _normalized_migration("0017_candidate_review_decisions.sql")

    assert "create table candidate_review_decisions" in sql
    for column in (
        "batch_id uuid not null",
        "extraction_id uuid not null",
        "decision_revision integer not null",
        "expected_extraction_version integer not null",
        "supersedes_decision_id uuid",
        "action text not null",
        "corrected_payload jsonb",
        "corrected_financial_subtype text",
        "exclusion_reason text",
        "actor text not null",
    ):
        assert column in sql
    assert "candidate_review_decisions_linear_revision_key" in sql
    assert "candidate_review_decisions_one_successor_key" in sql
    assert "extracted_records_id_batch_id_key" in sql
    assert "candidate_review_decisions_exact_candidate_fkey" in sql
    assert "candidate_review_decisions_exact_predecessor_fkey" in sql
    assert "candidate_review_decisions_action_payload_shape" in sql
    assert "candidate_review_decisions_insert_guard" in sql
    assert "for update of candidate, batch" in sql
    assert "candidate_version <> new.expected_extraction_version" in sql
    assert "candidate decision revision is stale or would fork history" in sql
    assert "candidate_review_decisions_immutable" in sql
    assert "before update or delete on candidate_review_decisions" in sql
    assert "candidate_review_decisions_insert_audit" in sql
    assert "correction_sha256" in sql
    assert "reason_sha256" in sql
    assert "verified_records_active_candidate_guard" in sql
    assert "deferrable initially deferred" in sql
    assert "verified records require one active approved financial candidate" in sql

    assert "update extracted_records set status" not in sql
    assert "insert into verified_records" not in sql
    for forbidden_status in ("accepted", "included", "excluded"):
        assert f"status = '{forbidden_status}'" not in sql


def test_activation_and_duplicate_scope_migration_constraints_are_additive() -> None:
    sql = _normalized_migration("0017_candidate_review_decisions.sql")

    for column in (
        "add column activation_vector_sha256 text",
        "add column activated_by text",
        "add column activated_at timestamptz",
        "add column activation_included_count integer",
        "add column activation_excluded_count integer",
        "add column accepted_exclusions boolean not null default false",
        "add column accepted_empty boolean not null default false",
    ):
        assert column in sql
    assert "extraction_batches_activation_metadata_complete" in sql
    assert "extraction_batches_activation_counts_reconcile" in sql
    assert "extraction_batches_empty_activation_consent" in sql
    assert "extraction_batches_exclusion_activation_consent" in sql
    assert "extraction_batches_activation_insert_guard" in sql
    assert "activation evidence is immutable once recorded" in sql
    assert "new active batches require complete activation evidence" in sql
    assert "'activation_evidence'" in sql

    assert "drop constraint if exists duplicate_flags_document_suspect_key" in sql
    assert "duplicate_flags_scope_shape" in sql
    assert "duplicate_flags_exact_source_fkey" in sql
    assert "duplicate_flags_exact_candidate_fkey" in sql
    assert "duplicate_flags_document_scope_key" in sql
    assert "duplicate_flags_source_scope_key" in sql
    assert "duplicate_flags_candidate_scope_key" in sql
    assert "duplicate_flags_scope_insert_guard" in sql
    assert "duplicate evidence scope is immutable" in sql


def test_models_expose_decisions_activation_evidence_and_scoped_duplicates() -> None:
    assert [action.value for action in CandidateDecisionAction] == ["include", "exclude"]
    assert [status.value for status in ExtractionStatus] == [
        "pending_review",
        "approved",
        "rejected",
        "superseded",
    ]

    decision = Base.metadata.tables["candidate_review_decisions"]
    assert {
        "id",
        "batch_id",
        "extraction_id",
        "decision_revision",
        "expected_extraction_version",
        "supersedes_decision_id",
        "action",
        "corrected_payload",
        "corrected_financial_subtype",
        "exclusion_reason",
        "actor",
        "created_at",
    } == set(decision.columns.keys())
    decision_constraints = {constraint.name for constraint in decision.constraints}
    assert {
        "candidate_review_decisions_exact_identity_key",
        "candidate_review_decisions_linear_revision_key",
        "candidate_review_decisions_one_successor_key",
        "candidate_review_decisions_exact_candidate_fkey",
        "candidate_review_decisions_exact_predecessor_fkey",
        "candidate_review_decisions_action_payload_shape",
        "candidate_review_decisions_predecessor_shape",
    } <= decision_constraints

    extracted = Base.metadata.tables["extracted_records"]
    extracted_constraints = {constraint.name for constraint in extracted.constraints}
    assert "extracted_records_id_batch_id_key" in extracted_constraints

    batch = Base.metadata.tables["extraction_batches"]
    assert {
        "activation_vector_sha256",
        "activated_by",
        "activated_at",
        "activation_included_count",
        "activation_excluded_count",
        "accepted_exclusions",
        "accepted_empty",
    } <= set(batch.columns.keys())

    duplicate = Base.metadata.tables["duplicate_flags"]
    assert {
        "source_file_id",
        "source_version",
        "batch_id",
        "extraction_id",
        "candidate_key",
        "record_kind",
    } <= set(duplicate.columns.keys())
    duplicate_constraints = {constraint.name for constraint in duplicate.constraints}
    assert "duplicate_flags_scope_shape" in duplicate_constraints
    assert "duplicate_flags_exact_source_fkey" in duplicate_constraints
    assert "duplicate_flags_exact_candidate_fkey" in duplicate_constraints


async def _seed_candidate_graph(connection: AsyncConnection) -> dict[str, str]:
    ids = {
        "document": uuid4().hex,
        "suspected_document": uuid4().hex,
        "source": uuid4().hex,
        "intake": uuid4().hex,
        "batch": uuid4().hex,
        "extraction": uuid4().hex,
    }
    source_sha256 = "a" * 64
    await connection.execute(
        text(
            "INSERT INTO documents (id, document_class, status, source_filename) VALUES "
            "(:document, 'receipt', 'in_review', 'source.png'), "
            "(:suspected_document, 'receipt', 'verified', 'suspected.png')"
        ),
        ids,
    )
    await connection.execute(
        text(
            "INSERT INTO document_files ("
            "id, document_id, version, kind, content_path, sha256, mime, source_filename"
            ") VALUES ("
            ":source, :document, 1, 'original', 'originals/source.png', :sha256, "
            "'image/png', 'source.png')"
        ),
        {**ids, "sha256": source_sha256},
    )
    await connection.execute(
        text(
            "INSERT INTO source_intakes ("
            "id, document_id, source_file_id, source_version, source_sha256, "
            "policy_version, requirements_digest, intake_intent, state, "
            "execution_profile, sandbox_verified"
            ") VALUES ("
            ":intake, :document, :source, 1, :sha256, 'phase4-test', :sha256, "
            "'bill_scan', 'processed', 'legacy_compat', false)"
        ),
        {**ids, "sha256": source_sha256},
    )
    reconciliation = json.dumps(
        {
            "mapped_candidate": 1,
            "residual_generic_candidate": 0,
            "explicit_ignore": 0,
            "blank": 0,
            "parse_error": 0,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    await connection.execute(
        text(
            "INSERT INTO extraction_batches ("
            "id, source_intake_id, document_id, source_file_id, source_version, source_sha256, "
            "normalized_sha256, structure_fingerprint, producer, producer_version, origin, "
            "intake_intent, lifecycle, idempotency_key, candidate_count, "
            "reconciliation_counts, reconciliation_digest, version"
            ") VALUES ("
            ":batch, :intake, :document, :source, 1, :sha256, :normalized, :structure, "
            "'test-producer', '1', 'direct', 'bill_scan', 'open', 'phase4-test', 1, "
            ":reconciliation, :digest, 1)"
        ),
        {
            **ids,
            "sha256": source_sha256,
            "normalized": "b" * 64,
            "structure": "c" * 64,
            "reconciliation": reconciliation,
            "digest": hashlib.sha256(reconciliation.encode("utf-8")).hexdigest(),
        },
    )
    await connection.execute(
        text(
            "INSERT INTO extracted_records ("
            "id, document_id, source_file_id, source_version, batch_id, candidate_ordinal, "
            "candidate_key, record_kind, financial_subtype, source_locator, row_fingerprint, "
            "validation_issues, evidence_group_keys, payload, field_confidences, source_spans, "
            "model_name, prompt_version, status, version"
            ") VALUES ("
            ":extraction, :document, :source, 1, :batch, 1, :candidate_key, 'financial', "
            "'receipt', 'page:1', :row_fingerprint, '[]', '[]', :payload, '{}', '{}', "
            "'test-model', 'test-prompt', 'pending_review', 1)"
        ),
        {
            **ids,
            "candidate_key": "d" * 64,
            "row_fingerprint": "e" * 64,
            "payload": json.dumps({"counterparty": {"value": "Local Store"}}),
        },
    )
    return ids


@pytest.mark.asyncio
async def test_sqlite_decision_chain_is_append_only_and_staging_creates_no_authority() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys = ON"))
            await connection.run_sync(Base.metadata.create_all)
            await upgrade_sqlite_demo_schema(connection)
            ids = await _seed_candidate_graph(connection)
            first = uuid4().hex
            await connection.execute(
                text(
                    "INSERT INTO candidate_review_decisions ("
                    "id, batch_id, extraction_id, decision_revision, "
                    "expected_extraction_version, action, corrected_payload, "
                    "corrected_financial_subtype, actor"
                    ") VALUES ("
                    ":id, :batch, :extraction, 1, 1, 'include', :payload, 'receipt', 'reviewer')"
                ),
                {
                    **ids,
                    "id": first,
                    "payload": json.dumps({"counterparty": "Reviewed Store"}),
                },
            )

            assert (
                await connection.scalar(
                    text("SELECT status FROM extracted_records WHERE id = :extraction"), ids
                )
                == "pending_review"
            )
            assert await connection.scalar(text("SELECT count(*) FROM verified_records")) == 0
            with pytest.raises(DBAPIError, match="active approved financial candidate"):
                await connection.execute(
                    text(
                        "INSERT INTO verified_records ("
                        "id, document_id, extracted_id, transaction_date, total_amount, "
                        "counterparty, reviewer, version"
                        ") VALUES ("
                        ":id, :document, :extraction, '2026-08-23', 100, "
                        "'Local Store', 'reviewer', 1)"
                    ),
                    {**ids, "id": uuid4().hex},
                )

            with pytest.raises(DBAPIError, match="append-only"):
                await connection.execute(
                    text("UPDATE candidate_review_decisions SET actor = 'other' WHERE id = :id"),
                    {"id": first},
                )
            with pytest.raises(DBAPIError, match="append-only"):
                await connection.execute(
                    text("DELETE FROM candidate_review_decisions WHERE id = :id"), {"id": first}
                )
            with pytest.raises(DBAPIError, match="stale or would fork"):
                await connection.execute(
                    text(
                        "INSERT INTO candidate_review_decisions ("
                        "id, batch_id, extraction_id, decision_revision, "
                        "expected_extraction_version, supersedes_decision_id, action, "
                        "exclusion_reason, actor"
                        ") VALUES ("
                        ":id, :batch, :extraction, 3, 1, :first, 'exclude', 'duplicate', "
                        "'reviewer')"
                    ),
                    {**ids, "id": uuid4().hex, "first": first},
                )

            second = uuid4().hex
            await connection.execute(
                text(
                    "INSERT INTO candidate_review_decisions ("
                    "id, batch_id, extraction_id, decision_revision, "
                    "expected_extraction_version, supersedes_decision_id, action, "
                    "exclusion_reason, actor"
                    ") VALUES ("
                    ":id, :batch, :extraction, 2, 1, :first, 'exclude', 'duplicate', 'reviewer')"
                ),
                {**ids, "id": second, "first": first},
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT max(decision_revision) FROM candidate_review_decisions "
                        "WHERE batch_id = :batch AND extraction_id = :extraction"
                    ),
                    ids,
                )
                == 2
            )

            with pytest.raises(DBAPIError, match="version is stale"):
                await connection.execute(
                    text(
                        "INSERT INTO candidate_review_decisions ("
                        "id, batch_id, extraction_id, decision_revision, "
                        "expected_extraction_version, supersedes_decision_id, action, actor"
                        ") VALUES ("
                        ":id, :batch, :extraction, 3, 2, :second, 'include', 'reviewer')"
                    ),
                    {**ids, "id": uuid4().hex, "second": second},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_activation_evidence_and_candidate_duplicate_scope_are_guarded() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys = ON"))
            await connection.run_sync(Base.metadata.create_all)
            await upgrade_sqlite_demo_schema(connection)
            ids = await _seed_candidate_graph(connection)

            with pytest.raises(DBAPIError, match="require activation evidence"):
                await connection.execute(
                    text(
                        "UPDATE extraction_batches SET lifecycle = 'active', version = 2 "
                        "WHERE id = :batch"
                    ),
                    ids,
                )

            await connection.execute(
                text(
                    "UPDATE extraction_batches SET lifecycle = 'active', "
                    "activation_vector_sha256 = :vector, activated_by = 'reviewer', "
                    "activated_at = CURRENT_TIMESTAMP, activation_included_count = 0, "
                    "activation_excluded_count = 1, accepted_exclusions = true, version = 2 "
                    "WHERE id = :batch"
                ),
                {**ids, "vector": "f" * 64},
            )
            with pytest.raises(DBAPIError, match="immutable once recorded"):
                await connection.execute(
                    text(
                        "UPDATE extraction_batches SET activation_vector_sha256 = :vector, "
                        "version = 3 WHERE id = :batch"
                    ),
                    {**ids, "vector": "0" * 64},
                )

            document_scope_id = uuid4().hex
            await connection.execute(
                text(
                    "INSERT INTO duplicate_flags ("
                    "id, document_id, suspected_document_id, reason, score, evidence"
                    ") VALUES ("
                    ":id, :document, :suspected_document, 'relationship', 0.5, '{}')"
                ),
                {**ids, "id": document_scope_id},
            )
            candidate_scope_id = uuid4().hex
            await connection.execute(
                text(
                    "INSERT INTO duplicate_flags ("
                    "id, document_id, suspected_document_id, source_file_id, source_version, "
                    "batch_id, extraction_id, candidate_key, record_kind, reason, score, evidence"
                    ") VALUES ("
                    ":id, :document, :suspected_document, :source, 1, :batch, :extraction, "
                    ":candidate_key, 'financial', 'candidate_match', 0.8, '{}')"
                ),
                {**ids, "id": candidate_scope_id, "candidate_key": "d" * 64},
            )
            assert await connection.scalar(text("SELECT count(*) FROM duplicate_flags")) == 2

            with pytest.raises(DBAPIError, match="exact candidate lineage"):
                await connection.execute(
                    text(
                        "INSERT INTO duplicate_flags ("
                        "id, document_id, suspected_document_id, source_file_id, source_version, "
                        "batch_id, extraction_id, candidate_key, record_kind, "
                        "reason, score, evidence"
                        ") VALUES ("
                        ":id, :document, :suspected_document, :source, 1, :batch, :extraction, "
                        ":candidate_key, 'financial', 'wrong_candidate', 0.8, '{}')"
                    ),
                    {**ids, "id": uuid4().hex, "candidate_key": "0" * 64},
                )
            with pytest.raises(DBAPIError, match="scope is immutable"):
                await connection.execute(
                    text(
                        "UPDATE duplicate_flags SET source_file_id = NULL, source_version = NULL, "
                        "batch_id = NULL, extraction_id = NULL, candidate_key = NULL, "
                        "record_kind = NULL WHERE id = :id"
                    ),
                    {"id": candidate_scope_id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_upgrade_replaces_legacy_document_wide_duplicate_uniqueness() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys = ON"))
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("DROP TABLE duplicate_flags"))
            await connection.execute(
                text(
                    "CREATE TABLE duplicate_flags ("
                    "id CHAR(32) NOT NULL PRIMARY KEY, "
                    "document_id CHAR(32) NOT NULL REFERENCES documents(id), "
                    "suspected_document_id CHAR(32) NOT NULL REFERENCES documents(id), "
                    "reason TEXT NOT NULL, score NUMERIC(6, 4) NOT NULL, "
                    "evidence JSON NOT NULL DEFAULT '{}', "
                    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "UNIQUE (document_id, suspected_document_id))"
                )
            )

            await upgrade_sqlite_demo_schema(connection)

            columns = {
                str(row[1])
                for row in await connection.execute(text("PRAGMA table_info(duplicate_flags)"))
            }
            indexes = {
                str(row[1])
                for row in await connection.execute(text("PRAGMA index_list(duplicate_flags)"))
            }
        assert set(_DUPLICATE_SCOPE_COLUMN_NAMES) <= columns
        assert {
            "duplicate_flags_document_scope_key",
            "duplicate_flags_source_scope_key",
            "duplicate_flags_candidate_scope_key",
        } <= indexes
    finally:
        await engine.dispose()


_DUPLICATE_SCOPE_COLUMN_NAMES = (
    "source_file_id",
    "source_version",
    "batch_id",
    "extraction_id",
    "candidate_key",
    "record_kind",
)
