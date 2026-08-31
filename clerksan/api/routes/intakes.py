"""Exact-source intake status endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.api.deps import database_session, settings_from_request
from clerksan.api.intake_actions import plan_exact_source_reprocess
from clerksan.api.schemas import (
    ErrorOut,
    IntakeJobReference,
    ReprocessAccepted,
    SourceIntakeActionIn,
    SourceIntakeDetail,
)
from clerksan.config import Settings
from clerksan.db.models import Job, SourceIntake
from clerksan.db.repositories import DocumentRepo, SourceIntakeRepo, StaleSourceIntakeError
from clerksan.ingest.jobs import enqueue

router = APIRouter(tags=["intakes"])


def _not_found(intake_id: UUID) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content=ErrorOut(
            code="source_intake_not_found",
            message="Source intake not found",
            detail={"intake_id": str(intake_id)},
        ).model_dump(mode="json"),
    )


async def _latest_job(session: AsyncSession, intake: SourceIntake) -> Job | None:
    """Find the newest job whose payload names this exact intake.

    The payload binding is intentionally checked in Python: the same query works on
    SQLite and PostgreSQL without relying on backend-specific JSON operators.
    """

    jobs = (
        await session.scalars(
            select(Job)
            .where(Job.document_id == intake.document_id)
            .order_by(Job.created_at.desc(), Job.id.desc())
        )
    ).all()
    intake_id = str(intake.id)
    for job in jobs:
        if str(job.payload.get("source_intake_id", "")) == intake_id:
            return job
    return None


async def _detail(session: AsyncSession, intake: SourceIntake) -> SourceIntakeDetail:
    job = await _latest_job(session, intake)
    return SourceIntakeDetail(
        intake_id=intake.id,
        document_id=intake.document_id,
        source_file_id=intake.source_file_id,
        source_version=intake.source_version,
        source_sha256=intake.source_sha256,
        upload_idempotency_key=intake.upload_idempotency_key,
        intake_intent=intake.intake_intent.value,
        detected_format=intake.detected_format,
        state=intake.state.value,
        reason_code=intake.reason_code,
        retryable=intake.retryable,
        failure_phase=intake.failure_phase,
        version=intake.version,
        job_reference=(
            IntakeJobReference(
                job_id=job.id,
                job_type=job.job_type,
                status=job.status.value,
            )
            if job is not None
            else None
        ),
    )


@router.get("/intakes/{intake_id}", response_model=SourceIntakeDetail)
async def get_intake(
    intake_id: UUID,
    session: AsyncSession = Depends(database_session),
) -> SourceIntakeDetail | JSONResponse:
    """Return one exact intake projection; harmless polling never conflicts."""

    intake = await SourceIntakeRepo(session).get(intake_id)
    if intake is None:
        return _not_found(intake_id)
    return await _detail(session, intake)


@router.get("/intakes", response_model=list[SourceIntakeDetail])
async def list_recent_intakes(
    limit: int | None = Query(default=None, ge=1),
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> list[SourceIntakeDetail]:
    """Return a bounded newest-first rehydration list for the local upload queue."""

    resolved_limit = limit or settings.recent_intakes_default_limit
    if resolved_limit > settings.recent_intakes_max_limit:
        resolved_limit = settings.recent_intakes_max_limit
    return [
        await _detail(session, intake)
        for intake in await SourceIntakeRepo(session).recent(limit=resolved_limit)
    ]


async def _reprocess(
    request: Request,
    intake_id: UUID,
    action: SourceIntakeActionIn,
    settings: Settings,
    session: AsyncSession,
) -> ReprocessAccepted | JSONResponse:
    repo = SourceIntakeRepo(session)
    intake = await repo.get(intake_id)
    if intake is None:
        return _not_found(intake_id)
    if intake.version != action.expected_version:
        return JSONResponse(
            status_code=409,
            content={
                "code": "source_intake_stale",
                "message": "Source intake changed; refresh before retrying.",
                "detail": (await _detail(session, intake)).model_dump(mode="json"),
            },
        )
    documents = DocumentRepo(session)
    try:
        intake = await documents.lock_current_reprocess_intake(
            intake.document_id,
            expected_intake_id=intake.id,
            expected_intake_version=action.expected_version,
        )
    except StaleSourceIntakeError:
        current = await repo.get(intake_id)
        if current is None:
            return _not_found(intake_id)
        return JSONResponse(
            status_code=409,
            content={
                "code": "source_intake_stale",
                "message": "Source intake is no longer the current source.",
                "detail": (await _detail(session, current)).model_dump(mode="json"),
            },
        )
    execution = await plan_exact_source_reprocess(
        request,
        session,
        settings,
        intake,
    )
    try:
        target = await documents.prepare_reprocess(intake.document_id, actor=action.actor.strip())
    except StaleSourceIntakeError:
        current = await repo.get(intake_id)
        if current is None:
            return _not_found(intake_id)
        return JSONResponse(
            status_code=409,
            content={
                "code": "source_intake_stale",
                "message": "Source intake changed; refresh before retrying.",
                "detail": (await _detail(session, current)).model_dump(mode="json"),
            },
        )
    detail = await documents.get(intake.document_id)
    source = next(
        item
        for item in detail["files"]
        if item["kind"] == "original" and item["version"] == target.original_version
    )
    current = await repo.get_for_source(intake.document_id, source["id"], target.original_version)
    if current is None:
        raise RuntimeError("reprocess target has no exact source intake")
    job_id = await enqueue(
        session,
        job_type="process_document",
        payload={
            "document_id": str(intake.document_id),
            "source_file_id": str(source["id"]),
            "source_intake_id": str(current.id),
            "source_version": target.original_version,
            **(
                {
                    "detected_format": execution.detected_format,
                    "adapter_key": execution.adapter_key,
                }
                if execution.detected_format is not None
                else {}
            ),
        },
        idempotency_key=target.idempotency_key,
        settings=settings,
        registry_digest=execution.registry.registry_digest,
        capabilities_digest=execution.registry.capabilities_digest,
        required_components=execution.required_components,
        intake_intent=current.intake_intent,
        capability_registry=execution.registry,
    )
    return ReprocessAccepted(
        document_id=intake.document_id,
        original_version=target.original_version,
        status="queued" if job_id is not None else "already_queued",
        job_id=job_id,
    )


@router.post("/intakes/{intake_id}/reprocess", status_code=202, response_model=ReprocessAccepted)
async def reprocess_intake(
    request: Request,
    intake_id: UUID,
    action: SourceIntakeActionIn,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> ReprocessAccepted | JSONResponse:
    return await _reprocess(request, intake_id, action, settings, session)


@router.post("/intakes/{intake_id}/retry", status_code=202, response_model=ReprocessAccepted)
async def retry_intake(
    request: Request,
    intake_id: UUID,
    action: SourceIntakeActionIn,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> ReprocessAccepted | JSONResponse:
    return await _reprocess(request, intake_id, action, settings, session)
