"""Dedicated worker entry point for durable Clerk-san ingestion jobs.

This module never imports the FastAPI application.  The API enqueues work; this
process leases and executes only handlers explicitly registered in ``jobs``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import signal
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clerksan.config import IntakeMode, Settings, get_settings
from clerksan.db.engine import get_sessionmaker
from clerksan.db.models import (
    DocumentStatus,
    IntakeIntent,
    Job,
    JobStatus,
    SourceIntake,
    SourceIntakeState,
)
from clerksan.db.repositories import (
    DocumentRepo,
    SourceIntakeRepo,
    SourceIntakeValidationError,
    SourceVersionSupersededError,
    StaleSourceIntakeError,
    WorkerCapabilityLeaseRepo,
)
from clerksan.ingest.capabilities import (
    LEGACY_COMPAT_EXECUTION_PROFILE,
    CapabilityRegistry,
    build_capability_registry,
)
from clerksan.ingest.jobs import (
    JobLeaseLostError,
    JobStateError,
    claim_next,
    default_worker_id,
    get_handler,
    mark_done,
    renew_lease,
    retry_or_bury,
)
from clerksan.ingest.parser_runner import (
    ParserRunner,
    SidecarSandboxBackend,
    UnavailableSandboxBackend,
)
from clerksan.ingest.storage_reconcile import async_storage_lock, finalize_reservation
from clerksan.llm.client import OllamaClient

logger = logging.getLogger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


async def run(
    concurrency: int | None = None,
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    stop_event: asyncio.Event | None = None,
    poll_interval: float = 0.2,
    model_check_interval: float = 5.0,
    worker_id: str | None = None,
    install_signal_handlers: bool = True,
    parser_runner: ParserRunner | None = None,
    capability_registry: CapabilityRegistry | None = None,
) -> None:
    """Claim and execute jobs until asked to stop, then drain active handlers.

    The optional dependencies make the lifecycle testable without a process-global
    database.  Production simply uses the configured session factory and signal
    handlers.
    """

    active_settings = settings or get_settings()
    active_parser_runner = parser_runner or _parser_runner_for_settings(active_settings)
    probe = (
        active_parser_runner.startup_probe()
        if active_settings.intake_mode is IntakeMode.UNIVERSAL
        else None
    )
    probed_registry = build_capability_registry(active_settings, probe)
    if capability_registry is not None and (
        capability_registry.registry_digest != probed_registry.registry_digest
        or capability_registry.capabilities_digest != probed_registry.capabilities_digest
    ):
        raise ValueError("injected capability registry does not match the parser probe")
    registry = capability_registry or probed_registry
    max_concurrency = concurrency if concurrency is not None else active_settings.worker_concurrency
    if max_concurrency < 1:
        raise ValueError("concurrency must be greater than zero")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")
    if model_check_interval <= 0:
        raise ValueError("model_check_interval must be greater than zero")

    factory = session_factory or get_sessionmaker(active_settings)
    stop = stop_event or asyncio.Event()
    owner = worker_id or default_worker_id()
    _load_default_handlers()
    pipeline_dependencies = None
    if active_settings.intake_mode is IntakeMode.UNIVERSAL:
        from clerksan.ingest.pipeline import build_default_dependencies

        pipeline_dependencies = build_default_dependencies(
            active_settings,
            parser_runner=active_parser_runner,
            capability_registry=registry,
        )
    restore_signals = _install_stop_signals(stop) if install_signal_handlers else lambda: None
    active: set[asyncio.Task[None]] = set()
    next_model_check = 0.0
    next_capability_refresh = 0.0
    models_ready = False
    sandbox_ready = registry.sandbox_verified

    try:
        while not stop.is_set():
            _collect_finished(active)
            loop_time = asyncio.get_running_loop().time()
            if loop_time >= next_capability_refresh:
                if active_settings.intake_mode is IntakeMode.UNIVERSAL:
                    sandbox_ready = _probe_matches_registry(
                        active_parser_runner,
                        active_settings,
                        registry,
                    )
                await _refresh_capability_lease(
                    factory,
                    owner,
                    registry,
                    active_settings,
                    sandbox_verified=sandbox_ready,
                )
                next_capability_refresh = (
                    loop_time + active_settings.worker_capability_heartbeat_seconds
                )
            if loop_time >= next_model_check:
                models_ready = await _models_ready(active_settings)
                next_model_check = loop_time + model_check_interval
            if not models_ready and active_settings.intake_mode is IntakeMode.LEGACY:
                await _wait_for_progress_or_stop(active, stop, min(model_check_interval, 1.0))
                continue
            if active_settings.intake_mode is IntakeMode.UNIVERSAL and not sandbox_ready:
                await _wait_for_progress_or_stop(
                    active,
                    stop,
                    min(active_settings.worker_capability_heartbeat_seconds, 1.0),
                )
                continue
            claimed_any = False
            while len(active) < max_concurrency and not stop.is_set():
                job = await _claim(
                    factory,
                    owner,
                    active_settings,
                    registry,
                    models_ready=models_ready,
                )
                if job is None:
                    break
                claimed_any = True
                task = asyncio.create_task(
                    _execute_claimed(
                        factory,
                        job,
                        owner,
                        active_settings,
                        pipeline_dependencies=pipeline_dependencies,
                    ),
                    name=f"clerksan-job-{job['id']}",
                )
                active.add(task)

            if claimed_any:
                continue
            await _wait_for_progress_or_stop(active, stop, poll_interval)
    except asyncio.CancelledError:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        raise
    finally:
        restore_signals()
        if active:
            # On SIGTERM, finish the jobs already leased before the process exits.  A
            # hard kill remains safe because the lease will expire and be reclaimed.
            await asyncio.gather(*active, return_exceptions=True)


async def _models_ready(settings: Settings) -> bool:
    """Keep production jobs queued until every configured local model is available."""

    if settings.demo_mode:
        return True
    client = OllamaClient(settings)
    try:
        installed = await client.list_models()
    except Exception:  # noqa: BLE001 - readiness failures must not consume job attempts
        return False
    finally:
        await client.aclose()

    by_name = {
        _canonical_model_name(name): model
        for model in installed
        if isinstance(name := (model.get("name") or model.get("model")), str)
    }
    if any(_canonical_model_name(model) not in by_name for model in settings.required_models):
        return False
    if settings.embed_model and settings.embed_model_digest:
        embedded = by_name.get(_canonical_model_name(settings.embed_model))
        if not embedded or embedded.get("digest") != settings.embed_model_digest:
            return False
    return True


def _canonical_model_name(model: str) -> str:
    return model.strip().removesuffix(":latest")


def _parser_runner_for_settings(settings: Settings) -> ParserRunner:
    if settings.intake_mode is IntakeMode.UNIVERSAL:
        return ParserRunner(
            SidecarSandboxBackend(
                str(settings.parser_socket_path),
                timeout_seconds=settings.parser_request_timeout_seconds,
            )
        )
    return ParserRunner(UnavailableSandboxBackend())


async def _claim(
    session_factory: SessionFactory,
    owner: str,
    settings: Settings,
    registry: CapabilityRegistry | None = None,
    *,
    models_ready: bool = True,
) -> dict[str, Any] | None:
    async with session_factory() as session:
        async with session.begin():
            return await claim_next(
                session,
                lease_owner=owner,
                settings=settings,
                on_terminal_lease=_mark_terminal_processing_failure,
                capability_registry=registry,
                models_ready=models_ready,
            )


async def _refresh_capability_lease(
    session_factory: SessionFactory,
    worker_id: str,
    registry: CapabilityRegistry,
    settings: Settings,
    *,
    heartbeat_at: dt.datetime | None = None,
    sandbox_verified: bool | None = None,
) -> None:
    """Publish one expiring API/worker parity record without elevating sandbox state."""

    heartbeat = heartbeat_at or dt.datetime.now(dt.UTC)
    expires_at = heartbeat + dt.timedelta(seconds=settings.worker_capability_lease_seconds)
    async with session_factory() as session:
        async with session.begin():
            await WorkerCapabilityLeaseRepo(session).refresh(
                worker_id=worker_id,
                registry_digest=registry.registry_digest,
                capabilities_digest=registry.capabilities_digest,
                sandbox_verified=(
                    registry.sandbox_verified if sandbox_verified is None else sandbox_verified
                ),
                heartbeat_at=heartbeat,
                expires_at=expires_at,
            )


def _probe_matches_registry(
    parser_runner: ParserRunner,
    settings: Settings,
    expected: CapabilityRegistry,
) -> bool:
    """Re-probe the sidecar before every lease heartbeat and reject any drift."""

    try:
        current = build_capability_registry(settings, parser_runner.startup_probe())
    except Exception:  # noqa: BLE001 - a failed runtime probe must withdraw the lease
        return False
    return (
        current.sandbox_verified
        and current.registry_digest == expected.registry_digest
        and current.capabilities_digest == expected.capabilities_digest
    )


async def _execute_claimed(
    session_factory: SessionFactory,
    job: dict[str, Any],
    worker_id: str,
    settings: Settings,
    *,
    pipeline_dependencies: Any | None = None,
) -> None:
    """Execute one registered handler in a fresh transaction and persist its outcome."""

    if _job_pipeline_completed(job["payload"]):
        async with session_factory() as session:
            async with session.begin():
                await mark_done(session, job["id"], lease_owner=worker_id)
        return

    try:
        handler = get_handler(job["job_type"])
    except Exception as error:  # noqa: BLE001 - unknown jobs become observable retries
        await _record_failure(session_factory, job["id"], worker_id, settings, error)
        return

    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _renew_lease_until_stopped(
            session_factory,
            job["id"],
            worker_id,
            settings,
            heartbeat_stop,
        ),
        name=f"clerksan-lease-{job['id']}",
    )
    try:
        await _mark_claimed_source_processing(session_factory, job)
        handler_payload = dict(job["payload"])
        intake_intent = _job_intake_intent(job)
        handler_payload.update(
            {
                "_capabilities_digest": job.get("capabilities_digest"),
                "_execution_profile": job.get("execution_profile", LEGACY_COMPAT_EXECUTION_PROFILE),
                "_job_id": str(job["id"]),
                "_job_attempt": job["attempts"],
                "_lease_owner": worker_id,
                "_registry_digest": job.get("registry_digest"),
                "_required_components": list(job.get("required_components") or ()),
                "_requirements_digest": job.get("requirements_digest"),
                "_sandbox_verified": bool(job.get("sandbox_verified", False)),
                "_intake_intent": intake_intent.value,
                "intake_intent": intake_intent.value,
            }
        )
        uses_pipeline_artifact_storage = (
            pipeline_dependencies is not None and job["job_type"] == "process_document"
        )
        storage_lease = (
            async_storage_lock(settings.storage_dir, shared=True)
            if uses_pipeline_artifact_storage
            else contextlib.nullcontext()
        )
        async with storage_lease:
            async with session_factory() as session:
                async with session.begin():
                    if pipeline_dependencies is not None and job["job_type"] in {
                        "process_document",
                        "rebuild_format_derivatives",
                    }:
                        await handler(
                            session,
                            handler_payload,
                            dependencies=pipeline_dependencies,
                        )
                    else:
                        await handler(session, handler_payload)
                if uses_pipeline_artifact_storage:
                    from clerksan.ingest.pipeline import take_committed_artifact_reservations

                    reservations = take_committed_artifact_reservations(session)
                else:
                    reservations = ()
            for reservation in reservations:
                try:
                    finalize_reservation(reservation)
                except Exception:  # noqa: BLE001 - startup reconciliation owns cleanup
                    logger.exception(
                        "unable to finalize a committed derivative reservation",
                        extra={"job_id": str(job["id"])},
                    )

        # A handler can write its job payload as part of its durable result. Commit
        # that transaction before stopping the heartbeat: otherwise a heartbeat that
        # is waiting on the job row could deadlock with this task awaiting it.
        await _stop_heartbeat(heartbeat_stop, heartbeat)
        async with session_factory() as session:
            async with session.begin():
                await mark_done(session, job["id"], lease_owner=worker_id)
    except asyncio.CancelledError:
        # Do not mark a cancellation failed: the current lease preserves exclusivity
        # until another worker can safely reclaim it.
        raise
    except Exception as error:  # noqa: BLE001 - handler faults must not kill the loop
        await _stop_heartbeat(heartbeat_stop, heartbeat)
        logger.exception("worker job failed", extra={"job_id": str(job["id"])})
        await _record_failure(session_factory, job["id"], worker_id, settings, error)
    finally:
        await _stop_heartbeat(heartbeat_stop, heartbeat)


async def _mark_claimed_source_processing(
    session_factory: SessionFactory,
    job: dict[str, Any],
) -> None:
    """Commit exact-source processing state before a long legacy handler begins."""

    if job["job_type"] != "process_document":
        return
    source_version = _job_source_version(job["payload"])
    if source_version is None:
        logger.warning(
            "claimed process job has no source version; leaving intake state unchanged",
            extra={"job_id": str(job["id"])},
        )
        return
    async with session_factory() as session:
        async with session.begin():
            intake = await _source_intake_for_job(
                session,
                document_id=job["document_id"],
                payload=job["payload"],
                source_version=source_version,
                intake_intent=_job_intake_intent(job),
            )
            if intake is None:
                raise LookupError("process job has no exact source intake for its source version")
            if intake.state is SourceIntakeState.PROCESSING:
                return
            if intake.state is not SourceIntakeState.QUEUED:
                raise SourceIntakeValidationError(
                    "process job requires an explicitly requeued source intake"
                )
            await SourceIntakeRepo(session).transition(
                intake.id,
                expected_version=intake.version,
                state=SourceIntakeState.PROCESSING,
                actor="worker",
                reason_code="processing_queued",
                retryable=False,
            )


async def _record_failure(
    session_factory: SessionFactory,
    job_id: Any,
    worker_id: str,
    settings: Settings,
    error: Exception,
) -> None:
    async with session_factory() as session:
        try:
            async with session.begin():
                await retry_or_bury(
                    session,
                    job_id,
                    f"{type(error).__name__}: {error}",
                    lease_owner=worker_id,
                    settings=settings,
                )
                job = await session.get(Job, job_id)
                if job is not None:
                    await _mark_terminal_processing_failure(session, job)
        except JobLeaseLostError:
            logger.warning(
                "worker job lease was lost before failure could be recorded",
                extra={"job_id": str(job_id)},
            )


async def _mark_terminal_processing_failure(session: AsyncSession, job: Job) -> None:
    """Recover crash-only rebuild leases or expose a current source processing failure."""

    if (
        job.job_type == "rebuild_format_derivatives"
        and job.status is JobStatus.DEAD
        and job.last_error == "lease expired after final attempt"
    ):
        # A migration maintenance job has no user-facing failure state. If a process died
        # before its durable completion marker, return it to the queue rather than silently
        # leave mutable projections absent forever. A real handler exception remains dead.
        job.status = JobStatus.QUEUED
        job.attempts = 0
        job.available_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        job.last_error = None
        await session.flush()
        return

    if job.job_type != "process_document" or job.status is not JobStatus.DEAD:
        return
    source_version = _job_source_version(job.payload)
    if source_version is None:
        logger.warning(
            "terminal process job has no source version; leaving document state unchanged",
            extra={"job_id": str(job.id)},
        )
        return
    await _mark_source_intake_failed(session, job, source_version)
    try:
        await DocumentRepo(session).set_status(
            job.document_id,
            DocumentStatus.FAILED,
            source_version=source_version,
        )
    except SourceVersionSupersededError:
        logger.info(
            "terminal process failure belongs to a superseded source",
            extra={"job_id": str(job.id), "source_version": source_version},
        )


async def _mark_source_intake_failed(
    session: AsyncSession,
    job: Job,
    source_version: int,
) -> None:
    """Record terminal source processing failure without touching derivative jobs."""

    intake = await _source_intake_for_job(
        session,
        document_id=job.document_id,
        payload=job.payload,
        source_version=source_version,
        intake_intent=job.intake_intent,
    )
    if intake is None:
        if (
            job.payload.get("source_file_id") is not None
            or job.payload.get("source_intake_id") is not None
        ):
            raise LookupError("terminal process job exact source identity does not match an intake")
        logger.warning(
            "terminal process job has no exact source intake",
            extra={"job_id": str(job.id), "source_version": source_version},
        )
        return
    if intake.state is SourceIntakeState.FAILED:
        return
    try:
        await SourceIntakeRepo(session).transition(
            intake.id,
            expected_version=intake.version,
            state=SourceIntakeState.FAILED,
            actor="worker",
            reason_code="processing_failed",
            retryable=True,
            failure_phase="legacy_job",
        )
    except (SourceIntakeValidationError, StaleSourceIntakeError):
        # Preserve the existing durable job/document failure semantics if another
        # lifecycle writer won the race or exposed an invalid state edge. The intake
        # remains visibly inconsistent instead of being rewritten outside its repo.
        logger.error(
            "terminal process job could not transition its source intake",
            extra={
                "intake_id": str(intake.id),
                "job_id": str(job.id),
                "source_version": source_version,
            },
            exc_info=True,
        )


async def _source_intake_for_job(
    session: AsyncSession,
    *,
    document_id: Any,
    payload: dict[str, Any],
    source_version: int,
    intake_intent: IntakeIntent | str,
) -> SourceIntake | None:
    """Lock the intake matching every exact-source identifier a job carries."""

    statement = select(SourceIntake).where(
        SourceIntake.document_id == document_id,
        SourceIntake.source_version == source_version,
    )
    source_file_id = _job_optional_uuid(payload, "source_file_id")
    if source_file_id is not None:
        statement = statement.where(SourceIntake.source_file_id == source_file_id)
    source_intake_id = _job_optional_uuid(payload, "source_intake_id")
    if source_intake_id is not None:
        statement = statement.where(SourceIntake.id == source_intake_id)
    intake = await session.scalar(statement.with_for_update())
    if intake is None:
        return None
    expected_intent = _job_intake_intent({"intake_intent": intake_intent})
    if intake.intake_intent != expected_intent:
        raise SourceIntakeValidationError(
            "process job intake intent does not match its persisted source intake"
        )
    return intake


def _job_intake_intent(job: dict[str, Any]) -> IntakeIntent:
    """Read durable job intent, retaining the legacy default for old claim payloads."""

    value = job.get("intake_intent", IntakeIntent.LEGACY_UNSPECIFIED)
    if isinstance(value, IntakeIntent):
        return value
    try:
        return IntakeIntent(value)
    except (TypeError, ValueError) as error:
        raise SourceIntakeValidationError("job has an unsupported intake_intent") from error


def _job_optional_uuid(payload: dict[str, Any], field_name: str) -> UUID | None:
    value = payload.get(field_name)
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"payload.{field_name} must be a UUID") from error


def _job_source_version(payload: dict[str, Any]) -> int | None:
    value = payload.get("source_version")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _job_pipeline_completed(payload: dict[str, Any]) -> bool:
    pipeline = payload.get("_pipeline")
    return isinstance(pipeline, dict) and pipeline.get("completed") is True


async def _renew_lease_until_stopped(
    session_factory: SessionFactory,
    job_id: Any,
    worker_id: str,
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Keep long-running work leased without sharing the handler's transaction."""

    interval = max(0.1, settings.job_lease_seconds / 3)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            async with session_factory() as session:
                async with session.begin():
                    await renew_lease(
                        session,
                        job_id,
                        lease_owner=worker_id,
                        settings=settings,
                    )
        except (JobLeaseLostError, JobStateError):
            logger.warning(
                "worker job lease can no longer be renewed",
                extra={"job_id": str(job_id)},
            )
            return
        except Exception:  # noqa: BLE001 - a later heartbeat may recover a transient DB fault
            logger.warning(
                "worker lease renewal failed",
                extra={"job_id": str(job_id)},
                exc_info=True,
            )


async def _stop_heartbeat(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    if task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return
    stop.set()
    await task


async def _wait_for_progress_or_stop(
    active: set[asyncio.Task[None]], stop: asyncio.Event, poll_interval: float
) -> None:
    if stop.is_set():
        return
    if active:
        stop_wait = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                {*active, stop_wait}, timeout=poll_interval, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not stop_wait.done():
                stop_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_wait
    else:
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except TimeoutError:
            pass


def _collect_finished(active: set[asyncio.Task[None]]) -> None:
    for task in tuple(active):
        if not task.done():
            continue
        active.remove(task)
        # _execute_claimed consumes handler failures itself, but retrieving a result
        # prevents an unexpected task exception from becoming an unobserved warning.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()


def _load_default_handlers() -> None:
    # These imports register explicit worker handlers.  No API module is imported, so
    # this remains a genuine process boundary.
    from clerksan.ingest import embedded_media as _embedded_media  # noqa: F401
    from clerksan.ingest import pipeline as _pipeline  # noqa: F401
    from clerksan.search import indexer as _indexer  # noqa: F401


def _install_stop_signals(stop: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            continue

    def restore() -> None:
        for signum in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)

    return restore


def main() -> None:
    """CLI entry point for ``python -m clerksan.ingest.worker``."""

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
