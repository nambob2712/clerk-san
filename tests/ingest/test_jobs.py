from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import clerksan.ingest.jobs as jobs_module
from clerksan.config import Settings
from clerksan.db.models import Base, ExecutionProfile, IntakeIntent, Job, JobStatus, SourceIntake
from clerksan.db.repositories import DocumentRepo
from clerksan.ingest.capabilities import build_capability_registry
from clerksan.ingest.jobs import claim_next, enqueue, get_handler, retry_or_bury


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _document(session, digest: str = "a" * 64):
    return await DocumentRepo(session).create_with_raw(
        filename="receipt.png",
        content_path="/tmp/receipt.png",
        sha256=digest,
        mime="image/png",
    )


def _settings(tmp_path: Path, *, attempts: int = 3) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'jobs.sqlite'}",
        intake_mode="legacy",
        job_lease_seconds=30,
        job_retry_base_seconds=2,
        job_max_attempts=attempts,
    )


@pytest.mark.asyncio
async def test_enqueue_is_document_scoped_and_idempotent(session_factory, tmp_path: Path) -> None:
    async with session_factory() as session:
        first_document = await _document(session)
        first_job = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(first_document)},
            idempotency_key="process_document",
        )
        duplicate = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(first_document)},
            idempotency_key="process_document",
        )
        second_document = await _document(session, "b" * 64)
        second_job = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(second_document)},
            idempotency_key="process_document",
        )
        await session.commit()

    assert first_job is not None
    assert duplicate is None
    assert second_job is not None
    assert first_job != second_job


@pytest.mark.asyncio
async def test_standalone_sqlite_enqueue_rolls_back_with_its_outer_transaction(
    session_factory,
) -> None:
    async with session_factory() as session:
        document_id = await _document(session)
        await session.commit()
        # Reproduce a standalone caller whose autobegun transaction contains only
        # reads before enqueue opens its uniqueness-conflict savepoint.
        assert await session.get(Job, UUID(int=0)) is None
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="rollback-standalone-enqueue",
        )
        assert job_id is not None
        await session.rollback()

    async with session_factory() as session:
        assert await session.get(Job, job_id) is None


@pytest.mark.asyncio
async def test_enqueue_persists_and_serializes_default_legacy_capability_evidence(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    registry = build_capability_registry(settings)
    expected_requirements_digest = hashlib.sha256(b"[]").hexdigest()

    async with session_factory() as session:
        document_id = await _document(session)
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="default-evidence",
            settings=settings,
        )
        assert job_id is not None
        persisted = await session.get(Job, job_id)
        assert persisted is not None
        claimed = await claim_next(session, lease_owner="worker-evidence", settings=settings)
        await session.commit()

    assert persisted.execution_profile is ExecutionProfile.LEGACY_COMPAT
    assert persisted.sandbox_verified is False
    assert persisted.registry_digest == registry.registry_digest
    assert persisted.capabilities_digest == registry.capabilities_digest
    assert persisted.required_components == []
    assert persisted.requirements_digest == expected_requirements_digest
    assert persisted.intake_intent is IntakeIntent.LEGACY_UNSPECIFIED
    assert persisted.payload["intake_intent"] == IntakeIntent.LEGACY_UNSPECIFIED.value
    assert claimed is not None
    assert claimed["execution_profile"] == ExecutionProfile.LEGACY_COMPAT.value
    assert claimed["sandbox_verified"] is False
    assert claimed["registry_digest"] == registry.registry_digest
    assert claimed["capabilities_digest"] == registry.capabilities_digest
    assert claimed["required_components"] == []
    assert claimed["requirements_digest"] == expected_requirements_digest
    assert claimed["intake_intent"] == IntakeIntent.LEGACY_UNSPECIFIED.value


@pytest.mark.asyncio
async def test_claim_next_refreshes_expired_updated_at_before_serializing(
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    original_claim = jobs_module._claim_sqlite

    async def claim_with_server_generated_timestamp(*args, **kwargs):
        job = await original_claim(*args, **kwargs)
        if job is not None:
            args[0].expire(job, ["updated_at"])
        return job

    monkeypatch.setattr(jobs_module, "_claim_sqlite", claim_with_server_generated_timestamp)

    async with session_factory() as session:
        document_id = await _document(session)
        await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="expired-updated-at",
        )
        claimed = await claim_next(session, lease_owner="worker-evidence", settings=settings)

    assert claimed is not None
    assert claimed["updated_at"] is not None


@pytest.mark.asyncio
async def test_enqueue_without_capability_context_keeps_registry_evidence_nullable(
    session_factory,
) -> None:
    async with session_factory() as session:
        document_id = await _document(session)
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="nullable-registry-evidence",
        )
        assert job_id is not None
        persisted = await session.get(Job, job_id)

    assert persisted is not None
    assert persisted.execution_profile is ExecutionProfile.LEGACY_COMPAT
    assert persisted.sandbox_verified is False
    assert persisted.registry_digest is None
    assert persisted.capabilities_digest is None
    assert persisted.required_components == []
    assert persisted.requirements_digest == hashlib.sha256(b"[]").hexdigest()


@pytest.mark.asyncio
async def test_enqueue_uses_source_intake_evidence_over_payload_or_conflicting_request(
    session_factory,
) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="transactions.csv",
            content_path="/tmp/transactions.csv",
            sha256="e" * 64,
            mime="text/csv",
            intake_intent=IntakeIntent.GENERIC_FILE,
        )
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == document_id)
        )
        assert intake is not None
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={
                "document_id": str(document_id),
                "source_file_id": str(intake.source_file_id),
                "source_intake_id": str(intake.id),
                "source_version": intake.source_version,
                "intake_intent": IntakeIntent.BILL_SCAN.value,
            },
            idempotency_key="canonical-source-intent",
        )
        assert job_id is not None
        persisted = await session.get(Job, job_id)
        assert persisted is not None
        with pytest.raises(ValueError, match="must match the persisted source intake"):
            await enqueue(
                session,
                job_type="process_document",
                payload={
                    "document_id": str(document_id),
                    "source_file_id": str(intake.source_file_id),
                    "source_intake_id": str(intake.id),
                    "source_version": intake.source_version,
                },
                idempotency_key="conflicting-source-intent",
                intake_intent=IntakeIntent.BILL_SCAN,
            )

    assert persisted.intake_intent is IntakeIntent.GENERIC_FILE
    assert persisted.payload["intake_intent"] == IntakeIntent.GENERIC_FILE.value
    assert persisted.payload["source_intake_id"] == str(intake.id)
    assert persisted.payload["source_file_id"] == str(intake.source_file_id)
    assert persisted.payload["source_version"] == intake.source_version


@pytest.mark.asyncio
async def test_enqueue_never_downgrades_a_sandboxed_source_to_legacy_execution(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="notes.rtf",
            content_path="/tmp/notes.rtf",
            sha256="5" * 64,
            mime="application/rtf",
            registry_digest="6" * 64,
            capabilities_digest="7" * 64,
            intake_intent=IntakeIntent.GENERIC_FILE,
            execution_profile=ExecutionProfile.UNIVERSAL_SANDBOXED,
            sandbox_verified=True,
        )
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == document_id)
        )
        assert intake is not None

        with pytest.raises(ValueError, match="cannot use legacy execution"):
            await enqueue(
                session,
                job_type="process_document",
                payload={
                    "document_id": str(document_id),
                    "source_intake_id": str(intake.id),
                },
                idempotency_key="forbidden-profile-downgrade",
                settings=settings,
            )

        assert (
            await session.scalars(select(Job).where(Job.document_id == document_id))
        ).all() == []


@pytest.mark.asyncio
async def test_enqueue_rejects_mismatched_source_identity_and_canonicalizes_partial_identity(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="transactions.csv",
            content_path="/tmp/transactions.csv",
            sha256="6" * 64,
            mime="text/csv",
            intake_intent=IntakeIntent.GENERIC_FILE,
        )
        first_intake = await session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == document_id,
                SourceIntake.source_version == 1,
            )
        )
        assert first_intake is not None
        replacement = await documents.append_raw_source(
            document_id,
            filename="receipt.png",
            content_path="/tmp/receipt.png",
            sha256="7" * 64,
            mime="image/png",
            actor="reviewer",
            intake_intent=IntakeIntent.BILL_SCAN,
        )
        second_intake = await session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == document_id,
                SourceIntake.source_version == replacement.version,
            )
        )
        assert second_intake is not None

        with pytest.raises(ValueError, match="source identity must resolve"):
            await enqueue(
                session,
                job_type="process_document",
                payload={
                    "document_id": str(document_id),
                    "source_intake_id": str(first_intake.id),
                    "source_file_id": str(first_intake.source_file_id),
                    "source_version": second_intake.source_version,
                },
                idempotency_key="mismatched-source-identity",
            )
        assert (
            await session.scalars(select(Job).where(Job.document_id == document_id))
        ).all() == []

        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={
                "document_id": str(document_id),
                "source_version": second_intake.source_version,
            },
            idempotency_key="canonical-partial-source-identity",
        )
        assert job_id is not None
        persisted = await session.get(Job, job_id)
        assert persisted is not None

    assert persisted.intake_intent is IntakeIntent.BILL_SCAN
    assert persisted.payload["source_intake_id"] == str(second_intake.id)
    assert persisted.payload["source_file_id"] == str(second_intake.source_file_id)
    assert persisted.payload["source_version"] == second_intake.source_version


@pytest.mark.asyncio
async def test_enqueue_canonicalizes_explicit_requirements_and_ignores_payload_evidence(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    registry_digest = "1" * 64
    capabilities_digest = "2" * 64
    canonical_components = ["database", "model:ocr", "storage"]
    requirements_digest = hashlib.sha256(
        json.dumps(
            canonical_components,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    async with session_factory() as session:
        document_id = await _document(session)
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={
                "document_id": str(document_id),
                "intake_mode": "universal",
                "execution_profile": "universal_sandboxed",
                "sandbox_verified": True,
                "registry_digest": "f" * 64,
                "capabilities_digest": "e" * 64,
                "requirements_digest": "d" * 64,
                "required_components": ["untrusted-payload-component"],
            },
            idempotency_key="explicit-evidence",
            settings=settings,
            registry_digest=registry_digest,
            capabilities_digest=capabilities_digest,
            requirements_digest=requirements_digest,
            required_components=[" storage ", "model:ocr", "database"],
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="worker-evidence", settings=settings)
        await session.commit()

    assert claimed is not None
    assert claimed["execution_profile"] == ExecutionProfile.LEGACY_COMPAT.value
    assert claimed["sandbox_verified"] is False
    assert claimed["registry_digest"] == registry_digest
    assert claimed["capabilities_digest"] == capabilities_digest
    assert claimed["required_components"] == canonical_components
    assert claimed["requirements_digest"] == requirements_digest
    # Job payloads remain backward-compatible data, but never become authoritative
    # evidence for scheduling or execution-profile decisions.
    assert claimed["payload"]["execution_profile"] == "universal_sandboxed"
    assert claimed["payload"]["sandbox_verified"] is True


@pytest.mark.asyncio
async def test_enqueue_rejects_noncanonical_explicit_evidence(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    async with session_factory() as session:
        document_id = await _document(session)
        with pytest.raises(ValueError, match="registry_digest"):
            await enqueue(
                session,
                job_type="process_document",
                payload={"document_id": str(document_id)},
                idempotency_key="bad-digest",
                settings=settings,
                registry_digest="NOT-A-DIGEST",
            )
        with pytest.raises(ValueError, match="duplicates"):
            await enqueue(
                session,
                job_type="process_document",
                payload={"document_id": str(document_id)},
                idempotency_key="duplicate-components",
                settings=settings,
                required_components=["database", " database "],
            )
        with pytest.raises(ValueError, match="must match canonical"):
            await enqueue(
                session,
                job_type="process_document",
                payload={"document_id": str(document_id)},
                idempotency_key="mismatched-requirements-digest",
                settings=settings,
                requirements_digest="3" * 64,
                required_components=["database"],
            )


@pytest.mark.asyncio
async def test_sqlite_conditional_claim_prevents_double_claim(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    async with session_factory() as session:
        document_id = await _document(session)
        await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="initial",
        )
        await session.commit()

    async def claim(owner: str):
        async with session_factory() as session:
            claimed = await claim_next(session, lease_owner=owner, settings=settings)
            await session.commit()
            return claimed

    first, second = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    claimed = [job for job in (first, second) if job is not None]

    assert len(claimed) == 1
    assert claimed[0]["status"] == JobStatus.RUNNING.value
    assert claimed[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_expired_leases_retry_with_backoff_then_become_dead(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, attempts=2)
    async with session_factory() as session:
        document_id = await _document(session)
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="initial",
        )
        assert job_id is not None
        first = await claim_next(session, lease_owner="worker-a", settings=settings)
        assert first is not None
        await retry_or_bury(
            session,
            job_id,
            "temporary failure",
            lease_owner="worker-a",
            settings=settings,
        )
        retry_job = await session.get(Job, job_id)
        assert retry_job is not None
        assert retry_job.status is JobStatus.QUEUED
        available_at = retry_job.available_at
        if available_at.tzinfo is None:  # SQLite does not round-trip timezone info.
            available_at = available_at.replace(tzinfo=dt.UTC)
        assert available_at > dt.datetime.now(dt.UTC)

        retry_job.available_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.flush()
        second = await claim_next(session, lease_owner="worker-b", settings=settings)
        assert second is not None
        assert second["attempts"] == 2
        await retry_or_bury(
            session,
            job_id,
            "final failure",
            lease_owner="worker-b",
            settings=settings,
        )
        await session.commit()

    async with session_factory() as session:
        final = await session.get(Job, job_id)
        assert final is not None
        assert final.status is JobStatus.DEAD
        assert final.last_error == "final failure"


@pytest.mark.asyncio
async def test_expired_running_lease_is_reclaimed_once(session_factory, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    async with session_factory() as session:
        document_id = await _document(session)
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="initial",
        )
        assert job_id is not None
        first = await claim_next(session, lease_owner="worker-a", settings=settings)
        assert first is not None
        row = await session.get(Job, job_id)
        assert row is not None
        row.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.flush()
        second = await claim_next(session, lease_owner="worker-b", settings=settings)
        await session.commit()

    assert second is not None
    assert second["id"] == first["id"]
    assert second["attempts"] == 2
    assert second["lease_owner"] == "worker-b"


def test_only_registered_handlers_can_be_resolved() -> None:
    with pytest.raises(LookupError, match="no handler registered"):
        get_handler("not-registered-for-test")
