"""Human review endpoints with extraction-version optimistic locking."""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.api.deps import database_session, settings_from_request
from clerksan.api.intake_actions import plan_exact_source_reprocess
from clerksan.api.schemas import (
    ErrorOut,
    ReviewActivationPreviewOut,
    ReviewBatchActivateIn,
    ReviewBatchActivationOut,
    ReviewBatchDecisionResultOut,
    ReviewBatchDecisionsIn,
    ReviewBatchPageOut,
    ReviewBatchRejectAndReprocessIn,
    ReviewBatchReprocessOut,
    ReviewBatchSummaryOut,
    ReviewCandidateOut,
    ReviewCandidatePageOut,
    ReviewDecisionIn,
    ReviewDecisionRevisionOut,
    ReviewDuplicateEvidenceOut,
    ReviewItemOut,
    ReviewRejectIn,
)
from clerksan.config import Settings
from clerksan.db.models import BatchLifecycle
from clerksan.db.repositories import (
    BatchReviewRequiredError,
    ReviewBatchConflictError,
    ReviewBatchNotFoundError,
    ReviewBatchValidationError,
)
from clerksan.review.queue import (
    activate_batch,
    apply_batch_decisions,
    approve,
    batch_candidates,
    batches,
    lock_batch_reprocess_intake,
    pending,
    preview_batch_activation,
    reject,
    reject_batch_and_reprocess,
)

router = APIRouter(prefix="/review", tags=["review"])


@router.get("", response_model=list[ReviewItemOut])
async def list_pending(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(database_session),
    settings: Settings = Depends(settings_from_request),
) -> list[ReviewItemOut]:
    return [
        ReviewItemOut(**item)
        for item in await pending(
            session,
            limit=limit,
            offset=offset,
            confidence_threshold=settings.confidence_threshold,
        )
    ]


@router.post("/approve", response_model=None)
async def approve_review(
    decision: ReviewDecisionIn, session: AsyncSession = Depends(database_session)
) -> dict[str, str] | JSONResponse:
    try:
        verified_id = await approve(
            session,
            decision.extraction_id,
            expected_version=decision.expected_version,
            corrections=decision.corrections,
            reviewer=decision.reviewer,
        )
    except BatchReviewRequiredError as error:
        return _review_error(409, error.code, str(error), error.detail)
    except (
        ReviewBatchNotFoundError,
        ReviewBatchConflictError,
        ReviewBatchValidationError,
    ) as error:
        return _batch_exception(error)
    return {"verified_id": str(verified_id)}


@router.post("/reject", response_model=None)
async def reject_review(
    body: ReviewRejectIn, session: AsyncSession = Depends(database_session)
) -> dict[str, str] | JSONResponse:
    try:
        await reject(session, body.extraction_id, reason=body.reason, reviewer=body.reviewer)
    except BatchReviewRequiredError as error:
        return _review_error(409, error.code, str(error), error.detail)
    except (
        ReviewBatchNotFoundError,
        ReviewBatchConflictError,
        ReviewBatchValidationError,
    ) as error:
        return _batch_exception(error)
    return {"status": "rejected"}


@router.get(
    "/batches",
    response_model=ReviewBatchPageOut,
    responses={422: {"model": ErrorOut}},
)
async def list_review_batches(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    lifecycle: BatchLifecycle | None = Query(default=None),
    session: AsyncSession = Depends(database_session),
) -> ReviewBatchPageOut:
    rows, total = await batches(
        session,
        limit=limit,
        offset=offset,
        lifecycle=lifecycle,
    )
    return ReviewBatchPageOut(
        items=[ReviewBatchSummaryOut(**asdict(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/batches/{batch_id}/candidates",
    response_model=ReviewCandidatePageOut,
    responses={404: {"model": ErrorOut}},
)
async def list_review_batch_candidates(
    batch_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    exceptions_only: bool = Query(default=False),
    session: AsyncSession = Depends(database_session),
) -> ReviewCandidatePageOut | JSONResponse:
    try:
        page = await batch_candidates(
            session,
            batch_id,
            limit=limit,
            offset=offset,
            exceptions_only=exceptions_only,
        )
    except ReviewBatchNotFoundError as error:
        return _review_error(
            404,
            "review_batch_not_found",
            "Review batch not found.",
            {"batch_id": str(error)},
        )
    return ReviewCandidatePageOut(
        batch_id=page.batch_id,
        batch_version=page.batch_version,
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        items=[_candidate_out(item) for item in page.items],
        source_duplicate_evidence=[
            ReviewDuplicateEvidenceOut(**item) for item in page.source_duplicate_evidence
        ],
    )


@router.get(
    "/batches/{batch_id}/activation-preview",
    response_model=ReviewActivationPreviewOut,
    responses={404: {"model": ErrorOut}},
)
async def get_review_activation_preview(
    batch_id: UUID,
    session: AsyncSession = Depends(database_session),
) -> ReviewActivationPreviewOut | JSONResponse:
    try:
        preview = await preview_batch_activation(session, batch_id)
    except ReviewBatchNotFoundError as error:
        return _review_error(
            404,
            "review_batch_not_found",
            "Review batch not found.",
            {"batch_id": str(error)},
        )
    return ReviewActivationPreviewOut(**asdict(preview))


@router.post(
    "/batches/{batch_id}/decisions",
    response_model=ReviewBatchDecisionResultOut,
    status_code=201,
    responses={
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
async def decide_review_batch(
    batch_id: UUID,
    body: ReviewBatchDecisionsIn,
    session: AsyncSession = Depends(database_session),
) -> ReviewBatchDecisionResultOut | JSONResponse:
    try:
        result = await apply_batch_decisions(
            session,
            batch_id,
            expected_batch_version=body.expected_batch_version,
            decisions=body.decisions,
            actor=body.actor,
        )
    except (
        ReviewBatchNotFoundError,
        ReviewBatchConflictError,
        ReviewBatchValidationError,
    ) as error:
        return _batch_exception(error)
    return ReviewBatchDecisionResultOut(
        batch_id=result.batch_id,
        previous_batch_version=result.previous_batch_version,
        batch_version=result.batch_version,
        lifecycle=result.lifecycle.value,
        decisions=[_decision_out(decision) for decision in result.decisions],
    )


@router.post(
    "/batches/{batch_id}/activate",
    response_model=ReviewBatchActivationOut,
    responses={
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
async def activate_review_batch(
    batch_id: UUID,
    body: ReviewBatchActivateIn,
    session: AsyncSession = Depends(database_session),
) -> ReviewBatchActivationOut | JSONResponse:
    try:
        result = await activate_batch(
            session,
            batch_id,
            expected_batch_version=body.expected_batch_version,
            expected_vector_sha256=body.expected_vector_sha256,
            actor=body.actor,
            accept_exclusions=body.accept_exclusions,
            accept_empty=body.accept_empty,
        )
    except (
        ReviewBatchNotFoundError,
        ReviewBatchConflictError,
        ReviewBatchValidationError,
    ) as error:
        return _batch_exception(error)
    return ReviewBatchActivationOut(
        batch_id=result.batch_id,
        document_id=result.document_id,
        batch_version=result.batch_version,
        lifecycle=result.lifecycle.value,
        activation_vector_sha256=result.activation_vector_sha256,
        included_count=result.included_count,
        excluded_count=result.excluded_count,
        accepted_exclusions=result.accepted_exclusions,
        accepted_empty=result.accepted_empty,
        verified_by_extraction=result.verified_by_extraction,
    )


@router.post(
    "/batches/{batch_id}/reject-and-reprocess",
    response_model=ReviewBatchReprocessOut,
    status_code=202,
    responses={
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
async def reject_and_reprocess_review_batch(
    request: Request,
    batch_id: UUID,
    body: ReviewBatchRejectAndReprocessIn,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> ReviewBatchReprocessOut | JSONResponse:
    try:
        intake = await lock_batch_reprocess_intake(
            session,
            batch_id,
            expected_batch_version=body.expected_batch_version,
        )
        execution = await plan_exact_source_reprocess(request, session, settings, intake)
        result, job_id = await reject_batch_and_reprocess(
            session,
            batch_id,
            expected_batch_version=body.expected_batch_version,
            actor=body.actor,
            reason=body.reason,
            settings=settings,
            capability_registry=execution.registry,
            adapter_key=execution.adapter_key,
            detected_format=execution.detected_format,
            required_components=execution.required_components,
        )
    except (
        ReviewBatchNotFoundError,
        ReviewBatchConflictError,
        ReviewBatchValidationError,
    ) as error:
        return _batch_exception(error)
    return ReviewBatchReprocessOut(
        batch_id=result.batch_id,
        document_id=result.document_id,
        source_intake_id=result.source_intake_id,
        source_file_id=result.source_file_id,
        source_version=result.source_version,
        batch_version=result.batch_version,
        lifecycle=result.lifecycle.value,
        status="queued" if job_id is not None else "already_queued",
        job_id=job_id,
    )


def _decision_out(decision: object) -> ReviewDecisionRevisionOut:
    return ReviewDecisionRevisionOut(
        id=getattr(decision, "id"),
        extraction_id=getattr(decision, "extraction_id"),
        decision_revision=getattr(decision, "decision_revision"),
        action=getattr(decision, "action"),
        expected_extraction_version=getattr(decision, "expected_extraction_version"),
        corrections=getattr(decision, "corrected_payload"),
        corrected_financial_subtype=getattr(decision, "corrected_financial_subtype"),
        exclusion_reason=getattr(decision, "exclusion_reason"),
        actor=getattr(decision, "actor"),
        created_at=getattr(decision, "created_at"),
    )


def _candidate_out(candidate: object) -> ReviewCandidateOut:
    latest = getattr(candidate, "latest_decision")
    return ReviewCandidateOut(
        extraction_id=getattr(candidate, "extraction_id"),
        batch_id=getattr(candidate, "batch_id"),
        candidate_ordinal=getattr(candidate, "candidate_ordinal"),
        candidate_key=getattr(candidate, "candidate_key"),
        row_fingerprint=getattr(candidate, "row_fingerprint"),
        record_kind=getattr(candidate, "record_kind"),
        financial_subtype=getattr(candidate, "financial_subtype"),
        source_locator=getattr(candidate, "source_locator"),
        version=getattr(candidate, "version"),
        status=getattr(candidate, "status").value,
        payload=getattr(candidate, "payload"),
        field_confidences=getattr(candidate, "field_confidences"),
        source_spans=getattr(candidate, "source_spans"),
        validation_issues=list(getattr(candidate, "validation_issues")),
        evidence_group_keys=list(getattr(candidate, "evidence_group_keys")),
        latest_decision=_decision_out(latest) if latest is not None else None,
        duplicate_evidence=[
            ReviewDuplicateEvidenceOut(**item) for item in getattr(candidate, "duplicate_evidence")
        ],
    )


def _batch_exception(
    error: ReviewBatchNotFoundError | ReviewBatchConflictError | ReviewBatchValidationError,
) -> JSONResponse:
    if isinstance(error, ReviewBatchNotFoundError):
        return _review_error(
            404,
            "review_batch_not_found",
            "Review batch not found.",
            {"batch_id": str(error)},
        )
    if isinstance(error, ReviewBatchConflictError):
        return _review_error(409, error.code, str(error), error.detail)
    return _review_error(422, error.code, str(error), error.detail)


def _review_error(
    status_code: int,
    code: str,
    message: str,
    detail: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorOut(
            code=code,
            message=message,
            detail=detail,
        ).model_dump(mode="json"),
    )
