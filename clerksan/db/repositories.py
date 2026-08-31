"""Transactional data access for immutable documents and reviewed records."""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, case, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from clerksan.bills.service import BillConflictError, BillValidationError, record_verified_bill
from clerksan.db.active_records import restrict_to_active_verified
from clerksan.db.audit import audit_actor
from clerksan.db.models import (
    BatchLifecycle,
    CandidateDecisionAction,
    CandidateReviewDecision,
    Chunk,
    Document,
    DocumentClass,
    DocumentFile,
    DocumentStatus,
    DuplicateFlag,
    EmbeddedMedia,
    ExecutionProfile,
    ExpenseKind,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    FileKind,
    FinancialSubtype,
    IntakeIntent,
    Issuer,
    Job,
    JobStatus,
    MappingSet,
    MappingSetEntry,
    RecordKind,
    RecurringBill,
    SchemaMapping,
    SourceIntake,
    SourceIntakeState,
    SpreadsheetRow,
    UploadIdempotencyReservation,
    VerifiedRecord,
    WorkerCapabilityLease,
)
from clerksan.extract.recurring import (
    RecurringBillNormalizationError,
    bill_correction_fields,
    bill_projection_correction_fields,
    normalize_recurring_bill_payload,
)
from clerksan.ingest.jobs import enqueue
from clerksan.ingest.mapping import (
    DateStyle,
    DecimalStyle,
    FieldParser,
    FieldRule,
    MappingApplication,
    MappingContract,
    MappingSetContract,
    MappingSetEntryContract,
    MappingValidationError,
    SignRule,
)
from clerksan.ingest.normalized import canonical_digest, canonical_json
from clerksan.ingest.records import CandidateDraft, CompositionLedger


class DocumentNotFoundError(LookupError):
    """A requested document does not exist."""


class StaleExtractionError(RuntimeError):
    """An extraction was superseded, reviewed, or changed after it was displayed."""


class ReviewValidationError(ValueError):
    """A reviewer supplied a correction that cannot be safely promoted."""


class RawSourceVersionError(ValueError):
    """A raw-source version cannot be appended without violating retention rules."""


class ReprocessStateError(RuntimeError):
    """A document is not in a lifecycle state that can safely be reprocessed."""


class SourceVersionSupersededError(RuntimeError):
    """A worker attempted to emit output for an older retained source version."""


class StaleSourceIntakeError(RuntimeError):
    """A source-intake projection changed before an optimistic transition committed."""


class SourceIntakeValidationError(ValueError):
    """Source-intake evidence is incomplete, malformed, or internally inconsistent."""


class UploadIdempotencyConflictError(RuntimeError):
    """An upload key is already bound to different bytes or canonical intent."""


class MappingConflictError(RuntimeError):
    """Exact mapping input no longer matches its immutable source or version."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class MappingNotFoundError(LookupError):
    """A requested immutable mapping or mapping set does not exist."""


class ReviewBatchNotFoundError(LookupError):
    """A requested extraction batch does not exist."""


class ReviewBatchConflictError(RuntimeError):
    """A batch mutation no longer matches the review state shown to the user."""

    code = "stale_review_batch"

    def __init__(self, message: str, *, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.detail = detail


class ReviewBatchValidationError(ValueError):
    """A batch decision or activation request is internally unsafe."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class BatchReviewRequiredError(RuntimeError):
    """A legacy document-wide mutation cannot safely represent this batch."""

    code = "batch_review_required"

    def __init__(self, batch_id: UUID, message: str) -> None:
        super().__init__(message)
        self.detail = {"batch_id": str(batch_id)}


class UploadIdempotencyOutcome(enum.StrEnum):
    NEW = "new"
    REPLAY = "replay"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class RawSourceVersion:
    """The immutable source version appended to a document."""

    version: int
    sha256: str
    intake_intent: IntakeIntent = IntakeIntent.LEGACY_UNSPECIFIED


@dataclass(frozen=True)
class ReprocessTarget:
    """The source and lifecycle version that make one reprocess request idempotent."""

    original_version: int
    original_sha256: str
    idempotency_key: str
    lifecycle_id: UUID | None = None
    lifecycle_version: int | None = None
    intake_intent: IntakeIntent = IntakeIntent.LEGACY_UNSPECIFIED


@dataclass(frozen=True)
class DerivativeRetryTarget:
    """Current-source derivative jobs scheduled for an explicit retry.

    A terminal indexing or embedded-media OCR job does not invalidate a human
    review.  Keeping the source version and failed rows intact lets an operator
    retry just that bounded derivative stage after fixing its underlying cause.
    """

    original_version: int
    queued_job_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class IntakeReplayMetadata:
    """Stable accepted-result evidence returned after a lost upload response."""

    intake_id: UUID
    document_id: UUID
    source_file_id: UUID
    source_version: int
    source_sha256: str
    duplicate_of_document_id: UUID | None
    intake_intent: IntakeIntent
    state: SourceIntakeState
    reason_code: str | None
    job_id: UUID | None
    job_type: str | None
    job_status: JobStatus | None
    job_idempotency_key: str | None


@dataclass(frozen=True)
class UploadIdempotencyResult:
    outcome: UploadIdempotencyOutcome
    replay: IntakeReplayMetadata | None = None


@dataclass(frozen=True)
class ExactMappingSource:
    """One current source identity locked for a mapping mutation."""

    intake_id: UUID
    document_id: UUID
    source_file_id: UUID
    source_version: int
    source_sha256: str
    intake_intent: IntakeIntent


@dataclass(frozen=True)
class MappingSetSnapshot:
    """Persisted mapping-set identity plus its reconstructed bounded contract."""

    id: UUID
    source_intake_id: UUID
    document_id: UUID
    source_file_id: UUID
    source_version: int
    source_sha256: str
    structure_fingerprint: str
    set_digest: str
    version: int
    created_by: str
    created_at: dt.datetime
    contract: MappingSetContract


@dataclass(frozen=True)
class ExtractionBatchSummary:
    """Bounded mapping-apply result; candidates remain available through review."""

    id: UUID
    document_id: UUID
    source_intake_id: UUID
    source_file_id: UUID
    source_version: int
    source_sha256: str
    normalized_sha256: str
    structure_fingerprint: str
    mapping_set_id: UUID | None
    mapping_set_version: int | None
    mapping_set_digest: str | None
    lifecycle: BatchLifecycle
    candidate_count: int
    reconciliation_counts: dict[str, int]
    reconciliation_digest: str
    version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReviewDecisionDraft:
    """One optimistic reviewer instruction before it becomes an immutable revision."""

    extraction_id: UUID
    expected_extraction_version: int
    expected_decision_revision: int
    action: CandidateDecisionAction | str
    corrected_payload: dict[str, Any] | None = None
    corrected_financial_subtype: FinancialSubtype | str | None = None
    exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewDecisionRevision:
    id: UUID
    extraction_id: UUID
    decision_revision: int
    action: CandidateDecisionAction
    expected_extraction_version: int
    corrected_payload: dict[str, Any] | None
    corrected_financial_subtype: FinancialSubtype | None
    exclusion_reason: str | None
    actor: str
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ReviewDecisionBatchResult:
    batch_id: UUID
    previous_batch_version: int
    batch_version: int
    lifecycle: BatchLifecycle
    decisions: tuple[ReviewDecisionRevision, ...]


@dataclass(frozen=True, slots=True)
class ReviewBatchSummarySnapshot:
    id: UUID
    document_id: UUID
    source_intake_id: UUID
    source_file_id: UUID
    source_version: int
    lifecycle: BatchLifecycle
    version: int
    candidate_count: int
    pending_count: int
    included_count: int
    excluded_count: int
    error_count: int
    exception_count: int
    reconciliation_counts: dict[str, int]
    reconciliation_digest: str
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ReviewCandidateSnapshot:
    extraction_id: UUID
    batch_id: UUID
    candidate_ordinal: int
    candidate_key: str
    record_kind: RecordKind
    financial_subtype: FinancialSubtype | None
    source_locator: str
    row_fingerprint: str | None
    version: int
    status: ExtractionStatus
    payload: dict[str, Any]
    field_confidences: dict[str, Any]
    source_spans: dict[str, Any]
    validation_issues: tuple[str, ...]
    evidence_group_keys: tuple[str, ...]
    latest_decision: ReviewDecisionRevision | None
    duplicate_evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ReviewCandidatePage:
    batch_id: UUID
    batch_version: int
    total: int
    limit: int
    offset: int
    items: tuple[ReviewCandidateSnapshot, ...]
    source_duplicate_evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ReviewActivationPreview:
    batch_id: UUID
    document_id: UUID
    source_intake_id: UUID
    source_file_id: UUID
    source_version: int
    batch_version: int
    lifecycle: BatchLifecycle
    total_count: int
    pending_count: int
    included_count: int
    excluded_count: int
    error_count: int
    reconciliation_counts: dict[str, int]
    reconciliation_digest: str
    candidate_count_matches: bool
    source_is_current: bool
    requires_accept_exclusions: bool
    requires_accept_empty: bool
    ready_for_activation: bool
    activation_vector_sha256: str
    errors: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ReviewActivationResult:
    batch_id: UUID
    document_id: UUID
    batch_version: int
    lifecycle: BatchLifecycle
    activation_vector_sha256: str
    included_count: int
    excluded_count: int
    accepted_exclusions: bool
    accepted_empty: bool
    verified_by_extraction: dict[UUID, UUID]


@dataclass(frozen=True, slots=True)
class ReviewBatchRejectionResult:
    batch_id: UUID
    document_id: UUID
    source_intake_id: UUID
    source_file_id: UUID
    source_version: int
    batch_version: int
    lifecycle: BatchLifecycle


_RECOVERABLE_DERIVATIVE_JOB_TYPES = frozenset({"index_document", "process_embedded_media"})


def _as_document_dict(document: Document) -> dict[str, Any]:
    ordered_files = sorted(document.files, key=lambda file: (file.version, str(file.id)))
    latest_extraction = _current_extraction(document.extractions)
    approved_extraction_ids = {
        extraction.id
        for extraction in document.extractions
        if extraction.status is ExtractionStatus.APPROVED
    }
    active_verified = [
        record
        for record in document.verified_records
        if record.extracted_id in approved_extraction_ids
    ]
    latest_verified = _latest_verified(active_verified)
    verified_extraction = next(
        (
            record
            for record in document.extractions
            if latest_verified is not None and record.id == latest_verified.extracted_id
        ),
        None,
    )
    verified = _as_verified_dict(latest_verified) if latest_verified else None
    if verified is not None and verified_extraction is not None:
        verified["source_file_id"] = verified_extraction.source_file_id
        verified["source_version"] = verified_extraction.source_version
    processing_error = _current_source_processing_error(document, ordered_files)
    return {
        "id": document.id,
        "doc_class": document.document_class.value,
        "status": document.status.value,
        "source_filename": document.source_filename,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "files": [_as_file_dict(file) for file in ordered_files],
        "extracted": _as_extraction_dict(latest_extraction) if latest_extraction else None,
        "verified": verified,
        "processing_error": processing_error,
    }


def _as_file_dict(file: DocumentFile) -> dict[str, Any]:
    return {
        "id": file.id,
        "version": file.version,
        "kind": file.kind.value,
        "source_file_id": file.source_file_id,
        "source_version": file.source_version,
        "page_number": file.page_number,
        "content_path": file.content_path,
        "sha256": file.sha256,
        "mime": file.mime,
        "source_filename": file.source_filename,
        "ocr_text": file.ocr_text,
        "text_provenance": file.text_provenance,
        "created_at": file.created_at,
    }


def _current_extraction(records: list[ExtractedRecord]) -> ExtractedRecord | None:
    """Prefer a reviewable extraction, then an approved extraction, deterministically."""

    for status in (ExtractionStatus.PENDING_REVIEW, ExtractionStatus.APPROVED):
        candidates = [record for record in records if record.status is status]
        if candidates:
            return max(candidates, key=_extraction_order)
    return max(records, key=_extraction_order) if records else None


def _latest_extraction_for_source(
    records: list[ExtractedRecord], status: ExtractionStatus, source_version: int
) -> ExtractedRecord | None:
    """Return the newest lifecycle record for one retained original version."""

    candidates = [
        record
        for record in records
        if record.status is status and record.source_version == source_version
    ]
    return max(candidates, key=_extraction_order, default=None)


def _extraction_order(record: ExtractedRecord) -> tuple[dt.datetime, int, str]:
    created_at = record.created_at or dt.datetime.min.replace(tzinfo=dt.UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.UTC)
    return created_at, record.version, str(record.id)


def _latest_verified(records: list[VerifiedRecord]) -> VerifiedRecord | None:
    if not records:
        return None
    return max(
        records,
        key=lambda record: (
            _aware_datetime(record.verified_at),
            _aware_datetime(record.created_at),
            str(record.id),
        ),
    )


def _aware_datetime(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _latest_original(files: list[DocumentFile]) -> DocumentFile | None:
    originals = [file for file in files if file.kind is FileKind.ORIGINAL]
    return max(originals, key=lambda file: (file.version, str(file.id)), default=None)


def _current_source_processing_error(
    document: Document, ordered_files: list[DocumentFile]
) -> str | None:
    """Return an unresolved terminal error for the current original only."""

    original = _latest_original(ordered_files)
    if original is None:
        return None
    current_jobs = _current_source_process_jobs(document.jobs, original.version)
    if current_jobs:
        latest = current_jobs[-1]
        if latest.status in {JobStatus.DEAD, JobStatus.FAILED}:
            return latest.last_error

    derivative_failures: list[Job] = []
    for jobs in _current_source_derivative_jobs(document.jobs, original.version).values():
        # A newer queued/running/successful successor resolves the prior terminal
        # failure for display purposes. The original DEAD row remains durable
        # evidence in the queue.
        if any(job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE} for job in jobs):
            continue
        latest = max(jobs, key=_job_order)
        if latest.status in {JobStatus.DEAD, JobStatus.FAILED}:
            derivative_failures.append(latest)
    if derivative_failures:
        latest = max(derivative_failures, key=_job_order)
        error = latest.last_error or "background derivative failed without an error message"
        return f"{latest.job_type}: {error}"
    return None


def _current_source_process_jobs(jobs: list[Job], source_version: int) -> list[Job]:
    """Order process jobs that are explicitly bound to one immutable source."""

    return sorted(
        (
            job
            for job in jobs
            if job.job_type == "process_document"
            and _job_source_version(job.payload) == source_version
        ),
        key=_job_order,
    )


def _current_source_derivative_jobs(
    jobs: list[Job], source_version: int
) -> dict[tuple[str, int, str], list[Job]]:
    """Group retry-safe derivative jobs by their exact immutable input target."""

    grouped: dict[tuple[str, int, str], list[Job]] = {}
    for job in jobs:
        target = _derivative_job_target(job)
        if target is not None and target[1] == source_version:
            grouped.setdefault(target, []).append(job)
    return grouped


def _derivative_job_target(job: Job) -> tuple[str, int, str] | None:
    """Return a validated immutable retry target, never a best-effort guess."""

    if job.job_type not in _RECOVERABLE_DERIVATIVE_JOB_TYPES:
        return None
    source_version = _job_source_version(job.payload)
    checksum_field = "normalized_sha256" if job.job_type == "index_document" else "media_sha256"
    checksum = job.payload.get(checksum_field)
    if source_version is None or not _is_sha256(checksum):
        return None
    return job.job_type, source_version, checksum


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _job_source_version(payload: dict[str, Any]) -> int | None:
    value = payload.get("source_version")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _job_order(job: Job) -> tuple[dt.datetime, dt.datetime, str]:
    return _aware_datetime(job.created_at), _aware_datetime(job.updated_at), str(job.id)


def _latest_derivative_recovery_leaf(terminal: list[Job], jobs: list[Job]) -> Job:
    """Pick the newest retry-chain leaf without trusting timestamp precision.

    SQLite can assign identical second-level timestamps to a failed job and its
    successor. The successor's recovery key is an explicit durable edge, so prefer
    a terminal job that has no recorded successor before using timestamps only as a
    deterministic tie-breaker for malformed/manual branches.
    """

    existing_keys = {job.idempotency_key for job in jobs}
    leaves = [job for job in terminal if f"recovery:{job.id}" not in existing_keys]
    return max(leaves or terminal, key=_job_order)


def _failed_reprocess_key(source_version: int, failed_job_id: UUID) -> str:
    """A terminal job ID makes each explicit recovery attempt a new logical job."""

    return f"reprocess:{source_version}:failure:{failed_job_id}"


def _as_extraction_dict(record: ExtractedRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "document_id": record.document_id,
        "source_file_id": record.source_file_id,
        "source_version": record.source_version,
        "payload": record.payload,
        "field_confidences": record.field_confidences,
        "source_spans": record.source_spans,
        "model_name": record.model_name,
        "prompt_version": record.prompt_version,
        "status": record.status.value,
        "version": record.version,
        "reviewer": record.reviewer,
        "rejection_reason": record.rejection_reason,
        "reviewed_at": record.reviewed_at,
        "created_at": record.created_at,
    }


def _as_verified_dict(record: VerifiedRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "document_id": record.document_id,
        "extracted_id": record.extracted_id,
        "transaction_date": record.transaction_date,
        "total_amount": record.total_amount,
        "counterparty": record.counterparty,
        "currency": record.currency,
        "category": record.category,
        "expense_kind": record.expense_kind.value if record.expense_kind else None,
        "due_date": record.due_date,
        "registration_number": record.registration_number,
        "tax_8_amount": record.tax_8_amount,
        "tax_10_amount": record.tax_10_amount,
        "reviewer": record.reviewer,
        "version": record.version,
        "verified_at": record.verified_at,
        "created_at": record.created_at,
    }


def _clean_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("sha256 must be a 64-character lowercase hexadecimal digest")


def _validate_optional_digest(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceIntakeValidationError(f"{field_name} must be a string")
    cleaned = value.strip()
    if len(cleaned) != 64 or any(character not in "0123456789abcdef" for character in cleaned):
        raise SourceIntakeValidationError(
            f"{field_name} must be a 64-character lowercase hexadecimal digest"
        )
    return cleaned


def _normalize_intake_intent(value: IntakeIntent | str) -> IntakeIntent:
    if isinstance(value, IntakeIntent):
        return value
    try:
        return IntakeIntent(value)
    except (TypeError, ValueError) as error:
        raise SourceIntakeValidationError("unsupported intake_intent") from error


def _normalize_required_components(values: tuple[str, ...] | list[str]) -> list[str]:
    if not isinstance(values, (tuple, list)):
        raise SourceIntakeValidationError("required_components must be a sequence")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise SourceIntakeValidationError("required_components must contain strings")
        cleaned = value.strip()
        if not cleaned:
            raise SourceIntakeValidationError("required_components must not contain blanks")
        normalized.append(cleaned)
    if len(normalized) != len(set(normalized)):
        raise SourceIntakeValidationError("required_components must not contain duplicates")
    return sorted(normalized)


def _required_components_digest(values: list[str]) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_upload_idempotency_key(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise SourceIntakeValidationError("upload_idempotency_key must be a UUID") from error


def _clean_optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceIntakeValidationError(f"{field_name} must be a string")
    cleaned = value.strip()
    return cleaned or None


async def _ensure_sqlite_outer_write_transaction(session: AsyncSession) -> None:
    """Prevent an outermost SQLite savepoint from committing repository work early."""

    if session.get_bind().dialect.name == "sqlite":
        await session.execute(text("UPDATE documents SET updated_at = updated_at WHERE 0"))


def _extraction_source_spans(payload: dict[str, Any]) -> dict[str, str]:
    source_spans: dict[str, str] = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            source_span = value.get("source_span")
            if isinstance(source_span, str) and source_span:
                source_spans[path] = source_span
            for key, child in value.items():
                if key not in {"value", "confidence", "source_span"}:
                    visit(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(payload, "")
    return source_spans


def _field_value(payload: dict[str, Any], field_name: str) -> Any:
    value = payload.get(field_name)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _as_date(value: Any, field_name: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO date") from error
    raise ValueError(f"{field_name} is required")


def _as_decimal(value: Any, field_name: str, *, required: bool) -> Decimal | None:
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required")
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a number") from error


async def _supersede_pending_extractions(
    session: AsyncSession, document_id: UUID, *, reviewer: str, reviewed_at: dt.datetime
) -> int:
    """Atomically supersede every reviewable extraction for a replaced source.

    SQLite ignores ``FOR UPDATE``.  A conditional SQL update makes source replacement
    and review approval contend on the same lifecycle rows on both supported engines.
    """

    result = await session.execute(
        update(ExtractedRecord)
        .where(
            ExtractedRecord.document_id == document_id,
            ExtractedRecord.status == ExtractionStatus.PENDING_REVIEW,
        )
        .values(
            status=ExtractionStatus.SUPERSEDED,
            version=ExtractedRecord.version + 1,
            reviewer=reviewer,
            reviewed_at=reviewed_at,
        )
    )
    return int(result.rowcount or 0)


async def _approve_pending_extraction(
    session: AsyncSession,
    extraction_id: UUID,
    expected_version: int,
    *,
    reviewer: str,
    reviewed_at: dt.datetime,
) -> bool:
    """Advance one displayed review version exactly once across supported databases."""

    result = await session.execute(
        update(ExtractedRecord)
        .where(
            ExtractedRecord.id == extraction_id,
            ExtractedRecord.status == ExtractionStatus.PENDING_REVIEW,
            ExtractedRecord.version == expected_version,
        )
        .values(
            status=ExtractionStatus.APPROVED,
            version=ExtractedRecord.version + 1,
            reviewer=reviewer,
            reviewed_at=reviewed_at,
        )
    )
    return result.rowcount == 1


_SOURCE_INTAKE_TRANSITIONS = {
    SourceIntakeState.QUEUED: frozenset(
        {
            SourceIntakeState.PROCESSING,
            SourceIntakeState.PROCESSED,
            SourceIntakeState.NEEDS_MAPPING,
            SourceIntakeState.STORED_UNPROCESSED,
            SourceIntakeState.FAILED,
        }
    ),
    SourceIntakeState.PROCESSING: frozenset(
        {
            SourceIntakeState.QUEUED,
            SourceIntakeState.PROCESSED,
            SourceIntakeState.NEEDS_MAPPING,
            SourceIntakeState.STORED_UNPROCESSED,
            SourceIntakeState.FAILED,
        }
    ),
    SourceIntakeState.PROCESSED: frozenset({SourceIntakeState.QUEUED}),
    SourceIntakeState.NEEDS_MAPPING: frozenset(
        {SourceIntakeState.QUEUED, SourceIntakeState.PROCESSING, SourceIntakeState.FAILED}
    ),
    SourceIntakeState.STORED_UNPROCESSED: frozenset(
        {SourceIntakeState.QUEUED, SourceIntakeState.PROCESSING, SourceIntakeState.FAILED}
    ),
    SourceIntakeState.FAILED: frozenset({SourceIntakeState.QUEUED}),
}


class SourceIntakeRepo:
    """Persist one exact source projection and transition it optimistically."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve_upload_idempotency(
        self,
        key: UUID | str,
        source_sha256: str,
        intent_digest: str,
    ) -> UploadIdempotencyResult:
        """Serialize and resolve a streamed upload key in the current transaction.

        PostgreSQL holds a transaction-scoped advisory lock from this call through
        the caller's commit. SQLite acquires its single-writer lock by inserting a
        durable reservation row before returning ``new``. The savepoint lets a
        losing writer resolve the committed binding without leaving partial rows.
        """

        normalized_key = _normalize_upload_idempotency_key(key)
        assert normalized_key is not None
        _validate_sha256(source_sha256)
        normalized_intent = _validate_optional_digest(intent_digest, "intent_digest")
        assert normalized_intent is not None

        dialect_name = self.session.get_bind().dialect.name
        if dialect_name == "postgresql":
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": str(normalized_key)},
            )

        owned_keys = self.session.info.setdefault("clerksan.upload_idempotency_reservations", set())
        key_text = str(normalized_key)
        reservation: UploadIdempotencyReservation | None
        if key_text in owned_keys:
            reservation = await self.session.get(UploadIdempotencyReservation, normalized_key)
            # Session-local ownership cannot outlive a transaction rollback. Prune
            # the hint when the row is no longer present, then reserve normally.
            if reservation is None:
                owned_keys.discard(key_text)
        if key_text not in owned_keys:
            if dialect_name == "sqlite":
                # A savepoint can become SQLite's outermost transaction when no
                # prior DML exists; releasing it would publish the key too early.
                # This conflict-free INSERT starts the real outer write transaction
                # and keeps its single-writer lock until the caller commits.
                inserted_key = await self.session.scalar(
                    text(
                        "INSERT INTO upload_idempotency_reservations ("
                        "upload_idempotency_key, source_sha256, intent_digest"
                        ") VALUES (:key, :source_sha256, :intent_digest) "
                        "ON CONFLICT (upload_idempotency_key) DO NOTHING "
                        "RETURNING upload_idempotency_key"
                    ),
                    {
                        "key": normalized_key.hex,
                        "source_sha256": source_sha256,
                        "intent_digest": normalized_intent,
                    },
                )
                if inserted_key is not None:
                    owned_keys.add(key_text)
                    return UploadIdempotencyResult(UploadIdempotencyOutcome.NEW)
                reservation = await self.session.scalar(
                    select(UploadIdempotencyReservation)
                    .where(UploadIdempotencyReservation.upload_idempotency_key == normalized_key)
                    .with_for_update()
                )
            else:
                candidate = UploadIdempotencyReservation(
                    upload_idempotency_key=normalized_key,
                    source_sha256=source_sha256,
                    intent_digest=normalized_intent,
                )
                try:
                    async with self.session.begin_nested():
                        self.session.add(candidate)
                        await self.session.flush()
                except IntegrityError:
                    reservation = await self.session.scalar(
                        select(UploadIdempotencyReservation)
                        .where(
                            UploadIdempotencyReservation.upload_idempotency_key == normalized_key
                        )
                        .with_for_update()
                    )
                else:
                    owned_keys.add(key_text)
                    return UploadIdempotencyResult(UploadIdempotencyOutcome.NEW)

        if reservation is None:
            raise SourceIntakeValidationError(
                "upload idempotency reservation disappeared during serialization"
            )
        if (
            reservation.source_sha256 != source_sha256
            or reservation.intent_digest != normalized_intent
        ):
            return UploadIdempotencyResult(UploadIdempotencyOutcome.CONFLICT)

        if reservation.source_intake_id is None:
            if key_text in owned_keys:
                return UploadIdempotencyResult(UploadIdempotencyOutcome.NEW)
            # A committed unbound row should be impossible because reservation and
            # source intake bind in one transaction. Fail closed rather than publish.
            return UploadIdempotencyResult(UploadIdempotencyOutcome.CONFLICT)
        intake = await self.session.get(SourceIntake, reservation.source_intake_id)
        if intake is None or intake.upload_idempotency_key != normalized_key:
            return UploadIdempotencyResult(UploadIdempotencyOutcome.CONFLICT)

        jobs = (
            await self.session.scalars(
                select(Job)
                .where(Job.document_id == intake.document_id)
                .order_by(Job.created_at.asc(), Job.id.asc())
            )
        ).all()
        source_jobs = [
            job
            for job in jobs
            if _job_source_version(job.payload) == intake.source_version
            and job.job_type == "process_document"
        ]
        replay_job = max(source_jobs, key=_job_order, default=None)
        return UploadIdempotencyResult(
            UploadIdempotencyOutcome.REPLAY,
            IntakeReplayMetadata(
                intake_id=intake.id,
                document_id=intake.document_id,
                source_file_id=intake.source_file_id,
                source_version=intake.source_version,
                source_sha256=intake.source_sha256,
                duplicate_of_document_id=intake.duplicate_of_document_id,
                intake_intent=intake.intake_intent,
                state=intake.state,
                reason_code=intake.reason_code,
                job_id=replay_job.id if replay_job else None,
                job_type=replay_job.job_type if replay_job else None,
                job_status=replay_job.status if replay_job else None,
                job_idempotency_key=replay_job.idempotency_key if replay_job else None,
            ),
        )

    async def create_legacy_companion(
        self,
        *,
        document_id: UUID,
        source_file: DocumentFile,
        duplicate_of_document_id: UUID | None = None,
        upload_idempotency_key: UUID | str | None = None,
        intent_digest: str | None = None,
        registry_digest: str | None = None,
        capabilities_digest: str | None = None,
        policy_version: str = "legacy-compat-v1",
        required_components: tuple[str, ...] | list[str] = (),
        intake_intent: IntakeIntent | str = IntakeIntent.LEGACY_UNSPECIFIED,
    ) -> UUID:
        """Create the non-sandboxed companion owned by a legacy source write."""

        return await self.create_source_companion(
            document_id=document_id,
            source_file=source_file,
            duplicate_of_document_id=duplicate_of_document_id,
            upload_idempotency_key=upload_idempotency_key,
            intent_digest=intent_digest,
            registry_digest=registry_digest,
            capabilities_digest=capabilities_digest,
            policy_version=policy_version,
            required_components=required_components,
            intake_intent=intake_intent,
        )

    async def create_source_companion(
        self,
        *,
        document_id: UUID,
        source_file: DocumentFile,
        duplicate_of_document_id: UUID | None = None,
        upload_idempotency_key: UUID | str | None = None,
        intent_digest: str | None = None,
        registry_digest: str | None = None,
        capabilities_digest: str | None = None,
        policy_version: str = "legacy-compat-v1",
        required_components: tuple[str, ...] | list[str] = (),
        intake_intent: IntakeIntent | str = IntakeIntent.LEGACY_UNSPECIFIED,
        detected_family: str | None = None,
        detected_format: str | None = None,
        canonical_mime: str | None = None,
        detection_evidence: list[dict[str, Any]] | None = None,
        state: SourceIntakeState | str = SourceIntakeState.QUEUED,
        reason_code: str | None = "processing_queued",
        retryable: bool = False,
        failure_phase: str | None = None,
        execution_profile: ExecutionProfile | str = ExecutionProfile.LEGACY_COMPAT,
        sandbox_verified: bool = False,
    ) -> UUID:
        """Create the exact-source intake projection chosen by the live policy."""

        key = _normalize_upload_idempotency_key(upload_idempotency_key)
        normalized_intent = _validate_optional_digest(intent_digest, "intent_digest")
        if (key is None) != (normalized_intent is None):
            raise SourceIntakeValidationError(
                "upload_idempotency_key and intent_digest must be supplied together"
            )
        if source_file.document_id != document_id or source_file.kind is not FileKind.ORIGINAL:
            raise SourceIntakeValidationError(
                "source intake must reference the exact same-document original"
            )
        _validate_sha256(source_file.sha256)
        if duplicate_of_document_id == document_id:
            raise SourceIntakeValidationError("duplicate_of_document_id must not be self")
        normalized_components = _normalize_required_components(required_components)
        normalized_intake_intent = _normalize_intake_intent(intake_intent)
        normalized_policy_version = _clean_text(policy_version, "policy_version")
        normalized_registry_digest = _validate_optional_digest(registry_digest, "registry_digest")
        normalized_capabilities_digest = _validate_optional_digest(
            capabilities_digest, "capabilities_digest"
        )
        normalized_family = _clean_optional_text(detected_family, "detected_family")
        normalized_format = _clean_optional_text(detected_format, "detected_format")
        normalized_mime = _clean_optional_text(canonical_mime, "canonical_mime")
        normalized_reason = _clean_optional_text(reason_code, "reason_code")
        normalized_failure_phase = _clean_optional_text(failure_phase, "failure_phase")
        if not isinstance(state, SourceIntakeState):
            try:
                state = SourceIntakeState(state)
            except ValueError as error:
                raise SourceIntakeValidationError("unsupported source intake state") from error
        if not isinstance(execution_profile, ExecutionProfile):
            try:
                execution_profile = ExecutionProfile(execution_profile)
            except ValueError as error:
                raise SourceIntakeValidationError("unsupported execution profile") from error
        if (execution_profile is ExecutionProfile.UNIVERSAL_SANDBOXED) is not bool(
            sandbox_verified
        ):
            raise SourceIntakeValidationError("execution profile and sandbox evidence must agree")
        if execution_profile is ExecutionProfile.UNIVERSAL_SANDBOXED and (
            normalized_registry_digest is None or normalized_capabilities_digest is None
        ):
            raise SourceIntakeValidationError(
                "universal intake requires registry and capability digests"
            )
        if state is SourceIntakeState.FAILED and normalized_reason is None:
            raise SourceIntakeValidationError("failed source intake requires a reason_code")
        evidence = list(detection_evidence or ())
        if not all(isinstance(item, dict) for item in evidence):
            raise SourceIntakeValidationError("detection_evidence must contain objects")
        reservation: UploadIdempotencyReservation | None = None
        if key is not None:
            assert normalized_intent is not None
            outcome = await self.reserve_upload_idempotency(
                key, source_file.sha256, normalized_intent
            )
            if outcome.outcome is not UploadIdempotencyOutcome.NEW:
                raise SourceIntakeValidationError(
                    "upload idempotency reservation could not be bound"
                )
            reservation = await self.session.scalar(
                select(UploadIdempotencyReservation)
                .where(UploadIdempotencyReservation.upload_idempotency_key == key)
                .with_for_update()
            )
            if reservation is None or (
                reservation.source_sha256 != source_file.sha256
                or reservation.intent_digest != normalized_intent
                or reservation.source_intake_id is not None
            ):
                raise SourceIntakeValidationError(
                    "upload idempotency reservation does not match this source"
                )
        intake = SourceIntake(
            document_id=document_id,
            source_file_id=source_file.id,
            source_version=source_file.version,
            source_sha256=source_file.sha256,
            duplicate_of_document_id=duplicate_of_document_id,
            detected_family=normalized_family,
            detected_format=normalized_format,
            canonical_mime=normalized_mime or source_file.mime,
            detection_evidence=evidence,
            policy_version=normalized_policy_version,
            registry_digest=normalized_registry_digest,
            capabilities_digest=normalized_capabilities_digest,
            requirements_digest=_required_components_digest(normalized_components),
            required_components=normalized_components,
            intake_intent=normalized_intake_intent,
            state=state,
            reason_code=normalized_reason,
            retryable=bool(retryable),
            failure_phase=normalized_failure_phase,
            execution_profile=execution_profile,
            sandbox_verified=bool(sandbox_verified),
            upload_idempotency_key=key,
            intent_digest=normalized_intent,
        )
        self.session.add(intake)
        await self.session.flush()
        if reservation is not None:
            reservation.source_intake_id = intake.id
            await self.session.flush()
        return intake.id

    async def get(self, intake_id: UUID) -> SourceIntake | None:
        return await self.session.get(SourceIntake, intake_id)

    async def get_for_source(
        self, document_id: UUID, source_file_id: UUID, source_version: int
    ) -> SourceIntake | None:
        return await self.session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == document_id,
                SourceIntake.source_file_id == source_file_id,
                SourceIntake.source_version == source_version,
            )
        )

    async def recent(self, *, limit: int = 50) -> list[SourceIntake]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return list(
            await self.session.scalars(
                select(SourceIntake)
                .order_by(SourceIntake.created_at.desc(), SourceIntake.id.desc())
                .limit(limit)
            )
        )

    async def transition(
        self,
        intake_id: UUID,
        *,
        expected_version: int,
        state: SourceIntakeState,
        actor: str,
        reason_code: str | None = None,
        retryable: bool = False,
        failure_phase: str | None = None,
    ) -> int:
        """Advance one intake under an exact optimistic version and state edge."""

        if expected_version < 1:
            raise SourceIntakeValidationError("expected_version must be greater than zero")
        if not isinstance(state, SourceIntakeState):
            try:
                state = SourceIntakeState(state)
            except ValueError as error:
                raise SourceIntakeValidationError("unsupported source intake state") from error
        current = await self.session.scalar(
            select(SourceIntake).where(SourceIntake.id == intake_id).with_for_update()
        )
        if current is None or current.version != expected_version:
            raise StaleSourceIntakeError("source intake changed before this transition")
        if state not in _SOURCE_INTAKE_TRANSITIONS[current.state]:
            raise SourceIntakeValidationError(
                f"source intake cannot transition from {current.state.value} to {state.value}"
            )
        cleaned_reason = _clean_optional_text(reason_code, "reason_code")
        cleaned_failure_phase = _clean_optional_text(failure_phase, "failure_phase")
        if state is SourceIntakeState.FAILED and cleaned_reason is None:
            raise SourceIntakeValidationError("failed source intake requires a reason_code")

        async with audit_actor(self.session, _clean_text(actor, "actor")):
            result = await self.session.execute(
                update(SourceIntake)
                .where(
                    SourceIntake.id == intake_id,
                    SourceIntake.version == expected_version,
                    SourceIntake.state == current.state,
                )
                .values(
                    state=state,
                    reason_code=cleaned_reason,
                    retryable=bool(retryable),
                    failure_phase=cleaned_failure_phase,
                    version=SourceIntake.version + 1,
                    updated_at=dt.datetime.now(dt.UTC),
                )
            )
        if result.rowcount != 1:
            raise StaleSourceIntakeError("source intake changed before this transition")
        return expected_version + 1


class WorkerCapabilityLeaseRepo:
    """Refresh and read expiring API/worker capability-parity evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def refresh(
        self,
        *,
        worker_id: str,
        registry_digest: str,
        capabilities_digest: str,
        sandbox_verified: bool,
        heartbeat_at: dt.datetime,
        expires_at: dt.datetime,
    ) -> None:
        cleaned_worker_id = _clean_text(worker_id, "worker_id")
        normalized_registry = _validate_optional_digest(registry_digest, "registry_digest")
        normalized_capabilities = _validate_optional_digest(
            capabilities_digest, "capabilities_digest"
        )
        assert normalized_registry is not None and normalized_capabilities is not None
        heartbeat_at = _aware_datetime(heartbeat_at)
        expires_at = _aware_datetime(expires_at)
        if expires_at <= heartbeat_at:
            raise SourceIntakeValidationError("capability lease must expire after its heartbeat")
        lease = await self.session.scalar(
            select(WorkerCapabilityLease)
            .where(WorkerCapabilityLease.worker_id == cleaned_worker_id)
            .with_for_update()
        )
        if lease is None:
            self.session.add(
                WorkerCapabilityLease(
                    worker_id=cleaned_worker_id,
                    registry_digest=normalized_registry,
                    capabilities_digest=normalized_capabilities,
                    sandbox_verified=bool(sandbox_verified),
                    heartbeat_at=heartbeat_at,
                    expires_at=expires_at,
                )
            )
        else:
            lease.registry_digest = normalized_registry
            lease.capabilities_digest = normalized_capabilities
            lease.sandbox_verified = bool(sandbox_verified)
            lease.heartbeat_at = heartbeat_at
            lease.expires_at = expires_at
        await self.session.flush()

    async def get(self, worker_id: str) -> WorkerCapabilityLease | None:
        return await self.session.get(WorkerCapabilityLease, _clean_text(worker_id, "worker_id"))


class DocumentRepo:
    """Create and retrieve documents with their immutable file artifacts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_with_raw(
        self,
        *,
        filename: str,
        content_path: str,
        sha256: str,
        mime: str,
        doc_class: DocumentClass = DocumentClass.OTHER,
        duplicate_of_document_id: UUID | None = None,
        upload_idempotency_key: UUID | str | None = None,
        intent_digest: str | None = None,
        registry_digest: str | None = None,
        capabilities_digest: str | None = None,
        policy_version: str = "legacy-compat-v1",
        required_components: tuple[str, ...] | list[str] = (),
        intake_intent: IntakeIntent | str = IntakeIntent.LEGACY_UNSPECIFIED,
        detected_family: str | None = None,
        detected_format: str | None = None,
        detection_evidence: list[dict[str, Any]] | None = None,
        intake_state: SourceIntakeState | str = SourceIntakeState.QUEUED,
        reason_code: str | None = "processing_queued",
        retryable: bool = False,
        execution_profile: ExecutionProfile | str = ExecutionProfile.LEGACY_COMPAT,
        sandbox_verified: bool = False,
    ) -> UUID:
        """Insert a document, original, and exact source intake in one transaction."""

        filename = _clean_text(filename, "filename")
        content_path = _clean_text(content_path, "content_path")
        mime = _clean_text(mime, "mime")
        _validate_sha256(sha256)
        normalized_intake_intent = _normalize_intake_intent(intake_intent)

        key = _normalize_upload_idempotency_key(upload_idempotency_key)
        if key is not None:
            if intent_digest is None:
                raise SourceIntakeValidationError(
                    "intent_digest is required with upload_idempotency_key"
                )
            reservation = await SourceIntakeRepo(self.session).reserve_upload_idempotency(
                key, sha256, intent_digest
            )
            if reservation.outcome is UploadIdempotencyOutcome.REPLAY:
                assert reservation.replay is not None
                return reservation.replay.document_id
            if reservation.outcome is UploadIdempotencyOutcome.CONFLICT:
                raise UploadIdempotencyConflictError(
                    "upload idempotency key is already bound to different content or intent"
                )
        elif intent_digest is not None:
            raise SourceIntakeValidationError(
                "upload_idempotency_key is required with intent_digest"
            )

        document = Document(document_class=doc_class, source_filename=filename)
        original = DocumentFile(
            document=document,
            version=1,
            kind=FileKind.ORIGINAL,
            content_path=content_path,
            sha256=sha256,
            mime=mime,
            source_filename=filename,
        )
        await _ensure_sqlite_outer_write_transaction(self.session)
        try:
            async with self.session.begin_nested():
                self.session.add_all((document, original))
                await self.session.flush()
                await SourceIntakeRepo(self.session).create_source_companion(
                    document_id=document.id,
                    source_file=original,
                    duplicate_of_document_id=duplicate_of_document_id,
                    upload_idempotency_key=key,
                    intent_digest=intent_digest,
                    registry_digest=registry_digest,
                    capabilities_digest=capabilities_digest,
                    policy_version=policy_version,
                    required_components=required_components,
                    intake_intent=normalized_intake_intent,
                    detected_family=detected_family,
                    detected_format=detected_format,
                    canonical_mime=mime,
                    detection_evidence=detection_evidence,
                    state=intake_state,
                    reason_code=reason_code,
                    retryable=retryable,
                    execution_profile=execution_profile,
                    sandbox_verified=sandbox_verified,
                )
        except IntegrityError:
            if key is not None and intent_digest is not None:
                reservation = await SourceIntakeRepo(self.session).reserve_upload_idempotency(
                    key, sha256, intent_digest
                )
                if reservation.outcome is UploadIdempotencyOutcome.REPLAY:
                    assert reservation.replay is not None
                    return reservation.replay.document_id
                if reservation.outcome is UploadIdempotencyOutcome.CONFLICT:
                    raise UploadIdempotencyConflictError(
                        "upload idempotency key is already bound to different content or intent"
                    ) from None
            raise
        return document.id

    async def add_artifact(
        self,
        document_id: UUID,
        *,
        kind: FileKind,
        content_path: str,
        sha256: str,
        mime: str,
        source_file_id: UUID,
        source_version: int,
        ocr_text: str | None = None,
        text_provenance: str | None = None,
        page_number: int | None = None,
    ) -> UUID:
        """Append a derivative bound to the exact current immutable original."""

        document = await self._locked_document(document_id)
        if kind is FileKind.ORIGINAL:
            raise ValueError("add_artifact accepts derivative file kinds only")
        original = _latest_original(document.files)
        if original is None:
            raise SourceVersionSupersededError("document has no immutable original")
        if original.version != source_version:
            raise SourceVersionSupersededError(
                "the source version was replaced before a derivative could be recorded"
            )
        if original.id != source_file_id:
            raise SourceVersionSupersededError(
                "the source file was replaced before a derivative could be recorded"
            )
        if kind is FileKind.PAGE_RENDER:
            if isinstance(page_number, bool) or page_number is None or page_number <= 0:
                raise ValueError("page_render artifacts require a positive page_number")
        elif page_number is not None:
            raise ValueError("page_number is valid only for page_render artifacts")
        _validate_sha256(sha256)
        max_version = max((file.version for file in document.files), default=0)
        artifact = DocumentFile(
            document_id=document.id,
            version=max_version + 1,
            kind=kind,
            source_file_id=original.id,
            source_version=original.version,
            page_number=page_number,
            content_path=_clean_text(content_path, "content_path"),
            sha256=sha256,
            mime=_clean_text(mime, "mime"),
            source_filename=original.source_filename,
            ocr_text=ocr_text,
            text_provenance=text_provenance,
        )
        self.session.add(artifact)
        await self.session.flush()
        return artifact.id

    async def resolve_append_intake_intent(
        self,
        document_id: UUID,
        requested: IntakeIntent | str | None,
    ) -> IntakeIntent:
        """Resolve a replacement's immutable intent while retaining its document lock."""

        normalized_requested = (
            _normalize_intake_intent(requested) if requested is not None else None
        )
        document = await self._locked_document(document_id)
        return await self._resolve_append_intake_intent_for_locked_document(
            document,
            normalized_requested,
        )

    async def append_raw_source(
        self,
        document_id: UUID,
        *,
        filename: str,
        content_path: str,
        sha256: str,
        mime: str,
        actor: str,
        duplicate_of_document_id: UUID | None = None,
        upload_idempotency_key: UUID | str | None = None,
        intent_digest: str | None = None,
        registry_digest: str | None = None,
        capabilities_digest: str | None = None,
        policy_version: str = "legacy-compat-v1",
        required_components: tuple[str, ...] | list[str] = (),
        intake_intent: IntakeIntent | str | None = None,
        detected_family: str | None = None,
        detected_format: str | None = None,
        detection_evidence: list[dict[str, Any]] | None = None,
        intake_state: SourceIntakeState | str = SourceIntakeState.QUEUED,
        reason_code: str | None = "processing_queued",
        retryable: bool = False,
        execution_profile: ExecutionProfile | str = ExecutionProfile.LEGACY_COMPAT,
        sandbox_verified: bool = False,
    ) -> RawSourceVersion:
        """Append a new original and retire stale derived projections atomically."""

        filename = _clean_text(filename, "filename")
        content_path = _clean_text(content_path, "content_path")
        mime = _clean_text(mime, "mime")
        _validate_sha256(sha256)
        cleaned_actor = _clean_text(actor, "actor")
        requested_intake_intent = (
            _normalize_intake_intent(intake_intent) if intake_intent is not None else None
        )
        key = _normalize_upload_idempotency_key(upload_idempotency_key)
        if key is None and intent_digest is not None:
            raise SourceIntakeValidationError(
                "upload_idempotency_key is required with intent_digest"
            )

        # Lock and resolve before touching the idempotency reservation.  All
        # replacement paths therefore hold database locks in document -> key
        # order, while an omitted intent remains bound to the exact predecessor.
        document = await self._locked_document(document_id)
        resolved_intake_intent = await self._resolve_append_intake_intent_for_locked_document(
            document,
            requested_intake_intent,
        )

        if key is not None:
            if intent_digest is None:
                raise SourceIntakeValidationError(
                    "intent_digest is required with upload_idempotency_key"
                )
            reservation = await SourceIntakeRepo(self.session).reserve_upload_idempotency(
                key, sha256, intent_digest
            )
            if reservation.outcome is UploadIdempotencyOutcome.REPLAY:
                assert reservation.replay is not None
                if reservation.replay.document_id != document_id:
                    raise UploadIdempotencyConflictError(
                        "upload idempotency key belongs to another document"
                    )
                return RawSourceVersion(
                    version=reservation.replay.source_version,
                    sha256=reservation.replay.source_sha256,
                    intake_intent=reservation.replay.intake_intent,
                )
            if reservation.outcome is UploadIdempotencyOutcome.CONFLICT:
                raise UploadIdempotencyConflictError(
                    "upload idempotency key is already bound to different content or intent"
                )
        if any(file.kind is FileKind.ORIGINAL and file.sha256 == sha256 for file in document.files):
            raise RawSourceVersionError("this document already retains that file checksum")

        next_version = max((file.version for file in document.files), default=0) + 1
        now = dt.datetime.now(dt.UTC)
        await _ensure_sqlite_outer_write_transaction(self.session)
        try:
            async with self.session.begin_nested():
                async with audit_actor(self.session, cleaned_actor):
                    await _supersede_pending_extractions(
                        self.session,
                        document_id,
                        reviewer=cleaned_actor,
                        reviewed_at=now,
                    )
                    # These are current-source projections, unlike immutable raw files
                    # and reviewed history. Removing them in this transaction means a
                    # replacement cannot expose old search text, spreadsheet aggregates,
                    # or embedded-media OCR while it waits to be processed.
                    await self.session.execute(
                        delete(Chunk).where(Chunk.document_id == document_id)
                    )
                    await self.session.execute(
                        delete(SpreadsheetRow).where(SpreadsheetRow.document_id == document_id)
                    )
                    await self.session.execute(
                        delete(EmbeddedMedia).where(EmbeddedMedia.document_id == document_id)
                    )
                    original = DocumentFile(
                        document_id=document.id,
                        version=next_version,
                        kind=FileKind.ORIGINAL,
                        content_path=content_path,
                        sha256=sha256,
                        mime=mime,
                        source_filename=filename,
                    )
                    self.session.add(original)
                    document.source_filename = filename
                    document.status = DocumentStatus.NEEDS_REPROCESS
                    await self.session.flush()
                    await SourceIntakeRepo(self.session).create_source_companion(
                        document_id=document.id,
                        source_file=original,
                        duplicate_of_document_id=duplicate_of_document_id,
                        upload_idempotency_key=key,
                        intent_digest=intent_digest,
                        registry_digest=registry_digest,
                        capabilities_digest=capabilities_digest,
                        policy_version=policy_version,
                        required_components=required_components,
                        intake_intent=resolved_intake_intent,
                        detected_family=detected_family,
                        detected_format=detected_format,
                        canonical_mime=mime,
                        detection_evidence=detection_evidence,
                        state=intake_state,
                        reason_code=reason_code,
                        retryable=retryable,
                        execution_profile=execution_profile,
                        sandbox_verified=sandbox_verified,
                    )
        except IntegrityError as error:
            raise RawSourceVersionError(
                "the document changed before this source version could be appended; retry"
            ) from error
        return RawSourceVersion(
            version=next_version,
            sha256=sha256,
            intake_intent=resolved_intake_intent,
        )

    async def _resolve_append_intake_intent_for_locked_document(
        self,
        document: Document,
        requested: IntakeIntent | None,
    ) -> IntakeIntent:
        if requested is not None:
            return requested

        previous_original = _latest_original(document.files)
        if previous_original is None:
            return IntakeIntent.LEGACY_UNSPECIFIED
        previous_intake = await SourceIntakeRepo(self.session).get_for_source(
            document.id,
            previous_original.id,
            previous_original.version,
        )
        if previous_intake is None:
            return IntakeIntent.LEGACY_UNSPECIFIED
        return previous_intake.intake_intent

    async def prepare_reprocess(self, document_id: UUID, *, actor: str) -> ReprocessTarget:
        """Mark an eligible exact source for one safe, idempotent retry job."""

        cleaned_actor = _clean_text(actor, "actor")
        document = await self._locked_document(document_id)
        original = _latest_original(document.files)
        if original is None:
            raise ReprocessStateError("document has no preserved original to reprocess")
        intake = await SourceIntakeRepo(self.session).get_for_source(
            document.id, original.id, original.version
        )
        if intake is None:
            raise SourceIntakeValidationError(
                "current original is missing its required source intake companion"
            )

        approved = _latest_extraction_for_source(
            document.extractions, ExtractionStatus.APPROVED, original.version
        )
        rejected = _latest_extraction_for_source(
            document.extractions, ExtractionStatus.REJECTED, original.version
        )
        current_source_jobs = _current_source_process_jobs(document.jobs, original.version)
        active_job = next(
            (
                job
                for job in reversed(current_source_jobs)
                if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
            ),
            None,
        )
        terminal_job = next(
            (job for job in reversed(current_source_jobs) if job.status is JobStatus.DEAD),
            None,
        )
        idempotency_key: str | None = None
        if document.status is DocumentStatus.NEEDS_REPROCESS:
            # A rejected replacement should be retriable even while an older approved
            # record remains visible as historical evidence.  A same-source approved
            # record is the idempotent second request before its queued work runs.
            extraction = rejected or approved
            if extraction is None and active_job is not None:
                idempotency_key = active_job.idempotency_key
            elif extraction is None and terminal_job is not None:
                idempotency_key = _failed_reprocess_key(original.version, terminal_job.id)
        elif document.status is DocumentStatus.VERIFIED:
            extraction = approved
        elif document.status is DocumentStatus.FAILED:
            extraction = None
            if active_job is not None:
                idempotency_key = active_job.idempotency_key
            elif terminal_job is not None:
                idempotency_key = _failed_reprocess_key(original.version, terminal_job.id)
        elif intake.state is SourceIntakeState.STORED_UNPROCESSED:
            extraction = None
            idempotency_key = (
                f"reprocess:intake:{intake.id}:source:{original.version}:attempt:{intake.version}"
            )
        else:
            extraction = None
        if extraction is None and idempotency_key is None:
            raise ReprocessStateError(
                "reprocess is available only after rejection, terminal failure, "
                "from a verified document, or when a preserved source gains a capability"
            )

        async with audit_actor(self.session, cleaned_actor):
            document.status = DocumentStatus.NEEDS_REPROCESS
            await self.session.flush()
        if intake.state is not SourceIntakeState.QUEUED:
            await SourceIntakeRepo(self.session).transition(
                intake.id,
                expected_version=intake.version,
                state=SourceIntakeState.QUEUED,
                actor=cleaned_actor,
                reason_code="processing_queued",
                retryable=False,
            )
        if extraction is not None:
            idempotency_key = f"reprocess:{original.version}:{extraction.id}:{extraction.version}"
        assert idempotency_key is not None
        return ReprocessTarget(
            original_version=original.version,
            original_sha256=original.sha256,
            idempotency_key=idempotency_key,
            lifecycle_id=extraction.id if extraction is not None else None,
            lifecycle_version=extraction.version if extraction is not None else None,
            intake_intent=intake.intake_intent,
        )

    async def lock_current_reprocess_intake(
        self,
        document_id: UUID,
        *,
        expected_intake_id: UUID | None = None,
        expected_intake_version: int | None = None,
    ) -> SourceIntake:
        """Lock and return the exact current intake before capability evaluation."""

        document = await self._locked_document(document_id)
        original = _latest_original(document.files)
        if original is None:
            raise ReprocessStateError("document has no preserved original to reprocess")
        intake = await SourceIntakeRepo(self.session).get_for_source(
            document.id,
            original.id,
            original.version,
        )
        if intake is None:
            raise SourceIntakeValidationError(
                "current original is missing its required source intake companion"
            )
        if expected_intake_id is not None and intake.id != expected_intake_id:
            raise StaleSourceIntakeError("source intake is no longer current")
        if expected_intake_version is not None and intake.version != expected_intake_version:
            raise StaleSourceIntakeError("source intake changed before reprocessing")
        return intake

    async def retry_current_derivatives(self, document_id: UUID) -> DerivativeRetryTarget:
        """Queue one successor for each dead current-source derivative target.

        Failed jobs remain terminal evidence with their original attempts and error.
        The successor copies the exact source-bound payload, rather than rerunning
        document extraction or resetting a circuit breaker. A recovery key derived
        from the failed job makes repeat requests idempotent and allows a later
        explicit retry after a successor itself becomes terminal.
        """

        document = await self._locked_document(document_id)
        original = _latest_original(document.files)
        if original is None:
            raise ReprocessStateError("document has no preserved original for derivative retry")

        queued_job_ids: list[UUID] = []
        derivative_groups = _current_source_derivative_jobs(document.jobs, original.version)
        for jobs in derivative_groups.values():
            # Any active or successful successor already resolves this exact input.
            if any(
                job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE} for job in jobs
            ):
                continue
            terminal = [job for job in jobs if job.status in {JobStatus.DEAD, JobStatus.FAILED}]
            if not terminal:
                continue
            failed_job = _latest_derivative_recovery_leaf(terminal, jobs)
            recovery_key = f"recovery:{failed_job.id}"
            if any(job.idempotency_key == recovery_key for job in document.jobs):
                continue
            recovery_id = await enqueue(
                self.session,
                job_type=failed_job.job_type,
                payload=dict(failed_job.payload),
                idempotency_key=recovery_key,
                registry_digest=failed_job.registry_digest,
                capabilities_digest=failed_job.capabilities_digest,
                required_components=list(failed_job.required_components),
                intake_intent=failed_job.intake_intent,
            )
            if recovery_id is not None:
                queued_job_ids.append(recovery_id)

        return DerivativeRetryTarget(
            original_version=original.version,
            queued_job_ids=tuple(queued_job_ids),
        )

    async def get(self, document_id: UUID) -> dict[str, Any]:
        """Return all persisted tiers for one document."""

        document = await self._document_with_relations(document_id)
        if document is None:
            raise DocumentNotFoundError(str(document_id))
        return _as_document_dict(document)

    async def list(
        self,
        *,
        doc_class: DocumentClass | None = None,
        status: DocumentStatus | None = None,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        amount_min: Decimal | float | None = None,
        amount_max: Decimal | float | None = None,
        counterparty: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List documents with optional class, state, and verified-date filters."""

        _validate_page(limit, offset)
        statement: Select[tuple[Document]] = select(Document)
        if doc_class is not None:
            statement = statement.where(Document.document_class == doc_class)
        if status is not None:
            statement = statement.where(Document.status == status)
        if any(
            value is not None
            for value in (date_from, date_to, amount_min, amount_max, counterparty)
        ):
            statement = statement.join(VerifiedRecord).join(
                ExtractedRecord,
                and_(
                    VerifiedRecord.extracted_id == ExtractedRecord.id,
                    VerifiedRecord.document_id == ExtractedRecord.document_id,
                ),
            )
            statement = statement.where(ExtractedRecord.status == ExtractionStatus.APPROVED)
            if date_from is not None:
                statement = statement.where(VerifiedRecord.transaction_date >= date_from)
            if date_to is not None:
                statement = statement.where(VerifiedRecord.transaction_date <= date_to)
            if amount_min is not None:
                statement = statement.where(VerifiedRecord.total_amount >= Decimal(str(amount_min)))
            if amount_max is not None:
                statement = statement.where(VerifiedRecord.total_amount <= Decimal(str(amount_max)))
            if counterparty is not None:
                statement = statement.where(VerifiedRecord.counterparty == counterparty.strip())

        statement = (
            statement.options(*_document_load_options())
            .order_by(Document.created_at.desc())
            .distinct()
            .limit(limit)
            .offset(offset)
        )
        documents = (await self.session.scalars(statement)).unique().all()
        return [_as_document_dict(document) for document in documents]

    async def set_status(
        self,
        document_id: UUID,
        status: DocumentStatus,
        *,
        source_version: int | None = None,
    ) -> None:
        document = await self._locked_document(document_id)
        if source_version is not None:
            original = _latest_original(document.files)
            if original is None or original.version != source_version:
                raise SourceVersionSupersededError(
                    "the source version was replaced before document state could be recorded"
                )
        document.status = status
        await self.session.flush()

    async def find_by_sha256(self, sha256: str) -> UUID | None:
        result = await self.session.scalar(
            select(DocumentFile.document_id)
            .where(DocumentFile.sha256 == sha256, DocumentFile.kind == FileKind.ORIGINAL)
            .order_by(DocumentFile.created_at.asc())
            .limit(1)
        )
        return result

    async def _locked_document(self, document_id: UUID) -> Document:
        result = await self.session.scalars(
            select(Document)
            .where(Document.id == document_id)
            .options(
                selectinload(Document.files),
                selectinload(Document.extractions),
                selectinload(Document.jobs),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        document = result.one_or_none()
        if document is None:
            raise DocumentNotFoundError(str(document_id))
        return document

    async def _document_with_relations(self, document_id: UUID) -> Document | None:
        return await self.session.scalar(
            select(Document)
            .where(Document.id == document_id)
            .options(*_document_load_options())
            .execution_options(populate_existing=True)
        )


class MappingSourceRepo:
    """Resolve one exact current source without relying on cached relationships."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def require_current(
        self,
        document_id: UUID,
        *,
        source_intake_id: UUID,
        source_file_id: UUID,
        source_version: int,
        source_sha256: str,
        for_update: bool = False,
    ) -> ExactMappingSource:
        _validate_sha256(source_sha256)
        if source_version < 1:
            raise MappingConflictError(
                "stale_source",
                "The mapping request refers to an invalid source version.",
            )
        document_statement = select(Document).where(Document.id == document_id)
        if for_update:
            document_statement = document_statement.with_for_update()
        document = await self.session.scalar(document_statement)
        if document is None:
            raise DocumentNotFoundError(str(document_id))

        current = await self.session.scalar(
            select(DocumentFile)
            .where(
                DocumentFile.document_id == document_id,
                DocumentFile.kind == FileKind.ORIGINAL,
            )
            .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
            .limit(1)
        )
        intake_statement = select(SourceIntake).where(
            SourceIntake.id == source_intake_id,
            SourceIntake.document_id == document_id,
        )
        if for_update:
            intake_statement = intake_statement.with_for_update()
        intake = await self.session.scalar(intake_statement)
        expected = {
            "source_intake_id": str(source_intake_id),
            "source_file_id": str(source_file_id),
            "source_version": source_version,
            "source_sha256": source_sha256,
        }
        if current is None or intake is None:
            raise MappingConflictError(
                "stale_source",
                "The exact source is no longer available for mapping.",
                detail={"expected": expected},
            )
        actual = {
            "source_intake_id": str(intake.id),
            "source_file_id": str(current.id),
            "source_version": current.version,
            "source_sha256": current.sha256,
        }
        if (
            current.id != source_file_id
            or current.version != source_version
            or current.sha256 != source_sha256
            or intake.source_file_id != source_file_id
            or intake.source_version != source_version
            or intake.source_sha256 != source_sha256
        ):
            raise MappingConflictError(
                "stale_source",
                "The source changed after its schema was displayed.",
                detail={"expected": expected, "current": actual},
            )
        return ExactMappingSource(
            intake_id=intake.id,
            document_id=document_id,
            source_file_id=current.id,
            source_version=current.version,
            source_sha256=current.sha256,
            intake_intent=intake.intake_intent,
        )

    async def current(self, document_id: UUID) -> ExactMappingSource:
        document = await self.session.scalar(select(Document).where(Document.id == document_id))
        if document is None:
            raise DocumentNotFoundError(str(document_id))
        current = await self.session.scalar(
            select(DocumentFile)
            .where(
                DocumentFile.document_id == document_id,
                DocumentFile.kind == FileKind.ORIGINAL,
            )
            .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
            .limit(1)
        )
        if current is None:
            raise MappingConflictError(
                "mapping_not_ready",
                "The document has no preserved source to map.",
            )
        intake = await self.session.scalar(
            select(SourceIntake).where(
                SourceIntake.document_id == document_id,
                SourceIntake.source_file_id == current.id,
                SourceIntake.source_version == current.version,
                SourceIntake.source_sha256 == current.sha256,
            )
        )
        if intake is None:
            raise MappingConflictError(
                "mapping_not_ready",
                "The current source has no intake evidence for mapping.",
            )
        return ExactMappingSource(
            intake_id=intake.id,
            document_id=document_id,
            source_file_id=current.id,
            source_version=current.version,
            source_sha256=current.sha256,
            intake_intent=intake.intake_intent,
        )


class SchemaMappingRepo:
    """Create and read immutable declarative schema mappings."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        document_id: UUID,
        contract: MappingContract,
        *,
        created_by: str,
        source_intake_id: UUID,
        source_file_id: UUID,
        source_version: int,
        source_sha256: str,
    ) -> SchemaMapping:
        await MappingSourceRepo(self.session).require_current(
            document_id,
            source_intake_id=source_intake_id,
            source_file_id=source_file_id,
            source_version=source_version,
            source_sha256=source_sha256,
            for_update=True,
        )
        cleaned_creator = _bounded_mapping_text(created_by, "created_by", 255)
        if len(contract.table_locator) > 1_024:
            raise MappingValidationError("table_locator is too long for persistence")
        serialized_rules = {"rules": [_field_rule_payload(rule) for rule in contract.field_rules]}
        if len(json.dumps(serialized_rules, ensure_ascii=True).encode("utf-8")) > 262_144:
            raise MappingValidationError("mapping rule payload is too large")
        existing = await self.session.get(SchemaMapping, contract.mapping_id)
        if existing is not None:
            self._verify_replay(existing, contract, cleaned_creator)
            return existing

        record = SchemaMapping(
            id=contract.mapping_id,
            table_locator=contract.table_locator,
            schema_fingerprint=contract.schema_fingerprint,
            record_kind=contract.record_kind,
            financial_subtype=contract.financial_subtype,
            field_rules=serialized_rules,
            required_fields=list(contract.required_fields),
            mapping_version=contract.mapping_version,
            mapping_digest=contract.contract_digest,
            created_by=cleaned_creator,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(record)
                await self.session.flush()
        except IntegrityError as error:
            winner = await self.session.get(SchemaMapping, contract.mapping_id)
            if winner is None:
                winner = await self.session.scalar(
                    select(SchemaMapping).where(
                        SchemaMapping.mapping_digest == contract.contract_digest
                    )
                )
            if winner is None:
                raise
            try:
                self._verify_replay(winner, contract, cleaned_creator)
            except MappingConflictError as conflict:
                raise conflict from error
            return winner
        return record

    async def get_contract(
        self,
        mapping_id: UUID,
        *,
        expected_version: int | None = None,
    ) -> tuple[SchemaMapping, MappingContract]:
        record = await self.session.get(SchemaMapping, mapping_id)
        if record is None:
            raise MappingNotFoundError(str(mapping_id))
        if expected_version is not None and record.mapping_version != expected_version:
            raise MappingConflictError(
                "stale_mapping_version",
                "The mapping version changed after it was displayed.",
                detail={
                    "mapping_id": str(mapping_id),
                    "expected_version": expected_version,
                    "current_version": record.mapping_version,
                },
            )
        contract = _mapping_contract_from_record(record)
        if contract.contract_digest != record.mapping_digest:
            raise MappingConflictError(
                "mapping_digest_mismatch",
                "The persisted mapping contract no longer matches its digest.",
                detail={"mapping_id": str(mapping_id)},
            )
        return record, contract

    async def list_for_schemas(self, schema_fingerprints: set[str]) -> list[SchemaMapping]:
        if not schema_fingerprints:
            return []
        return list(
            (
                await self.session.scalars(
                    select(SchemaMapping)
                    .where(SchemaMapping.schema_fingerprint.in_(schema_fingerprints))
                    .order_by(
                        SchemaMapping.created_at.desc(),
                        SchemaMapping.id.asc(),
                    )
                )
            ).all()
        )

    @staticmethod
    def _verify_replay(
        record: SchemaMapping,
        contract: MappingContract,
        created_by: str,
    ) -> None:
        if (
            record.id != contract.mapping_id
            or record.mapping_digest != contract.contract_digest
            or record.created_by != created_by
        ):
            raise MappingConflictError(
                "mapping_idempotency_conflict",
                "The mapping idempotency key is already bound to different input.",
                detail={"mapping_id": str(contract.mapping_id)},
            )


class MappingSetRepo:
    """Persist complete immutable mapped-or-ignored locator sets."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        document_id: UUID,
        contract: MappingSetContract,
        *,
        source_intake_id: UUID,
        source_sha256: str,
    ) -> MappingSetSnapshot:
        source = await MappingSourceRepo(self.session).require_current(
            document_id,
            source_intake_id=source_intake_id,
            source_file_id=contract.source_file_id,
            source_version=contract.source_version,
            source_sha256=source_sha256,
            for_update=True,
        )
        cleaned_creator = _bounded_mapping_text(contract.created_by, "created_by", 255)
        if cleaned_creator != contract.created_by:
            contract = MappingSetContract(
                source_file_id=contract.source_file_id,
                source_version=contract.source_version,
                structure_fingerprint=contract.structure_fingerprint,
                entries=contract.entries,
                created_by=cleaned_creator,
                version=contract.version,
                mapping_set_id=contract.mapping_set_id,
            )
        if any(len(entry.table_locator) > 1_024 for entry in contract.entries):
            raise MappingValidationError("mapping-set table locator is too long")
        await self._verify_mapping_members(contract)
        existing = await self.session.get(MappingSet, contract.mapping_set_id)
        if existing is None:
            existing = await self.session.scalar(
                select(MappingSet).where(
                    MappingSet.source_intake_id == source_intake_id,
                    MappingSet.set_digest == contract.set_digest,
                )
            )
        if existing is not None:
            snapshot = await self._snapshot(existing)
            self._verify_replay(snapshot, contract, source, source_sha256)
            return snapshot

        record = MappingSet(
            id=contract.mapping_set_id,
            source_intake_id=source.intake_id,
            document_id=document_id,
            source_file_id=source.source_file_id,
            source_version=source.source_version,
            source_sha256=source.source_sha256,
            structure_fingerprint=contract.structure_fingerprint,
            set_digest=contract.set_digest,
            version=contract.version,
            created_by=contract.created_by,
        )
        entries = [
            MappingSetEntry(
                mapping_set_id=record.id,
                ordinal=ordinal,
                table_locator=entry.table_locator,
                schema_fingerprint=entry.schema_fingerprint,
                mapping_id=entry.mapping.mapping_id if entry.mapping else None,
                mapping_version=(entry.mapping.mapping_version if entry.mapping else None),
                ignore_reason=entry.ignore_reason,
            )
            for ordinal, entry in enumerate(contract.entries)
        ]
        try:
            async with self.session.begin_nested():
                self.session.add(record)
                self.session.add_all(entries)
                await self.session.flush()
        except IntegrityError as error:
            winner = await self.session.get(MappingSet, contract.mapping_set_id)
            if winner is None:
                winner = await self.session.scalar(
                    select(MappingSet).where(
                        MappingSet.source_intake_id == source_intake_id,
                        MappingSet.set_digest == contract.set_digest,
                    )
                )
            if winner is None:
                raise
            snapshot = await self._snapshot(winner)
            try:
                self._verify_replay(snapshot, contract, source, source_sha256)
            except MappingConflictError as conflict:
                raise conflict from error
            return snapshot
        return await self._snapshot(record)

    async def get(
        self,
        document_id: UUID,
        mapping_set_id: UUID,
        *,
        expected_version: int | None = None,
        expected_digest: str | None = None,
    ) -> MappingSetSnapshot:
        record = await self.session.scalar(
            select(MappingSet).where(
                MappingSet.id == mapping_set_id,
                MappingSet.document_id == document_id,
            )
        )
        if record is None:
            raise MappingNotFoundError(str(mapping_set_id))
        if expected_version is not None and record.version != expected_version:
            raise MappingConflictError(
                "stale_mapping_set_version",
                "The mapping set version changed after it was displayed.",
                detail={
                    "mapping_set_id": str(mapping_set_id),
                    "expected_version": expected_version,
                    "current_version": record.version,
                },
            )
        if expected_digest is not None and record.set_digest != expected_digest:
            raise MappingConflictError(
                "stale_mapping_set_digest",
                "The mapping set digest does not match the displayed contract.",
                detail={"mapping_set_id": str(mapping_set_id)},
            )
        return await self._snapshot(record)

    async def _verify_mapping_members(self, contract: MappingSetContract) -> None:
        mappings = SchemaMappingRepo(self.session)
        for entry in contract.entries:
            if entry.mapping is None:
                continue
            _, persisted = await mappings.get_contract(
                entry.mapping.mapping_id,
                expected_version=entry.mapping.mapping_version,
            )
            if persisted.contract_digest != entry.mapping.contract_digest:
                raise MappingConflictError(
                    "stale_mapping_digest",
                    "A mapping-set entry does not match its immutable mapping.",
                    detail={"mapping_id": str(entry.mapping.mapping_id)},
                )

    async def _snapshot(self, record: MappingSet) -> MappingSetSnapshot:
        rows = list(
            (
                await self.session.scalars(
                    select(MappingSetEntry)
                    .where(MappingSetEntry.mapping_set_id == record.id)
                    .order_by(MappingSetEntry.ordinal.asc())
                )
            ).all()
        )
        entries: list[MappingSetEntryContract] = []
        mappings = SchemaMappingRepo(self.session)
        for row in rows:
            mapping: MappingContract | None = None
            if row.mapping_id is not None:
                assert row.mapping_version is not None
                _, mapping = await mappings.get_contract(
                    row.mapping_id,
                    expected_version=row.mapping_version,
                )
            entries.append(
                MappingSetEntryContract(
                    table_locator=row.table_locator,
                    schema_fingerprint=row.schema_fingerprint,
                    mapping=mapping,
                    ignore_reason=row.ignore_reason,
                )
            )
        contract = MappingSetContract(
            source_file_id=record.source_file_id,
            source_version=record.source_version,
            structure_fingerprint=record.structure_fingerprint,
            entries=tuple(entries),
            created_by=record.created_by,
            version=record.version,
            mapping_set_id=record.id,
        )
        if contract.set_digest != record.set_digest:
            raise MappingConflictError(
                "mapping_set_digest_mismatch",
                "The persisted mapping set no longer matches its digest.",
                detail={"mapping_set_id": str(record.id)},
            )
        return MappingSetSnapshot(
            id=record.id,
            source_intake_id=record.source_intake_id,
            document_id=record.document_id,
            source_file_id=record.source_file_id,
            source_version=record.source_version,
            source_sha256=record.source_sha256,
            structure_fingerprint=record.structure_fingerprint,
            set_digest=record.set_digest,
            version=record.version,
            created_by=record.created_by,
            created_at=record.created_at,
            contract=contract,
        )

    @staticmethod
    def _verify_replay(
        snapshot: MappingSetSnapshot,
        contract: MappingSetContract,
        source: ExactMappingSource,
        source_sha256: str,
    ) -> None:
        if (
            snapshot.set_digest != contract.set_digest
            or snapshot.source_intake_id != source.intake_id
            or snapshot.source_file_id != contract.source_file_id
            or snapshot.source_version != contract.source_version
            or snapshot.source_sha256 != source_sha256
            or snapshot.created_by != contract.created_by
        ):
            raise MappingConflictError(
                "mapping_set_idempotency_conflict",
                "The mapping-set idempotency key is already bound to different input.",
                detail={"mapping_set_id": str(contract.mapping_set_id)},
            )


class ExtractionBatchRepo:
    """Atomically persist one immutable 0/1/N candidate cohort."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_candidate_batch(
        self,
        document_id: UUID,
        *,
        source_intake_id: UUID,
        source_file_id: UUID,
        source_version: int,
        source_sha256: str,
        normalized_sha256: str,
        structure_fingerprint: str,
        candidates: tuple[CandidateDraft, ...],
        ledger: CompositionLedger,
        producer: str,
        producer_version: str,
        origin: str,
        idempotency_key: str,
        producer_job_id: UUID | None = None,
    ) -> ExtractionBatchSummary:
        """Persist a provider-neutral direct candidate cohort for one exact source."""

        _validate_sha256(normalized_sha256)
        _validate_sha256(structure_fingerprint)
        source = await MappingSourceRepo(self.session).require_current(
            document_id,
            source_intake_id=source_intake_id,
            source_file_id=source_file_id,
            source_version=source_version,
            source_sha256=source_sha256,
            for_update=True,
        )
        cleaned_producer = _bounded_mapping_text(producer, "producer", 255)
        cleaned_producer_version = _bounded_mapping_text(producer_version, "producer_version", 255)
        cleaned_origin = _bounded_mapping_text(origin, "origin", 64)
        cleaned_key = _bounded_mapping_text(idempotency_key, "idempotency_key", 255)
        counts = ledger.reconciliation_counts
        expected_candidate_count = counts["mapped_candidate"] + counts["residual_generic_candidate"]
        if len(candidates) != expected_candidate_count:
            raise MappingValidationError("candidate count does not match structural reconciliation")
        if tuple(draft.candidate_ordinal for draft in candidates) != tuple(
            range(1, len(candidates) + 1)
        ):
            raise MappingValidationError("candidate ordinals must be contiguous and one-based")
        reconciliation_digest = _candidate_reconciliation_digest(candidates, ledger)

        existing = await self.session.scalar(
            select(ExtractionBatch).where(
                ExtractionBatch.source_intake_id == source.intake_id,
                ExtractionBatch.idempotency_key == cleaned_key,
            )
        )
        if existing is not None:
            self._verify_candidate_replay(
                existing,
                normalized_sha256=normalized_sha256,
                structure_fingerprint=structure_fingerprint,
                candidate_count=len(candidates),
                reconciliation_digest=reconciliation_digest,
                producer=cleaned_producer,
                producer_version=cleaned_producer_version,
                origin=cleaned_origin,
                producer_job_id=producer_job_id,
            )
            return _batch_summary(existing, replayed=True)

        intake = await self.session.get(SourceIntake, source.intake_id)
        if intake is None:
            raise MappingConflictError(
                "stale_source",
                "The exact source intake disappeared before candidate persistence.",
            )
        if intake.state not in {
            SourceIntakeState.PROCESSING,
            SourceIntakeState.PROCESSED,
        }:
            raise MappingConflictError(
                "candidate_batch_not_ready",
                "The source is not in a state that accepts direct candidates.",
                detail={"state": intake.state.value, "version": intake.version},
            )

        batch = ExtractionBatch(
            id=uuid4(),
            source_intake_id=source.intake_id,
            document_id=document_id,
            source_file_id=source.source_file_id,
            source_version=source.source_version,
            source_sha256=source.source_sha256,
            normalized_sha256=normalized_sha256,
            structure_fingerprint=structure_fingerprint,
            mapping_set_id=None,
            mapping_set_version=None,
            mapping_set_digest=None,
            producer=cleaned_producer,
            producer_version=cleaned_producer_version,
            origin=cleaned_origin,
            intake_intent=source.intake_intent,
            lifecycle=BatchLifecycle.OPEN,
            idempotency_key=cleaned_key,
            producer_job_id=producer_job_id,
            candidate_count=len(candidates),
            reconciliation_counts=dict(counts),
            reconciliation_digest=reconciliation_digest,
        )
        records = [
            ExtractedRecord(
                document_id=document_id,
                source_file_id=source.source_file_id,
                source_version=source.source_version,
                batch_id=batch.id,
                candidate_ordinal=draft.candidate_ordinal,
                candidate_key=draft.candidate_key,
                record_kind=draft.record_kind,
                financial_subtype=draft.financial_subtype,
                source_locator=draft.source_locator,
                row_fingerprint=draft.row_fingerprint,
                validation_issues=list(draft.validation_issues),
                evidence_group_keys=list(draft.evidence_group_keys),
                payload=dict(draft.payload),
                field_confidences=dict(draft.confidences),
                source_spans={
                    "source_locator": draft.source_locator,
                    "row_fingerprint": draft.row_fingerprint,
                },
                model_name=cleaned_producer,
                prompt_version=cleaned_producer_version,
                status=ExtractionStatus.PENDING_REVIEW,
            )
            for draft in candidates
        ]
        try:
            async with self.session.begin_nested():
                self.session.add(batch)
                await self.session.flush()
                self.session.add_all(records)
                if intake.state is SourceIntakeState.PROCESSING:
                    intake.state = SourceIntakeState.PROCESSED
                    intake.reason_code = None
                    intake.retryable = False
                    intake.failure_phase = None
                    intake.version += 1
                await self.session.flush()
        except IntegrityError as error:
            winner = await self.session.scalar(
                select(ExtractionBatch).where(
                    ExtractionBatch.source_intake_id == source.intake_id,
                    ExtractionBatch.idempotency_key == cleaned_key,
                )
            )
            if winner is None:
                raise
            try:
                self._verify_candidate_replay(
                    winner,
                    normalized_sha256=normalized_sha256,
                    structure_fingerprint=structure_fingerprint,
                    candidate_count=len(candidates),
                    reconciliation_digest=reconciliation_digest,
                    producer=cleaned_producer,
                    producer_version=cleaned_producer_version,
                    origin=cleaned_origin,
                    producer_job_id=producer_job_id,
                )
            except MappingConflictError as conflict:
                raise conflict from error
            return _batch_summary(winner, replayed=True)
        return _batch_summary(batch, replayed=False)

    @staticmethod
    def _verify_candidate_replay(
        batch: ExtractionBatch,
        *,
        normalized_sha256: str,
        structure_fingerprint: str,
        candidate_count: int,
        reconciliation_digest: str,
        producer: str,
        producer_version: str,
        origin: str,
        producer_job_id: UUID | None,
    ) -> None:
        if (
            batch.normalized_sha256 != normalized_sha256
            or batch.structure_fingerprint != structure_fingerprint
            or batch.mapping_set_id is not None
            or batch.mapping_set_version is not None
            or batch.mapping_set_digest is not None
            or batch.candidate_count != candidate_count
            or batch.reconciliation_digest != reconciliation_digest
            or batch.producer != producer
            or batch.producer_version != producer_version
            or batch.origin != origin
            or batch.producer_job_id != producer_job_id
        ):
            raise MappingConflictError(
                "batch_idempotency_conflict",
                "The candidate idempotency key is already bound to different input.",
                detail={"batch_id": str(batch.id)},
            )

    async def add_mapping_batch(
        self,
        document_id: UUID,
        *,
        source_intake_id: UUID,
        source_file_id: UUID,
        source_version: int,
        source_sha256: str,
        normalized_sha256: str,
        structure_fingerprint: str,
        mapping_set: MappingSetSnapshot,
        application: MappingApplication,
        producer: str,
        producer_version: str,
        origin: str,
        idempotency_key: str,
        producer_job_id: UUID | None = None,
    ) -> ExtractionBatchSummary:
        _validate_sha256(normalized_sha256)
        _validate_sha256(structure_fingerprint)
        source = await MappingSourceRepo(self.session).require_current(
            document_id,
            source_intake_id=source_intake_id,
            source_file_id=source_file_id,
            source_version=source_version,
            source_sha256=source_sha256,
            for_update=True,
        )
        if (
            mapping_set.source_intake_id != source.intake_id
            or mapping_set.document_id != document_id
            or mapping_set.source_file_id != source.source_file_id
            or mapping_set.source_version != source.source_version
            or mapping_set.source_sha256 != source.source_sha256
            or mapping_set.structure_fingerprint != structure_fingerprint
        ):
            raise MappingConflictError(
                "stale_mapping_set_source",
                "The mapping set is not bound to the current exact source.",
                detail={"mapping_set_id": str(mapping_set.id)},
            )
        cleaned_producer = _bounded_mapping_text(producer, "producer", 255)
        cleaned_producer_version = _bounded_mapping_text(producer_version, "producer_version", 255)
        cleaned_origin = _bounded_mapping_text(origin, "origin", 64)
        cleaned_key = _bounded_mapping_text(idempotency_key, "idempotency_key", 255)
        counts = application.ledger.reconciliation_counts
        expected_candidate_count = counts["mapped_candidate"] + counts["residual_generic_candidate"]
        if len(application.candidates) != expected_candidate_count:
            raise MappingValidationError("candidate count does not match structural reconciliation")
        reconciliation_digest = _reconciliation_digest(application)

        existing = await self.session.scalar(
            select(ExtractionBatch).where(
                ExtractionBatch.source_intake_id == source.intake_id,
                ExtractionBatch.idempotency_key == cleaned_key,
            )
        )
        if existing is not None:
            self._verify_replay(
                existing,
                normalized_sha256=normalized_sha256,
                structure_fingerprint=structure_fingerprint,
                mapping_set=mapping_set,
                candidate_count=len(application.candidates),
                reconciliation_digest=reconciliation_digest,
                producer=cleaned_producer,
                producer_version=cleaned_producer_version,
                origin=cleaned_origin,
                producer_job_id=producer_job_id,
            )
            return _batch_summary(existing, replayed=True)

        intake = await self.session.get(SourceIntake, source.intake_id)
        if intake is None:
            raise MappingConflictError(
                "stale_source",
                "The exact source intake disappeared before mapping apply.",
            )
        if intake.state not in {
            SourceIntakeState.NEEDS_MAPPING,
            SourceIntakeState.PROCESSED,
        }:
            raise MappingConflictError(
                "mapping_not_ready",
                "The source is not in a state that accepts a confirmed mapping.",
                detail={"state": intake.state.value, "version": intake.version},
            )

        batch = ExtractionBatch(
            id=uuid4(),
            source_intake_id=source.intake_id,
            document_id=document_id,
            source_file_id=source.source_file_id,
            source_version=source.source_version,
            source_sha256=source.source_sha256,
            normalized_sha256=normalized_sha256,
            structure_fingerprint=structure_fingerprint,
            mapping_set_id=mapping_set.id,
            mapping_set_version=mapping_set.version,
            mapping_set_digest=mapping_set.set_digest,
            producer=cleaned_producer,
            producer_version=cleaned_producer_version,
            origin=cleaned_origin,
            intake_intent=source.intake_intent,
            lifecycle=BatchLifecycle.OPEN,
            idempotency_key=cleaned_key,
            producer_job_id=producer_job_id,
            candidate_count=len(application.candidates),
            reconciliation_counts=dict(counts),
            reconciliation_digest=reconciliation_digest,
        )
        candidates = [
            ExtractedRecord(
                document_id=document_id,
                source_file_id=source.source_file_id,
                source_version=source.source_version,
                batch_id=batch.id,
                candidate_ordinal=draft.candidate_ordinal,
                candidate_key=draft.candidate_key,
                record_kind=draft.record_kind,
                financial_subtype=draft.financial_subtype,
                source_locator=draft.source_locator,
                row_fingerprint=draft.row_fingerprint,
                validation_issues=list(draft.validation_issues),
                evidence_group_keys=list(draft.evidence_group_keys),
                payload=dict(draft.payload),
                field_confidences=dict(draft.confidences),
                source_spans={
                    "source_locator": draft.source_locator,
                    "row_fingerprint": draft.row_fingerprint,
                    "mapping_set_id": str(mapping_set.id),
                    "mapping_set_digest": mapping_set.set_digest,
                },
                model_name=cleaned_producer,
                prompt_version=cleaned_producer_version,
                status=ExtractionStatus.PENDING_REVIEW,
            )
            for draft in application.candidates
        ]
        try:
            async with self.session.begin_nested():
                self.session.add(batch)
                # The SQLite compatibility trigger checks exact batch/source
                # membership at candidate INSERT time, so establish the cohort
                # row before inserting its immutable members.
                await self.session.flush()
                self.session.add_all(candidates)
                intake.reason_code = None
                intake.retryable = False
                intake.failure_phase = None
                if intake.state is SourceIntakeState.NEEDS_MAPPING:
                    # The audited intake state machine deliberately requires a
                    # processing hop before a mapped source can become processed.
                    intake.state = SourceIntakeState.PROCESSING
                    intake.version += 1
                    await self.session.flush()
                    intake.state = SourceIntakeState.PROCESSED
                    intake.version += 1
                await self.session.flush()
        except IntegrityError as error:
            winner = await self.session.scalar(
                select(ExtractionBatch).where(
                    ExtractionBatch.source_intake_id == source.intake_id,
                    ExtractionBatch.idempotency_key == cleaned_key,
                )
            )
            if winner is None:
                raise
            try:
                self._verify_replay(
                    winner,
                    normalized_sha256=normalized_sha256,
                    structure_fingerprint=structure_fingerprint,
                    mapping_set=mapping_set,
                    candidate_count=len(application.candidates),
                    reconciliation_digest=reconciliation_digest,
                    producer=cleaned_producer,
                    producer_version=cleaned_producer_version,
                    origin=cleaned_origin,
                    producer_job_id=producer_job_id,
                )
            except MappingConflictError as conflict:
                raise conflict from error
            return _batch_summary(winner, replayed=True)
        return _batch_summary(batch, replayed=False)

    @staticmethod
    def _verify_replay(
        batch: ExtractionBatch,
        *,
        normalized_sha256: str,
        structure_fingerprint: str,
        mapping_set: MappingSetSnapshot,
        candidate_count: int,
        reconciliation_digest: str,
        producer: str,
        producer_version: str,
        origin: str,
        producer_job_id: UUID | None,
    ) -> None:
        if (
            batch.normalized_sha256 != normalized_sha256
            or batch.structure_fingerprint != structure_fingerprint
            or batch.mapping_set_id != mapping_set.id
            or batch.mapping_set_version != mapping_set.version
            or batch.mapping_set_digest != mapping_set.set_digest
            or batch.candidate_count != candidate_count
            or batch.reconciliation_digest != reconciliation_digest
            or batch.producer != producer
            or batch.producer_version != producer_version
            or batch.origin != origin
            or batch.producer_job_id != producer_job_id
        ):
            raise MappingConflictError(
                "batch_idempotency_conflict",
                "The apply idempotency key is already bound to different input.",
                detail={"batch_id": str(batch.id)},
            )


def _field_rule_payload(rule: FieldRule) -> dict[str, Any]:
    return {
        "target_field": rule.target_field,
        "source_columns": list(rule.source_columns),
        "literal": rule.literal,
        "separator": rule.separator,
        "trim": rule.trim,
        "null_markers": list(rule.null_markers),
        "value_map": [list(pair) for pair in rule.value_map],
        "parser": rule.parser.value,
        "date_style": rule.date_style.value if rule.date_style else None,
        "decimal_style": rule.decimal_style.value if rule.decimal_style else None,
        "sign_rule": rule.sign_rule.value,
        "currency_aliases": [list(pair) for pair in rule.currency_aliases],
    }


def _bounded_mapping_text(value: str, field_name: str, limit: int) -> str:
    cleaned = _clean_text(value, field_name)
    if len(cleaned) > limit:
        raise MappingValidationError(f"{field_name} is too long")
    return cleaned


def _mapping_contract_from_record(record: SchemaMapping) -> MappingContract:
    rules = record.field_rules.get("rules") if isinstance(record.field_rules, dict) else None
    if not isinstance(rules, list):
        raise MappingConflictError(
            "mapping_digest_mismatch",
            "The persisted mapping rule payload is malformed.",
            detail={"mapping_id": str(record.id)},
        )
    try:
        field_rules = tuple(
            FieldRule(
                target_field=str(item["target_field"]),
                source_columns=tuple(str(value) for value in item["source_columns"]),
                literal=item.get("literal"),
                separator=str(item.get("separator", " ")),
                trim=bool(item.get("trim", True)),
                null_markers=tuple(str(value) for value in item.get("null_markers", [])),
                value_map=tuple((str(pair[0]), str(pair[1])) for pair in item.get("value_map", [])),
                parser=FieldParser(item.get("parser", FieldParser.RAW.value)),
                date_style=(
                    DateStyle(item["date_style"]) if item.get("date_style") is not None else None
                ),
                decimal_style=(
                    DecimalStyle(item["decimal_style"])
                    if item.get("decimal_style") is not None
                    else None
                ),
                sign_rule=SignRule(item.get("sign_rule", SignRule.PRESERVE.value)),
                currency_aliases=tuple(
                    (str(pair[0]), str(pair[1])) for pair in item.get("currency_aliases", [])
                ),
            )
            for item in rules
        )
        return MappingContract(
            table_locator=record.table_locator,
            record_kind=RecordKind(record.record_kind),
            financial_subtype=(
                FinancialSubtype(record.financial_subtype)
                if record.financial_subtype is not None
                else None
            ),
            schema_fingerprint=record.schema_fingerprint,
            field_rules=field_rules,
            required_fields=tuple(str(value) for value in record.required_fields),
            mapping_version=record.mapping_version,
            mapping_id=record.id,
        )
    except (KeyError, TypeError, ValueError, MappingValidationError) as error:
        raise MappingConflictError(
            "mapping_digest_mismatch",
            "The persisted mapping rule payload is malformed.",
            detail={"mapping_id": str(record.id)},
        ) from error


def _reconciliation_digest(application: MappingApplication) -> str:
    return _ledger_digest(application.ledger)


def _ledger_digest(ledger: CompositionLedger) -> str:
    return canonical_digest(
        {
            "counts": ledger.reconciliation_counts,
            "decisions": [
                {
                    "unit_id": decision.unit_id,
                    "locator": decision.locator,
                    "content_digest": decision.content_digest,
                    "disposition": decision.disposition.value,
                    "candidate_key": decision.candidate_key,
                    "reason": decision.reason,
                }
                for decision in ledger.decisions
            ],
        }
    )


def _candidate_reconciliation_digest(
    candidates: tuple[CandidateDraft, ...], ledger: CompositionLedger
) -> str:
    return canonical_digest(
        {
            "ledger_digest": _ledger_digest(ledger),
            "candidates": [
                {
                    "candidate_ordinal": draft.candidate_ordinal,
                    "candidate_key": draft.candidate_key,
                    "record_kind": draft.record_kind.value,
                    "financial_subtype": (
                        draft.financial_subtype.value if draft.financial_subtype else None
                    ),
                    "payload": dict(draft.payload),
                    "confidences": dict(draft.confidences),
                    "source_locator": draft.source_locator,
                    "row_fingerprint": draft.row_fingerprint,
                    "validation_issues": list(draft.validation_issues),
                    "evidence_group_keys": list(draft.evidence_group_keys),
                }
                for draft in candidates
            ],
        }
    )


def _batch_summary(batch: ExtractionBatch, *, replayed: bool) -> ExtractionBatchSummary:
    return ExtractionBatchSummary(
        id=batch.id,
        document_id=batch.document_id,
        source_intake_id=batch.source_intake_id,
        source_file_id=batch.source_file_id,
        source_version=batch.source_version,
        source_sha256=batch.source_sha256,
        normalized_sha256=batch.normalized_sha256,
        structure_fingerprint=batch.structure_fingerprint,
        mapping_set_id=batch.mapping_set_id,
        mapping_set_version=batch.mapping_set_version,
        mapping_set_digest=batch.mapping_set_digest,
        lifecycle=batch.lifecycle,
        candidate_count=batch.candidate_count,
        reconciliation_counts=dict(batch.reconciliation_counts),
        reconciliation_digest=batch.reconciliation_digest,
        version=batch.version,
        replayed=replayed,
    )


_MAX_REVIEW_DECISIONS = 100
_MAX_REVIEW_PAGE = 100
_MAX_REVIEW_REQUEST_BYTES = 262_144
_MAX_CORRECTED_PAYLOAD_BYTES = 65_536
_MAX_CORRECTION_DEPTH = 12
_MAX_CORRECTION_STRING = 8_192
_RECURRING_PREVIEW_VERSION = "recurring-normalizer-v1"


@dataclass(slots=True)
class _CandidateActivation:
    candidate: ExtractedRecord
    decision: CandidateReviewDecision | None
    verified_values: dict[str, Any] | None
    effective_subtype: FinancialSubtype | None
    recurring_projection: Any | None
    recurring_prior: RecurringBill | None
    correction_sha256: str
    reason_sha256: str
    search_manifest_sha256: str
    recurring_preview_sha256: str
    errors: list[dict[str, Any]]


@dataclass(slots=True)
class _ReviewActivationState:
    batch: ExtractionBatch
    document: Document
    intake: SourceIntake | None
    current_source: DocumentFile | None
    prior_active: ExtractionBatch | None
    prior_approved: list[ExtractedRecord]
    prior_bills: list[RecurringBill]
    candidates: list[ExtractedRecord]
    latest_decisions: dict[UUID, CandidateReviewDecision]
    candidate_states: list[_CandidateActivation]
    preview: ReviewActivationPreview


class ReviewBatchRepo:
    """Stage immutable candidate decisions and atomically switch one source cohort."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_batches(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        lifecycle: BatchLifecycle | str | None = None,
    ) -> tuple[list[ReviewBatchSummarySnapshot], int]:
        """Return one stable server page without loading every batch candidate."""

        _validate_review_page(limit, offset)
        resolved_lifecycle = _normalize_batch_lifecycle(lifecycle)
        filters = (
            (ExtractionBatch.lifecycle == resolved_lifecycle,)
            if resolved_lifecycle is not None
            else ()
        )
        total = int(
            await self.session.scalar(
                select(func.count()).select_from(ExtractionBatch).where(*filters)
            )
            or 0
        )
        batches = list(
            (
                await self.session.scalars(
                    select(ExtractionBatch)
                    .where(*filters)
                    .order_by(
                        case(
                            (ExtractionBatch.lifecycle == BatchLifecycle.READY_TO_ACTIVATE, 0),
                            (ExtractionBatch.lifecycle == BatchLifecycle.OPEN, 1),
                            else_=2,
                        ),
                        ExtractionBatch.created_at.asc(),
                        ExtractionBatch.id.asc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        if not batches:
            return [], total

        batch_ids = [batch.id for batch in batches]
        candidate_rows = (
            await self.session.execute(
                select(
                    ExtractedRecord.batch_id,
                    func.count(ExtractedRecord.id),
                    func.sum(
                        case(
                            (ExtractedRecord.validation_issues == [], 0),
                            else_=1,
                        )
                    ),
                )
                .where(ExtractedRecord.batch_id.in_(batch_ids))
                .group_by(ExtractedRecord.batch_id)
            )
        ).all()
        candidates_by_batch = {
            row[0]: (int(row[1] or 0), int(row[2] or 0)) for row in candidate_rows
        }
        decision_counts = await self._latest_decision_counts(batch_ids)
        snapshots: list[ReviewBatchSummarySnapshot] = []
        for batch in batches:
            actual_count, exception_count = candidates_by_batch.get(batch.id, (0, 0))
            included, excluded = decision_counts.get(batch.id, (0, 0))
            snapshots.append(
                ReviewBatchSummarySnapshot(
                    id=batch.id,
                    document_id=batch.document_id,
                    source_intake_id=batch.source_intake_id,
                    source_file_id=batch.source_file_id,
                    source_version=batch.source_version,
                    lifecycle=batch.lifecycle,
                    version=batch.version,
                    candidate_count=batch.candidate_count,
                    pending_count=max(0, actual_count - included - excluded),
                    included_count=included,
                    excluded_count=excluded,
                    error_count=exception_count,
                    exception_count=exception_count,
                    reconciliation_counts=dict(batch.reconciliation_counts),
                    reconciliation_digest=batch.reconciliation_digest,
                    created_at=batch.created_at,
                    updated_at=batch.updated_at,
                )
            )
        return snapshots, total

    async def list_candidates(
        self,
        batch_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        exceptions_only: bool = False,
    ) -> ReviewCandidatePage:
        """Return a bounded candidate page plus once-per-source duplicate evidence."""

        _validate_review_page(limit, offset)
        batch = await self.session.get(ExtractionBatch, batch_id)
        if batch is None:
            raise ReviewBatchNotFoundError(str(batch_id))
        candidate_filters = [ExtractedRecord.batch_id == batch_id]
        candidate_duplicate_exists = (
            select(DuplicateFlag.id)
            .where(
                DuplicateFlag.batch_id == batch_id,
                DuplicateFlag.extraction_id == ExtractedRecord.id,
            )
            .exists()
        )
        candidate_has_exception = or_(
            ExtractedRecord.validation_issues != [],
            ExtractedRecord.evidence_group_keys != [],
            candidate_duplicate_exists,
        )
        if exceptions_only:
            candidate_filters.append(candidate_has_exception)
        total = int(
            await self.session.scalar(
                select(func.count()).select_from(ExtractedRecord).where(*candidate_filters)
            )
            or 0
        )
        candidates = list(
            (
                await self.session.scalars(
                    select(ExtractedRecord)
                    .where(*candidate_filters)
                    .order_by(
                        case((candidate_has_exception, 0), else_=1).asc(),
                        ExtractedRecord.candidate_ordinal.asc(),
                        ExtractedRecord.id.asc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        candidate_ids = [candidate.id for candidate in candidates]
        latest = await self._latest_decisions(batch_id, candidate_ids)
        scoped_flags = list(
            (
                await self.session.scalars(
                    select(DuplicateFlag)
                    .where(
                        DuplicateFlag.batch_id == batch_id,
                        DuplicateFlag.extraction_id.in_(candidate_ids)
                        if candidate_ids
                        else text("0 = 1"),
                    )
                    .order_by(
                        DuplicateFlag.extraction_id.asc(),
                        DuplicateFlag.score.desc(),
                        DuplicateFlag.created_at.asc(),
                        DuplicateFlag.id.asc(),
                    )
                )
            ).all()
        )
        flags_by_candidate: dict[UUID, list[dict[str, Any]]] = {}
        for flag in scoped_flags:
            if flag.extraction_id is not None:
                flags_by_candidate.setdefault(flag.extraction_id, []).append(
                    _duplicate_evidence_dict(flag)
                )
        source_flags = list(
            (
                await self.session.scalars(
                    select(DuplicateFlag)
                    .where(
                        DuplicateFlag.document_id == batch.document_id,
                        DuplicateFlag.batch_id.is_(None),
                        or_(
                            and_(
                                DuplicateFlag.source_file_id.is_(None),
                                DuplicateFlag.source_version.is_(None),
                            ),
                            and_(
                                DuplicateFlag.source_file_id == batch.source_file_id,
                                DuplicateFlag.source_version == batch.source_version,
                            ),
                        ),
                    )
                    .order_by(
                        DuplicateFlag.score.desc(),
                        DuplicateFlag.created_at.asc(),
                        DuplicateFlag.id.asc(),
                    )
                )
            ).all()
        )
        return ReviewCandidatePage(
            batch_id=batch.id,
            batch_version=batch.version,
            total=total,
            limit=limit,
            offset=offset,
            items=tuple(
                ReviewCandidateSnapshot(
                    extraction_id=candidate.id,
                    batch_id=batch.id,
                    candidate_ordinal=int(candidate.candidate_ordinal or 0),
                    candidate_key=candidate.candidate_key or "",
                    record_kind=candidate.record_kind or RecordKind.GENERIC_DOCUMENT,
                    financial_subtype=candidate.financial_subtype,
                    source_locator=candidate.source_locator or "",
                    row_fingerprint=candidate.row_fingerprint,
                    version=candidate.version,
                    status=candidate.status,
                    payload=dict(candidate.payload),
                    field_confidences=dict(candidate.field_confidences),
                    source_spans=dict(candidate.source_spans),
                    validation_issues=tuple(candidate.validation_issues or ()),
                    evidence_group_keys=tuple(candidate.evidence_group_keys or ()),
                    latest_decision=(
                        _decision_revision(latest[candidate.id]) if candidate.id in latest else None
                    ),
                    duplicate_evidence=tuple(flags_by_candidate.get(candidate.id, ())),
                )
                for candidate in candidates
            ),
            source_duplicate_evidence=tuple(
                _duplicate_evidence_dict(flag) for flag in source_flags
            ),
        )

    async def apply_decisions(
        self,
        batch_id: UUID,
        expected_batch_version: int,
        decisions: Sequence[ReviewDecisionDraft | Mapping[str, Any] | Any],
        actor: str,
    ) -> ReviewDecisionBatchResult:
        """Append at most 100 revisions after validating the complete request."""

        cleaned_actor = _bounded_review_text(actor, "actor", 255)
        if (
            isinstance(expected_batch_version, bool)
            or not isinstance(expected_batch_version, int)
            or expected_batch_version < 1
        ):
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "expected_batch_version must be greater than zero",
            )
        if isinstance(decisions, (str, bytes)) or not isinstance(decisions, Sequence):
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "decisions must be one bounded sequence",
            )
        if not 1 <= len(decisions) <= _MAX_REVIEW_DECISIONS:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                f"decisions must contain between 1 and {_MAX_REVIEW_DECISIONS} items",
            )
        normalized = tuple(_normalize_review_decision(item) for item in decisions)
        request_evidence = {
            "expected_batch_version": expected_batch_version,
            "actor": cleaned_actor,
            "decisions": [
                {
                    "extraction_id": str(decision.extraction_id),
                    "expected_extraction_version": decision.expected_extraction_version,
                    "expected_decision_revision": decision.expected_decision_revision,
                    "action": CandidateDecisionAction(decision.action).value,
                    "corrections": decision.corrected_payload,
                    "corrected_financial_subtype": (
                        FinancialSubtype(decision.corrected_financial_subtype).value
                        if decision.corrected_financial_subtype is not None
                        else None
                    ),
                    "exclusion_reason": decision.exclusion_reason,
                }
                for decision in normalized
            ],
        }
        if len(canonical_json(request_evidence).encode("utf-8")) > _MAX_REVIEW_REQUEST_BYTES:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "decision request exceeds 262144 encoded bytes",
            )
        candidate_ids = [decision.extraction_id for decision in normalized]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "one request cannot decide the same extraction more than once",
                detail={"extraction_ids": [str(value) for value in candidate_ids]},
            )

        await _ensure_sqlite_outer_write_transaction(self.session)
        batch = await self.session.scalar(
            select(ExtractionBatch)
            .where(ExtractionBatch.id == batch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch is None:
            raise ReviewBatchNotFoundError(str(batch_id))
        self._require_mutable_batch(
            batch,
            expected_batch_version=expected_batch_version,
            affected_ids=candidate_ids,
        )
        candidates = list(
            (
                await self.session.scalars(
                    select(ExtractedRecord)
                    .where(
                        ExtractedRecord.batch_id == batch_id,
                        ExtractedRecord.id.in_(candidate_ids),
                    )
                    .order_by(ExtractedRecord.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        candidates_by_id = {candidate.id: candidate for candidate in candidates}
        missing = [value for value in candidate_ids if value not in candidates_by_id]
        if missing:
            raise ReviewBatchConflictError(
                "One or more candidates no longer belong to this batch.",
                detail=_stale_batch_detail(
                    batch,
                    expected_version=expected_batch_version,
                    affected_ids=missing,
                ),
            )
        latest = await self._latest_decisions(batch_id, candidate_ids, for_update=True)
        revisions: list[CandidateReviewDecision] = []
        for decision in normalized:
            candidate = candidates_by_id[decision.extraction_id]
            prior = latest.get(candidate.id)
            current_revision = prior.decision_revision if prior is not None else 0
            if (
                candidate.status is not ExtractionStatus.PENDING_REVIEW
                or candidate.version != decision.expected_extraction_version
                or current_revision != decision.expected_decision_revision
            ):
                raise ReviewBatchConflictError(
                    "A candidate or decision revision changed after it was displayed.",
                    detail=_stale_batch_detail(
                        batch,
                        expected_version=expected_batch_version,
                        affected_ids=[candidate.id],
                        current_decision_revisions={candidate.id: current_revision},
                    ),
                )
            _validate_review_decision(candidate, decision)
            revision_values: dict[str, Any] = {
                "id": uuid4(),
                "batch_id": batch.id,
                "extraction_id": candidate.id,
                "decision_revision": current_revision + 1,
                "expected_extraction_version": candidate.version,
                "supersedes_decision_id": prior.id if prior is not None else None,
                "action": CandidateDecisionAction(decision.action),
                "corrected_financial_subtype": (
                    FinancialSubtype(decision.corrected_financial_subtype)
                    if decision.corrected_financial_subtype is not None
                    else None
                ),
                "exclusion_reason": (
                    decision.exclusion_reason.strip()
                    if decision.exclusion_reason is not None
                    else None
                ),
                "actor": cleaned_actor,
            }
            # Python None is JSON ``null`` for SQLAlchemy's JSON type. Omit this
            # nullable JSON field so the database action-shape guard sees SQL NULL.
            if decision.corrected_payload is not None:
                revision_values["corrected_payload"] = dict(decision.corrected_payload)
            revision = CandidateReviewDecision(**revision_values)
            revisions.append(revision)
            latest[candidate.id] = revision

        actual_candidate_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(ExtractedRecord)
                .where(ExtractedRecord.batch_id == batch.id)
            )
            or 0
        )
        latest_decision_count = int(
            await self.session.scalar(
                select(func.count(func.distinct(CandidateReviewDecision.extraction_id))).where(
                    CandidateReviewDecision.batch_id == batch.id
                )
            )
            or 0
        )
        newly_decided = sum(1 for revision in revisions if revision.decision_revision == 1)
        ready = (
            actual_candidate_count == batch.candidate_count
            and latest_decision_count + newly_decided == batch.candidate_count
        )
        previous_version = batch.version
        write_conflict_detail = _stale_batch_detail(
            batch,
            expected_version=expected_batch_version,
            affected_ids=candidate_ids,
        )
        try:
            async with self.session.begin_nested():
                async with audit_actor(self.session, cleaned_actor):
                    self.session.add_all(revisions)
                    await self.session.flush()
                    batch.lifecycle = (
                        BatchLifecycle.READY_TO_ACTIVATE if ready else BatchLifecycle.OPEN
                    )
                    batch.version += 1
                    batch.updated_at = dt.datetime.now(dt.UTC)
                    await self.session.flush()
        except IntegrityError as error:
            raise ReviewBatchConflictError(
                "The batch changed while its decisions were being appended.",
                detail=write_conflict_detail,
            ) from error
        return ReviewDecisionBatchResult(
            batch_id=batch.id,
            previous_batch_version=previous_version,
            batch_version=batch.version,
            lifecycle=batch.lifecycle,
            decisions=tuple(_decision_revision(revision) for revision in revisions),
        )

    async def _latest_decision_counts(
        self, batch_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[int, int]]:
        latest_revisions = (
            select(
                CandidateReviewDecision.batch_id.label("batch_id"),
                CandidateReviewDecision.extraction_id.label("extraction_id"),
                func.max(CandidateReviewDecision.decision_revision).label("revision"),
            )
            .where(CandidateReviewDecision.batch_id.in_(batch_ids))
            .group_by(
                CandidateReviewDecision.batch_id,
                CandidateReviewDecision.extraction_id,
            )
            .subquery()
        )
        rows = (
            await self.session.execute(
                select(
                    CandidateReviewDecision.batch_id,
                    CandidateReviewDecision.action,
                    func.count(CandidateReviewDecision.id),
                )
                .join(
                    latest_revisions,
                    and_(
                        latest_revisions.c.batch_id == CandidateReviewDecision.batch_id,
                        latest_revisions.c.extraction_id == CandidateReviewDecision.extraction_id,
                        latest_revisions.c.revision == CandidateReviewDecision.decision_revision,
                    ),
                )
                .group_by(
                    CandidateReviewDecision.batch_id,
                    CandidateReviewDecision.action,
                )
            )
        ).all()
        counts: dict[UUID, list[int]] = {}
        for batch_id, action, count in rows:
            values = counts.setdefault(batch_id, [0, 0])
            values[0 if action is CandidateDecisionAction.INCLUDE else 1] = int(count)
        return {batch_id: (values[0], values[1]) for batch_id, values in counts.items()}

    async def _latest_decisions(
        self,
        batch_id: UUID,
        extraction_ids: Sequence[UUID] | None = None,
        *,
        for_update: bool = False,
    ) -> dict[UUID, CandidateReviewDecision]:
        statement = select(CandidateReviewDecision).where(
            CandidateReviewDecision.batch_id == batch_id
        )
        if extraction_ids is not None:
            if not extraction_ids:
                return {}
            statement = statement.where(CandidateReviewDecision.extraction_id.in_(extraction_ids))
        statement = statement.order_by(
            CandidateReviewDecision.extraction_id.asc(),
            CandidateReviewDecision.decision_revision.desc(),
            CandidateReviewDecision.id.desc(),
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        rows = list((await self.session.scalars(statement)).all())
        latest: dict[UUID, CandidateReviewDecision] = {}
        for row in rows:
            latest.setdefault(row.extraction_id, row)
        return latest

    @staticmethod
    def _require_mutable_batch(
        batch: ExtractionBatch,
        *,
        expected_batch_version: int,
        affected_ids: Sequence[UUID],
    ) -> None:
        if batch.version != expected_batch_version or batch.lifecycle not in {
            BatchLifecycle.OPEN,
            BatchLifecycle.READY_TO_ACTIVATE,
        }:
            raise ReviewBatchConflictError(
                "The review batch changed after it was displayed.",
                detail=_stale_batch_detail(
                    batch,
                    expected_version=expected_batch_version,
                    affected_ids=affected_ids,
                ),
            )

    async def activation_preview(self, batch_id: UUID) -> ReviewActivationPreview:
        """Hash the complete persisted candidate/decision/projection vector."""

        return (await self._load_activation_state(batch_id, for_update=False)).preview

    async def activate(
        self,
        batch_id: UUID,
        expected_batch_version: int,
        expected_vector_sha256: str,
        actor: str,
        accept_exclusions: bool = False,
        accept_empty: bool = False,
    ) -> ReviewActivationResult:
        """Switch one fully reviewed current-source cohort in one transaction."""

        cleaned_actor = _bounded_review_text(actor, "actor", 255)
        if (
            isinstance(expected_batch_version, bool)
            or not isinstance(expected_batch_version, int)
            or expected_batch_version < 1
        ):
            raise ReviewBatchValidationError(
                "invalid_review_activation",
                "expected_batch_version must be greater than zero",
            )
        try:
            _validate_sha256(expected_vector_sha256)
        except (TypeError, ValueError) as error:
            raise ReviewBatchValidationError(
                "invalid_review_activation",
                "expected_vector_sha256 must be a lowercase SHA-256 value",
            ) from error

        state = await self._load_activation_state(batch_id, for_update=True)
        batch = state.batch
        affected_ids = [candidate.id for candidate in state.candidates]
        self._require_mutable_batch(
            batch,
            expected_batch_version=expected_batch_version,
            affected_ids=affected_ids,
        )
        if not state.preview.source_is_current:
            raise ReviewBatchConflictError(
                "The source changed after the activation preview was displayed.",
                detail=_stale_batch_detail(
                    batch,
                    expected_version=expected_batch_version,
                    affected_ids=affected_ids,
                ),
            )
        if expected_vector_sha256 != state.preview.activation_vector_sha256:
            raise ReviewBatchConflictError(
                "The candidate decision vector changed after it was displayed.",
                detail={
                    **_stale_batch_detail(
                        batch,
                        expected_version=expected_batch_version,
                        affected_ids=affected_ids,
                    ),
                    "expected_vector_sha256": expected_vector_sha256,
                    "current_vector_sha256": state.preview.activation_vector_sha256,
                },
            )
        if state.preview.errors:
            raise ReviewBatchValidationError(
                "invalid_review_reconciliation",
                "The complete candidate cohort is not safe to activate.",
                detail={
                    "batch_id": str(batch.id),
                    "errors": list(state.preview.errors),
                    "pending_count": state.preview.pending_count,
                    "error_count": state.preview.error_count,
                },
            )
        missing_consents: list[str] = []
        if state.preview.requires_accept_exclusions and not accept_exclusions:
            missing_consents.append("accept_exclusions")
        if state.preview.requires_accept_empty and not accept_empty:
            missing_consents.append("accept_empty")
        if missing_consents:
            raise ReviewBatchValidationError(
                "activation_consent_required",
                "Explicit consent is required for this cohort activation.",
                detail={
                    "batch_id": str(batch.id),
                    "required": missing_consents,
                },
            )

        now = dt.datetime.now(dt.UTC)
        included_states = [
            item
            for item in state.candidate_states
            if item.decision is not None and item.decision.action is CandidateDecisionAction.INCLUDE
        ]
        excluded_states = [
            item
            for item in state.candidate_states
            if item.decision is not None and item.decision.action is CandidateDecisionAction.EXCLUDE
        ]
        pre_activation_stale_detail = _stale_batch_detail(
            batch,
            expected_version=expected_batch_version,
            affected_ids=affected_ids,
        )
        verified_by_extraction: dict[UUID, UUID] = {}
        try:
            async with self.session.begin_nested():
                async with audit_actor(self.session, cleaned_actor):
                    for prior in state.prior_approved:
                        prior.status = ExtractionStatus.SUPERSEDED
                        prior.version += 1
                        prior.reviewer = cleaned_actor
                        prior.reviewed_at = now
                    for bill in state.prior_bills:
                        if bill.superseded_at is None:
                            bill.superseded_at = now
                    if state.prior_active is not None:
                        state.prior_active.lifecycle = BatchLifecycle.SUPERSEDED
                        state.prior_active.version += 1
                        state.prior_active.updated_at = now
                    # SQLite enforces the one-active-batch index immediately. Flush the
                    # old cohort first; the enclosing savepoint still makes the switch atomic.
                    await self.session.flush()

                    for item in included_states:
                        item.candidate.status = ExtractionStatus.APPROVED
                        item.candidate.version += 1
                        item.candidate.reviewer = cleaned_actor
                        item.candidate.rejection_reason = None
                        item.candidate.reviewed_at = now
                    for item in excluded_states:
                        assert item.decision is not None
                        item.candidate.status = ExtractionStatus.SUPERSEDED
                        item.candidate.version += 1
                        item.candidate.reviewer = cleaned_actor
                        item.candidate.rejection_reason = item.decision.exclusion_reason
                        item.candidate.reviewed_at = now

                    batch.lifecycle = BatchLifecycle.ACTIVE
                    batch.activation_vector_sha256 = state.preview.activation_vector_sha256
                    batch.activated_by = cleaned_actor
                    batch.activated_at = now
                    batch.activation_included_count = len(included_states)
                    batch.activation_excluded_count = len(excluded_states)
                    batch.accepted_exclusions = bool(excluded_states)
                    batch.accepted_empty = not state.candidates
                    batch.version += 1
                    batch.updated_at = now
                    state.document.status = DocumentStatus.VERIFIED
                    # The SQLite compatibility trigger checks active/approved state at
                    # verified-row INSERT time, so publish the cohort before its rows.
                    await self.session.flush()

                    financial_items = [
                        item
                        for item in included_states
                        if item.candidate.record_kind is RecordKind.FINANCIAL
                    ]
                    verified_rows: list[tuple[_CandidateActivation, VerifiedRecord]] = []
                    for item in financial_items:
                        if item.verified_values is None:
                            raise ReviewBatchValidationError(
                                "invalid_review_reconciliation",
                                "An included financial candidate has no verified projection.",
                                detail={"extraction_id": str(item.candidate.id)},
                            )
                        verified = VerifiedRecord(
                            document_id=batch.document_id,
                            extracted_id=item.candidate.id,
                            reviewer=cleaned_actor,
                            **item.verified_values,
                        )
                        self.session.add(verified)
                        verified_rows.append((item, verified))
                    await self.session.flush()
                    for item, verified in verified_rows:
                        verified_by_extraction[item.candidate.id] = verified.id
                        if item.recurring_projection is None:
                            continue
                        projection = item.recurring_projection
                        corrections = (
                            item.decision.corrected_payload
                            if item.decision is not None
                            and item.decision.corrected_payload is not None
                            else {}
                        )
                        bill = await record_verified_bill(
                            self.session,
                            verified_record_id=verified.id,
                            issuer_name=projection.issuer_name,
                            issuer_kind=projection.issuer_kind,
                            billing_period=projection.billing_period,
                            due_date=projection.due_date,
                            consumption_value=projection.consumption_value,
                            consumption_unit=projection.consumption_unit,
                            reviewer=cleaned_actor,
                            review_corrections={
                                field: True
                                for field in bill_correction_fields()
                                if field in corrections
                            },
                        )
                        if item.recurring_prior is not None:
                            bill.payment_status = item.recurring_prior.payment_status
                            bill.paid_at = item.recurring_prior.paid_at
                    await self.session.flush()
        except (BillConflictError, BillValidationError) as error:
            raise ReviewBatchValidationError(
                "recurring_projection_conflict",
                str(error),
                detail={"batch_id": str(batch_id)},
            ) from error
        except IntegrityError as error:
            raise ReviewBatchConflictError(
                "The batch changed while its cohort was being activated.",
                detail=pre_activation_stale_detail,
            ) from error
        return ReviewActivationResult(
            batch_id=batch.id,
            document_id=batch.document_id,
            batch_version=batch.version,
            lifecycle=batch.lifecycle,
            activation_vector_sha256=state.preview.activation_vector_sha256,
            included_count=len(included_states),
            excluded_count=len(excluded_states),
            accepted_exclusions=bool(excluded_states),
            accepted_empty=not state.candidates,
            verified_by_extraction=verified_by_extraction,
        )

    async def reject_batch(
        self,
        batch_id: UUID,
        *,
        expected_batch_version: int,
        actor: str,
        reason: str,
    ) -> ReviewBatchRejectionResult:
        """Reject all pending members without disturbing an older active authority."""

        cleaned_actor = _bounded_review_text(actor, "actor", 255)
        cleaned_reason = _bounded_review_text(reason, "reason", 2_048)
        if (
            isinstance(expected_batch_version, bool)
            or not isinstance(expected_batch_version, int)
            or expected_batch_version < 1
        ):
            raise ReviewBatchValidationError(
                "invalid_review_rejection",
                "expected_batch_version must be greater than zero",
            )
        state = await self._load_activation_state(batch_id, for_update=True)
        self._require_mutable_batch(
            state.batch,
            expected_batch_version=expected_batch_version,
            affected_ids=[candidate.id for candidate in state.candidates],
        )
        if not state.preview.source_is_current:
            raise ReviewBatchConflictError(
                "The source changed before the batch could be rejected.",
                detail=_stale_batch_detail(
                    state.batch,
                    expected_version=expected_batch_version,
                    affected_ids=[candidate.id for candidate in state.candidates],
                ),
            )
        stale = [
            candidate.id
            for candidate in state.candidates
            if candidate.status is not ExtractionStatus.PENDING_REVIEW
        ]
        if stale:
            raise ReviewBatchConflictError(
                "One or more batch candidates are no longer pending review.",
                detail=_stale_batch_detail(
                    state.batch,
                    expected_version=expected_batch_version,
                    affected_ids=stale,
                ),
            )
        now = dt.datetime.now(dt.UTC)
        rejection_conflict_detail = _stale_batch_detail(
            state.batch,
            expected_version=expected_batch_version,
            affected_ids=[candidate.id for candidate in state.candidates],
        )
        try:
            async with self.session.begin_nested():
                async with audit_actor(self.session, cleaned_actor):
                    for candidate in state.candidates:
                        candidate.status = ExtractionStatus.REJECTED
                        candidate.version += 1
                        candidate.reviewer = cleaned_actor
                        candidate.rejection_reason = cleaned_reason
                        candidate.reviewed_at = now
                    state.batch.lifecycle = BatchLifecycle.REJECTED
                    state.batch.version += 1
                    state.batch.updated_at = now
                    state.document.status = (
                        DocumentStatus.VERIFIED
                        if state.prior_active is not None or state.prior_approved
                        else DocumentStatus.NEEDS_REPROCESS
                    )
                    await self.session.flush()
        except IntegrityError as error:
            raise ReviewBatchConflictError(
                "The batch changed while it was being rejected.",
                detail=rejection_conflict_detail,
            ) from error
        return ReviewBatchRejectionResult(
            batch_id=state.batch.id,
            document_id=state.batch.document_id,
            source_intake_id=state.batch.source_intake_id,
            source_file_id=state.batch.source_file_id,
            source_version=state.batch.source_version,
            batch_version=state.batch.version,
            lifecycle=state.batch.lifecycle,
        )

    async def lock_reprocess_intake(
        self,
        batch_id: UUID,
        *,
        expected_batch_version: int,
    ) -> SourceIntake:
        """Lock and validate the exact source before runtime capability planning."""

        if (
            isinstance(expected_batch_version, bool)
            or not isinstance(expected_batch_version, int)
            or expected_batch_version < 1
        ):
            raise ReviewBatchValidationError(
                "invalid_review_rejection",
                "expected_batch_version must be greater than zero",
            )
        await _ensure_sqlite_outer_write_transaction(self.session)
        batch = await self.session.scalar(
            select(ExtractionBatch)
            .where(ExtractionBatch.id == batch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch is None:
            raise ReviewBatchNotFoundError(str(batch_id))
        self._require_mutable_batch(
            batch,
            expected_batch_version=expected_batch_version,
            affected_ids=[],
        )
        intake = await self.session.scalar(
            select(SourceIntake)
            .where(SourceIntake.id == batch.source_intake_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current_source = await self.session.scalar(
            select(DocumentFile)
            .where(
                DocumentFile.document_id == batch.document_id,
                DocumentFile.kind == FileKind.ORIGINAL,
            )
            .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            intake is None
            or current_source is None
            or current_source.id != batch.source_file_id
            or current_source.version != batch.source_version
            or current_source.sha256 != batch.source_sha256
            or intake.document_id != batch.document_id
            or intake.source_file_id != batch.source_file_id
            or intake.source_version != batch.source_version
            or intake.source_sha256 != batch.source_sha256
            or intake.state is not SourceIntakeState.PROCESSED
        ):
            raise ReviewBatchConflictError(
                "The exact source changed before reprocessing could be planned.",
                detail=_stale_batch_detail(
                    batch,
                    expected_version=expected_batch_version,
                    affected_ids=[],
                ),
            )
        return intake

    async def _load_activation_state(
        self,
        batch_id: UUID,
        *,
        for_update: bool,
    ) -> _ReviewActivationState:
        if for_update:
            await _ensure_sqlite_outer_write_transaction(self.session)
            batch = await self.session.scalar(
                select(ExtractionBatch)
                .where(ExtractionBatch.id == batch_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        else:
            batch = await self.session.get(ExtractionBatch, batch_id)
        if batch is None:
            raise ReviewBatchNotFoundError(str(batch_id))

        document_statement = select(Document).where(Document.id == batch.document_id)
        source_statement = (
            select(DocumentFile)
            .where(
                DocumentFile.document_id == batch.document_id,
                DocumentFile.kind == FileKind.ORIGINAL,
            )
            .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
            .limit(1)
        )
        intake_statement = select(SourceIntake).where(SourceIntake.id == batch.source_intake_id)
        if for_update:
            document_statement = document_statement.with_for_update().execution_options(
                populate_existing=True
            )
            source_statement = source_statement.with_for_update().execution_options(
                populate_existing=True
            )
            intake_statement = intake_statement.with_for_update().execution_options(
                populate_existing=True
            )
        document = await self.session.scalar(document_statement)
        current_source = await self.session.scalar(source_statement)
        intake = await self.session.scalar(intake_statement)
        if document is None:
            raise ReviewBatchNotFoundError(str(batch_id))

        prior_statement = select(ExtractionBatch).where(
            ExtractionBatch.document_id == batch.document_id,
            ExtractionBatch.id != batch.id,
            ExtractionBatch.lifecycle == BatchLifecycle.ACTIVE,
        )
        candidate_statement = (
            select(ExtractedRecord)
            .where(ExtractedRecord.batch_id == batch.id)
            .order_by(ExtractedRecord.id.asc())
        )
        if for_update:
            prior_statement = prior_statement.with_for_update().execution_options(
                populate_existing=True
            )
            candidate_statement = candidate_statement.with_for_update().execution_options(
                populate_existing=True
            )
        prior_active = await self.session.scalar(prior_statement)
        candidates = list((await self.session.scalars(candidate_statement)).all())
        latest = await self._latest_decisions(
            batch.id,
            [candidate.id for candidate in candidates],
            for_update=for_update,
        )
        chunk_statement = (
            select(Chunk)
            .where(Chunk.batch_id == batch.id)
            .order_by(Chunk.extraction_id.asc(), Chunk.seq.asc(), Chunk.id.asc())
        )
        if for_update:
            chunk_statement = chunk_statement.with_for_update().execution_options(
                populate_existing=True
            )
        chunks = list((await self.session.scalars(chunk_statement)).all())
        chunks_by_candidate: dict[UUID, list[Chunk]] = {}
        for chunk in chunks:
            if chunk.extraction_id is not None:
                chunks_by_candidate.setdefault(chunk.extraction_id, []).append(chunk)

        legacy_candidate_statement = select(ExtractedRecord).where(
            ExtractedRecord.document_id == batch.document_id,
            ExtractedRecord.batch_id.is_(None),
            ExtractedRecord.status == ExtractionStatus.APPROVED,
        )
        if for_update:
            legacy_candidate_statement = (
                legacy_candidate_statement.with_for_update().execution_options(
                    populate_existing=True
                )
            )
        prior_approved = list((await self.session.scalars(legacy_candidate_statement)).all())
        if prior_active is not None:
            prior_candidate_statement = select(ExtractedRecord).where(
                ExtractedRecord.batch_id == prior_active.id,
                ExtractedRecord.status == ExtractionStatus.APPROVED,
            )
            if for_update:
                prior_candidate_statement = (
                    prior_candidate_statement.with_for_update().execution_options(
                        populate_existing=True
                    )
                )
            prior_approved.extend((await self.session.scalars(prior_candidate_statement)).all())
        prior_ids = [candidate.id for candidate in prior_approved]
        prior_bills: list[RecurringBill] = []
        if prior_ids:
            prior_bill_statement = (
                select(RecurringBill)
                .join(
                    VerifiedRecord,
                    RecurringBill.verified_record_id == VerifiedRecord.id,
                )
                .where(
                    VerifiedRecord.extracted_id.in_(prior_ids),
                    RecurringBill.superseded_at.is_(None),
                )
            )
            if for_update:
                prior_bill_statement = prior_bill_statement.with_for_update().execution_options(
                    populate_existing=True
                )
            prior_bills = list((await self.session.scalars(prior_bill_statement)).all())

        candidate_states = [
            _candidate_activation(
                candidate,
                latest.get(candidate.id),
                chunks_by_candidate.get(candidate.id, ()),
            )
            for candidate in candidates
        ]
        await self._attach_recurring_conflicts(
            candidate_states,
            document_id=batch.document_id,
            prior_approved_ids={candidate.id for candidate in prior_approved},
            for_update=for_update,
        )
        source_is_current = bool(
            current_source is not None
            and intake is not None
            and current_source.id == batch.source_file_id
            and current_source.version == batch.source_version
            and current_source.sha256 == batch.source_sha256
            and intake.document_id == batch.document_id
            and intake.source_file_id == batch.source_file_id
            and intake.source_version == batch.source_version
            and intake.source_sha256 == batch.source_sha256
            and intake.state is SourceIntakeState.PROCESSED
        )
        preview = _activation_preview_from_state(
            batch,
            candidates,
            candidate_states,
            prior_active=prior_active,
            prior_approved=prior_approved,
            source_is_current=source_is_current,
        )
        return _ReviewActivationState(
            batch=batch,
            document=document,
            intake=intake,
            current_source=current_source,
            prior_active=prior_active,
            prior_approved=prior_approved,
            prior_bills=prior_bills,
            candidates=candidates,
            latest_decisions=latest,
            candidate_states=candidate_states,
            preview=preview,
        )

    async def _attach_recurring_conflicts(
        self,
        candidate_states: Sequence[_CandidateActivation],
        *,
        document_id: UUID,
        prior_approved_ids: set[UUID],
        for_update: bool,
    ) -> None:
        recurring = [
            item
            for item in candidate_states
            if item.recurring_projection is not None and not item.errors
        ]
        if not recurring:
            return
        by_key: dict[tuple[str, dt.date], list[_CandidateActivation]] = {}
        for item in recurring:
            projection = item.recurring_projection
            by_key.setdefault((projection.issuer_name, projection.billing_period), []).append(item)
        for key, items in by_key.items():
            if len(items) > 1:
                for item in items:
                    item.errors.append(
                        _candidate_error(
                            item.candidate.id,
                            "recurring_projection_conflict",
                            f"more than one candidate claims {key[0]!r} for {key[1]}",
                        )
                    )

        issuer_names = sorted({key[0] for key in by_key})
        issuer_statement = select(Issuer).where(Issuer.name.in_(issuer_names))
        if for_update:
            issuer_statement = issuer_statement.with_for_update().execution_options(
                populate_existing=True
            )
        issuers = list((await self.session.scalars(issuer_statement)).all())
        issuers_by_name = {issuer.name: issuer for issuer in issuers}
        if not issuers:
            return
        bill_statement = select(RecurringBill).where(
            RecurringBill.issuer_id.in_([issuer.id for issuer in issuers]),
            RecurringBill.superseded_at.is_(None),
        )
        if for_update:
            bill_statement = bill_statement.with_for_update().execution_options(
                populate_existing=True
            )
        bills = list((await self.session.scalars(bill_statement)).all())
        verified_ids = [bill.verified_record_id for bill in bills]
        verified = (
            {
                row.id: row
                for row in (
                    await self.session.scalars(
                        select(VerifiedRecord).where(VerifiedRecord.id.in_(verified_ids))
                    )
                ).all()
            }
            if verified_ids
            else {}
        )
        owner_by_key = {
            (issuer.name, bill.billing_period): bill
            for bill in bills
            if (issuer := next((row for row in issuers if row.id == bill.issuer_id), None))
            is not None
        }
        for item in recurring:
            projection = item.recurring_projection
            issuer = issuers_by_name.get(projection.issuer_name)
            if issuer is not None and issuer.kind != projection.issuer_kind:
                item.errors.append(
                    _candidate_error(
                        item.candidate.id,
                        "recurring_projection_conflict",
                        "the issuer name is already bound to another issuer kind",
                    )
                )
                continue
            owner = owner_by_key.get((projection.issuer_name, projection.billing_period))
            if owner is None:
                continue
            source = verified.get(owner.verified_record_id)
            if (
                owner.document_id != document_id
                or source is None
                or source.extracted_id not in prior_approved_ids
            ):
                item.errors.append(
                    _candidate_error(
                        item.candidate.id,
                        "recurring_projection_conflict",
                        "the issuer and billing period already belong to another active source",
                    )
                )
            else:
                item.recurring_prior = owner


def _validate_review_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= _MAX_REVIEW_PAGE:
        raise ValueError(f"limit must be between 1 and {_MAX_REVIEW_PAGE}")
    if offset < 0:
        raise ValueError("offset must not be negative")


def _normalize_batch_lifecycle(
    lifecycle: BatchLifecycle | str | None,
) -> BatchLifecycle | None:
    if lifecycle is None or isinstance(lifecycle, BatchLifecycle):
        return lifecycle
    try:
        return BatchLifecycle(lifecycle)
    except ValueError as error:
        raise ValueError(f"unsupported batch lifecycle: {lifecycle!r}") from error


def _bounded_review_text(value: Any, field_name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            f"{field_name} must be text",
        )
    cleaned = value.strip()
    if not cleaned:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            f"{field_name} must not be blank",
        )
    if "\x00" in cleaned:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            f"{field_name} must not contain null characters",
        )
    if len(cleaned) > limit:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            f"{field_name} is too long",
        )
    return cleaned


def _normalize_review_decision(value: Any) -> ReviewDecisionDraft:
    if isinstance(value, ReviewDecisionDraft):
        decision = value
    else:
        if not isinstance(value, Mapping):
            dumper = getattr(value, "model_dump", None)
            if dumper is None:
                raise ReviewBatchValidationError(
                    "invalid_review_decision",
                    "each decision must be an object",
                )
            value = dumper(mode="python")
        allowed = {
            "extraction_id",
            "expected_extraction_version",
            "expected_decision_revision",
            "action",
            "corrected_payload",
            "corrections",
            "corrected_financial_subtype",
            "exclusion_reason",
        }
        unexpected = set(value) - allowed
        if unexpected:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "unsupported decision fields: " + ", ".join(sorted(unexpected)),
            )
        if "corrected_payload" in value and "corrections" in value:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "choose corrected_payload or corrections, not both",
            )
        try:
            extraction_value = value["extraction_id"]
            extraction_id = (
                extraction_value
                if isinstance(extraction_value, UUID)
                else UUID(str(extraction_value))
            )
            expected_extraction_version = value["expected_extraction_version"]
            expected_decision_revision = value["expected_decision_revision"]
            if (
                isinstance(expected_extraction_version, bool)
                or not isinstance(expected_extraction_version, int)
                or isinstance(expected_decision_revision, bool)
                or not isinstance(expected_decision_revision, int)
            ):
                raise TypeError
            decision = ReviewDecisionDraft(
                extraction_id=extraction_id,
                expected_extraction_version=expected_extraction_version,
                expected_decision_revision=expected_decision_revision,
                action=value["action"],
                corrected_payload=value.get("corrected_payload", value.get("corrections")),
                corrected_financial_subtype=value.get("corrected_financial_subtype"),
                exclusion_reason=value.get("exclusion_reason"),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "decision identity and optimistic versions are required",
            ) from error
    if decision.expected_extraction_version < 1:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "expected_extraction_version must be greater than zero",
        )
    if decision.expected_decision_revision < 0:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "expected_decision_revision must not be negative",
        )
    try:
        CandidateDecisionAction(decision.action)
    except (TypeError, ValueError) as error:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "decision action must be include or exclude",
        ) from error
    if decision.corrected_financial_subtype is not None:
        try:
            FinancialSubtype(decision.corrected_financial_subtype)
        except (TypeError, ValueError) as error:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "corrected_financial_subtype is unsupported",
            ) from error
    if decision.corrected_payload is not None:
        if not isinstance(decision.corrected_payload, dict):
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "corrected_payload must be one JSON object",
            )
        _validate_correction_tree(decision.corrected_payload)
        try:
            encoded = canonical_json(decision.corrected_payload).encode("utf-8")
        except ValueError as error:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "corrected_payload must contain finite JSON values",
            ) from error
        if len(encoded) > _MAX_CORRECTED_PAYLOAD_BYTES:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "corrected_payload exceeds 65536 encoded bytes",
            )
    if decision.exclusion_reason is not None:
        _bounded_review_text(decision.exclusion_reason, "exclusion_reason", 2_048)
    return decision


def _validate_correction_tree(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_CORRECTION_DEPTH:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "corrected_payload nesting is too deep",
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 255 or "\x00" in key:
                raise ReviewBatchValidationError(
                    "invalid_review_decision",
                    "corrected_payload keys must be bounded nonblank strings",
                )
            _validate_correction_tree(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 1_000:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "corrected_payload arrays are too large",
            )
        for child in value:
            _validate_correction_tree(child, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > _MAX_CORRECTION_STRING:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "corrected_payload contains an oversized string",
            )
        if "\x00" in value:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "corrected_payload contains an unsupported null character",
            )
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "corrected_payload contains a non-JSON value",
        )


def _validate_review_decision(
    candidate: ExtractedRecord,
    decision: ReviewDecisionDraft,
) -> None:
    action = CandidateDecisionAction(decision.action)
    corrections = decision.corrected_payload
    if action is CandidateDecisionAction.EXCLUDE:
        if corrections is not None or decision.corrected_financial_subtype is not None:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "excluded candidates cannot carry corrections or a financial subtype",
                detail={"extraction_id": str(candidate.id)},
            )
        if decision.exclusion_reason is None or not decision.exclusion_reason.strip():
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "excluded candidates require a nonblank reason",
                detail={"extraction_id": str(candidate.id)},
            )
        return
    if decision.exclusion_reason is not None:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "included candidates cannot carry an exclusion reason",
            detail={"extraction_id": str(candidate.id)},
        )
    if candidate.record_kind is RecordKind.GENERIC_DOCUMENT:
        if corrections is not None or decision.corrected_financial_subtype is not None:
            raise ReviewBatchValidationError(
                "invalid_review_decision",
                "generic candidates cannot carry financial corrections",
                detail={"extraction_id": str(candidate.id)},
            )
        return
    if candidate.record_kind is not RecordKind.FINANCIAL:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "candidate record kind is unavailable",
            detail={"extraction_id": str(candidate.id)},
        )
    normalized_corrections = dict(corrections or {})
    allowed = VerifiedRepo._CORRECTION_FIELDS | bill_correction_fields()
    unexpected = set(normalized_corrections) - allowed
    if unexpected:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "unsupported correction fields: " + ", ".join(sorted(unexpected)),
            detail={"extraction_id": str(candidate.id)},
        )
    effective_subtype = (
        FinancialSubtype(decision.corrected_financial_subtype)
        if decision.corrected_financial_subtype is not None
        else candidate.financial_subtype
    )
    if effective_subtype is None:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "financial candidates require an effective subtype",
            detail={"extraction_id": str(candidate.id)},
        )
    recurring_only = set(normalized_corrections).intersection(bill_projection_correction_fields())
    if effective_subtype is not FinancialSubtype.RECURRING_BILL and recurring_only:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            "recurring-bill corrections require the recurring_bill subtype",
            detail={"extraction_id": str(candidate.id)},
        )
    try:
        verified_values = _verified_values(
            candidate.payload,
            {
                key: value
                for key, value in normalized_corrections.items()
                if key not in bill_projection_correction_fields()
            },
        )
        if effective_subtype is FinancialSubtype.RECURRING_BILL:
            if verified_values["total_amount"] <= 0:
                raise ValueError("recurring-bill total_amount must be greater than zero")
            normalize_recurring_bill_payload(candidate.payload, normalized_corrections)
    except (ValueError, RecurringBillNormalizationError) as error:
        raise ReviewBatchValidationError(
            "invalid_review_decision",
            str(error),
            detail={"extraction_id": str(candidate.id)},
        ) from error


def _decision_revision(decision: CandidateReviewDecision) -> ReviewDecisionRevision:
    return ReviewDecisionRevision(
        id=decision.id,
        extraction_id=decision.extraction_id,
        decision_revision=decision.decision_revision,
        action=decision.action,
        expected_extraction_version=decision.expected_extraction_version,
        corrected_payload=(
            dict(decision.corrected_payload) if decision.corrected_payload is not None else None
        ),
        corrected_financial_subtype=decision.corrected_financial_subtype,
        exclusion_reason=decision.exclusion_reason,
        actor=decision.actor,
        created_at=decision.created_at,
    )


def _duplicate_evidence_dict(flag: DuplicateFlag) -> dict[str, Any]:
    return {
        "id": flag.id,
        "suspected_document_id": flag.suspected_document_id,
        "reason": flag.reason,
        "score": float(flag.score),
        "evidence": dict(flag.evidence),
        "scope": (
            "candidate"
            if flag.batch_id is not None
            else "source"
            if flag.source_file_id is not None
            else "document"
        ),
    }


def _stale_batch_detail(
    batch: ExtractionBatch,
    *,
    expected_version: int,
    affected_ids: Sequence[UUID],
    current_decision_revisions: Mapping[UUID, int] | None = None,
) -> dict[str, Any]:
    return {
        "batch_id": str(batch.id),
        "expected_version": expected_version,
        "current_version": batch.version,
        "current_lifecycle": batch.lifecycle.value,
        "affected_extraction_ids": [str(value) for value in affected_ids],
        "current_decision_revisions": {
            str(key): value for key, value in (current_decision_revisions or {}).items()
        },
    }


def _candidate_error(extraction_id: UUID, code: str, message: str) -> dict[str, Any]:
    return {
        "extraction_id": str(extraction_id),
        "code": code,
        "message": message,
    }


def _candidate_activation(
    candidate: ExtractedRecord,
    decision: CandidateReviewDecision | None,
    chunks: Sequence[Chunk],
) -> _CandidateActivation:
    corrections = (
        dict(decision.corrected_payload)
        if decision is not None and decision.corrected_payload is not None
        else None
    )
    correction_sha256 = canonical_digest(corrections)
    reason_sha256 = canonical_digest(decision.exclusion_reason if decision is not None else None)
    errors: list[dict[str, Any]] = []
    verified_values: dict[str, Any] | None = None
    effective_subtype = candidate.financial_subtype
    recurring_projection = None
    if (
        candidate.batch_id is None
        or candidate.candidate_ordinal is None
        or candidate.candidate_key is None
        or candidate.record_kind is None
        or candidate.source_locator is None
        or candidate.row_fingerprint is None
    ):
        errors.append(
            _candidate_error(
                candidate.id,
                "candidate_lineage_invalid",
                "candidate lineage is incomplete",
            )
        )
    if candidate.status is not ExtractionStatus.PENDING_REVIEW:
        errors.append(
            _candidate_error(
                candidate.id,
                "candidate_not_pending",
                "candidate is no longer pending review",
            )
        )
    if decision is None:
        errors.append(
            _candidate_error(
                candidate.id,
                "decision_missing",
                "candidate has no staged include or exclude decision",
            )
        )
    elif decision.expected_extraction_version != candidate.version:
        errors.append(
            _candidate_error(
                candidate.id,
                "decision_version_stale",
                "latest decision does not bind the current candidate version",
            )
        )
    elif decision.action is CandidateDecisionAction.INCLUDE:
        draft = ReviewDecisionDraft(
            extraction_id=candidate.id,
            expected_extraction_version=candidate.version,
            expected_decision_revision=max(0, decision.decision_revision - 1),
            action=decision.action,
            corrected_payload=corrections,
            corrected_financial_subtype=decision.corrected_financial_subtype,
            exclusion_reason=None,
        )
        try:
            _validate_review_decision(candidate, draft)
            if candidate.record_kind is RecordKind.FINANCIAL:
                effective_subtype = (
                    decision.corrected_financial_subtype or candidate.financial_subtype
                )
                verified_values = _verified_values(
                    candidate.payload,
                    {
                        key: value
                        for key, value in (corrections or {}).items()
                        if key not in bill_projection_correction_fields()
                    },
                )
                if effective_subtype is FinancialSubtype.RECURRING_BILL:
                    recurring_projection = normalize_recurring_bill_payload(
                        candidate.payload,
                        corrections or {},
                    )
        except ReviewBatchValidationError as error:
            errors.append(_candidate_error(candidate.id, error.code, str(error)))
        if candidate.payload and not chunks:
            errors.append(
                _candidate_error(
                    candidate.id,
                    "candidate_search_chunks_missing",
                    "included non-empty candidate has no staged search chunks",
                )
            )
    recurring_preview_sha256 = canonical_digest(
        _recurring_projection_payload(recurring_projection)
        if recurring_projection is not None
        else {"version": _RECURRING_PREVIEW_VERSION, "state": "not_applicable"}
    )
    return _CandidateActivation(
        candidate=candidate,
        decision=decision,
        verified_values=verified_values,
        effective_subtype=effective_subtype,
        recurring_projection=recurring_projection,
        recurring_prior=None,
        correction_sha256=correction_sha256,
        reason_sha256=reason_sha256,
        search_manifest_sha256=_search_manifest_digest(chunks),
        recurring_preview_sha256=recurring_preview_sha256,
        errors=errors,
    )


def _search_manifest_digest(chunks: Sequence[Chunk]) -> str:
    return canonical_digest(
        [
            {
                "id": str(chunk.id),
                "seq": chunk.seq,
                "heading_path": chunk.heading_path,
                "text_sha256": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                "embedding_sha256": canonical_digest([float(value) for value in chunk.embedding]),
                "embed_model": chunk.embed_model,
                "embed_model_digest": chunk.embed_model_digest,
                "token_count": chunk.token_count,
            }
            for chunk in chunks
        ]
    )


def _recurring_projection_payload(projection: Any) -> dict[str, Any]:
    return {
        "version": _RECURRING_PREVIEW_VERSION,
        "issuer_name": projection.issuer_name,
        "issuer_kind": projection.issuer_kind.value,
        "billing_period": projection.billing_period.isoformat(),
        "due_date": projection.due_date.isoformat() if projection.due_date else None,
        "consumption_value": (
            str(projection.consumption_value) if projection.consumption_value is not None else None
        ),
        "consumption_unit": projection.consumption_unit,
    }


def _activation_preview_from_state(
    batch: ExtractionBatch,
    candidates: Sequence[ExtractedRecord],
    states: Sequence[_CandidateActivation],
    *,
    prior_active: ExtractionBatch | None,
    prior_approved: Sequence[ExtractedRecord],
    source_is_current: bool,
) -> ReviewActivationPreview:
    errors: list[dict[str, Any]] = []
    if batch.lifecycle not in {BatchLifecycle.OPEN, BatchLifecycle.READY_TO_ACTIVATE}:
        errors.append(
            {
                "code": "batch_not_reviewable",
                "message": f"batch lifecycle is {batch.lifecycle.value}",
            }
        )
    if not source_is_current:
        errors.append(
            {
                "code": "stale_source",
                "message": "batch is not bound to the current processed source",
            }
        )
    candidate_count_matches = len(candidates) == batch.candidate_count
    expected_from_reconciliation = int(
        batch.reconciliation_counts.get("mapped_candidate", -1)
    ) + int(batch.reconciliation_counts.get("residual_generic_candidate", -1))
    if not candidate_count_matches or expected_from_reconciliation != batch.candidate_count:
        errors.append(
            {
                "code": "candidate_reconciliation_mismatch",
                "message": "persisted candidate membership does not match reconciliation",
                "expected_candidate_count": batch.candidate_count,
                "actual_candidate_count": len(candidates),
            }
        )
    for state in states:
        errors.extend(state.errors)

    pending_count = sum(state.decision is None for state in states)
    included_count = sum(
        state.decision is not None and state.decision.action is CandidateDecisionAction.INCLUDE
        for state in states
    )
    excluded_count = sum(
        state.decision is not None and state.decision.action is CandidateDecisionAction.EXCLUDE
        for state in states
    )
    vector = {
        "batch": {
            "id": str(batch.id),
            "version": batch.version,
            "document_id": str(batch.document_id),
            "source_intake_id": str(batch.source_intake_id),
            "source_file_id": str(batch.source_file_id),
            "source_version": batch.source_version,
            "source_sha256": batch.source_sha256,
            "candidate_count": batch.candidate_count,
            "reconciliation_digest": batch.reconciliation_digest,
        },
        "prior_authority": {
            "active_batch": (
                {
                    "id": str(prior_active.id),
                    "version": prior_active.version,
                }
                if prior_active is not None
                else None
            ),
            "approved_children": [
                {
                    "extraction_id": str(candidate.id),
                    "extraction_version": candidate.version,
                }
                for candidate in sorted(
                    prior_approved,
                    key=lambda item: str(item.id),
                )
            ],
        },
        "candidates": [
            {
                "extraction_id": str(state.candidate.id),
                "extraction_version": state.candidate.version,
                "decision_id": (str(state.decision.id) if state.decision is not None else None),
                "decision_revision": (
                    state.decision.decision_revision if state.decision is not None else 0
                ),
                "action": (
                    state.decision.action.value if state.decision is not None else "pending"
                ),
                "record_kind": (
                    state.candidate.record_kind.value
                    if state.candidate.record_kind is not None
                    else None
                ),
                "source_financial_subtype": (
                    state.candidate.financial_subtype.value
                    if state.candidate.financial_subtype is not None
                    else None
                ),
                "effective_financial_subtype": (
                    state.effective_subtype.value if state.effective_subtype is not None else None
                ),
                "correction_sha256": state.correction_sha256,
                "reason_sha256": state.reason_sha256,
                "search_manifest_sha256": state.search_manifest_sha256,
                "recurring_preview_version": _RECURRING_PREVIEW_VERSION,
                "recurring_preview_sha256": state.recurring_preview_sha256,
                "prior_recurring_projection_id": (
                    str(state.recurring_prior.id) if state.recurring_prior is not None else None
                ),
                "prior_payment_status": (
                    state.recurring_prior.payment_status.value
                    if state.recurring_prior is not None
                    else None
                ),
                "prior_paid_at": (
                    state.recurring_prior.paid_at.isoformat()
                    if state.recurring_prior is not None
                    and state.recurring_prior.paid_at is not None
                    else None
                ),
            }
            for state in sorted(states, key=lambda item: str(item.candidate.id))
        ],
    }
    activation_vector_sha256 = canonical_digest(vector)
    return ReviewActivationPreview(
        batch_id=batch.id,
        document_id=batch.document_id,
        source_intake_id=batch.source_intake_id,
        source_file_id=batch.source_file_id,
        source_version=batch.source_version,
        batch_version=batch.version,
        lifecycle=batch.lifecycle,
        total_count=len(candidates),
        pending_count=pending_count,
        included_count=included_count,
        excluded_count=excluded_count,
        error_count=len(errors),
        reconciliation_counts=dict(batch.reconciliation_counts),
        reconciliation_digest=batch.reconciliation_digest,
        candidate_count_matches=candidate_count_matches,
        source_is_current=source_is_current,
        requires_accept_exclusions=excluded_count > 0,
        requires_accept_empty=len(candidates) == 0,
        ready_for_activation=(
            not errors and pending_count == 0 and included_count + excluded_count == len(candidates)
        ),
        activation_vector_sha256=activation_vector_sha256,
        errors=tuple(errors),
    )


class ExtractionRepo:
    """Append immutable extraction payloads and transition review lifecycle state."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        document_id: UUID,
        *,
        payload: dict[str, Any],
        field_confidences: dict[str, float],
        model_name: str,
        prompt_version: str,
        actor: str,
        source_version: int | None = None,
    ) -> UUID:
        """Append a pending extraction and supersede any prior pending version."""

        if not isinstance(payload, dict):
            raise TypeError("payload must be a JSON object")
        document = await DocumentRepo(self.session)._locked_document(document_id)
        original = _latest_original(document.files)
        if original is None or (source_version is not None and original.version != source_version):
            raise SourceVersionSupersededError(
                "the source version was replaced before extraction could be recorded"
            )
        async with audit_actor(self.session, actor):
            existing = (
                await self.session.scalars(
                    select(ExtractedRecord)
                    .where(
                        ExtractedRecord.document_id == document_id,
                        ExtractedRecord.status == ExtractionStatus.PENDING_REVIEW,
                    )
                    .with_for_update()
                )
            ).all()
            now = dt.datetime.now(dt.UTC)
            for record in existing:
                record.status = ExtractionStatus.SUPERSEDED
                record.version += 1
                record.reviewer = actor.strip()
                record.reviewed_at = now

            extracted = ExtractedRecord(
                document_id=document_id,
                source_file_id=original.id,
                source_version=original.version,
                payload=payload,
                field_confidences=field_confidences,
                source_spans=_extraction_source_spans(payload),
                model_name=_clean_text(model_name, "model_name"),
                prompt_version=_clean_text(prompt_version, "prompt_version"),
            )
            self.session.add(extracted)
            document.status = DocumentStatus.IN_REVIEW
            await self.session.flush()
        return extracted.id

    async def get(self, extraction_id: UUID) -> dict[str, Any] | None:
        record = await self.session.get(ExtractedRecord, extraction_id)
        return _as_extraction_dict(record) if record else None

    async def latest_for(self, document_id: UUID) -> dict[str, Any] | None:
        records = (
            await self.session.scalars(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).all()
        record = _current_extraction(records)
        return _as_extraction_dict(record) if record else None

    async def reject(self, extraction_id: UUID, *, reason: str, reviewer: str) -> None:
        """Reject an active extraction and return its document to reprocess-needed."""

        cleaned_reviewer = _clean_text(reviewer, "reviewer")
        cleaned_reason = _clean_text(reason, "reason")
        candidate = await self.session.get(ExtractedRecord, extraction_id)
        if candidate is None:
            raise StaleExtractionError("extraction is no longer pending review")
        document = await DocumentRepo(self.session)._locked_document(candidate.document_id)
        async with audit_actor(self.session, cleaned_reviewer):
            result = await self.session.execute(
                update(ExtractedRecord)
                .where(
                    ExtractedRecord.id == extraction_id,
                    ExtractedRecord.status == ExtractionStatus.PENDING_REVIEW,
                )
                .values(
                    status=ExtractionStatus.REJECTED,
                    version=ExtractedRecord.version + 1,
                    reviewer=cleaned_reviewer,
                    rejection_reason=cleaned_reason,
                    reviewed_at=dt.datetime.now(dt.UTC),
                )
            )
            if result.rowcount != 1:
                raise StaleExtractionError("extraction is no longer pending review")
            document.status = DocumentStatus.NEEDS_REPROCESS
            await self.session.flush()


class VerifiedRepo:
    """Promote reviewed extractions and run indexed verified-record searches."""

    _CORRECTION_FIELDS = {
        "transaction_date",
        "total_amount",
        "counterparty",
        "currency",
        "category",
        "expense_category",
        "expense_kind",
        "due_date",
        "registration_number",
        "tax_8_amount",
        "tax_10_amount",
    }

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def promote(
        self,
        extraction_id: UUID,
        expected_version: int,
        *,
        corrections: dict[str, Any],
        reviewer: str,
    ) -> UUID:
        """Promote exactly the displayed pending extraction or raise a stale conflict."""

        if expected_version < 1:
            raise ReviewValidationError("expected_version must be greater than zero")
        unexpected = set(corrections) - self._CORRECTION_FIELDS
        if unexpected:
            raise ReviewValidationError(
                f"unsupported correction fields: {', '.join(sorted(unexpected))}"
            )
        try:
            cleaned_reviewer = _clean_text(reviewer, "reviewer")
        except ValueError as error:
            raise ReviewValidationError(str(error)) from error

        candidate = await self.session.get(ExtractedRecord, extraction_id)
        if candidate is None:
            raise StaleExtractionError("extraction was superseded or has changed")
        document = await DocumentRepo(self.session)._locked_document(candidate.document_id)
        try:
            values = _verified_values(candidate.payload, corrections)
        except ValueError as error:
            raise ReviewValidationError(str(error)) from error

        try:
            async with self.session.begin_nested():
                async with audit_actor(self.session, cleaned_reviewer):
                    now = dt.datetime.now(dt.UTC)
                    approved = await _approve_pending_extraction(
                        self.session,
                        extraction_id,
                        expected_version,
                        reviewer=cleaned_reviewer,
                        reviewed_at=now,
                    )
                    if not approved:
                        raise StaleExtractionError("extraction was superseded or has changed")

                    previous_approvals = (
                        await self.session.scalars(
                            select(ExtractedRecord)
                            .where(
                                ExtractedRecord.document_id == candidate.document_id,
                                ExtractedRecord.id != extraction_id,
                                ExtractedRecord.status == ExtractionStatus.APPROVED,
                            )
                            .with_for_update()
                        )
                    ).all()
                    for previous in previous_approvals:
                        previous.status = ExtractionStatus.SUPERSEDED
                        previous.version += 1
                        previous.reviewer = cleaned_reviewer
                        previous.reviewed_at = now
                    verified = VerifiedRecord(
                        document_id=candidate.document_id,
                        extracted_id=extraction_id,
                        reviewer=cleaned_reviewer,
                        **values,
                    )
                    self.session.add(verified)
                    document.status = DocumentStatus.VERIFIED
                    await self.session.flush()
        except IntegrityError as error:
            raise StaleExtractionError("extraction was superseded or has changed") from error
        return verified.id

    async def range_query(
        self,
        *,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        amount_min: float | Decimal | None = None,
        amount_max: float | Decimal | None = None,
        counterparty: str | None = None,
        doc_class: DocumentClass | None = None,
    ) -> list[dict[str, Any]]:
        """Return verified-only records satisfying all supplied range conditions."""

        statement = restrict_to_active_verified(select(VerifiedRecord, Document).join(Document))
        if date_from is not None:
            statement = statement.where(VerifiedRecord.transaction_date >= date_from)
        if date_to is not None:
            statement = statement.where(VerifiedRecord.transaction_date <= date_to)
        if amount_min is not None:
            statement = statement.where(VerifiedRecord.total_amount >= Decimal(str(amount_min)))
        if amount_max is not None:
            statement = statement.where(VerifiedRecord.total_amount <= Decimal(str(amount_max)))
        if counterparty:
            statement = statement.where(VerifiedRecord.counterparty == counterparty.strip())
        if doc_class is not None:
            statement = statement.where(Document.document_class == doc_class)
        statement = statement.order_by(
            VerifiedRecord.transaction_date.desc(), VerifiedRecord.id.desc()
        )
        rows = (await self.session.execute(statement)).all()
        return [
            {
                **_as_verified_dict(verified),
                "source_filename": document.source_filename,
                "doc_class": document.document_class.value,
            }
            for verified, document in rows
        ]


def _document_load_options() -> tuple[Any, ...]:
    return (
        selectinload(Document.files),
        selectinload(Document.extractions),
        selectinload(Document.verified_records),
        selectinload(Document.jobs),
    )


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if offset < 0:
        raise ValueError("offset must not be negative")


def _verified_values(payload: dict[str, Any], corrections: dict[str, Any]) -> dict[str, Any]:
    source = {
        "transaction_date": _field_value(payload, "transaction_date"),
        "total_amount": _field_value(payload, "total_amount"),
        "counterparty": _field_value(payload, "counterparty"),
        "currency": _field_value(payload, "currency"),
        "category": _field_value(payload, "expense_category"),
        "expense_kind": _field_value(payload, "expense_kind"),
        "due_date": _field_value(payload, "due_date"),
        "registration_number": _field_value(payload, "registration_number"),
        "tax_8_amount": _field_value(payload, "tax_8_amount"),
        "tax_10_amount": _field_value(payload, "tax_10_amount"),
    }
    normalized_corrections = dict(corrections)
    if "expense_category" in normalized_corrections:
        if "category" in normalized_corrections:
            raise ValueError("choose either category or expense_category, not both")
        normalized_corrections["category"] = normalized_corrections.pop("expense_category")
    source.update(normalized_corrections)

    counterparty = source["counterparty"]
    if not isinstance(counterparty, str):
        raise ValueError("counterparty is required")
    expense_kind = source["expense_kind"]
    if expense_kind in (None, ""):
        normalized_expense_kind = None
    else:
        try:
            normalized_expense_kind = ExpenseKind(str(expense_kind).strip())
        except ValueError as error:
            raise ValueError(f"unsupported expense_kind: {expense_kind!r}") from error
    return {
        "transaction_date": _as_date(source["transaction_date"], "transaction_date"),
        "total_amount": _as_decimal(source["total_amount"], "total_amount", required=True),
        "counterparty": _clean_text(counterparty, "counterparty"),
        "currency": _optional_text(source["currency"]),
        "category": _optional_text(source["category"]),
        "expense_kind": normalized_expense_kind,
        "due_date": _as_optional_date(source["due_date"], "due_date"),
        "registration_number": _optional_text(source["registration_number"]),
        "tax_8_amount": _as_decimal(source["tax_8_amount"], "tax_8_amount", required=False),
        "tax_10_amount": _as_decimal(source["tax_10_amount"], "tax_10_amount", required=False),
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional text fields must be strings")
    cleaned = value.strip()
    return cleaned or None


def _as_optional_date(value: Any, field_name: str) -> dt.date | None:
    if value in (None, ""):
        return None
    return _as_date(value, field_name)
