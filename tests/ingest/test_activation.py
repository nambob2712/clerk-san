from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.config import Settings
from clerksan.db.models import Base
from clerksan.db.repositories import DocumentRepo, WorkerCapabilityLeaseRepo
from clerksan.ingest.activation import evaluate_universal_activation
from clerksan.ingest.capabilities import CapabilityRegistry, SandboxEvidence
from clerksan.ingest.jobs import enqueue
from clerksan.ingest.policy import PublicReasonCode


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activation.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _settings(tmp_path: Path, mode: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'activation.sqlite'}",
        storage_dir=tmp_path / "storage",
        intake_mode=mode,
        demo_mode=True,
    )


def _registry(settings: Settings) -> CapabilityRegistry:
    return CapabilityRegistry(
        process=("csv", "docx", "jpeg", "md", "pdf", "png", "webp", "xlsx"),
        limits={"max_upload_bytes": settings.max_upload_bytes},
        flags={"intake_mode": "universal"},
        sandbox=SandboxEvidence(
            verified=True,
            backend="parser-sidecar",
            evidence_digest="a" * 64,
        ),
    )


@pytest.mark.asyncio
async def test_universal_activation_requires_fresh_matching_worker_lease(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, "universal")
    registry = _registry(settings)
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        missing = await evaluate_universal_activation(session, settings, registry, now=now)
        assert missing.reason_code is PublicReasonCode.WORKER_CAPABILITY_STALE

        await WorkerCapabilityLeaseRepo(session).refresh(
            worker_id="worker",
            registry_digest=registry.registry_digest,
            capabilities_digest=registry.capabilities_digest,
            sandbox_verified=True,
            heartbeat_at=now,
            expires_at=now + dt.timedelta(seconds=30),
        )
        ready = await evaluate_universal_activation(session, settings, registry, now=now)

    assert ready.ready is True
    assert ready.reason_code is None


@pytest.mark.parametrize(
    ("lease_change", "expected_reason"),
    (
        ("expired", PublicReasonCode.WORKER_CAPABILITY_STALE),
        ("registry_mismatch", PublicReasonCode.REGISTRY_MISMATCH),
        ("capabilities_mismatch", PublicReasonCode.REGISTRY_MISMATCH),
        ("sandbox_withdrawn", PublicReasonCode.REGISTRY_MISMATCH),
    ),
)
@pytest.mark.asyncio
async def test_universal_activation_rejects_each_stale_or_mismatched_lease_dimension(
    session_factory,
    tmp_path: Path,
    lease_change: str,
    expected_reason: PublicReasonCode,
) -> None:
    settings = _settings(tmp_path, "universal")
    registry = _registry(settings)
    now = dt.datetime.now(dt.UTC)
    registry_digest = registry.registry_digest
    capabilities_digest = registry.capabilities_digest
    sandbox_verified = True
    expires_at = now + dt.timedelta(seconds=30)
    if lease_change == "expired":
        expires_at = now - dt.timedelta(seconds=1)
    elif lease_change == "registry_mismatch":
        registry_digest = "b" * 64
    elif lease_change == "capabilities_mismatch":
        capabilities_digest = "c" * 64
    else:
        sandbox_verified = False

    async with session_factory() as session:
        await WorkerCapabilityLeaseRepo(session).refresh(
            worker_id="worker",
            registry_digest=registry_digest,
            capabilities_digest=capabilities_digest,
            sandbox_verified=sandbox_verified,
            heartbeat_at=now - dt.timedelta(seconds=60),
            expires_at=expires_at,
        )
        status = await evaluate_universal_activation(session, settings, registry, now=now)

    assert status.ready is False
    assert status.reason_code is expected_reason
    assert status.legacy_jobs_blocking is False


@pytest.mark.asyncio
async def test_activation_rejects_an_unverified_registry_before_considering_a_lease(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, "universal")
    registry = CapabilityRegistry(
        process=(),
        limits={"max_upload_bytes": settings.max_upload_bytes},
        flags={"intake_mode": "universal"},
        sandbox=SandboxEvidence(verified=False),
    )

    async with session_factory() as session:
        status = await evaluate_universal_activation(session, settings, registry)

    assert status.ready is False
    assert status.reason_code is PublicReasonCode.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_legacy_mode_does_not_require_or_consume_universal_lease_evidence(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, "legacy")
    registry = _registry(settings)

    async with session_factory() as session:
        status = await evaluate_universal_activation(session, settings, registry)

    assert status.ready is True
    assert status.reason_code is None
    assert status.lease is None


@pytest.mark.asyncio
async def test_legacy_parser_job_blocks_universal_cutover(session_factory, tmp_path: Path) -> None:
    legacy_settings = _settings(tmp_path, "legacy")
    universal_settings = _settings(tmp_path, "universal")
    registry = _registry(universal_settings)
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="legacy.md",
            content_path="originals/legacy.md",
            sha256="b" * 64,
            mime="text/markdown",
        )
        await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="legacy-parser-job",
            settings=legacy_settings,
        )
        await WorkerCapabilityLeaseRepo(session).refresh(
            worker_id="worker",
            registry_digest=registry.registry_digest,
            capabilities_digest=registry.capabilities_digest,
            sandbox_verified=True,
            heartbeat_at=now,
            expires_at=now + dt.timedelta(seconds=30),
        )
        status = await evaluate_universal_activation(session, universal_settings, registry, now=now)

    assert status.ready is False
    assert status.legacy_jobs_blocking is True
    assert status.reason_code is PublicReasonCode.REGISTRY_MISMATCH
