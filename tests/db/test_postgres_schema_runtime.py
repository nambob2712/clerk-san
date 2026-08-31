"""Opt-in PostgreSQL coverage for the migrated Clerk-san schema and pgvector index."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import clerksan.db.migrate as migrate_module
from clerksan.config import Settings
from clerksan.db.engine import dispose_engines, get_engine
from clerksan.db.migrate import discover_migrations, run_migrations

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
POSTGRES_IMAGE = (
    "pgvector/pgvector:0.8.5-pg16-bookworm"
    "@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
)
PLANNER_FIXTURE_ROWS = 4_096
RUN_POSTGRES_TESTS = os.getenv("CLERKSAN_RUN_POSTGRES_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not (RUN_POSTGRES_TESTS and shutil.which("docker")),
    reason="set CLERKSAN_RUN_POSTGRES_TESTS=1 with Docker installed to run PostgreSQL schema tests",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_for_database(engine: AsyncEngine) -> None:
    deadline = time.monotonic() + 30
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return
        except (SQLAlchemyError, OSError) as current_error:
            error = current_error
            await asyncio.sleep(0.25)
    raise AssertionError(f"PostgreSQL did not become ready: {error}")


def _migration_checksums() -> dict[str, str]:
    checksums: dict[str, str] = {}
    for path in discover_migrations(MIGRATIONS):
        content = path.read_text(encoding="utf-8")
        checksums[path.name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return checksums


def _copy_migrations_before(destination: Path, cutoff_filename: str) -> list[str]:
    """Create an on-disk schema snapshot for an upgrade-path migration test."""

    destination.mkdir()
    copied: list[str] = []
    for path in discover_migrations(MIGRATIONS):
        if path.name >= cutoff_filename:
            continue
        shutil.copy2(path, destination / path.name)
        copied.append(path.name)
    return copied


def _reflect_public_schema(connection: object) -> dict[str, object]:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names(schema="public"))
    return {
        "tables": tables,
        "columns": {
            table: {column["name"] for column in inspector.get_columns(table, schema="public")}
            for table in tables
        },
        "indexes": {
            table: {index["name"] for index in inspector.get_indexes(table, schema="public")}
            for table in tables
        },
    }


async def _assert_runtime_schema(engine: AsyncEngine, checksums: dict[str, str]) -> None:
    async with engine.connect() as connection:
        reflected = await connection.run_sync(_reflect_public_schema)
        expected_tables = {
            "schema_migrations",
            "documents",
            "document_files",
            "extracted_records",
            "verified_records",
            "audit_log",
            "jobs",
            "chunks",
            "issuers",
            "recurring_bills",
            "embedded_media",
            "spreadsheet_rows",
            "source_intakes",
            "upload_idempotency_reservations",
            "worker_capability_leases",
            "schema_mappings",
            "mapping_sets",
            "mapping_set_entries",
            "extraction_batches",
            "candidate_review_decisions",
            "duplicate_flags",
        }
        assert expected_tables <= reflected["tables"]

        columns = reflected["columns"]
        assert {
            "source_file_id",
            "source_version",
            "rejection_reason",
            "batch_id",
            "candidate_ordinal",
            "candidate_key",
            "record_kind",
            "financial_subtype",
            "source_locator",
            "row_fingerprint",
            "validation_issues",
            "evidence_group_keys",
        } <= columns["extracted_records"]
        assert {"source_version"} <= columns["embedded_media"]
        assert {"source_version"} <= columns["spreadsheet_rows"]
        assert {"review_corrections", "superseded_at"} <= columns["recurring_bills"]
        assert {"source_file_id", "source_version", "page_number"} <= columns["document_files"]
        assert {
            "execution_profile",
            "sandbox_verified",
            "registry_digest",
            "capabilities_digest",
            "requirements_digest",
            "required_components",
        } <= columns["jobs"]
        assert {
            "document_id",
            "source_file_id",
            "source_version",
            "source_sha256",
            "duplicate_of_document_id",
            "upload_idempotency_key",
            "intent_digest",
            "execution_profile",
            "sandbox_verified",
            "version",
        } <= columns["source_intakes"]
        assert {
            "source_intake_id",
            "document_id",
            "source_file_id",
            "source_version",
            "source_sha256",
            "normalized_sha256",
            "structure_fingerprint",
            "mapping_set_id",
            "mapping_set_version",
            "mapping_set_digest",
            "intake_intent",
            "lifecycle",
            "candidate_count",
            "reconciliation_counts",
            "reconciliation_digest",
            "activation_vector_sha256",
            "activated_by",
            "activated_at",
            "activation_included_count",
            "activation_excluded_count",
            "accepted_exclusions",
            "accepted_empty",
        } <= columns["extraction_batches"]
        assert {
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
        } <= columns["candidate_review_decisions"]
        assert {
            "document_id",
            "suspected_document_id",
            "source_file_id",
            "source_version",
            "batch_id",
            "extraction_id",
            "candidate_key",
            "record_kind",
        } <= columns["duplicate_flags"]
        assert {
            "batch_id",
            "extraction_id",
            "record_kind",
            "source_file_id",
            "source_version",
            "candidate_key",
        } <= columns["chunks"]

        indexes = reflected["indexes"]
        assert {
            "chunks_document_seq_idx",
            "chunks_embedding_hnsw_idx",
            "chunks_legacy_document_seq_key",
            "chunks_batch_candidate_seq_key",
        } <= indexes["chunks"]
        assert "verified_records_combined_search_idx" in indexes["verified_records"]
        assert {
            "document_files_original_sha256_key",
            "document_files_page_render_slot_key",
            "document_files_preview_manifest_key",
        } <= indexes["document_files"]
        assert {
            "candidate_review_decisions_latest_idx",
            "candidate_review_decisions_batch_created_idx",
        } <= indexes["candidate_review_decisions"]
        assert {
            "duplicate_flags_document_scope_key",
            "duplicate_flags_source_scope_key",
            "duplicate_flags_candidate_scope_key",
            "duplicate_flags_candidate_lookup_idx",
        } <= indexes["duplicate_flags"]
        assert "extraction_batches_activated_at_idx" in indexes["extraction_batches"]

        applied_rows = await connection.execute(
            text("SELECT filename, checksum FROM schema_migrations ORDER BY filename")
        )
        assert dict(applied_rows.tuples().all()) == checksums

        extensions = await connection.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('pgcrypto', 'vector')")
        )
        assert {row[0] for row in extensions} == {"pgcrypto", "vector"}

        vector_type = await connection.execute(
            text(
                "SELECT data_type, udt_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'chunks' "
                "AND column_name = 'embedding'"
            )
        )
        assert vector_type.one() == ("USER-DEFINED", "vector")

        hnsw_definition = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexname = 'chunks_embedding_hnsw_idx'"
            )
        )
        normalized_hnsw_definition = hnsw_definition.scalar_one().lower()
        assert "using hnsw" in normalized_hnsw_definition
        assert "vector_cosine_ops" in normalized_hnsw_definition

        constraints = await connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid IN ('public.extracted_records'::regclass, "
                "'public.verified_records'::regclass, "
                "'public.document_files'::regclass, 'public.source_intakes'::regclass, "
                "'public.mapping_sets'::regclass, 'public.extraction_batches'::regclass, "
                "'public.chunks'::regclass, "
                "'public.candidate_review_decisions'::regclass, "
                "'public.duplicate_flags'::regclass)"
            )
        )
        constraint_names = {row[0] for row in constraints}
        assert "extracted_records_source_file_document_fkey" in constraint_names
        assert "verified_records_extracted_id_key" in constraint_names
        assert "document_files_exact_source_identity_key" in constraint_names
        assert "document_files_source_identity_fkey" in constraint_names
        assert "source_intakes_source_identity_key" in constraint_names
        assert "source_intakes_id_source_identity_key" in constraint_names
        assert "source_intakes_exact_source_fkey" in constraint_names
        assert "mapping_sets_exact_source_fkey" in constraint_names
        assert "extraction_batches_exact_source_fkey" in constraint_names
        assert "extraction_batches_exact_mapping_set_fkey" in constraint_names
        assert "extracted_records_exact_batch_source_fkey" in constraint_names
        assert "chunks_exact_candidate_lineage_fkey" in constraint_names
        assert "extracted_records_id_batch_id_key" in constraint_names
        assert "candidate_review_decisions_exact_identity_key" in constraint_names
        assert "candidate_review_decisions_linear_revision_key" in constraint_names
        assert "candidate_review_decisions_one_successor_key" in constraint_names
        assert "candidate_review_decisions_exact_candidate_fkey" in constraint_names
        assert "candidate_review_decisions_exact_predecessor_fkey" in constraint_names
        assert "candidate_review_decisions_action_payload_shape" in constraint_names
        assert "candidate_review_decisions_predecessor_shape" in constraint_names
        assert "duplicate_flags_scope_shape" in constraint_names
        assert "duplicate_flags_exact_source_fkey" in constraint_names
        assert "duplicate_flags_exact_candidate_fkey" in constraint_names

        trigger_rows = await connection.execute(
            text(
                "SELECT relation.relname, trigger.tgname "
                "FROM pg_trigger AS trigger "
                "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                "WHERE relation.relnamespace = 'public'::regnamespace "
                "AND NOT trigger.tgisinternal"
            )
        )
        triggers = {(row[0], row[1]) for row in trigger_rows}
        assert {
            ("document_files", "document_files_append_only"),
            ("extracted_records", "extracted_records_content_immutable"),
            ("extracted_records", "extracted_records_rejection_reason_guard"),
            ("audit_log", "audit_log_write_guard"),
            ("document_files", "document_files_source_lineage_guard"),
            ("jobs", "jobs_execution_evidence_guard"),
            ("source_intakes", "source_intakes_write_guard"),
            ("source_intakes", "source_intakes_delete_guard"),
            ("source_intakes", "source_intakes_audit"),
            (
                "upload_idempotency_reservations",
                "upload_idempotency_reservations_guard",
            ),
            ("schema_mappings", "schema_mappings_immutable"),
            ("schema_mappings", "schema_mappings_insert_audit"),
            ("mapping_sets", "mapping_sets_immutable"),
            ("mapping_sets", "mapping_sets_insert_audit"),
            ("mapping_set_entries", "mapping_set_entries_immutable"),
            ("mapping_set_entries", "mapping_set_entries_insert_audit"),
            ("extraction_batches", "extraction_batches_mutation_guard"),
            ("extraction_batches", "extraction_batches_candidate_count_guard"),
            ("extraction_batches", "extraction_batches_lifecycle_audit"),
            ("extraction_batches", "extraction_batches_activation_insert_guard"),
            ("extracted_records", "extracted_records_candidate_count_guard"),
            ("extracted_records", "extracted_records_batch_membership_delete_guard"),
            ("candidate_review_decisions", "candidate_review_decisions_insert_guard"),
            ("candidate_review_decisions", "candidate_review_decisions_immutable"),
            ("candidate_review_decisions", "candidate_review_decisions_insert_audit"),
            ("verified_records", "verified_records_active_candidate_guard"),
            ("duplicate_flags", "duplicate_flags_scope_insert_guard"),
            ("duplicate_flags", "duplicate_flags_scope_immutable"),
            ("duplicate_flags", "duplicate_flags_insert_audit"),
        } <= triggers


async def _seed_and_assert_hnsw_plan(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "WITH seed_documents AS ("
                "INSERT INTO documents (id, document_class, status, source_filename) "
                "SELECT gen_random_uuid(), 'other', 'uploaded', "
                "'planner-fixture-' || sample.value || '.txt' "
                "FROM generate_series(1, CAST(:row_count AS integer)) AS sample(value) "
                "RETURNING id"
                "), numbered_documents AS ("
                "SELECT id, row_number() OVER (ORDER BY id) AS sequence "
                "FROM seed_documents"
                ") "
                "INSERT INTO chunks ("
                "document_id, seq, heading_path, text, embedding, embed_model, "
                "embed_model_digest, token_count"
                ") "
                "SELECT id, 0, 'planner', 'planner fixture', "
                "('[1,' || ((sequence % 31)::real / 31.0::real)::text || ',' "
                "|| repeat('0,', 765) || '0]')::vector, "
                "'nomic-embed-text:v1.5', "
                "'0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f', "
                "1 FROM numbered_documents"
            ),
            {"row_count": PLANNER_FIXTURE_ROWS},
        )
        await connection.execute(text("ANALYZE chunks"))
        query_embedding = "[1," + ",".join("0" for _ in range(767)) + "]"
        result = await connection.execute(
            text(
                "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF) "
                "SELECT id FROM chunks "
                "ORDER BY embedding <=> CAST(:query_embedding AS vector) LIMIT 10"
            ),
            {"query_embedding": query_embedding},
        )
        plan = result.scalar_one()
        assert "chunks_embedding_hnsw_idx" in json.dumps(plan)


async def _assert_rejection_reason_lifecycle(engine: AsyncEngine) -> None:
    document_id = uuid4()
    source_file_id = uuid4()
    extraction_id = uuid4()
    reason = "source is not a receipt"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO documents (id, document_class, status, source_filename) "
                "VALUES (:id, 'receipt', 'uploaded', 'rejection-proof.png')"
            ),
            {"id": document_id},
        )
        await connection.execute(
            text(
                "INSERT INTO document_files ("
                "id, document_id, version, kind, content_path, sha256, mime, source_filename"
                ") VALUES ("
                ":id, :document_id, 1, 'original', 'raw/rejection-proof.png', "
                ":sha256, 'image/png', 'rejection-proof.png'"
                ")"
            ),
            {
                "id": source_file_id,
                "document_id": document_id,
                "sha256": "a" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO source_intakes ("
                "document_id, source_file_id, source_version, source_sha256, canonical_mime, "
                "policy_version, state, reason_code, execution_profile, sandbox_verified"
                ") VALUES ("
                ":document_id, :source_file_id, 1, :sha256, 'image/png', "
                "'postgres-schema-test', 'processed', NULL, 'legacy_compat', false"
                ")"
            ),
            {
                "document_id": document_id,
                "source_file_id": source_file_id,
                "sha256": "a" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO extracted_records ("
                "id, document_id, source_file_id, source_version, payload, field_confidences, "
                "source_spans, model_name, prompt_version"
                ") VALUES ("
                ":id, :document_id, :source_file_id, 1, CAST(:payload AS jsonb), "
                "'{}'::jsonb, '{}'::jsonb, 'schema-test', 'schema-test'"
                ")"
            ),
            {
                "id": extraction_id,
                "document_id": document_id,
                "source_file_id": source_file_id,
                "payload": json.dumps({}),
            },
        )
        await connection.execute(
            text("SELECT set_config('clerksan.actor', :actor, true)"),
            {"actor": "postgres-schema-test"},
        )
        transitioned = await connection.execute(
            text(
                "UPDATE extracted_records "
                "SET status = 'rejected', version = version + 1, reviewer = 'reviewer', "
                "rejection_reason = :reason, reviewed_at = now() "
                "WHERE id = :id RETURNING status, rejection_reason"
            ),
            {"id": extraction_id, "reason": reason},
        )
        assert tuple(transitioned.one()) == ("rejected", reason)
        audit_rows = await connection.execute(
            text(
                "SELECT old_value, new_value, actor FROM audit_log "
                "WHERE table_name = 'extracted_records' AND row_pk = :row_pk "
                "AND field = 'rejection_reason'"
            ),
            {"row_pk": str(extraction_id)},
        )
        assert [tuple(row) for row in audit_rows] == [
            ("null", json.dumps(reason), "postgres-schema-test")
        ]


async def _assert_exact_derivative_slots(engine: AsyncEngine) -> None:
    document_id = uuid4()
    source_file_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO documents (id, document_class, status, source_filename) "
                "VALUES (:id, 'other', 'uploaded', 'preview.pdf')"
            ),
            {"id": document_id},
        )
        await connection.execute(
            text(
                "INSERT INTO document_files ("
                "id, document_id, version, kind, content_path, sha256, mime, source_filename"
                ") VALUES ("
                ":id, :document_id, 1, 'original', 'originals/preview.pdf', :sha256, "
                "'application/pdf', 'preview.pdf'"
                ")"
            ),
            {"id": source_file_id, "document_id": document_id, "sha256": "b" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO source_intakes ("
                "document_id, source_file_id, source_version, source_sha256, policy_version, "
                "state, execution_profile, sandbox_verified"
                ") VALUES ("
                ":document_id, :source_file_id, 1, :sha256, 'postgres-schema-test', "
                "'processed', 'legacy_compat', false"
                ")"
            ),
            {
                "document_id": document_id,
                "source_file_id": source_file_id,
                "sha256": "b" * 64,
            },
        )
        for version, page_number in ((2, 1), (3, 2)):
            await connection.execute(
                text(
                    "INSERT INTO document_files ("
                    "document_id, version, kind, source_file_id, source_version, page_number, "
                    "content_path, sha256, mime, source_filename"
                    ") VALUES ("
                    ":document_id, :version, 'page_render', :source_file_id, 1, :page_number, "
                    ":content_path, :sha256, 'image/png', 'preview.pdf'"
                    ")"
                ),
                {
                    "document_id": document_id,
                    "version": version,
                    "source_file_id": source_file_id,
                    "page_number": page_number,
                    "content_path": f"renders/page-{page_number}.png",
                    "sha256": "c" * 64,
                },
            )

    async with engine.connect() as connection:
        pages = await connection.execute(
            text(
                "SELECT page_number, sha256 FROM document_files "
                "WHERE document_id = :document_id AND kind = 'page_render' "
                "ORDER BY page_number"
            ),
            {"document_id": document_id},
        )
        assert [tuple(row) for row in pages] == [(1, "c" * 64), (2, "c" * 64)]

    with pytest.raises(SQLAlchemyError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO document_files ("
                    "document_id, version, kind, source_file_id, source_version, page_number, "
                    "content_path, sha256, mime, source_filename"
                    ") VALUES ("
                    ":document_id, 4, 'page_render', :source_file_id, 1, 1, "
                    "'renders/duplicate.png', :sha256, 'image/png', 'preview.pdf'"
                    ")"
                ),
                {
                    "document_id": document_id,
                    "source_file_id": source_file_id,
                    "sha256": "d" * 64,
                },
            )


async def _seed_pre_upgrade_format_and_rejection_data(engine: AsyncEngine) -> tuple[object, object]:
    """Seed rows as they existed immediately before migrations 0012 and 0013."""

    document_id = uuid4()
    source_file_id = uuid4()
    extraction_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO documents (id, document_class, status, source_filename) "
                "VALUES (:id, 'receipt', 'uploaded', 'legacy.xlsx')"
            ),
            {"id": document_id},
        )
        await connection.execute(
            text(
                "INSERT INTO document_files ("
                "id, document_id, version, kind, content_path, sha256, mime, source_filename"
                ") VALUES ("
                ":id, :document_id, 1, 'original', 'raw/legacy.xlsx', "
                ":sha256, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', "
                "'legacy.xlsx'"
                ")"
            ),
            {
                "id": source_file_id,
                "document_id": document_id,
                "sha256": "b" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO embedded_media ("
                "document_id, sha256, content_path, mime, source_location"
                ") VALUES ("
                ":document_id, :sha256, 'embedded/legacy.png', 'image/png', 'xl/media/image1.png'"
                ")"
            ),
            {"document_id": document_id, "sha256": "c" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO spreadsheet_rows ("
                "document_id, source_location, row_index, values, value_types"
                ") VALUES ("
                ":document_id, 'sheet:July:table:1', 1, "
                "CAST(:values AS jsonb), CAST(:value_types AS jsonb)"
                ")"
            ),
            {
                "document_id": document_id,
                "values": json.dumps({"amount": 1200}),
                "value_types": json.dumps({"amount": "integer"}),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO extracted_records ("
                "id, document_id, source_file_id, source_version, payload, field_confidences, "
                "source_spans, model_name, prompt_version, status"
                ") VALUES ("
                ":id, :document_id, :source_file_id, 1, '{}'::jsonb, '{}'::jsonb, "
                "'{}'::jsonb, 'legacy', 'legacy', 'rejected'"
                ")"
            ),
            {
                "id": extraction_id,
                "document_id": document_id,
                "source_file_id": source_file_id,
            },
        )
    return document_id, extraction_id


async def _assert_upgrade_rebuild_and_legacy_rejection(
    engine: AsyncEngine, document_id: object, extraction_id: object
) -> None:
    marker = "Legacy rejection reason unavailable at migration"
    async with engine.connect() as connection:
        media_count = await connection.scalar(
            text("SELECT count(*) FROM embedded_media WHERE document_id = :document_id"),
            {"document_id": document_id},
        )
        row_count = await connection.scalar(
            text("SELECT count(*) FROM spreadsheet_rows WHERE document_id = :document_id"),
            {"document_id": document_id},
        )
        assert media_count == 0
        assert row_count == 0
        queued = await connection.execute(
            text(
                "SELECT job_type, payload->>'source_version', payload->>'migration', "
                "execution_profile, sandbox_verified, requirements_digest, required_components "
                "FROM jobs WHERE document_id = :document_id"
            ),
            {"document_id": document_id},
        )
        assert [tuple(row) for row in queued] == [
            (
                "rebuild_format_derivatives",
                "1",
                "0013_source_bound_format_derivatives",
                "legacy_compat",
                False,
                "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
                [],
            )
        ]
        intake = await connection.execute(
            text(
                "SELECT state, reason_code, execution_profile, sandbox_verified, "
                "requirements_digest, required_components FROM source_intakes "
                "WHERE document_id = :document_id"
            ),
            {"document_id": document_id},
        )
        assert tuple(intake.one()) == (
            "processed",
            None,
            "legacy_compat",
            False,
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            [],
        )
        reason = await connection.scalar(
            text("SELECT rejection_reason FROM extracted_records WHERE id = :id"),
            {"id": extraction_id},
        )
        assert reason == marker
        legacy_batch = await connection.execute(
            text(
                "SELECT batch.id, batch.origin, batch.lifecycle, extraction.record_kind, "
                "extraction.financial_subtype, extraction.source_locator "
                "FROM extraction_batches AS batch "
                "JOIN extracted_records AS extraction ON extraction.batch_id = batch.id "
                "WHERE extraction.id = :id"
            ),
            {"id": extraction_id},
        )
        assert tuple(legacy_batch.one()) == (
            extraction_id,
            "legacy_singleton",
            "rejected",
            "financial",
            "receipt",
            "legacy_unknown",
        )
        legacy_audit = await connection.execute(
            text(
                "SELECT old_value, new_value FROM audit_log "
                "WHERE table_name = 'extracted_records' AND row_pk = :row_pk "
                "AND field = 'rejection_reason'"
            ),
            {"row_pk": str(extraction_id)},
        )
        assert [tuple(row) for row in legacy_audit] == [("null", json.dumps(marker))]


@pytest.mark.asyncio
async def test_all_migrations_reflect_schema_and_use_hnsw_for_seeded_ann_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise every migration against PostgreSQL, then verify an ANN plan uses HNSW."""
    for name in [name for name in os.environ if name.startswith("CLERKSAN_")]:
        monkeypatch.delenv(name)
    container = f"clerksan-schema-{uuid4().hex}"
    port = _free_port()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--publish",
            f"127.0.0.1:{port}:5432",
            "--env",
            "POSTGRES_PASSWORD=test-only-password",
            "--env",
            "POSTGRES_DB=clerksan_test",
            POSTGRES_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    direct_engine = create_async_engine(
        f"postgresql+asyncpg://postgres:test-only-password@127.0.0.1:{port}/clerksan_test"
    )
    settings = Settings(
        _env_file=None,
        database_url=(
            f"postgresql+asyncpg://postgres:test-only-password@127.0.0.1:{port}/clerksan_test"
        ),
    )
    try:
        await _wait_for_database(direct_engine)
        legacy_migrations = _copy_migrations_before(tmp_path / "pre-0012", "0012_")
        assert await run_migrations(migrations_dir=tmp_path / "pre-0012", settings=settings) == (
            legacy_migrations
        )
        migration_engine = get_engine(settings)
        (
            legacy_document_id,
            legacy_extraction_id,
        ) = await _seed_pre_upgrade_format_and_rejection_data(migration_engine)
        checksums = _migration_checksums()
        original_split_sql = migrate_module.split_sql
        split_calls = 0

        def delayed_first_split(script: str) -> list[str]:
            nonlocal split_calls
            statements = original_split_sql(script)
            if split_calls == 0:
                split_calls += 1
                return ["SELECT pg_sleep(0.2)", *statements]
            split_calls += 1
            return statements

        monkeypatch.setattr(migrate_module, "split_sql", delayed_first_split)
        concurrent_results = await asyncio.wait_for(
            asyncio.gather(*(run_migrations(settings=settings) for _ in range(4))),
            timeout=30,
        )
        applied_before_upgrade = set(legacy_migrations)
        pending = [
            migration.name
            for migration in discover_migrations(MIGRATIONS)
            if migration.name not in applied_before_upgrade
        ]
        assert concurrent_results.count(pending) == 1
        assert concurrent_results.count([]) == 3
        assert await run_migrations(settings=settings) == []

        migrated_engine = get_engine(settings)
        await _assert_runtime_schema(migrated_engine, checksums)
        await _assert_upgrade_rebuild_and_legacy_rejection(
            migrated_engine, legacy_document_id, legacy_extraction_id
        )
        await _assert_rejection_reason_lifecycle(migrated_engine)
        await _assert_exact_derivative_slots(migrated_engine)
        await _seed_and_assert_hnsw_plan(migrated_engine)
    finally:
        await direct_engine.dispose()
        await dispose_engines()
        subprocess.run(
            ["docker", "rm", "--force", container],
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.mark.asyncio
async def test_extraction_batch_migration_aborts_ambiguous_legacy_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PostgreSQL must roll back all 0016 DDL when legacy authority is ambiguous."""

    for name in [name for name in os.environ if name.startswith("CLERKSAN_")]:
        monkeypatch.delenv(name)
    container = f"clerksan-ambiguous-{uuid4().hex}"
    port = _free_port()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--publish",
            f"127.0.0.1:{port}:5432",
            "--env",
            "POSTGRES_PASSWORD=test-only-password",
            "--env",
            "POSTGRES_DB=clerksan_test",
            POSTGRES_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    database_url = (
        f"postgresql+asyncpg://postgres:test-only-password@127.0.0.1:{port}/clerksan_test"
    )
    direct_engine = create_async_engine(database_url)
    settings = Settings(_env_file=None, database_url=database_url)
    try:
        await _wait_for_database(direct_engine)
        pre_phase3 = _copy_migrations_before(tmp_path / "pre-0016", "0016_")
        assert await run_migrations(migrations_dir=tmp_path / "pre-0016", settings=settings) == (
            pre_phase3
        )
        engine = get_engine(settings)
        document_id = uuid4()
        source_file_id = uuid4()
        extraction_ids = (uuid4(), uuid4())
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO documents (id, document_class, status, source_filename) "
                    "VALUES (:id, 'receipt', 'verified', 'ambiguous.png')"
                ),
                {"id": document_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO document_files ("
                    "id, document_id, version, kind, content_path, sha256, mime, source_filename"
                    ") VALUES ("
                    ":id, :document_id, 1, 'original', 'raw/ambiguous.png', :sha256, "
                    "'image/png', 'ambiguous.png')"
                ),
                {"id": source_file_id, "document_id": document_id, "sha256": "a" * 64},
            )
            await connection.execute(
                text(
                    "INSERT INTO source_intakes ("
                    "document_id, source_file_id, source_version, source_sha256, canonical_mime, "
                    "policy_version, state, execution_profile, sandbox_verified"
                    ") VALUES ("
                    ":document_id, :source_file_id, 1, :sha256, 'image/png', "
                    "'ambiguous-test', 'processed', 'legacy_compat', false)"
                ),
                {
                    "document_id": document_id,
                    "source_file_id": source_file_id,
                    "sha256": "a" * 64,
                },
            )
            for extraction_id in extraction_ids:
                await connection.execute(
                    text(
                        "INSERT INTO extracted_records ("
                        "id, document_id, source_file_id, source_version, payload, "
                        "field_confidences, source_spans, model_name, prompt_version, "
                        "status, version, reviewer, reviewed_at"
                        ") VALUES ("
                        ":id, :document_id, :source_file_id, 1, '{}'::jsonb, '{}'::jsonb, "
                        "'{}'::jsonb, 'legacy', 'legacy', 'approved', 1, 'reviewer', now())"
                    ),
                    {
                        "id": extraction_id,
                        "document_id": document_id,
                        "source_file_id": source_file_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO verified_records ("
                        "document_id, extracted_id, transaction_date, total_amount, "
                        "counterparty, reviewer"
                        ") VALUES ("
                        ":document_id, :extracted_id, '2026-08-22', 1200, "
                        "'Ambiguous Shop', 'reviewer')"
                    ),
                    {"document_id": document_id, "extracted_id": extraction_id},
                )

        with pytest.raises(SQLAlchemyError, match="legacy authority reconciliation required"):
            await run_migrations(settings=settings)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('extraction_batches')")) is None
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM schema_migrations "
                        "WHERE filename = '0016_extraction_batches_and_mappings.sql'"
                    )
                )
            ) == 0
    finally:
        await direct_engine.dispose()
        await dispose_engines()
        subprocess.run(
            ["docker", "rm", "--force", container],
            capture_output=True,
            text=True,
            check=False,
        )
