"""Shared predicate for records whose source extraction is currently approved."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, and_, or_

from clerksan.db.models import (
    BatchLifecycle,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    RecordKind,
    VerifiedRecord,
)


def restrict_to_active_verified(statement: Select[Any]) -> Select[Any]:
    """Restrict authority to legacy approvals or one active financial batch."""

    statement = statement.join(
        ExtractedRecord,
        and_(
            VerifiedRecord.extracted_id == ExtractedRecord.id,
            VerifiedRecord.document_id == ExtractedRecord.document_id,
        ),
    )
    return join_active_extraction_batch(statement).where(active_extraction_authority())


def join_active_extraction_batch(statement: Select[Any]) -> Select[Any]:
    """Add the nullable batch join needed by :func:`active_extraction_authority`."""

    return statement.outerjoin(
        ExtractionBatch,
        and_(
            ExtractionBatch.id == ExtractedRecord.batch_id,
            ExtractionBatch.document_id == ExtractedRecord.document_id,
        ),
    )


def active_extraction_authority() -> Any:
    """Return the shared legacy-compatible active financial authority predicate."""

    return and_(
        ExtractedRecord.status == ExtractionStatus.APPROVED,
        or_(
            ExtractedRecord.batch_id.is_(None),
            and_(
                ExtractionBatch.lifecycle == BatchLifecycle.ACTIVE,
                ExtractedRecord.record_kind == RecordKind.FINANCIAL,
            ),
        ),
    )
