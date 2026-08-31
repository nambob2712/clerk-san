"""Regression coverage for safe upgrades of persisted local demo data."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from clerksan.api.main import create_app
from clerksan.config import Settings
from clerksan.db.engine import dispose_engines, get_engine, get_session
from clerksan.db.models import Base
from clerksan.db.repositories import DocumentRepo

_LEGACY_VERIFIED_RECORDS = """
CREATE TABLE verified_records (
    id CHAR(32) NOT NULL PRIMARY KEY,
    document_id CHAR(32) NOT NULL,
    extracted_id CHAR(32) NOT NULL,
    transaction_date DATE NOT NULL,
    total_amount NUMERIC(18, 2) NOT NULL,
    counterparty TEXT NOT NULL,
    currency VARCHAR(16),
    category TEXT,
    registration_number VARCHAR(14),
    tax_8_amount NUMERIC(18, 2),
    tax_10_amount NUMERIC(18, 2),
    reviewer TEXT NOT NULL,
    verified_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_PRE_UNIVERSAL_DOCUMENT_FILES = """
CREATE TABLE document_files__pre_universal (
    id CHAR(32) NOT NULL PRIMARY KEY,
    document_id CHAR(32) NOT NULL,
    version INTEGER NOT NULL,
    kind VARCHAR(11) NOT NULL,
    content_path TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    mime TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    ocr_text TEXT,
    text_provenance TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (document_id, version),
    UNIQUE (document_id, sha256)
)
"""

_PRE_UNIVERSAL_JOBS = """
CREATE TABLE jobs__pre_universal (
    id CHAR(32) NOT NULL PRIMARY KEY,
    document_id CHAR(32) NOT NULL,
    job_type TEXT NOT NULL,
    payload JSON NOT NULL,
    idempotency_key TEXT NOT NULL,
    status VARCHAR(7) NOT NULL,
    attempts INTEGER NOT NULL,
    last_error TEXT,
    available_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_expires_at DATETIME,
    lease_owner TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (document_id, idempotency_key)
)
"""

_PRE_PHASE3_EXTRACTED_RECORDS = """
CREATE TABLE extracted_records (
    id CHAR(32) NOT NULL PRIMARY KEY,
    document_id CHAR(32) NOT NULL,
    source_file_id CHAR(32) NOT NULL,
    source_version INTEGER NOT NULL,
    payload JSON NOT NULL,
    field_confidences JSON NOT NULL,
    source_spans JSON NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status VARCHAR(14) NOT NULL,
    version INTEGER NOT NULL,
    reviewer TEXT,
    rejection_reason TEXT,
    reviewed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, document_id)
)
"""


async def _create_legacy_demo(settings: Settings) -> None:
    engine = get_engine(settings)
    async with engine.begin() as connection:
        await connection.execute(text(_LEGACY_VERIFIED_RECORDS))
        await connection.run_sync(Base.metadata.create_all)
    async with get_session(settings) as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="legacy-receipt.png",
            content_path="originals/legacy-receipt.png",
            sha256="a" * 64,
            mime="image/png",
        )
    async with engine.begin() as connection:
        original = await connection.execute(
            text(
                "SELECT id, version FROM document_files "
                "WHERE document_id = :document_id AND kind = 'original'"
            ),
            {"document_id": document_id.hex},
        )
        source_file_id, source_version = original.one()
        await connection.execute(
            text(
                "INSERT INTO document_files ("
                "id, document_id, version, kind, source_file_id, source_version, "
                "content_path, sha256, mime, source_filename"
                ") VALUES ("
                ":id, :document_id, 2, 'normalized', :source_file_id, :source_version, "
                "'normalized/legacy.txt', :sha256, 'text/plain', 'legacy-receipt.png'"
                ")"
            ),
            {
                "id": uuid4().hex,
                "document_id": document_id.hex,
                "source_file_id": source_file_id,
                "source_version": source_version,
                "sha256": "b" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO jobs (id, document_id, job_type, payload, idempotency_key, status, "
                "attempts) VALUES (:id, :document_id, 'process_document', :payload, "
                "'legacy-process:1', 'done', 1)"
            ),
            {
                "id": uuid4().hex,
                "document_id": document_id.hex,
                "payload": '{"source_version":1}',
            },
        )
        await connection.execute(
            text(
                "INSERT INTO verified_records "
                "(id, document_id, extracted_id, transaction_date, total_amount, counterparty, "
                "currency, reviewer) "
                "VALUES (:id, :document_id, :extracted_id, '2026-08-18', 1200, "
                "'Legacy Shop', 'JPY', 'local-user')"
            ),
            {
                "id": uuid4().hex,
                "document_id": document_id.hex,
                "extracted_id": uuid4().hex,
            },
        )
        await _downgrade_to_pre_universal_schema(connection)


async def _downgrade_to_pre_universal_schema(connection) -> None:
    """Build the exact pre-0015 table shapes around preserved demo rows."""

    await connection.execute(text("DROP TABLE upload_idempotency_reservations"))
    await connection.execute(text("DROP TABLE source_intakes"))
    await connection.execute(text("DROP TABLE worker_capability_leases"))
    await connection.execute(text(_PRE_UNIVERSAL_DOCUMENT_FILES))
    await connection.execute(
        text(
            "INSERT INTO document_files__pre_universal ("
            "id, document_id, version, kind, content_path, sha256, mime, source_filename, "
            "ocr_text, text_provenance, created_at"
            ") SELECT id, document_id, version, kind, content_path, sha256, mime, "
            "source_filename, ocr_text, text_provenance, created_at FROM document_files"
        )
    )
    await connection.execute(text("DROP TABLE document_files"))
    await connection.execute(
        text("ALTER TABLE document_files__pre_universal RENAME TO document_files")
    )
    await connection.execute(text(_PRE_UNIVERSAL_JOBS))
    await connection.execute(
        text(
            "INSERT INTO jobs__pre_universal ("
            "id, document_id, job_type, payload, idempotency_key, status, attempts, last_error, "
            "available_at, lease_expires_at, lease_owner, created_at, updated_at"
            ") SELECT id, document_id, job_type, payload, idempotency_key, status, attempts, "
            "last_error, available_at, lease_expires_at, lease_owner, created_at, updated_at "
            "FROM jobs"
        )
    )
    await connection.execute(text("DROP TABLE jobs"))
    await connection.execute(text("ALTER TABLE jobs__pre_universal RENAME TO jobs"))


def test_legacy_sqlite_demo_is_upgraded_before_listing_documents(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    asyncio.run(_create_legacy_demo(settings))
    try:
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
            ready = client.get("/ready")
            listing = client.get("/documents")

        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.json()["demo_mode"] is True
        assert ready.json()["intake_ready"] is True
        assert listing.status_code == 200
        assert len(listing.json()["items"]) == 1
        columns = {row[1] for row in asyncio.run(_sqlite_table_info(settings, "verified_records"))}
        assert {"expense_kind", "due_date", "version"} <= columns
        assert asyncio.run(_legacy_record_version(settings)) == 1
        universal = asyncio.run(_universal_upgrade_evidence(settings))
        assert universal["document_file_columns"] >= {
            "source_file_id",
            "source_version",
            "page_number",
        }
        assert universal["job_columns"] >= {
            "execution_profile",
            "sandbox_verified",
            "registry_digest",
            "capabilities_digest",
            "requirements_digest",
            "required_components",
        }
        assert universal["legacy_global_sha_unique"] is False
        assert universal["intake"] == (
            "processed",
            None,
            "legacy_compat",
            0,
            "legacy-pre-0015",
        )
        assert universal["derivative_lineage"] == universal["original_identity"]
        assert universal["job_evidence"] == (
            "legacy_compat",
            0,
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            "[]",
        )
    finally:
        asyncio.run(dispose_engines())


def test_phase4_child_added_around_pre_phase3_parent_upgrades_before_global_fk_check(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'phase4-parent-gap.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    asyncio.run(_create_phase4_parent_gap(settings))
    try:
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
            ready = client.get("/ready")

        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        indexes = asyncio.run(_sqlite_index_names(settings, "extracted_records"))
        assert "extracted_records_id_batch_id_key" in indexes
    finally:
        asyncio.run(dispose_engines())


async def _create_phase4_parent_gap(settings: Settings) -> None:
    engine = get_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await dispose_engines()

    database = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        candidate_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'candidate_review_decisions'"
        ).fetchone()[0]
        connection.execute("DROP TABLE candidate_review_decisions")
        connection.execute("DROP TABLE extracted_records")
        connection.execute(_PRE_PHASE3_EXTRACTED_RECORDS)
        connection.execute(candidate_sql)
        connection.execute(
            "CREATE INDEX candidate_review_decisions_latest_idx "
            "ON candidate_review_decisions (batch_id, extraction_id, decision_revision)"
        )
        connection.execute(
            "CREATE INDEX candidate_review_decisions_batch_created_idx "
            "ON candidate_review_decisions (batch_id, created_at)"
        )
        connection.execute("DROP TABLE document_files")
        connection.execute(
            _PRE_UNIVERSAL_DOCUMENT_FILES.replace("document_files__pre_universal", "document_files")
        )
        connection.commit()


async def _sqlite_index_names(settings: Settings, table: str) -> set[str]:
    async with get_engine(settings).connect() as connection:
        rows = await connection.execute(text(f"PRAGMA index_list({table})"))
        return {str(row[1]) for row in rows}


async def _sqlite_table_info(settings: Settings, table: str) -> list[tuple[object, ...]]:
    async with get_engine(settings).connect() as connection:
        return [tuple(row) for row in await connection.execute(text(f"PRAGMA table_info({table})"))]


async def _legacy_record_version(settings: Settings) -> int:
    async with get_engine(settings).connect() as connection:
        result = await connection.execute(text("SELECT version FROM verified_records"))
        return int(result.scalar_one())


async def _universal_upgrade_evidence(settings: Settings) -> dict[str, object]:
    async with get_engine(settings).connect() as connection:
        document_file_info = await connection.execute(text("PRAGMA table_info(document_files)"))
        job_info = await connection.execute(text("PRAGMA table_info(jobs)"))
        indexes = await connection.execute(text("PRAGMA index_list(document_files)"))
        legacy_global_sha_unique = False
        for row in indexes:
            if not bool(row[2]) or bool(row[4]):
                continue
            index_name = str(row[1]).replace('"', '""')
            index_info = await connection.execute(text(f'PRAGMA index_info("{index_name}")'))
            if [str(index_row[2]) for index_row in index_info] == [
                "document_id",
                "sha256",
            ]:
                legacy_global_sha_unique = True
        intake = await connection.execute(
            text(
                "SELECT state, reason_code, execution_profile, sandbox_verified, policy_version "
                "FROM source_intakes"
            )
        )
        original = await connection.execute(
            text("SELECT id, version FROM document_files WHERE kind = 'original'")
        )
        derivative = await connection.execute(
            text(
                "SELECT source_file_id, source_version FROM document_files "
                "WHERE kind = 'normalized'"
            )
        )
        job = await connection.execute(
            text(
                "SELECT execution_profile, sandbox_verified, requirements_digest, "
                "required_components FROM jobs"
            )
        )
        return {
            "document_file_columns": {str(row[1]) for row in document_file_info},
            "job_columns": {str(row[1]) for row in job_info},
            "legacy_global_sha_unique": legacy_global_sha_unique,
            "intake": tuple(intake.one()),
            "original_identity": tuple(original.one()),
            "derivative_lineage": tuple(derivative.one()),
            "job_evidence": tuple(job.one()),
        }


def test_incompatible_sqlite_column_returns_a_structured_readiness_error(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unupgradeable.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    asyncio.run(_create_incompatible_demo(settings))
    try:
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
            ready = client.get("/ready")
            documents = client.get("/documents")

        for response in (ready, documents):
            assert response.status_code == 503
            assert response.json()["code"] == "local_data_needs_upgrade"
        assert ready.json()["detail"]["core_reason_codes"] == ["local_data_needs_upgrade"]
    finally:
        asyncio.run(dispose_engines())


async def _create_incompatible_demo(settings: Settings) -> None:
    engine = get_engine(settings)
    malformed = _LEGACY_VERIFIED_RECORDS.replace(
        "reviewer TEXT NOT NULL,",
        "version TEXT NOT NULL DEFAULT 'bad',\n    reviewer TEXT NOT NULL,",
    )
    async with engine.begin() as connection:
        await connection.execute(text(malformed))
        await connection.run_sync(Base.metadata.create_all)
