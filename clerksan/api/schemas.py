"""Pydantic HTTP contracts for the local Clerk-san API."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from clerksan.db.models import CandidateDecisionAction, FinancialSubtype, RecordKind
from clerksan.ingest.mapping import DateStyle, DecimalStyle, FieldParser, SignRule
from clerksan.ingest.policy import PublicReasonCode

T = TypeVar("T")


class ErrorOut(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] | None = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    limit: int
    offset: int


class UploadAccepted(BaseModel):
    document_id: UUID
    status: str
    duplicate_of: UUID | None = None
    source_file_id: UUID | None = None
    source_intake_id: UUID | None = None
    job_id: UUID | None = None
    reason_code: PublicReasonCode | None = None
    retryable: bool | None = None


class RawSourceAccepted(BaseModel):
    """A newly appended immutable source version and its processing job."""

    document_id: UUID
    version: int
    status: str
    job_id: UUID | None = None
    duplicate_of: UUID | None = None
    source_file_id: UUID | None = None
    source_intake_id: UUID | None = None


class CapabilityOut(BaseModel):
    """The only advertised universal-format authority."""

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(validation_alias="schema", serialization_alias="schema")
    version: int
    process: list[str] = Field(default_factory=list)
    sandbox_verified: bool
    registry_digest: str
    capabilities_digest: str


class ComponentReadinessOut(BaseModel):
    """Additive readiness evidence that does not replace the legacy top-level result."""

    intake_ready: bool
    review_ready: bool
    processing_ready: bool
    universal_processing_ready: bool
    processing_reason_codes: list[PublicReasonCode] = Field(default_factory=list)
    registry_digest: str
    capabilities_digest: str
    worker_registry_digest: str | None = None
    worker_capabilities_digest: str | None = None
    worker_capability_lease_age_seconds: float | None = None


class RecentIntakeOut(BaseModel):
    """Dark Phase 1 rehydration shape; its route activates only in Phase 2."""

    intake_id: UUID
    document_id: UUID
    source_file_id: UUID
    source_version: int = Field(ge=1)
    state: str
    reason_code: PublicReasonCode
    retryable: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class IntakeJobReference(BaseModel):
    """The latest processing job bound to the exact source intake."""

    job_id: UUID
    job_type: str
    status: str


class SourceIntakeDetail(BaseModel):
    """Immutable source identity plus the current durable intake projection."""

    intake_id: UUID
    document_id: UUID
    source_file_id: UUID
    source_version: int = Field(ge=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    upload_idempotency_key: UUID | None = None
    intake_intent: str
    detected_format: str | None = None
    state: str
    reason_code: PublicReasonCode | None = None
    retryable: bool
    failure_phase: str | None = None
    version: int = Field(ge=1)
    job_reference: IntakeJobReference | None = None


class SourceIntakeActionIn(BaseModel):
    expected_version: int = Field(ge=1)
    actor: str = Field(default="local-user", min_length=1)

    @field_validator("actor")
    @classmethod
    def actor_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("actor must not be blank")
        return cleaned


class MappingSourceRef(BaseModel):
    """Exact source and normalized-structure identity displayed to the user."""

    model_config = ConfigDict(extra="forbid")

    source_intake_id: UUID
    source_file_id: UUID
    source_version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    structure_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class MappingFieldRuleIn(BaseModel):
    """One bounded allowlisted projection; executable expressions are impossible."""

    model_config = ConfigDict(extra="forbid")

    target_field: str = Field(min_length=1, max_length=2_048)
    source_columns: list[str] = Field(default_factory=list, max_length=8)
    literal: str | None = Field(default=None, max_length=2_048)
    separator: str = Field(default=" ", max_length=2_048)
    trim: bool = True
    null_markers: list[str] = Field(default_factory=list, max_length=32)
    value_map: list[tuple[str, str]] = Field(default_factory=list, max_length=256)
    parser: FieldParser = FieldParser.RAW
    date_style: DateStyle | None = None
    decimal_style: DecimalStyle | None = None
    sign_rule: SignRule = SignRule.PRESERVE
    currency_aliases: list[tuple[str, str]] = Field(default_factory=list, max_length=256)


class MappingCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: MappingSourceRef
    idempotency_key: str = Field(min_length=1, max_length=255)
    table_locator: str = Field(min_length=1, max_length=1_024)
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_kind: RecordKind
    financial_subtype: FinancialSubtype | None = None
    field_rules: list[MappingFieldRuleIn] = Field(min_length=1, max_length=64)
    required_fields: list[str] = Field(default_factory=list, max_length=64)
    mapping_version: int = Field(default=1, ge=1)
    created_by: str = Field(default="local-user", min_length=1, max_length=255)

    @field_validator("idempotency_key", "table_locator", "created_by")
    @classmethod
    def mapping_text_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned


class MappingSetEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_locator: str = Field(min_length=1, max_length=1_024)
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping_id: UUID | None = None
    mapping_version: int | None = Field(default=None, ge=1)
    ignore_reason: str | None = Field(default=None, min_length=1, max_length=2_048)

    @model_validator(mode="after")
    def mapping_or_ignore_is_exclusive(self) -> MappingSetEntryIn:
        mapped = self.mapping_id is not None and self.mapping_version is not None
        ignored = self.ignore_reason is not None and self.mapping_id is None
        if not (mapped ^ ignored) or (self.mapping_id is None) != (self.mapping_version is None):
            raise ValueError("entry requires mapping id/version or one ignore reason")
        if self.ignore_reason is not None:
            self.ignore_reason = self.ignore_reason.strip()
            if not self.ignore_reason:
                raise ValueError("ignore_reason must not be blank")
        return self


class MappingSetDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: MappingSourceRef
    idempotency_key: str = Field(min_length=1, max_length=255)
    entries: list[MappingSetEntryIn] = Field(max_length=256)
    created_by: str = Field(default="local-user", min_length=1, max_length=255)
    preview_limit: int = Field(default=50, ge=1, le=50)

    @field_validator("idempotency_key", "created_by")
    @classmethod
    def mapping_set_text_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned


class MappingSetApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: MappingSourceRef
    mapping_set_version: int = Field(ge=1)
    mapping_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_mapping_versions: dict[UUID, int] = Field(max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=255)

    @field_validator("idempotency_key")
    @classmethod
    def apply_text_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned


class SchemaDescriptorOut(BaseModel):
    table_locator: str
    ordered_headers: list[str]
    inferred_types: list[str]
    row_count: int
    schema_fingerprint: str


class SchemaDescriptorsOut(BaseModel):
    document_id: UUID
    source: MappingSourceRef
    descriptors: list[SchemaDescriptorOut]


class MappingOut(BaseModel):
    id: UUID
    table_locator: str
    schema_fingerprint: str
    record_kind: RecordKind
    financial_subtype: FinancialSubtype | None
    field_rules: list[MappingFieldRuleIn]
    required_fields: list[str]
    mapping_version: int
    mapping_digest: str
    created_by: str
    created_at: dt.datetime


class MappingsOut(BaseModel):
    document_id: UUID
    source: MappingSourceRef
    items: list[MappingOut]


class MappingPreviewRowOut(BaseModel):
    row_ordinal: int
    source_locator: str
    values: dict[str, Any]
    errors: list[str]


class MappingPreviewOut(BaseModel):
    table_locator: str
    rows: list[MappingPreviewRowOut]
    total_rows: int
    valid_rows: int
    error_rows: int
    blank_rows: int
    truncated: bool


class MappingSetPreviewOut(BaseModel):
    document_id: UUID
    source: MappingSourceRef
    previews: list[MappingPreviewOut]
    reconciliation_counts: dict[str, int]
    candidate_count: int


class MappingSetEntryOut(BaseModel):
    ordinal: int
    table_locator: str
    schema_fingerprint: str
    mapping_id: UUID | None
    mapping_version: int | None
    ignore_reason: str | None


class MappingSetOut(BaseModel):
    id: UUID
    document_id: UUID
    source: MappingSourceRef
    set_digest: str
    version: int
    created_by: str
    created_at: dt.datetime
    entries: list[MappingSetEntryOut]


class ExtractionBatchOut(BaseModel):
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
    lifecycle: str
    candidate_count: int
    reconciliation_counts: dict[str, int]
    reconciliation_digest: str
    version: int
    replayed: bool


class ReprocessAccepted(BaseModel):
    """One idempotent request to process the current preserved source again."""

    document_id: UUID
    original_version: int
    status: str
    job_id: UUID | None = None


class DerivativeRetryAccepted(BaseModel):
    """The current-source background stages queued for an explicit retry."""

    document_id: UUID
    original_version: int
    status: str
    job_ids: list[UUID] = Field(default_factory=list)


class DocumentOut(BaseModel):
    id: UUID
    doc_class: str
    status: str
    source_filename: str
    created_at: dt.datetime
    updated_at: dt.datetime | None = None
    files: list[dict[str, Any]] = Field(default_factory=list)
    extracted: dict[str, Any] | None = None
    verified: dict[str, Any] | None = None
    processing_error: str | None = None
    audit_history: list[dict[str, Any]] = Field(default_factory=list)


class ReviewItemOut(BaseModel):
    document_id: UUID
    extraction_id: UUID
    version: int
    source_file_id: UUID
    source_version: int
    doc_class: str
    flagged_fields: list[str]
    suggested: dict[str, Any]
    source_spans: dict[str, Any] = Field(default_factory=dict)
    suspected_duplicate_of: list[UUID] = Field(default_factory=list)
    duplicate_candidates: list[dict[str, Any]] = Field(default_factory=list)
    batch_id: UUID | None = None
    batch_version: int | None = Field(default=None, ge=1)
    batch_candidate_count: int | None = Field(default=None, ge=0)
    record_kind: RecordKind | None = None
    financial_subtype: FinancialSubtype | None = None


class ReviewDecisionIn(BaseModel):
    extraction_id: UUID
    expected_version: int = Field(ge=1, strict=True)
    corrections: dict[str, Any] = Field(default_factory=dict)
    reviewer: str = Field(default="local-user", min_length=1)

    @field_validator("reviewer")
    @classmethod
    def reviewer_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reviewer must not be blank")
        return cleaned


class ReviewRejectIn(BaseModel):
    extraction_id: UUID
    reason: str = Field(min_length=1)
    reviewer: str = Field(default="local-user", min_length=1)

    @field_validator("reason", "reviewer")
    @classmethod
    def text_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned


class ReviewBatchSummaryOut(BaseModel):
    id: UUID
    document_id: UUID
    source_intake_id: UUID
    source_file_id: UUID
    source_version: int = Field(ge=1)
    lifecycle: str
    version: int = Field(ge=1)
    candidate_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    included_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    exception_count: int = Field(ge=0)
    reconciliation_counts: dict[str, int]
    reconciliation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: dt.datetime
    updated_at: dt.datetime


class ReviewBatchPageOut(BaseModel):
    items: list[ReviewBatchSummaryOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class ReviewDecisionRevisionOut(BaseModel):
    id: UUID
    extraction_id: UUID
    decision_revision: int = Field(ge=1)
    action: CandidateDecisionAction
    expected_extraction_version: int = Field(ge=1)
    corrections: dict[str, Any] | None = None
    corrected_financial_subtype: FinancialSubtype | None = None
    exclusion_reason: str | None = None
    actor: str
    created_at: dt.datetime


class ReviewDuplicateEvidenceOut(BaseModel):
    id: UUID
    suspected_document_id: UUID
    reason: str
    score: float = Field(ge=0, le=1)
    evidence: dict[str, Any]
    scope: str


class ReviewCandidateOut(BaseModel):
    extraction_id: UUID
    batch_id: UUID
    candidate_ordinal: int = Field(ge=1)
    candidate_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    record_kind: RecordKind
    financial_subtype: FinancialSubtype | None = None
    source_locator: str
    version: int = Field(ge=1)
    status: str
    payload: dict[str, Any]
    field_confidences: dict[str, Any]
    source_spans: dict[str, Any]
    validation_issues: list[str]
    evidence_group_keys: list[str]
    latest_decision: ReviewDecisionRevisionOut | None = None
    duplicate_evidence: list[ReviewDuplicateEvidenceOut] = Field(default_factory=list)


class ReviewCandidatePageOut(BaseModel):
    batch_id: UUID
    batch_version: int = Field(ge=1)
    items: list[ReviewCandidateOut]
    source_duplicate_evidence: list[ReviewDuplicateEvidenceOut] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class ReviewCandidateDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_id: UUID
    expected_extraction_version: int = Field(ge=1, strict=True)
    expected_decision_revision: int = Field(ge=0, strict=True)
    action: CandidateDecisionAction
    corrections: dict[str, Any] | None = None
    corrected_financial_subtype: FinancialSubtype | None = None
    exclusion_reason: str | None = Field(default=None, max_length=2_048)

    @model_validator(mode="after")
    def action_payload_shape(self) -> ReviewCandidateDecisionIn:
        if self.action is CandidateDecisionAction.EXCLUDE:
            if self.corrections is not None or self.corrected_financial_subtype is not None:
                raise ValueError("excluded candidates cannot carry corrections")
            if self.exclusion_reason is None or not self.exclusion_reason.strip():
                raise ValueError("excluded candidates require a nonblank reason")
            self.exclusion_reason = self.exclusion_reason.strip()
        elif self.exclusion_reason is not None:
            raise ValueError("included candidates cannot carry an exclusion reason")
        if self.corrections is not None:
            try:
                encoded = json.dumps(
                    self.corrections,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise ValueError("corrections must contain finite JSON values") from error
            if len(encoded) > 65_536:
                raise ValueError("corrections exceed 65536 encoded bytes")
            _validate_review_json(self.corrections)
        return self


class ReviewBatchDecisionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_batch_version: int = Field(ge=1, strict=True)
    decisions: list[ReviewCandidateDecisionIn] = Field(min_length=1, max_length=100)
    actor: str = Field(default="local-user", min_length=1, max_length=255)

    @field_validator("actor")
    @classmethod
    def decision_actor_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("actor must not be blank")
        return cleaned

    @model_validator(mode="after")
    def decision_request_is_bounded(self) -> ReviewBatchDecisionsIn:
        ids = [decision.extraction_id for decision in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("one request cannot decide an extraction twice")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 262_144:
            raise ValueError("decision request exceeds 262144 encoded bytes")
        return self


class ReviewBatchDecisionResultOut(BaseModel):
    batch_id: UUID
    previous_batch_version: int = Field(ge=1)
    batch_version: int = Field(ge=1)
    lifecycle: str
    decisions: list[ReviewDecisionRevisionOut]


class ReviewActivationPreviewOut(BaseModel):
    batch_id: UUID
    document_id: UUID
    source_intake_id: UUID
    source_file_id: UUID
    source_version: int = Field(ge=1)
    batch_version: int = Field(ge=1)
    lifecycle: str
    total_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    included_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    reconciliation_counts: dict[str, int]
    reconciliation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_count_matches: bool
    source_is_current: bool
    requires_accept_exclusions: bool
    requires_accept_empty: bool
    ready_for_activation: bool
    activation_vector_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    errors: list[dict[str, Any]] = Field(default_factory=list)


class ReviewBatchActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_batch_version: int = Field(ge=1, strict=True)
    expected_vector_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(default="local-user", min_length=1, max_length=255)
    accept_exclusions: bool = False
    accept_empty: bool = False

    @field_validator("actor")
    @classmethod
    def activation_actor_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("actor must not be blank")
        return cleaned


class ReviewBatchActivationOut(BaseModel):
    batch_id: UUID
    document_id: UUID
    batch_version: int = Field(ge=1)
    lifecycle: str
    activation_vector_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    included_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    accepted_exclusions: bool
    accepted_empty: bool
    verified_by_extraction: dict[UUID, UUID]


class ReviewBatchRejectAndReprocessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_batch_version: int = Field(ge=1, strict=True)
    actor: str = Field(default="local-user", min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=2_048)

    @field_validator("actor", "reason")
    @classmethod
    def reprocess_text_must_be_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned


class ReviewBatchReprocessOut(BaseModel):
    batch_id: UUID
    document_id: UUID
    source_intake_id: UUID
    source_file_id: UUID
    source_version: int = Field(ge=1)
    batch_version: int = Field(ge=1)
    lifecycle: str
    status: str
    job_id: UUID | None = None


def _validate_review_json(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        raise ValueError("corrections nesting is too deep")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 255 or "\x00" in key:
                raise ValueError("correction keys must be bounded nonblank strings")
            _validate_review_json(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 1_000:
            raise ValueError("correction arrays are too large")
        for child in value:
            _validate_review_json(child, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > 8_192:
            raise ValueError("corrections contain an oversized string")
        if "\x00" in value:
            raise ValueError("corrections contain an unsupported null character")


class QueryIn(BaseModel):
    question: str


class CitationOut(BaseModel):
    document_id: UUID
    heading_path: str
    snippet: str


class AnswerOut(BaseModel):
    text: str
    mode: str
    citations: list[CitationOut] = Field(default_factory=list)
    sql_result: dict[str, Any] | None = None


class BillOut(BaseModel):
    id: UUID
    issuer_id: UUID
    issuer: str
    issuer_kind: str
    billing_period: dt.date
    amount: float
    due_date: dt.date | None
    payment_status: str
    consumption_value: float | None = None
    consumption_unit: str | None = None
