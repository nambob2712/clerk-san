"""Universal review queue for immutable extraction payloads."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.bills.service import BillValidationError, record_verified_bill
from clerksan.config import DEFAULT_CONFIDENCE_THRESHOLD, Settings
from clerksan.db.models import (
    Document,
    DocumentClass,
    DuplicateFlag,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    RecordKind,
    SourceIntake,
    SourceIntakeState,
)
from clerksan.db.repositories import (
    BatchReviewRequiredError,
    ExtractionRepo,
    ReviewActivationPreview,
    ReviewActivationResult,
    ReviewBatchConflictError,
    ReviewBatchRejectionResult,
    ReviewBatchRepo,
    ReviewBatchSummarySnapshot,
    ReviewCandidatePage,
    ReviewDecisionBatchResult,
    ReviewDecisionDraft,
    ReviewValidationError,
    SourceIntakeRepo,
    StaleExtractionError,
    VerifiedRepo,
    _ensure_sqlite_outer_write_transaction,
)
from clerksan.extract.recurring import (
    RecurringBillNormalizationError,
    bill_correction_fields,
    bill_projection_correction_fields,
    normalize_recurring_bill_payload,
)
from clerksan.ingest.capabilities import CapabilityRegistry
from clerksan.ingest.jobs import enqueue


async def pending(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    confidence_threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Return one stable server-ordered page of pending extractions."""

    _validate_page(limit, offset)
    threshold = (
        confidence_threshold if confidence_threshold is not None else DEFAULT_CONFIDENCE_THRESHOLD
    )
    if not 0 <= threshold <= 1:
        raise ValueError("confidence_threshold must be between zero and one")
    rows = list(
        (
            await session.execute(
                select(ExtractedRecord, Document, ExtractionBatch)
                .join(Document, ExtractedRecord.document_id == Document.id)
                .outerjoin(ExtractionBatch, ExtractedRecord.batch_id == ExtractionBatch.id)
                .where(ExtractedRecord.status == ExtractionStatus.PENDING_REVIEW)
                .order_by(
                    case((ExtractedRecord.validation_issues == [], 1), else_=0).asc(),
                    case((Document.document_class == DocumentClass.OTHER, 0), else_=1).asc(),
                    ExtractedRecord.created_at.asc(),
                    ExtractedRecord.id.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    if not rows:
        return []
    record_ids = [record.id for record, _, _ in rows]
    document_ids = [record.document_id for record, _, _ in rows]
    source_pairs = sorted(
        {(record.source_file_id, record.source_version) for record, _, _ in rows},
        key=lambda item: (str(item[0]), item[1]),
    )
    flags = list(
        (
            await session.scalars(
                select(DuplicateFlag)
                .where(
                    DuplicateFlag.document_id.in_(document_ids),
                    or_(
                        and_(
                            DuplicateFlag.batch_id.is_(None),
                            DuplicateFlag.source_file_id.is_(None),
                        ),
                        and_(
                            DuplicateFlag.batch_id.is_(None),
                            tuple_(
                                DuplicateFlag.source_file_id,
                                DuplicateFlag.source_version,
                            ).in_(source_pairs),
                        ),
                        DuplicateFlag.extraction_id.in_(record_ids),
                    ),
                )
                .order_by(
                    DuplicateFlag.document_id.asc(),
                    DuplicateFlag.extraction_id.asc(),
                    DuplicateFlag.score.desc(),
                    DuplicateFlag.created_at.asc(),
                    DuplicateFlag.id.asc(),
                )
            )
        ).all()
    )
    source_flags: dict[UUID, list[DuplicateFlag]] = {}
    candidate_flags: dict[UUID, list[DuplicateFlag]] = {}
    for flag in flags:
        if flag.extraction_id is not None:
            candidate_flags.setdefault(flag.extraction_id, []).append(flag)
        else:
            source_flags.setdefault(flag.document_id, []).append(flag)
    items = [
        _review_item(
            record,
            document,
            batch,
            threshold,
            (
                *(
                    flag
                    for flag in source_flags.get(record.document_id, ())
                    if flag.source_file_id is None
                    or (
                        flag.source_file_id == record.source_file_id
                        and flag.source_version == record.source_version
                    )
                ),
                *candidate_flags.get(record.id, ()),
            ),
        )
        for record, document, batch in rows
    ]
    return items


async def batches(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    lifecycle: str | None = None,
) -> tuple[list[ReviewBatchSummarySnapshot], int]:
    """Return one stable page of batch-level review work."""

    return await ReviewBatchRepo(session).list_batches(
        limit=limit,
        offset=offset,
        lifecycle=lifecycle,
    )


async def batch_candidates(
    session: AsyncSession,
    batch_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
    exceptions_only: bool = False,
) -> ReviewCandidatePage:
    """Return one bounded candidate page with latest decisions and scoped evidence."""

    return await ReviewBatchRepo(session).list_candidates(
        batch_id,
        limit=limit,
        offset=offset,
        exceptions_only=exceptions_only,
    )


async def apply_batch_decisions(
    session: AsyncSession,
    batch_id: UUID,
    *,
    expected_batch_version: int,
    decisions: Sequence[ReviewDecisionDraft | Any],
    actor: str,
) -> ReviewDecisionBatchResult:
    """Append one all-or-none bounded decision request."""

    return await ReviewBatchRepo(session).apply_decisions(
        batch_id,
        expected_batch_version,
        decisions,
        actor,
    )


async def preview_batch_activation(
    session: AsyncSession,
    batch_id: UUID,
) -> ReviewActivationPreview:
    return await ReviewBatchRepo(session).activation_preview(batch_id)


async def lock_batch_reprocess_intake(
    session: AsyncSession,
    batch_id: UUID,
    *,
    expected_batch_version: int,
) -> SourceIntake:
    """Lock the exact current source before checking runtime reprocess capability."""

    return await ReviewBatchRepo(session).lock_reprocess_intake(
        batch_id,
        expected_batch_version=expected_batch_version,
    )


async def activate_batch(
    session: AsyncSession,
    batch_id: UUID,
    *,
    expected_batch_version: int,
    expected_vector_sha256: str,
    actor: str,
    accept_exclusions: bool = False,
    accept_empty: bool = False,
) -> ReviewActivationResult:
    return await ReviewBatchRepo(session).activate(
        batch_id,
        expected_batch_version,
        expected_vector_sha256,
        actor,
        accept_exclusions=accept_exclusions,
        accept_empty=accept_empty,
    )


async def reject_batch_and_reprocess(
    session: AsyncSession,
    batch_id: UUID,
    *,
    expected_batch_version: int,
    actor: str,
    reason: str,
    settings: Settings,
    capability_registry: CapabilityRegistry,
    adapter_key: str | None,
    detected_format: str | None,
    required_components: Sequence[str],
) -> tuple[ReviewBatchRejectionResult, UUID | None]:
    """Reject a cohort and queue its exact preserved source in one transaction."""

    await _ensure_sqlite_outer_write_transaction(session)
    async with session.begin_nested():
        result = await ReviewBatchRepo(session).reject_batch(
            batch_id,
            expected_batch_version=expected_batch_version,
            actor=actor,
            reason=reason,
        )
        intake = await session.scalar(
            select(SourceIntake)
            .where(SourceIntake.id == result.source_intake_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            intake is None
            or intake.document_id != result.document_id
            or intake.source_file_id != result.source_file_id
            or intake.source_version != result.source_version
            or intake.state is not SourceIntakeState.PROCESSED
        ):
            raise ReviewBatchConflictError(
                "The exact source intake changed before reprocessing could be queued.",
                detail={
                    "batch_id": str(result.batch_id),
                    "expected_version": expected_batch_version,
                    "current_version": result.batch_version,
                    "affected_extraction_ids": [],
                },
            )
        await SourceIntakeRepo(session).transition(
            intake.id,
            expected_version=intake.version,
            state=SourceIntakeState.QUEUED,
            actor=actor,
            reason_code="review_batch_reprocess_queued",
            retryable=False,
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={
                "document_id": str(result.document_id),
                "source_file_id": str(result.source_file_id),
                "source_intake_id": str(result.source_intake_id),
                "source_version": result.source_version,
                **(
                    {
                        "detected_format": detected_format,
                        "adapter_key": adapter_key,
                    }
                    if detected_format is not None
                    else {}
                ),
            },
            idempotency_key=(f"review-batch-reprocess:{result.batch_id}:{result.batch_version}"),
            settings=settings,
            registry_digest=capability_registry.registry_digest,
            capabilities_digest=capability_registry.capabilities_digest,
            required_components=tuple(required_components),
            intake_intent=intake.intake_intent,
            capability_registry=capability_registry,
        )
    return result, job_id


async def approve(
    session: AsyncSession,
    extraction_id: UUID,
    *,
    expected_version: int,
    corrections: dict[str, Any],
    reviewer: str,
) -> UUID:
    """Promote exactly the extraction version that the reviewer saw."""

    record = await session.scalar(
        select(ExtractedRecord).where(ExtractedRecord.id == extraction_id)
    )
    if record is not None and record.batch_id is not None:
        return await _approve_singleton_batch(
            session,
            record,
            expected_version=expected_version,
            corrections=corrections,
            reviewer=reviewer,
        )
    bill_projection = None
    bill_review_corrections: dict[str, bool] = {}
    if record is not None:
        document = await session.get(Document, record.document_id)
        if document is not None and document.document_class is DocumentClass.RECURRING_BILL:
            bill_corrections = {
                field: value
                for field, value in corrections.items()
                if field in bill_correction_fields()
            }
            try:
                bill_projection = normalize_recurring_bill_payload(record.payload, bill_corrections)
            except RecurringBillNormalizationError as error:
                raise BillValidationError(str(error)) from error
            try:
                source_projection = normalize_recurring_bill_payload(record.payload)
            except RecurringBillNormalizationError:
                # A human can supply missing recurring-bill fields. The original
                # payload cannot be compared when it is incomplete, so audit only
                # values that differ from what the model actually returned.
                bill_review_corrections = {
                    field: True
                    for field, value in bill_corrections.items()
                    if _raw_bill_value(record.payload, field) != value
                }
            else:
                bill_review_corrections = _changed_bill_fields(
                    source_projection, bill_projection, bill_corrections
                )
        elif set(corrections).intersection(bill_projection_correction_fields()):
            raise ReviewValidationError(
                "recurring-bill corrections require a recurring-bill document"
            )
    verified_id = await VerifiedRepo(session).promote(
        extraction_id,
        expected_version,
        corrections={
            field: value
            for field, value in corrections.items()
            if field not in bill_projection_correction_fields()
        },
        reviewer=reviewer,
    )
    if bill_projection is not None:
        await record_verified_bill(
            session,
            verified_record_id=verified_id,
            issuer_name=bill_projection.issuer_name,
            issuer_kind=bill_projection.issuer_kind,
            billing_period=bill_projection.billing_period,
            due_date=bill_projection.due_date,
            consumption_value=bill_projection.consumption_value,
            consumption_unit=bill_projection.consumption_unit,
            reviewer=reviewer,
            review_corrections=bill_review_corrections,
        )
    return verified_id


async def reject(session: AsyncSession, extraction_id: UUID, *, reason: str, reviewer: str) -> None:
    """Reject an extraction while retaining its immutable source and payload."""

    record = await session.scalar(
        select(ExtractedRecord).where(ExtractedRecord.id == extraction_id)
    )
    if record is not None and record.batch_id is not None:
        page = await ReviewBatchRepo(session).list_candidates(record.batch_id, limit=2)
        if (
            page.total != 1
            or not page.items
            or page.items[0].record_kind is not RecordKind.FINANCIAL
        ):
            raise BatchReviewRequiredError(
                record.batch_id,
                "Legacy rejection supports only one financial candidate; use batch review.",
            )
        await ReviewBatchRepo(session).reject_batch(
            record.batch_id,
            expected_batch_version=page.batch_version,
            actor=reviewer,
            reason=reason,
        )
        return
    await ExtractionRepo(session).reject(extraction_id, reason=reason, reviewer=reviewer)


async def _approve_singleton_batch(
    session: AsyncSession,
    record: ExtractedRecord,
    *,
    expected_version: int,
    corrections: dict[str, Any],
    reviewer: str,
) -> UUID:
    assert record.batch_id is not None
    repo = ReviewBatchRepo(session)
    page = await repo.list_candidates(record.batch_id, limit=2)
    if page.total != 1 or not page.items or page.items[0].record_kind is not RecordKind.FINANCIAL:
        raise BatchReviewRequiredError(
            record.batch_id,
            "Legacy approval supports only one financial candidate; use batch review.",
        )
    item = page.items[0]
    if record.version != expected_version or item.version != expected_version:
        raise StaleExtractionError("extraction was superseded or has changed")
    expected_decision_revision = (
        item.latest_decision.decision_revision if item.latest_decision is not None else 0
    )
    await _ensure_sqlite_outer_write_transaction(session)
    async with session.begin_nested():
        staged = await repo.apply_decisions(
            record.batch_id,
            page.batch_version,
            (
                ReviewDecisionDraft(
                    extraction_id=record.id,
                    expected_extraction_version=record.version,
                    expected_decision_revision=expected_decision_revision,
                    action="include",
                    corrected_payload=dict(corrections) if corrections else None,
                ),
            ),
            reviewer,
        )
        preview = await repo.activation_preview(record.batch_id)
        activated = await repo.activate(
            record.batch_id,
            staged.batch_version,
            preview.activation_vector_sha256,
            reviewer,
        )
        verified_id = activated.verified_by_extraction.get(record.id)
        if verified_id is None:
            raise RuntimeError("singleton financial activation did not create a verified row")
    return verified_id


def _review_item(
    record: ExtractedRecord,
    document: Document,
    batch: ExtractionBatch | None,
    threshold: float,
    flags: Sequence[DuplicateFlag],
) -> dict[str, Any]:
    candidates = [
        {
            "document_id": flag.suspected_document_id,
            "reason": flag.reason,
            "score": float(flag.score),
            "evidence": flag.evidence,
        }
        for flag in flags
    ]
    return {
        "document_id": record.document_id,
        "extraction_id": record.id,
        "version": record.version,
        "source_file_id": record.source_file_id,
        "source_version": record.source_version,
        "doc_class": document.document_class.value,
        "flagged_fields": _flagged_fields(record.payload, record.field_confidences, threshold),
        "suggested": record.payload,
        "source_spans": record.source_spans,
        "suspected_duplicate_of": [candidate["document_id"] for candidate in candidates],
        "duplicate_candidates": candidates,
        "batch_id": record.batch_id,
        "batch_version": batch.version if batch is not None else None,
        "batch_candidate_count": batch.candidate_count if batch is not None else None,
        "record_kind": record.record_kind,
        "financial_subtype": record.financial_subtype,
        "created_at": record.created_at or dt.datetime.min.replace(tzinfo=dt.UTC),
    }


def _changed_bill_fields(source: Any, projected: Any, supplied: dict[str, Any]) -> dict[str, bool]:
    """Record every normalized projection field a review action actually changed."""

    changed: dict[str, bool] = {}
    for field in bill_correction_fields():
        if field in supplied or getattr(source, field) != getattr(projected, field):
            if getattr(source, field) != getattr(projected, field):
                changed[field] = True
    return changed


def _raw_bill_value(payload: dict[str, Any], field: str) -> Any:
    """Return an untrusted field value solely for an audit comparison."""

    value = payload.get(field)
    return value.get("value") if isinstance(value, dict) else None


def _flagged_fields(
    payload: dict[str, Any], field_confidences: dict[str, Any], threshold: float
) -> list[str]:
    confidences = _flatten_confidences(field_confidences)
    if not confidences:
        confidences = _payload_confidences(payload)
    return sorted(name for name, value in confidences.items() if value < threshold)


def _flatten_confidences(value: Any, path: str = "") -> dict[str, float]:
    flattened: dict[str, float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(child, (int, float)) and not isinstance(child, bool):
                flattened[child_path] = float(child)
            else:
                flattened.update(_flatten_confidences(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            flattened.update(_flatten_confidences(child, f"{path}[{index}]"))
    return flattened


def _payload_confidences(value: Any, path: str = "") -> dict[str, float]:
    values: dict[str, float] = {}
    if isinstance(value, dict):
        confidence = value.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and path:
            values[path] = float(confidence)
        for key, child in value.items():
            if key not in {"value", "confidence", "source_span"}:
                values.update(_payload_confidences(child, f"{path}.{key}" if path else key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            values.update(_payload_confidences(child, f"{path}[{index}]"))
    return values


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if offset < 0:
        raise ValueError("offset must not be negative")


__all__ = [
    "StaleExtractionError",
    "activate_batch",
    "apply_batch_decisions",
    "approve",
    "batch_candidates",
    "batches",
    "lock_batch_reprocess_intake",
    "pending",
    "preview_batch_activation",
    "reject",
    "reject_batch_and_reprocess",
]
