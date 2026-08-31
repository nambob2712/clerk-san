"""A small, durable database queue used exclusively by the worker process.

The queue deliberately has no broker dependency.  PostgreSQL claims work with row
locks and ``SKIP LOCKED``; the local SQLite demo uses one conditional update so two
claimers cannot receive the same row.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import and_, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.config import IntakeMode, Settings, get_settings
from clerksan.db.models import ExecutionProfile, IntakeIntent, Job, JobStatus, SourceIntake
from clerksan.ingest.capabilities import CapabilityRegistry, build_capability_registry

Handler = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]
TerminalLeaseHandler = Callable[[AsyncSession, Job], Awaitable[None]]

_HANDLERS: dict[str, Handler] = {}
_MAX_RETRY_DELAY_SECONDS = 3_600
_MAX_ERROR_LENGTH = 4_000


class UnknownJobTypeError(LookupError):
    """A queued job has no explicitly registered worker handler."""


class JobLeaseLostError(RuntimeError):
    """A worker tried to finish a job after another worker reclaimed its lease."""


class JobStateError(RuntimeError):
    """An operation was attempted while a job was not in the required state."""


def register_handler(job_type: str, handler: Handler) -> None:
    """Register one coroutine handler, rejecting accidental handler replacement."""

    normalized = _clean_nonempty(job_type, "job_type")
    if not callable(handler):
        raise TypeError("handler must be callable")
    existing = _HANDLERS.get(normalized)
    if existing is not None and existing is not handler:
        raise ValueError(f"a handler is already registered for {normalized!r}")
    _HANDLERS[normalized] = handler


def get_handler(job_type: str) -> Handler:
    """Return a registered handler without falling back to arbitrary imports."""

    try:
        return _HANDLERS[job_type]
    except KeyError as error:
        raise UnknownJobTypeError(f"no handler registered for {job_type!r}") from error


def default_worker_id() -> str:
    """Produce a stable-enough lease owner identifier for one worker process."""

    return f"{socket.gethostname()}:{os.getpid()}"


async def enqueue(
    session: AsyncSession,
    *,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    settings: Settings | None = None,
    registry_digest: str | None = None,
    capabilities_digest: str | None = None,
    requirements_digest: str | None = None,
    required_components: tuple[str, ...] | list[str] = (),
    intake_intent: IntakeIntent | str | None = None,
    capability_registry: CapabilityRegistry | None = None,
) -> UUID | None:
    """Queue document-scoped work, returning ``None`` for an existing logical job.

    A document id belongs in the JSON payload rather than a duplicate function
    argument so callers have one serializable handoff object.  It is nevertheless
    validated and copied into the relational ``jobs.document_id`` key here.
    """

    active_registry = capability_registry
    if active_registry is None and settings is not None:
        active_registry = build_capability_registry(settings)
    normalized_type = _clean_nonempty(job_type, "job_type")
    normalized_key = _clean_nonempty(idempotency_key, "idempotency_key")
    default_registry_digest = (
        active_registry.registry_digest if active_registry is not None else None
    )
    default_capabilities_digest = (
        active_registry.capabilities_digest if active_registry is not None else None
    )
    normalized_registry_digest = _validate_optional_digest(
        registry_digest if registry_digest is not None else default_registry_digest,
        "registry_digest",
    )
    normalized_capabilities_digest = _validate_optional_digest(
        capabilities_digest if capabilities_digest is not None else default_capabilities_digest,
        "capabilities_digest",
    )
    universal = bool(settings is not None and settings.intake_mode is IntakeMode.UNIVERSAL)
    if universal:
        if active_registry is None or not active_registry.sandbox_verified:
            raise ValueError("universal jobs require a verified capability registry")
        if (
            normalized_registry_digest != active_registry.registry_digest
            or normalized_capabilities_digest != active_registry.capabilities_digest
        ):
            raise ValueError("universal job evidence must match the active registry")
    normalized_components = _normalize_required_components(required_components)
    canonical_requirements_digest = _required_components_digest(normalized_components)
    normalized_requirements_digest = _validate_optional_digest(
        requirements_digest,
        "requirements_digest",
    )
    if (
        normalized_requirements_digest is not None
        and normalized_requirements_digest != canonical_requirements_digest
    ):
        raise ValueError("requirements_digest must match canonical required_components")
    document_id, stored_payload = _job_document_payload(payload)
    normalized_intake_intent, source_intake = await _resolve_intake_intent(
        session,
        document_id=document_id,
        payload=stored_payload,
        requested=intake_intent,
    )
    if (
        source_intake is not None
        and source_intake.execution_profile is ExecutionProfile.UNIVERSAL_SANDBOXED
        and not universal
    ):
        raise ValueError("a sandboxed source intake cannot use legacy execution")
    # The relational evidence is authoritative. Persisting the same canonical
    # value in the handoff payload keeps downstream jobs inspectable, while the
    # worker still overwrites it from ``jobs.intake_intent`` before dispatch.
    stored_payload["intake_intent"] = normalized_intake_intent.value
    job = Job(
        document_id=document_id,
        job_type=normalized_type,
        payload=stored_payload,
        idempotency_key=normalized_key,
        execution_profile=(
            ExecutionProfile.UNIVERSAL_SANDBOXED if universal else ExecutionProfile.LEGACY_COMPAT
        ),
        sandbox_verified=universal,
        registry_digest=normalized_registry_digest,
        capabilities_digest=normalized_capabilities_digest,
        requirements_digest=normalized_requirements_digest or canonical_requirements_digest,
        required_components=normalized_components,
        intake_intent=normalized_intake_intent,
        status=JobStatus.QUEUED,
    )

    # A savepoint makes a uniqueness conflict local to this enqueue attempt; callers
    # may still be in the same transaction that created the document row. SQLite
    # needs prior DML so releasing this savepoint cannot publish an outer transaction.
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(text("UPDATE jobs SET updated_at = updated_at WHERE 0"))
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError:
        return None
    return job.id


async def claim_next(
    session: AsyncSession,
    *,
    lease_owner: str | None = None,
    settings: Settings | None = None,
    on_terminal_lease: TerminalLeaseHandler | None = None,
    capability_registry: CapabilityRegistry | None = None,
    models_ready: bool = True,
) -> dict[str, Any] | None:
    """Atomically lease one runnable job, reclaiming only expired work.

    PostgreSQL uses ``FOR UPDATE SKIP LOCKED`` so independent worker processes do not
    wait behind one another.  SQLite lacks that primitive; one conditional
    ``UPDATE .. RETURNING`` gives the demo the same no-double-claim property.
    """

    active_settings = settings or get_settings()
    active_registry = capability_registry or build_capability_registry(active_settings)
    owner = _clean_nonempty(lease_owner or default_worker_id(), "lease_owner")
    now = _utcnow()
    scope = _execution_scope(active_settings, active_registry, models_ready=models_ready)
    expired_final_jobs = await _bury_exhausted_expired_jobs(session, now, active_settings, scope)
    if on_terminal_lease is not None:
        for job in expired_final_jobs:
            await on_terminal_lease(session, job)
    eligible = and_(_eligible_clause(now, active_settings), scope)

    if session.get_bind().dialect.name == "sqlite":
        job = await _claim_sqlite(session, eligible, now, owner, active_settings)
    else:
        job = await _claim_postgresql(session, eligible, now, owner, active_settings)
    if job is None:
        return None
    if "updated_at" in inspect(job).expired_attributes:
        # Server-generated update timestamps require explicit async I/O before
        # synchronous serialization; implicit attribute loading raises MissingGreenlet.
        await session.refresh(job, attribute_names=["updated_at"])
    return _job_to_dict(job)


async def renew_lease(
    session: AsyncSession,
    job_id: UUID,
    *,
    lease_owner: str | None = None,
    settings: Settings | None = None,
) -> None:
    """Extend a running job's lease while preserving its current attempt count."""

    active_settings = settings or get_settings()
    job = await _locked_job(session, job_id)
    _require_running_lease(job, lease_owner)
    job.lease_expires_at = _utcnow() + dt.timedelta(seconds=active_settings.job_lease_seconds)
    await session.flush()


async def mark_done(session: AsyncSession, job_id: UUID, *, lease_owner: str | None = None) -> None:
    """Mark a successfully handled job terminal without touching its payload."""

    job = await _locked_job(session, job_id)
    _require_running_lease(job, lease_owner)
    job.status = JobStatus.DONE
    job.lease_expires_at = None
    job.lease_owner = None
    job.last_error = None
    await session.flush()


async def retry_or_bury(
    session: AsyncSession,
    job_id: UUID,
    error: str,
    *,
    lease_owner: str | None = None,
    settings: Settings | None = None,
) -> None:
    """Retry a failed running job with exponential backoff, or mark it dead."""

    active_settings = settings or get_settings()
    job = await _locked_job(session, job_id)
    _require_running_lease(job, lease_owner)
    job.last_error = _clean_error(error)
    job.lease_expires_at = None
    job.lease_owner = None

    if job.attempts >= active_settings.job_max_attempts:
        job.status = JobStatus.DEAD
    else:
        delay = _retry_delay(job.attempts, active_settings.job_retry_base_seconds)
        job.status = JobStatus.QUEUED
        job.available_at = _utcnow() + dt.timedelta(seconds=delay)
    await session.flush()


async def _claim_postgresql(
    session: AsyncSession,
    eligible: Any,
    now: dt.datetime,
    owner: str,
    settings: Settings,
) -> Job | None:
    statement = (
        select(Job)
        .where(eligible)
        .order_by(Job.available_at.asc(), Job.created_at.asc(), Job.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = await session.scalar(statement)
    if job is None:
        return None
    _lease(job, now, owner, settings)
    await session.flush()
    return job


async def _claim_sqlite(
    session: AsyncSession,
    eligible: Any,
    now: dt.datetime,
    owner: str,
    settings: Settings,
) -> Job | None:
    """Use a conditional write because SQLite has no row-level ``SKIP LOCKED``."""

    candidate = (
        select(Job.id)
        .where(eligible)
        .order_by(Job.available_at.asc(), Job.created_at.asc(), Job.id.asc())
        .limit(1)
        .scalar_subquery()
    )
    result = await session.execute(
        update(Job)
        .where(Job.id == candidate, eligible)
        .values(
            status=JobStatus.RUNNING,
            attempts=Job.attempts + 1,
            lease_owner=owner,
            lease_expires_at=now + dt.timedelta(seconds=settings.job_lease_seconds),
        )
        .returning(Job)
    )
    return result.scalar_one_or_none()


async def _bury_exhausted_expired_jobs(
    session: AsyncSession, now: dt.datetime, settings: Settings, scope: Any
) -> list[Job]:
    """Finalize expired final attempts and return only incomplete process work.

    A handler can durably record its stage completion just before a hard process kill.
    That work must become ``done`` rather than a false terminal failure.  The atomic
    update claims all other expired final attempts exactly once, allowing the worker
    to expose their document-level recovery state in the same transaction.
    """

    result = await session.execute(
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING,
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at <= now,
            Job.attempts >= settings.job_max_attempts,
            scope,
        )
        .values(
            status=JobStatus.DEAD,
            lease_expires_at=None,
            lease_owner=None,
            last_error="lease expired after final attempt",
        )
        .returning(Job)
    )
    expired_jobs = list(result.scalars())
    terminal_failures: list[Job] = []
    for job in expired_jobs:
        if _job_pipeline_completed(job.payload):
            job.status = JobStatus.DONE
            job.last_error = None
        else:
            terminal_failures.append(job)
    await session.flush()
    return terminal_failures


def _eligible_clause(now: dt.datetime, settings: Settings) -> Any:
    return or_(
        and_(Job.status == JobStatus.QUEUED, Job.available_at <= now),
        and_(
            Job.status == JobStatus.RUNNING,
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at <= now,
            Job.attempts < settings.job_max_attempts,
        ),
    )


def _execution_scope(
    settings: Settings,
    registry: CapabilityRegistry,
    *,
    models_ready: bool,
) -> Any:
    if settings.intake_mode is IntakeMode.UNIVERSAL:
        clauses = [
            Job.execution_profile == ExecutionProfile.UNIVERSAL_SANDBOXED,
            Job.sandbox_verified.is_(True),
            Job.registry_digest == registry.registry_digest,
            Job.capabilities_digest == registry.capabilities_digest,
        ]
    else:
        clauses = [
            Job.execution_profile == ExecutionProfile.LEGACY_COMPAT,
            Job.sandbox_verified.is_(False),
        ]
    if not models_ready:
        clauses.append(Job.requirements_digest == _required_components_digest([]))
    return and_(*clauses)


def _lease(job: Job, now: dt.datetime, owner: str, settings: Settings) -> None:
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.lease_owner = owner
    job.lease_expires_at = now + dt.timedelta(seconds=settings.job_lease_seconds)


async def _locked_job(session: AsyncSession, job_id: UUID) -> Job:
    job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise LookupError(f"job {job_id} does not exist")
    return job


def _require_running_lease(job: Job, lease_owner: str | None) -> None:
    if job.status is not JobStatus.RUNNING:
        raise JobStateError(f"job {job.id} is not running")
    if lease_owner is not None and job.lease_owner != lease_owner:
        raise JobLeaseLostError(f"job {job.id} is now leased by another worker")


def _job_document_payload(payload: Mapping[str, Any]) -> tuple[UUID, dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a JSON object")
    stored = dict(payload)
    document_value = stored.get("document_id")
    try:
        document_id = (
            document_value if isinstance(document_value, UUID) else UUID(str(document_value))
        )
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("payload.document_id must be a UUID") from error
    stored["document_id"] = str(document_id)
    try:
        json.dumps(stored, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise TypeError("payload must contain only JSON-safe values") from error
    return document_id, stored


async def _resolve_intake_intent(
    session: AsyncSession,
    *,
    document_id: UUID,
    payload: dict[str, Any],
    requested: IntakeIntent | str | None,
) -> tuple[IntakeIntent, SourceIntake | None]:
    """Bind a source-scoped job to the intake's immutable relational evidence.

    An API or retry payload can be stale or tampered with. When it identifies an
    existing source intake, the stored intent wins and an explicit disagreement is
    rejected. Jobs without an exact source continue to use the legacy default.
    """

    requested_intent = _normalize_optional_intake_intent(requested)
    source_intake = await _source_intake_for_payload(
        session,
        document_id=document_id,
        payload=payload,
    )
    if source_intake is None:
        return requested_intent or IntakeIntent.LEGACY_UNSPECIFIED, None
    persisted_intent = source_intake.intake_intent
    if requested_intent is not None and requested_intent != persisted_intent:
        raise ValueError("intake_intent must match the persisted source intake")
    return persisted_intent, source_intake


async def _source_intake_for_payload(
    session: AsyncSession,
    *,
    document_id: UUID,
    payload: dict[str, Any],
) -> SourceIntake | None:
    """Resolve a supplied source identity and canonicalize it before persistence.

    Older legitimate jobs may carry no source identity. Once a caller supplies
    any identity component, it must resolve to exactly one intake for that
    document; the complete immutable identity is then written into the job
    payload so the worker never receives a partial or internally mismatched
    source reference.
    """

    source_intake_id = _provided_payload_uuid(payload, "source_intake_id")
    source_file_id = _provided_payload_uuid(payload, "source_file_id")
    source_version = _provided_payload_source_version(payload, "source_version")
    if source_intake_id is None and source_file_id is None and source_version is None:
        return None

    statement = select(SourceIntake).where(SourceIntake.document_id == document_id)
    if source_intake_id is not None:
        statement = statement.where(SourceIntake.id == source_intake_id)
    if source_file_id is not None:
        statement = statement.where(SourceIntake.source_file_id == source_file_id)
    if source_version is not None:
        statement = statement.where(SourceIntake.source_version == source_version)
    matches = (await session.scalars(statement.limit(2))).all()
    if len(matches) != 1:
        raise ValueError(
            "payload source identity must resolve to exactly one persisted source intake"
        )
    intake = matches[0]
    payload.update(
        {
            "source_intake_id": str(intake.id),
            "source_file_id": str(intake.source_file_id),
            "source_version": intake.source_version,
        }
    )
    return intake


def _normalize_optional_intake_intent(value: IntakeIntent | str | None) -> IntakeIntent | None:
    if value is None:
        return None
    if isinstance(value, IntakeIntent):
        return value
    try:
        return IntakeIntent(value)
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported intake_intent") from error


def _payload_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _payload_source_version(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _provided_payload_uuid(payload: Mapping[str, Any], field_name: str) -> UUID | None:
    if field_name not in payload:
        return None
    value = _payload_uuid(payload[field_name])
    if value is None:
        raise ValueError(f"payload.{field_name} must be a UUID")
    return value


def _provided_payload_source_version(payload: Mapping[str, Any], field_name: str) -> int | None:
    if field_name not in payload:
        return None
    value = _payload_source_version(payload[field_name])
    if value is None:
        raise ValueError(f"payload.{field_name} must be a positive integer")
    return value


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "document_id": job.document_id,
        "job_type": job.job_type,
        "payload": dict(job.payload),
        "idempotency_key": job.idempotency_key,
        "execution_profile": job.execution_profile.value,
        "sandbox_verified": job.sandbox_verified,
        "registry_digest": job.registry_digest,
        "capabilities_digest": job.capabilities_digest,
        "requirements_digest": job.requirements_digest,
        "required_components": list(job.required_components),
        "intake_intent": job.intake_intent.value,
        "status": job.status.value,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "available_at": job.available_at,
        "lease_expires_at": job.lease_expires_at,
        "lease_owner": job.lease_owner,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _job_pipeline_completed(payload: dict[str, Any]) -> bool:
    pipeline = payload.get("_pipeline")
    return isinstance(pipeline, dict) and pipeline.get("completed") is True


def _clean_nonempty(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _validate_optional_digest(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if len(cleaned) != 64 or any(character not in "0123456789abcdef" for character in cleaned):
        raise ValueError(f"{field_name} must be a 64-character lowercase hexadecimal digest")
    return cleaned


def _normalize_required_components(
    values: tuple[str, ...] | list[str],
) -> list[str]:
    if not isinstance(values, (tuple, list)):
        raise TypeError("required_components must be a sequence")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("required_components must contain strings")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("required_components must not contain blanks")
        normalized.append(cleaned)
    if len(normalized) != len(set(normalized)):
        raise ValueError("required_components must not contain duplicates")
    return sorted(normalized)


def _required_components_digest(values: list[str]) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_error(error: str) -> str:
    if not isinstance(error, str):
        error = repr(error)
    cleaned = error.strip() or "handler failed without an error message"
    return cleaned[:_MAX_ERROR_LENGTH]


def _retry_delay(attempts: int, base_seconds: int) -> int:
    exponent = max(attempts - 1, 0)
    return min(base_seconds * (2**exponent), _MAX_RETRY_DELAY_SECONDS)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
