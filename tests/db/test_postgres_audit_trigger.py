"""Opt-in PostgreSQL coverage for the trigger-owned verified-record audit log."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from clerksan.bills.reminders import mark_paid
from clerksan.db.migrate import discover_migrations, split_sql

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = (
    "pgvector/pgvector:0.8.5-pg16-bookworm"
    "@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
)
RUN_POSTGRES_TESTS = os.getenv("CLERKSAN_RUN_POSTGRES_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not (RUN_POSTGRES_TESTS and shutil.which("docker")),
    reason="set CLERKSAN_RUN_POSTGRES_TESTS=1 with Docker installed to run PostgreSQL triggers",
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


async def _apply_audit_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for filename in (
            "0001_core_documents.sql",
            "0002_audit_log.sql",
            "0005_recurring_bills.sql",
            "0007_recurring_bill_versions.sql",
            "0009_audit_verified_record_canonicalization.sql",
        ):
            script = (ROOT / "migrations" / filename).read_text(encoding="utf-8")
            for statement in split_sql(script):
                await connection.execute(text(statement))


async def _apply_all_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for path in discover_migrations(ROOT / "migrations"):
            for statement in split_sql(path.read_text(encoding="utf-8")):
                await connection.execute(text(statement))


def _payload() -> dict[str, dict[str, str | None]]:
    return {
        "transaction_date": {"value": "2026-07-13"},
        "total_amount": {"value": "1200.00"},
        "counterparty": {"value": "North Cafe"},
        "currency": {"value": "JPY"},
        "expense_category": {"value": "Meals"},
        "registration_number": {"value": "T1234567890123"},
        "tax_8_amount": {"value": None},
        "tax_10_amount": {"value": "109.00"},
    }


async def _insert_source(
    engine: AsyncEngine,
    *,
    document_id: UUID,
    extraction_id: UUID,
    payload: dict[str, dict[str, str | None]],
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO documents (id, document_class, status, source_filename) "
                "VALUES (:id, 'receipt', 'uploaded', 'receipt.png')"
            ),
            {"id": document_id},
        )
        await connection.execute(
            text(
                "INSERT INTO extracted_records "
                "(id, document_id, payload, field_confidences, source_spans, "
                "model_name, prompt_version) "
                "VALUES (:id, :document_id, CAST(:payload AS jsonb), '{}'::jsonb, '{}'::jsonb, "
                "'integration', 'test')"
            ),
            {
                "id": extraction_id,
                "document_id": document_id,
                "payload": json.dumps(payload),
            },
        )


async def _insert_active_source(
    engine: AsyncEngine,
    *,
    document_id: UUID,
    extraction_id: UUID,
    payload: dict[str, dict[str, str | None]],
) -> None:
    source_file_id = uuid4()
    source_intake_id = uuid4()
    batch_id = uuid4()
    source_sha256 = "a" * 64
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
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO documents (id, document_class, status, source_filename) "
                "VALUES (:id, 'receipt', 'uploaded', 'receipt.png')"
            ),
            {"id": document_id},
        )
        await connection.execute(
            text(
                "INSERT INTO document_files ("
                "id, document_id, version, kind, content_path, sha256, mime, source_filename"
                ") VALUES ("
                ":id, :document_id, 1, 'original', 'originals/receipt.png', :sha256, "
                "'image/png', 'receipt.png')"
            ),
            {
                "id": source_file_id,
                "document_id": document_id,
                "sha256": source_sha256,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO source_intakes ("
                "id, document_id, source_file_id, source_version, source_sha256, "
                "policy_version, requirements_digest, intake_intent, state, "
                "execution_profile, sandbox_verified"
                ") VALUES ("
                ":id, :document_id, :source_file_id, 1, :sha256, 'audit-test', :sha256, "
                "'bill_scan', 'processed', 'legacy_compat', false)"
            ),
            {
                "id": source_intake_id,
                "document_id": document_id,
                "source_file_id": source_file_id,
                "sha256": source_sha256,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO extraction_batches ("
                "id, source_intake_id, document_id, source_file_id, source_version, "
                "source_sha256, normalized_sha256, structure_fingerprint, producer, "
                "producer_version, origin, intake_intent, lifecycle, idempotency_key, "
                "candidate_count, reconciliation_counts, reconciliation_digest, version"
                ") VALUES ("
                ":id, :source_intake_id, :document_id, :source_file_id, 1, :source_sha256, "
                ":normalized_sha256, :structure_fingerprint, 'audit-test', '1', 'direct', "
                "'bill_scan', 'open', 'audit-test', 1, CAST(:reconciliation AS jsonb), "
                ":reconciliation_digest, 1)"
            ),
            {
                "id": batch_id,
                "source_intake_id": source_intake_id,
                "document_id": document_id,
                "source_file_id": source_file_id,
                "source_sha256": source_sha256,
                "normalized_sha256": "b" * 64,
                "structure_fingerprint": "c" * 64,
                "reconciliation": reconciliation,
                "reconciliation_digest": hashlib.sha256(reconciliation.encode()).hexdigest(),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO extracted_records ("
                "id, document_id, source_file_id, source_version, batch_id, candidate_ordinal, "
                "candidate_key, record_kind, financial_subtype, source_locator, "
                "row_fingerprint, validation_issues, evidence_group_keys, payload, "
                "field_confidences, source_spans, model_name, prompt_version, status, version"
                ") VALUES ("
                ":id, :document_id, :source_file_id, 1, :batch_id, 1, :candidate_key, "
                "'financial', 'receipt', 'page:1', :row_fingerprint, '[]', '[]', "
                "CAST(:payload AS jsonb), '{}', '{}', 'integration', 'test', "
                "'pending_review', 1)"
            ),
            {
                "id": extraction_id,
                "document_id": document_id,
                "source_file_id": source_file_id,
                "batch_id": batch_id,
                "candidate_key": "d" * 64,
                "row_fingerprint": "e" * 64,
                "payload": json.dumps(payload),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO candidate_review_decisions ("
                "batch_id, extraction_id, decision_revision, expected_extraction_version, "
                "action, actor) VALUES (:batch_id, :extraction_id, 1, 1, 'include', "
                "'postgres-integration')"
            ),
            {"batch_id": batch_id, "extraction_id": extraction_id},
        )
        await connection.execute(
            text("UPDATE extracted_records SET status = 'approved', version = 2 WHERE id = :id"),
            {"id": extraction_id},
        )
        await connection.execute(
            text(
                "UPDATE extraction_batches SET lifecycle = 'active', "
                "activation_vector_sha256 = :activation_vector, "
                "activated_by = 'postgres-integration', activated_at = CURRENT_TIMESTAMP, "
                "activation_included_count = 1, activation_excluded_count = 0, version = 2 "
                "WHERE id = :id"
            ),
            {"id": batch_id, "activation_vector": "f" * 64},
        )


async def _insert_verified(
    engine: AsyncEngine,
    *,
    record_id: UUID,
    document_id: UUID,
    extraction_id: UUID,
    transaction_date: str,
    total_amount: str,
    category: str,
    actor: str,
) -> list[tuple[str, str, str, str]]:
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('clerksan.actor', :actor, true)"), {"actor": actor}
        )
        await connection.execute(
            text(
                "INSERT INTO verified_records "
                "(id, document_id, extracted_id, transaction_date, total_amount, "
                "counterparty, currency, "
                "category, registration_number, tax_8_amount, tax_10_amount, reviewer) "
                "VALUES (:id, :document_id, :extracted_id, CAST(:transaction_date AS date), "
                "CAST(:total_amount AS numeric), 'North Cafe', 'JPY', :category, 'T1234567890123', "
                "NULL, CAST('109.00' AS numeric), 'reviewer')"
            ),
            {
                "id": record_id,
                "document_id": document_id,
                "extracted_id": extraction_id,
                "transaction_date": dt.date.fromisoformat(transaction_date),
                "total_amount": Decimal(total_amount),
                "category": category,
            },
        )
        rows = await connection.execute(
            text(
                "SELECT field, old_value, new_value, actor FROM audit_log "
                "WHERE table_name = 'verified_records' AND row_pk = :row_pk ORDER BY field"
            ),
            {"row_pk": str(record_id)},
        )
        return [tuple(row) for row in rows]


async def _insert_recurring_bill(
    engine: AsyncEngine,
    *,
    bill_id: UUID,
    issuer_id: UUID,
    verified_record_id: UUID,
    document_id: UUID,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE extracted_records SET status = 'approved' WHERE document_id = :id"),
            {"id": document_id},
        )
        await connection.execute(
            text("INSERT INTO issuers (id, name, kind) VALUES (:id, 'Tokyo Electric', 'electric')"),
            {"id": issuer_id},
        )
        await connection.execute(
            text(
                "INSERT INTO recurring_bills "
                "(id, issuer_id, document_id, verified_record_id, "
                "billing_period, amount, due_date) "
                "VALUES (:id, :issuer_id, :document_id, :verified_record_id, "
                "CAST('2026-07-01' AS date), CAST('1200.00' AS numeric), "
                "CAST('2026-07-25' AS date))"
            ),
            {
                "id": bill_id,
                "issuer_id": issuer_id,
                "document_id": document_id,
                "verified_record_id": verified_record_id,
            },
        )


@pytest.mark.asyncio
async def test_verified_insert_audit_canonicalizes_typed_source_values_before_corrections() -> None:
    container = f"clerksan-audit-{uuid4().hex}"
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

    engine = create_async_engine(
        f"postgresql+asyncpg://postgres:test-only-password@127.0.0.1:{port}/clerksan_test"
    )
    try:
        await _wait_for_database(engine)
        await _apply_audit_schema(engine)

        unchanged_document_id = uuid4()
        unchanged_extraction_id = uuid4()
        await _insert_source(
            engine,
            document_id=unchanged_document_id,
            extraction_id=unchanged_extraction_id,
            payload=_payload(),
        )
        unchanged_rows = await _insert_verified(
            engine,
            record_id=uuid4(),
            document_id=unchanged_document_id,
            extraction_id=unchanged_extraction_id,
            transaction_date="2026-07-13",
            total_amount="1200.00",
            category="Meals",
            actor="postgres-integration",
        )
        assert unchanged_rows == []

        corrected_document_id = uuid4()
        corrected_extraction_id = uuid4()
        await _insert_source(
            engine,
            document_id=corrected_document_id,
            extraction_id=corrected_extraction_id,
            payload=_payload(),
        )
        corrected_rows = await _insert_verified(
            engine,
            record_id=uuid4(),
            document_id=corrected_document_id,
            extraction_id=corrected_extraction_id,
            transaction_date="2026-07-14",
            total_amount="1250.00",
            category="Travel",
            actor="postgres-integration",
        )
        assert corrected_rows == [
            ("category", '"Meals"', '"Travel"', "postgres-integration"),
            ("total_amount", "1200.00", "1250.00", "postgres-integration"),
            (
                "transaction_date",
                '"2026-07-13"',
                '"2026-07-14"',
                "postgres-integration",
            ),
        ]
    finally:
        await engine.dispose()
        subprocess.run(
            ["docker", "rm", "--force", container],
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.mark.asyncio
async def test_mark_paid_writes_exactly_one_audit_row_per_changed_payment_field() -> None:
    container = f"clerksan-payment-audit-{uuid4().hex}"
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

    engine = create_async_engine(
        f"postgresql+asyncpg://postgres:test-only-password@127.0.0.1:{port}/clerksan_test"
    )
    try:
        await _wait_for_database(engine)
        await _apply_all_migrations(engine)

        document_id = uuid4()
        extraction_id = uuid4()
        verified_record_id = uuid4()
        bill_id = uuid4()
        await _insert_active_source(
            engine,
            document_id=document_id,
            extraction_id=extraction_id,
            payload=_payload(),
        )
        await _insert_verified(
            engine,
            record_id=verified_record_id,
            document_id=document_id,
            extraction_id=extraction_id,
            transaction_date="2026-07-13",
            total_amount="1200.00",
            category="Meals",
            actor="postgres-integration",
        )
        await _insert_recurring_bill(
            engine,
            bill_id=bill_id,
            issuer_id=uuid4(),
            verified_record_id=verified_record_id,
            document_id=document_id,
        )

        Session = async_sessionmaker(engine, expire_on_commit=False)
        paid_at = dt.datetime(2026, 7, 13, 3, 4, 5, tzinfo=dt.UTC)
        async with Session.begin() as session:
            await mark_paid(session, bill_id, actor="pay-reviewer", paid_at=paid_at)
        async with Session.begin() as session:
            await mark_paid(session, bill_id, actor="pay-reviewer", paid_at=paid_at)

        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT field, old_value, new_value, actor FROM audit_log "
                    "WHERE table_name = 'recurring_bills' AND row_pk = :row_pk "
                    "ORDER BY field"
                ),
                {"row_pk": str(bill_id)},
            )
            audit_rows = [tuple(row) for row in rows]

        assert len(audit_rows) == 3
        by_field = {field: (old, new, actor) for field, old, new, actor in audit_rows}
        assert by_field["payment_status"] == ('"unpaid"', '"paid"', "pay-reviewer")
        assert by_field["reviewer"] == ("null", '"pay-reviewer"', "pay-reviewer")
        assert by_field["paid_at"][0] == "null"
        assert "2026-07-13" in by_field["paid_at"][1]
        assert by_field["paid_at"][2] == "pay-reviewer"
    finally:
        await engine.dispose()
        subprocess.run(
            ["docker", "rm", "--force", container],
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.mark.asyncio
async def test_source_intake_insert_and_optimistic_transition_are_trigger_audited() -> None:
    container = f"clerksan-intake-audit-{uuid4().hex}"
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

    engine = create_async_engine(
        f"postgresql+asyncpg://postgres:test-only-password@127.0.0.1:{port}/clerksan_test"
    )
    document_id = uuid4()
    source_file_id = uuid4()
    intake_id = uuid4()
    try:
        await _wait_for_database(engine)
        await _apply_all_migrations(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('clerksan.actor', 'intake-worker', true)")
            )
            await connection.execute(
                text(
                    "INSERT INTO documents (id, document_class, status, source_filename) "
                    "VALUES (:id, 'other', 'uploaded', 'source.png')"
                ),
                {"id": document_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO document_files ("
                    "id, document_id, version, kind, content_path, sha256, mime, source_filename"
                    ") VALUES ("
                    ":id, :document_id, 1, 'original', 'originals/source.png', :sha256, "
                    "'image/png', 'source.png'"
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
                    "id, document_id, source_file_id, source_version, source_sha256, "
                    "policy_version, state, reason_code, execution_profile, sandbox_verified"
                    ") VALUES ("
                    ":id, :document_id, :source_file_id, 1, :sha256, 'audit-test', "
                    "'queued', 'processing_queued', 'legacy_compat', false"
                    ")"
                ),
                {
                    "id": intake_id,
                    "document_id": document_id,
                    "source_file_id": source_file_id,
                    "sha256": "a" * 64,
                },
            )
            await connection.execute(
                text(
                    "UPDATE source_intakes SET state = 'processing', "
                    "reason_code = 'processing_started', version = version + 1 "
                    "WHERE id = :id"
                ),
                {"id": intake_id},
            )

        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT action, field, actor FROM audit_log "
                    "WHERE table_name = 'source_intakes' AND row_pk = :row_pk "
                    "ORDER BY id"
                ),
                {"row_pk": str(intake_id)},
            )
            audit_rows = [tuple(row) for row in rows]
        assert audit_rows == [
            ("INSERT", "source_identity", "intake-worker"),
            ("INSERT", "state", "intake-worker"),
            ("INSERT", "intake_intent", "intake-worker"),
            ("INSERT", "execution_profile", "intake-worker"),
            ("INSERT", "sandbox_verified", "intake-worker"),
            ("UPDATE", "state", "intake-worker"),
            ("UPDATE", "reason_code", "intake-worker"),
            ("UPDATE", "version", "intake-worker"),
        ]

        with pytest.raises(SQLAlchemyError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE source_intakes SET state = 'processed' WHERE id = :id"),
                    {"id": intake_id},
                )
    finally:
        await engine.dispose()
        subprocess.run(
            ["docker", "rm", "--force", container],
            capture_output=True,
            text=True,
            check=False,
        )
