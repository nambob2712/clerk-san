"""Database-backed universal activation and lease-drain evidence."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.config import IntakeMode, Settings
from clerksan.db.models import (
    ExecutionProfile,
    Job,
    JobStatus,
    WorkerCapabilityLease,
)
from clerksan.ingest.capabilities import CapabilityRegistry
from clerksan.ingest.policy import PublicReasonCode


@dataclass(frozen=True, slots=True)
class UniversalActivationStatus:
    ready: bool
    reason_code: PublicReasonCode | None
    lease: WorkerCapabilityLease | None
    legacy_jobs_blocking: bool


async def evaluate_universal_activation(
    session: AsyncSession,
    settings: Settings,
    registry: CapabilityRegistry,
    *,
    now: dt.datetime | None = None,
) -> UniversalActivationStatus:
    """Require one fresh exact worker lease and a completely drained legacy queue."""

    if settings.intake_mode is not IntakeMode.UNIVERSAL:
        return UniversalActivationStatus(True, None, None, False)
    if not registry.sandbox_verified or not registry.process:
        return UniversalActivationStatus(False, PublicReasonCode.SANDBOX_UNAVAILABLE, None, False)

    legacy_job = await session.scalar(
        select(Job.id)
        .where(
            Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            or_(
                Job.execution_profile.is_(None),
                Job.execution_profile != ExecutionProfile.UNIVERSAL_SANDBOXED,
                Job.sandbox_verified.is_(False),
            ),
        )
        .limit(1)
    )
    if legacy_job is not None:
        return UniversalActivationStatus(False, PublicReasonCode.REGISTRY_MISMATCH, None, True)

    lease = await session.scalar(
        select(WorkerCapabilityLease)
        .order_by(
            WorkerCapabilityLease.heartbeat_at.desc(),
            WorkerCapabilityLease.worker_id.asc(),
        )
        .limit(1)
    )
    if lease is None:
        return UniversalActivationStatus(
            False, PublicReasonCode.WORKER_CAPABILITY_STALE, None, False
        )
    current = now or dt.datetime.now(dt.UTC)
    expires_at = _aware(lease.expires_at)
    if expires_at <= current:
        return UniversalActivationStatus(
            False, PublicReasonCode.WORKER_CAPABILITY_STALE, lease, False
        )
    if (
        not lease.sandbox_verified
        or lease.registry_digest != registry.registry_digest
        or lease.capabilities_digest != registry.capabilities_digest
    ):
        return UniversalActivationStatus(False, PublicReasonCode.REGISTRY_MISMATCH, lease, False)
    return UniversalActivationStatus(True, None, lease, False)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
