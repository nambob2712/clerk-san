from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import clerksan.ingest.worker as worker_module
from clerksan.config import IntakeMode, SandboxUnavailable, Settings
from clerksan.db.models import (
    Base,
    DocumentStatus,
    Job,
    JobStatus,
    SourceIntake,
    SourceIntakeState,
    WorkerCapabilityLease,
)
from clerksan.db.repositories import DocumentRepo, SourceIntakeRepo
from clerksan.ingest.capabilities import build_capability_registry
from clerksan.ingest.filetype import FileType
from clerksan.ingest.jobs import claim_next, enqueue, get_handler, register_handler
from clerksan.ingest.parser_runner import ParserRunner, SandboxProbeResult
from clerksan.ingest.worker import _execute_claimed, _load_default_handlers, run


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _settings(tmp_path: Path, *, job_max_attempts: int = 2) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'worker.sqlite'}",
        intake_mode="legacy",
        demo_mode=True,
        job_lease_seconds=30,
        job_retry_base_seconds=2,
        job_max_attempts=job_max_attempts,
    )


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


async def _enqueue_document(session, job_type: str) -> object:
    document_id = await DocumentRepo(session).create_with_raw(
        filename="receipt.png",
        content_path="/tmp/receipt.png",
        sha256="c" * 64,
        mime="image/png",
    )
    job_id = await enqueue(
        session,
        job_type=job_type,
        payload={"document_id": str(document_id), "source_version": 1},
        idempotency_key=job_type,
    )
    assert job_id is not None
    return job_id


@pytest.mark.asyncio
async def test_worker_runs_only_registered_handler_and_marks_job_done(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    handled = asyncio.Event()
    stop = asyncio.Event()
    job_type = "worker-success-test"

    async def handler(session, payload) -> None:
        del session
        assert payload["_job_attempt"] == 1
        assert payload["_execution_profile"] == "legacy_compat"
        assert payload["_sandbox_verified"] is False
        assert payload["_registry_digest"] is None
        assert payload["_capabilities_digest"] is None
        assert payload["_requirements_digest"] is not None
        assert payload["_required_components"] == []
        assert payload["_intake_intent"] == "legacy_unspecified"
        assert payload["intake_intent"] == "legacy_unspecified"
        handled.set()

    register_handler(job_type, handler)
    async with session_factory() as session:
        job_id = await _enqueue_document(session, job_type)
        job = await session.get(Job, job_id)
        assert job is not None
        job.payload = {
            **job.payload,
            "_execution_profile": "universal_sandboxed",
            "_sandbox_verified": True,
            "_required_components": ["payload-spoof"],
            "intake_intent": "bill_scan",
        }
        await session.commit()

    worker_task = asyncio.create_task(
        run(
            settings=settings,
            session_factory=session_factory,
            stop_event=stop,
            poll_interval=0.01,
            worker_id="worker-test",
            install_signal_handlers=False,
        )
    )
    await asyncio.wait_for(handled.wait(), timeout=2)
    stop.set()
    await asyncio.wait_for(worker_task, timeout=2)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.DONE
        assert job.lease_owner is None


@pytest.mark.asyncio
async def test_worker_keeps_jobs_queued_until_local_models_are_ready(
    monkeypatch: pytest.MonkeyPatch, session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    handled = asyncio.Event()
    stop = asyncio.Event()
    ready = asyncio.Event()
    job_type = f"worker-model-gate-{uuid4()}"

    async def handler(session, payload) -> None:
        del session, payload
        handled.set()

    async def models_ready(_: Settings) -> bool:
        return ready.is_set()

    register_handler(job_type, handler)
    monkeypatch.setattr(worker_module, "_models_ready", models_ready)
    async with session_factory() as session:
        job_id = await _enqueue_document(session, job_type)
        await session.commit()

    worker_task = asyncio.create_task(
        run(
            settings=settings,
            session_factory=session_factory,
            stop_event=stop,
            poll_interval=0.01,
            model_check_interval=0.01,
            worker_id="worker-model-gate",
            install_signal_handlers=False,
        )
    )
    await asyncio.sleep(0.05)
    async with session_factory() as session:
        queued = await session.get(Job, job_id)
        assert queued is not None
        assert queued.status is JobStatus.QUEUED
        assert queued.attempts == 0

    ready.set()
    await asyncio.wait_for(handled.wait(), timeout=2)
    stop.set()
    await asyncio.wait_for(worker_task, timeout=2)


@pytest.mark.asyncio
async def test_worker_publishes_matching_empty_registry_lease(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    registry = build_capability_registry(settings)
    stop = asyncio.Event()
    worker_id = "worker-capability-evidence"

    worker_task = asyncio.create_task(
        run(
            settings=settings,
            session_factory=session_factory,
            stop_event=stop,
            poll_interval=0.01,
            worker_id=worker_id,
            install_signal_handlers=False,
        )
    )
    lease = None
    for _ in range(100):
        async with session_factory() as session:
            lease = await session.get(WorkerCapabilityLease, worker_id)
        if lease is not None:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(worker_task, timeout=2)

    assert lease is not None
    assert registry.process == ()
    assert lease.registry_digest == registry.registry_digest
    assert lease.capabilities_digest == registry.capabilities_digest
    assert lease.sandbox_verified is False
    heartbeat_at = _as_utc(lease.heartbeat_at)
    expires_at = _as_utc(lease.expires_at)
    assert expires_at - heartbeat_at == dt.timedelta(
        seconds=settings.worker_capability_lease_seconds
    )


@pytest.mark.asyncio
async def test_capability_lease_refresh_replaces_heartbeat_and_expiry(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    registry = build_capability_registry(settings)
    worker_id = "worker-capability-refresh"
    first_heartbeat = dt.datetime(2026, 8, 22, 1, 2, 3, tzinfo=dt.UTC)
    second_heartbeat = first_heartbeat + dt.timedelta(seconds=7)

    await worker_module._refresh_capability_lease(
        session_factory,
        worker_id,
        registry,
        settings,
        heartbeat_at=first_heartbeat,
    )
    await worker_module._refresh_capability_lease(
        session_factory,
        worker_id,
        registry,
        settings,
        heartbeat_at=second_heartbeat,
    )

    async with session_factory() as session:
        leases = (
            await session.scalars(
                select(WorkerCapabilityLease).where(WorkerCapabilityLease.worker_id == worker_id)
            )
        ).all()

    assert len(leases) == 1
    lease = leases[0]
    assert _as_utc(lease.heartbeat_at) == second_heartbeat
    assert _as_utc(lease.expires_at) == second_heartbeat + dt.timedelta(
        seconds=settings.worker_capability_lease_seconds
    )
    assert lease.registry_digest == registry.registry_digest
    assert lease.capabilities_digest == registry.capabilities_digest
    assert lease.sandbox_verified is False


@pytest.mark.asyncio
async def test_worker_fails_closed_for_premature_universal_mode(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    object.__setattr__(settings, "intake_mode", IntakeMode.UNIVERSAL)

    with pytest.raises(SandboxUnavailable, match="sandbox_unavailable"):
        await run(
            settings=settings,
            session_factory=session_factory,
            stop_event=asyncio.Event(),
            worker_id="worker-universal-rejected",
            install_signal_handlers=False,
        )


@pytest.mark.asyncio
async def test_universal_worker_withdraws_lease_when_live_sidecar_probe_fails(
    session_factory, tmp_path: Path
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'worker.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
        intake_mode="universal",
        worker_capability_heartbeat_seconds=1,
        worker_capability_lease_seconds=3,
    )

    class FlappingRunner(ParserRunner):
        def __init__(self) -> None:
            self.probe_count = 0

        def startup_probe(self) -> SandboxProbeResult:
            self.probe_count += 1
            if self.probe_count <= 2:
                return SandboxProbeResult(
                    verified=True,
                    backend="test-sidecar",
                    evidence_digest="d" * 64,
                    capabilities=tuple(item.value for item in FileType),
                )
            return SandboxProbeResult(verified=False, reason="sidecar_stopped")

    runner = FlappingRunner()
    stop = asyncio.Event()
    worker_id = "worker-sidecar-withdrawal"
    task = asyncio.create_task(
        run(
            settings=settings,
            session_factory=session_factory,
            stop_event=stop,
            poll_interval=0.01,
            worker_id=worker_id,
            install_signal_handlers=False,
            parser_runner=runner,
        )
    )
    observed_verified = False
    withdrawn = False
    for _ in range(250):
        async with session_factory() as session:
            lease = await session.get(WorkerCapabilityLease, worker_id)
        if lease is not None and lease.sandbox_verified:
            observed_verified = True
        if lease is not None and observed_verified and not lease.sandbox_verified:
            withdrawn = True
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert observed_verified is True
    assert withdrawn is True
    assert runner.probe_count >= 3


@pytest.mark.asyncio
async def test_handler_failure_is_recorded_without_stopping_the_worker(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    job_type = "worker-failure-test"

    async def handler(session, payload) -> None:
        del session, payload
        raise RuntimeError("intentional failure")

    register_handler(job_type, handler)
    async with session_factory() as session:
        job_id = await _enqueue_document(session, job_type)
        claimed = await claim_next(session, lease_owner="worker-test", settings=settings)
        assert claimed is not None
        await session.commit()

    await _execute_claimed(session_factory, claimed, "worker-test", settings)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.QUEUED
        assert "RuntimeError: intentional failure" in (job.last_error or "")
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == job.document_id)
        )
        assert intake is not None
        assert intake.state is SourceIntakeState.QUEUED


@pytest.mark.asyncio
async def test_process_retry_commits_processing_state_before_handler(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    observed_processing = asyncio.Event()

    async def failing_handler(session, payload) -> None:
        intake = await session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == UUID(payload["document_id"]),
                SourceIntake.source_version == payload["source_version"],
            )
        )
        assert intake is not None
        assert intake.state is SourceIntakeState.PROCESSING
        observed_processing.set()
        raise RuntimeError("temporary process failure")

    monkeypatch.setattr(worker_module, "get_handler", lambda job_type: failing_handler)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="9" * 64,
            mime="image/png",
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="process-retry-processing-state",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="worker-test", settings=settings)
        assert claimed is not None
        await session.commit()

    await _execute_claimed(session_factory, claimed, "worker-test", settings)

    assert observed_processing.is_set()
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        intake = await session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == document_id,
                SourceIntake.source_version == 1,
            )
        )

    assert job is not None
    assert job.status is JobStatus.QUEUED
    assert intake is not None
    assert intake.state is SourceIntakeState.PROCESSING


@pytest.mark.asyncio
async def test_terminal_process_failure_marks_current_document_failed_and_can_be_requeued(
    monkeypatch: pytest.MonkeyPatch, session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, job_max_attempts=1)

    async def failing_handler(session, payload) -> None:
        del session, payload
        raise RuntimeError("unreadable source")

    monkeypatch.setattr(worker_module, "get_handler", lambda job_type: failing_handler)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="d" * 64,
            mime="image/png",
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="process_document",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="worker-test", settings=settings)
        assert claimed is not None
        await session.commit()

    await _execute_claimed(session_factory, claimed, "worker-test", settings)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.DEAD
        assert job.last_error == "RuntimeError: unreadable source"
        intake = await session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == document_id,
                SourceIntake.source_version == 1,
            )
        )
        assert intake is not None
        assert intake.state is SourceIntakeState.FAILED
        assert intake.reason_code == "processing_failed"
        assert intake.retryable is True
        assert intake.failure_phase == "legacy_job"

        documents = DocumentRepo(session)
        detail = await documents.get(document_id)
        assert detail["status"] == DocumentStatus.FAILED.value
        assert detail["processing_error"] == "RuntimeError: unreadable source"

        first_target = await documents.prepare_reprocess(document_id, actor="local-reviewer")
        recovery_job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key=first_target.idempotency_key,
        )
        repeated_target = await documents.prepare_reprocess(document_id, actor="local-reviewer")
        duplicate = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key=repeated_target.idempotency_key,
        )
        await session.commit()

    assert recovery_job_id is not None
    assert repeated_target.idempotency_key == first_target.idempotency_key
    assert duplicate is None


@pytest.mark.asyncio
async def test_terminal_derivative_failure_gets_an_explicit_source_bound_recovery(
    monkeypatch: pytest.MonkeyPatch, session_factory, tmp_path: Path
) -> None:
    """A failed index must not force a new extraction or overwrite review state."""

    settings = _settings(tmp_path, job_max_attempts=1)

    async def failing_handler(session, payload) -> None:
        del session, payload
        raise RuntimeError("embedding service unavailable")

    monkeypatch.setattr(worker_module, "get_handler", lambda job_type: failing_handler)
    normalized_sha256 = "a" * 64
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="b" * 64,
            mime="image/png",
        )
        await documents.set_status(document_id, DocumentStatus.IN_REVIEW, source_version=1)
        job_id = await enqueue(
            session,
            job_type="index_document",
            payload={
                "document_id": str(document_id),
                "source_version": 1,
                "normalized_sha256": normalized_sha256,
            },
            idempotency_key=f"index:1:{normalized_sha256}",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="worker-test", settings=settings)
        assert claimed is not None
        await session.commit()

    await _execute_claimed(session_factory, claimed, "worker-test", settings)

    async with session_factory() as session:
        failed = await session.get(Job, job_id)
        assert failed is not None
        assert failed.status is JobStatus.DEAD
        assert failed.last_error == "RuntimeError: embedding service unavailable"
        intake = await session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == document_id,
                SourceIntake.source_version == 1,
            )
        )
        assert intake is not None
        assert intake.state is SourceIntakeState.QUEUED
        assert intake.reason_code == "processing_queued"
        assert intake.retryable is False
        failed_detail = await DocumentRepo(session).get(document_id)

        recovery = await DocumentRepo(session).retry_current_derivatives(document_id)
        repeated = await DocumentRepo(session).retry_current_derivatives(document_id)
        first_successor = await session.get(Job, recovery.queued_job_ids[0])
        assert first_successor is not None
        first_successor.status = JobStatus.DEAD
        first_successor.attempts = 1
        first_successor.last_error = "IndexingError: retry still unavailable"
        second_recovery = await DocumentRepo(session).retry_current_derivatives(document_id)
        detail = await DocumentRepo(session).get(document_id)
        successors = (
            await session.scalars(
                select(Job).where(Job.document_id == document_id).order_by(Job.created_at, Job.id)
            )
        ).all()
        await session.commit()

    assert detail["status"] == DocumentStatus.IN_REVIEW.value
    assert detail["processing_error"] is None
    assert (
        failed_detail["processing_error"]
        == "index_document: RuntimeError: embedding service unavailable"
    )
    assert len(recovery.queued_job_ids) == 1
    assert repeated.queued_job_ids == ()
    assert len(second_recovery.queued_job_ids) == 1
    assert len(successors) == 3
    successor = next(job for job in successors if job.id in recovery.queued_job_ids)
    second_successor = next(job for job in successors if job.id in second_recovery.queued_job_ids)
    assert successor.job_type == failed.job_type
    assert successor.payload == failed.payload
    assert successor.idempotency_key == f"recovery:{failed.id}"
    assert successor.status is JobStatus.DEAD
    assert successor.attempts == 1
    assert second_successor.payload == failed.payload
    assert second_successor.idempotency_key == f"recovery:{successor.id}"
    assert second_successor.status is JobStatus.QUEUED
    assert second_successor.attempts == 0


@pytest.mark.asyncio
async def test_derivative_recovery_skips_a_source_replaced_before_retry(
    session_factory, tmp_path: Path
) -> None:
    normalized_sha256 = "c" * 64
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="first.png",
            content_path="originals/first.png",
            sha256="d" * 64,
            mime="image/png",
        )
        job_id = await enqueue(
            session,
            job_type="index_document",
            payload={
                "document_id": str(document_id),
                "source_version": 1,
                "normalized_sha256": normalized_sha256,
            },
            idempotency_key=f"index:1:{normalized_sha256}",
        )
        assert job_id is not None
        failed = await session.get(Job, job_id)
        assert failed is not None
        failed.status = JobStatus.DEAD
        failed.last_error = "IndexingError: stale normalized artifact"
        await documents.append_raw_source(
            document_id,
            filename="replacement.png",
            content_path="originals/replacement.png",
            sha256="e" * 64,
            mime="image/png",
            actor="reviewer",
        )
        recovery = await documents.retry_current_derivatives(document_id)
        jobs = (await session.scalars(select(Job).where(Job.document_id == document_id))).all()
        detail = await documents.get(document_id)

    assert recovery.original_version == 2
    assert recovery.queued_job_ids == ()
    assert [job.id for job in jobs] == [job_id]
    assert detail["status"] == DocumentStatus.NEEDS_REPROCESS.value


@pytest.mark.asyncio
async def test_concurrent_derivative_retries_create_one_successor(
    session_factory, tmp_path: Path
) -> None:
    """The local SQLite path must turn a recovery-key collision into idempotency."""

    normalized_sha256 = "f" * 64
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="a" * 64,
            mime="image/png",
        )
        failed_job_id = await enqueue(
            session,
            job_type="index_document",
            payload={
                "document_id": str(document_id),
                "source_version": 1,
                "normalized_sha256": normalized_sha256,
            },
            idempotency_key=f"index:1:{normalized_sha256}",
        )
        assert failed_job_id is not None
        failed = await session.get(Job, failed_job_id)
        assert failed is not None
        failed.status = JobStatus.DEAD
        failed.last_error = "IndexingError: local model unavailable"
        await session.commit()

    ready = asyncio.Event()

    async def retry() -> tuple[object, ...]:
        async with session_factory() as session:
            await ready.wait()
            result = await DocumentRepo(session).retry_current_derivatives(document_id)
            await session.commit()
            return result.queued_job_ids

    first = asyncio.create_task(retry())
    second = asyncio.create_task(retry())
    await asyncio.sleep(0)
    ready.set()
    recovered = await asyncio.gather(first, second)

    assert sum(len(job_ids) for job_ids in recovered) == 1
    async with session_factory() as session:
        jobs = (
            await session.scalars(
                select(Job).where(Job.document_id == document_id).order_by(Job.created_at, Job.id)
            )
        ).all()

    assert len(jobs) == 2
    failed = next(job for job in jobs if job.id == failed_job_id)
    successor = next(job for job in jobs if job.id != failed_job_id)
    assert failed.status is JobStatus.DEAD
    assert successor.status is JobStatus.QUEUED
    assert successor.idempotency_key == f"recovery:{failed_job_id}"


@pytest.mark.asyncio
async def test_expired_final_process_lease_marks_document_failed_and_can_be_requeued(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, job_max_attempts=1)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="1" * 64,
            mime="image/png",
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="process_source:1",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="crashed-worker", settings=settings)
        assert claimed is not None
        job = await session.get(Job, job_id)
        assert job is not None
        job.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()

    assert await worker_module._claim(session_factory, "recovery-worker", settings) is None

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.DEAD
        assert job.last_error == "lease expired after final attempt"
        documents = DocumentRepo(session)
        detail = await documents.get(document_id)
        recovery = await documents.prepare_reprocess(document_id, actor="local-reviewer")

    assert detail["status"] == DocumentStatus.FAILED.value
    assert detail["processing_error"] == "lease expired after final attempt"
    assert recovery.idempotency_key == f"reprocess:1:failure:{job_id}"


@pytest.mark.asyncio
async def test_expired_completed_process_lease_becomes_done_without_false_failure(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, job_max_attempts=1)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="2" * 64,
            mime="image/png",
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="process_source:1",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="crashed-worker", settings=settings)
        assert claimed is not None
        job = await session.get(Job, job_id)
        assert job is not None
        job.payload = {**job.payload, "_pipeline": {"completed": True}}
        job.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()

    assert await worker_module._claim(session_factory, "recovery-worker", settings) is None

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.DONE
        assert job.last_error is None
        detail = await DocumentRepo(session).get(document_id)

    assert detail["status"] == DocumentStatus.UPLOADED.value
    assert detail["processing_error"] is None


@pytest.mark.asyncio
async def test_reclaimed_completed_process_job_skips_handler_before_intake_transition(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, job_max_attempts=2)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="8" * 64,
            mime="image/png",
        )
        intake = await session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == document_id,
                SourceIntake.source_version == 1,
            )
        )
        assert intake is not None
        await SourceIntakeRepo(session).transition(
            intake.id,
            expected_version=intake.version,
            state=SourceIntakeState.PROCESSED,
            actor="worker-test",
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="completed-before-reclaim",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="crashed-worker", settings=settings)
        assert claimed is not None
        job = await session.get(Job, job_id)
        assert job is not None
        job.payload = {**job.payload, "_pipeline": {"completed": True}}
        job.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()

    reclaimed = await worker_module._claim(session_factory, "recovery-worker", settings)
    assert reclaimed is not None
    assert reclaimed["attempts"] == 2
    monkeypatch.setattr(
        worker_module,
        "get_handler",
        lambda job_type: pytest.fail("completed job must not resolve a handler"),
    )

    await _execute_claimed(session_factory, reclaimed, "recovery-worker", settings)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == document_id)
        )
        detail = await DocumentRepo(session).get(document_id)

    assert job is not None
    assert job.status is JobStatus.DONE
    assert job.last_error is None
    assert intake is not None
    assert intake.state is SourceIntakeState.PROCESSED
    assert detail["status"] == DocumentStatus.UPLOADED.value
    assert detail["processing_error"] is None


@pytest.mark.asyncio
async def test_expired_completed_format_rebuild_lease_becomes_done(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, job_max_attempts=1)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="expenses.xlsx",
            content_path="originals/expenses.xlsx",
            sha256="3" * 64,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        job_id = await enqueue(
            session,
            job_type="rebuild_format_derivatives",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="format-derivatives:0013:1",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="crashed-worker", settings=settings)
        assert claimed is not None
        job = await session.get(Job, job_id)
        assert job is not None
        job.payload = {**job.payload, "_pipeline": {"completed": True}}
        job.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()

    assert await worker_module._claim(session_factory, "recovery-worker", settings) is None

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.DONE
        assert job.last_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_type", "payload"),
    [
        (
            "index_document",
            {"source_version": 1, "normalized_sha256": "a" * 64},
        ),
        (
            "process_embedded_media",
            {"source_version": 1, "media_sha256": "b" * 64},
        ),
    ],
)
async def test_expired_completed_derivative_lease_becomes_done(
    session_factory, tmp_path: Path, job_type: str, payload: dict[str, object]
) -> None:
    """A worker killed after a derivative handler commits must not create a false DEAD job."""

    settings = _settings(tmp_path, job_max_attempts=1)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="5" * 64,
            mime="image/png",
        )
        job_id = await enqueue(
            session,
            job_type=job_type,
            payload={"document_id": str(document_id), **payload},
            idempotency_key=f"{job_type}:completed",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="crashed-worker", settings=settings)
        assert claimed is not None
        job = await session.get(Job, job_id)
        assert job is not None
        job.payload = {**job.payload, "_pipeline": {"completed": True}}
        job.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()

    assert await worker_module._claim(session_factory, "recovery-worker", settings) is None

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.DONE
        assert job.last_error is None


@pytest.mark.asyncio
async def test_expired_incomplete_format_rebuild_lease_is_requeued(
    session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, job_max_attempts=1)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="expenses.xlsx",
            content_path="originals/expenses.xlsx",
            sha256="4" * 64,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        job_id = await enqueue(
            session,
            job_type="rebuild_format_derivatives",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="format-derivatives:0013:1",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="crashed-worker", settings=settings)
        assert claimed is not None
        job = await session.get(Job, job_id)
        assert job is not None
        job.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()

    reclaimed = await worker_module._claim(session_factory, "recovery-worker", settings)

    assert reclaimed is not None
    assert reclaimed["id"] == job_id
    assert reclaimed["attempts"] == 1


@pytest.mark.asyncio
async def test_terminal_failure_for_a_replaced_source_does_not_overwrite_current_state(
    monkeypatch: pytest.MonkeyPatch, session_factory, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, job_max_attempts=1)

    async def failing_handler(session, payload) -> None:
        del session, payload
        raise RuntimeError("obsolete source failed")

    monkeypatch.setattr(worker_module, "get_handler", lambda job_type: failing_handler)
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="first.png",
            content_path="originals/first.png",
            sha256="e" * 64,
            mime="image/png",
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="process_source:1",
        )
        assert job_id is not None
        claimed = await claim_next(session, lease_owner="worker-test", settings=settings)
        assert claimed is not None
        await session.commit()

    async with session_factory() as session:
        await DocumentRepo(session).append_raw_source(
            document_id,
            filename="replacement.png",
            content_path="originals/replacement.png",
            sha256="f" * 64,
            mime="image/png",
            actor="local-reviewer",
        )
        await session.commit()

    await _execute_claimed(session_factory, claimed, "worker-test", settings)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.DEAD
        detail = await DocumentRepo(session).get(document_id)

    assert detail["status"] == DocumentStatus.NEEDS_REPROCESS.value
    assert detail["processing_error"] is None


@pytest.mark.asyncio
async def test_worker_commits_handler_transaction_before_stopping_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A heartbeat must never wait on a job row held by the handler transaction."""

    active_transactions = 0
    events: list[str] = []

    class Transaction:
        async def __aenter__(self):
            nonlocal active_transactions
            active_transactions += 1
            return self

        async def __aexit__(self, exc_type, exc_value, traceback) -> None:
            del exc_type, exc_value, traceback
            nonlocal active_transactions
            active_transactions -= 1

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback) -> None:
            del exc_type, exc_value, traceback

        def begin(self) -> Transaction:
            return Transaction()

    class SessionFactory:
        def __call__(self) -> Session:
            return Session()

    job_type = f"worker-transaction-boundary-{uuid4()}"

    async def handler(session, payload) -> None:
        del session, payload
        assert active_transactions == 1
        events.append("handled")

    async def stop_heartbeat(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        if task.done():
            return
        assert active_transactions == 0
        events.append("heartbeat_stopped")
        stop.set()
        await task

    async def mark_job_done(session, job_id, *, lease_owner) -> None:
        del session, job_id, lease_owner
        assert active_transactions == 1
        events.append("done")

    register_handler(job_type, handler)
    monkeypatch.setattr(worker_module, "_stop_heartbeat", stop_heartbeat)
    monkeypatch.setattr(worker_module, "mark_done", mark_job_done)

    await worker_module._execute_claimed(
        SessionFactory(),
        {"id": uuid4(), "job_type": job_type, "payload": {}, "attempts": 1},
        "worker-test",
        _settings(tmp_path),
    )

    assert events == ["handled", "heartbeat_stopped", "done"]


def test_worker_registers_process_and_index_handlers_without_importing_the_api() -> None:
    _load_default_handlers()

    assert callable(get_handler("process_document"))
    assert callable(get_handler("rebuild_format_derivatives"))
    assert callable(get_handler("index_document"))
    assert callable(get_handler("process_embedded_media"))
