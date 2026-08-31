"""Pure, dark universal-intake policy contracts.

Nothing in this module is wired into the Phase 1 legacy API.  It defines stable values
and deterministic decisions for capability and safety tests ahead of sandbox activation.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Set
from dataclasses import dataclass

from clerksan.db.models import IntakeIntent
from clerksan.ingest.filetype import DetectedFormat


class PublicReasonCode(enum.StrEnum):
    """Exhaustive stable reason vocabulary from the Phase 1 contract."""

    PROCESSING_QUEUED = "processing_queued"
    MAPPING_REQUIRED = "mapping_required"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROCESSING_FAILED = "processing_failed"
    LEGACY_OUTCOME_UNAVAILABLE = "legacy_outcome_unavailable"

    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    DECODER_UNAVAILABLE = "decoder_unavailable"
    OPAQUE_SAFE_FALLBACK = "opaque_safe_fallback"

    INVALID_HOST = "invalid_host"
    EMPTY_FILE = "empty_file"
    REQUEST_TOO_LARGE = "request_too_large"
    MULTIPART_LIMIT_EXCEEDED = "multipart_limit_exceeded"
    JSON_LIMIT_EXCEEDED = "json_limit_exceeded"
    UPLOAD_CAPACITY_EXCEEDED = "upload_capacity_exceeded"
    UPLOAD_TOO_LARGE = "upload_too_large"
    PROHIBITED_AUDIO = "prohibited_audio"
    PROHIBITED_VIDEO = "prohibited_video"
    PROHIBITED_EXECUTABLE = "prohibited_executable"
    ACTIVE_CONTENT = "active_content"
    ENCRYPTED_CONTENT = "encrypted_content"
    INSPECTION_AMBIGUOUS = "inspection_ambiguous"
    MALFORMED_CONTENT = "malformed_content"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INTERNAL_ERROR = "internal_error"

    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    WORKER_CAPABILITY_STALE = "worker_capability_stale"
    REGISTRY_MISMATCH = "registry_mismatch"
    LOCAL_DATA_NEEDS_UPGRADE = "local_data_needs_upgrade"
    GENERIC_CANDIDATE_DEFERRED = "generic_candidate_deferred"
    INTAKE_INTENT_MISMATCH = "intake_intent_mismatch"


class IntakeAction(enum.StrEnum):
    PROCESS = "process"
    STORE_UNPROCESSED = "store_unprocessed"
    REJECT = "reject"


class PublicIntakeError(ValueError):
    """A pre-persistence rejection that is safe to expose by stable reason code."""

    def __init__(self, reason_code: PublicReasonCode) -> None:
        self.reason_code = reason_code
        self.retryable = reason_is_retryable(reason_code)
        super().__init__(reason_code.value)


@dataclass(frozen=True)
class IntakeDecision:
    action: IntakeAction
    reason_code: PublicReasonCode
    adapter_key: str | None = None
    retryable: bool = False


_FAMILY_REJECTIONS: dict[str, PublicReasonCode] = {
    "empty": PublicReasonCode.EMPTY_FILE,
    "audio": PublicReasonCode.PROHIBITED_AUDIO,
    "video": PublicReasonCode.PROHIBITED_VIDEO,
    "executable": PublicReasonCode.PROHIBITED_EXECUTABLE,
    "active": PublicReasonCode.ACTIVE_CONTENT,
    "encrypted": PublicReasonCode.ENCRYPTED_CONTENT,
    "ambiguous": PublicReasonCode.INSPECTION_AMBIGUOUS,
    "malformed": PublicReasonCode.MALFORMED_CONTENT,
}

_POLICY_REJECTIONS = frozenset(
    {
        PublicReasonCode.INVALID_HOST,
        PublicReasonCode.EMPTY_FILE,
        PublicReasonCode.REQUEST_TOO_LARGE,
        PublicReasonCode.MULTIPART_LIMIT_EXCEEDED,
        PublicReasonCode.JSON_LIMIT_EXCEEDED,
        PublicReasonCode.UPLOAD_CAPACITY_EXCEEDED,
        PublicReasonCode.UPLOAD_TOO_LARGE,
        PublicReasonCode.PROHIBITED_AUDIO,
        PublicReasonCode.PROHIBITED_VIDEO,
        PublicReasonCode.PROHIBITED_EXECUTABLE,
        PublicReasonCode.ACTIVE_CONTENT,
        PublicReasonCode.ENCRYPTED_CONTENT,
        PublicReasonCode.INSPECTION_AMBIGUOUS,
        PublicReasonCode.MALFORMED_CONTENT,
        PublicReasonCode.RESOURCE_LIMIT_EXCEEDED,
        PublicReasonCode.IDEMPOTENCY_CONFLICT,
        PublicReasonCode.INTERNAL_ERROR,
        PublicReasonCode.INTAKE_INTENT_MISMATCH,
    }
)

_RETRYABLE_REASONS = frozenset(
    {
        PublicReasonCode.MODEL_UNAVAILABLE,
        PublicReasonCode.PROCESSING_FAILED,
        PublicReasonCode.ADAPTER_UNAVAILABLE,
        PublicReasonCode.DECODER_UNAVAILABLE,
        PublicReasonCode.OPAQUE_SAFE_FALLBACK,
        PublicReasonCode.UPLOAD_CAPACITY_EXCEEDED,
        PublicReasonCode.INTERNAL_ERROR,
        PublicReasonCode.SANDBOX_UNAVAILABLE,
        PublicReasonCode.WORKER_CAPABILITY_STALE,
        PublicReasonCode.REGISTRY_MISMATCH,
        PublicReasonCode.GENERIC_CANDIDATE_DEFERRED,
    }
)


def decide_intake(
    detected: DetectedFormat,
    process_formats: Set[str] = frozenset(),
    *,
    adapter_keys: Mapping[str, str] | None = None,
    violation: PublicReasonCode | None = None,
    intake_intent: IntakeIntent | str | None = None,
) -> IntakeDecision:
    """Choose a deterministic dark disposition from structural and registry inputs.

    ``process_formats`` is deliberately a plain immutable-view contract.  The capability
    registry remains the advertised authority, while policy code stays independent of its
    implementation and of live adapter instances.
    """

    if violation is not None:
        if violation not in _POLICY_REJECTIONS:
            raise ValueError(f"{violation.value} is not a policy rejection")
        return _rejected(violation)

    evidence_rejection = _evidence_rejection(detected.evidence)
    reason = evidence_rejection or _FAMILY_REJECTIONS.get(detected.family)
    if reason is not None:
        return _rejected(reason)

    intent = _normalize_intake_intent(intake_intent)
    if intent is IntakeIntent.BILL_SCAN and not _is_bill_scan_format(detected):
        return _rejected(PublicReasonCode.INTAKE_INTENT_MISMATCH)

    # A positively identified archive is processable only when the verified
    # registry advertises its exact format. Unknown or unavailable containers
    # stay fail-closed instead of falling through to preserve-only handling.
    if detected.family == "container" and detected.format not in process_formats:
        return _rejected(PublicReasonCode.INSPECTION_AMBIGUOUS)

    # Phase 2 deliberately stops generic imports at a safe, user-mappable table
    # outcome.  Raster/PDF candidates are retained without inventing financial
    # records until the Phase 3 candidate-batch capability is activated.
    if intent is IntakeIntent.GENERIC_FILE:
        if _is_tabular_format(detected):
            return IntakeDecision(
                action=IntakeAction.PROCESS,
                reason_code=PublicReasonCode.MAPPING_REQUIRED,
                adapter_key=(adapter_keys or {}).get(detected.format, detected.format),
                retryable=False,
            )
        if _is_bill_scan_format(detected) and detected.format not in process_formats:
            return IntakeDecision(
                action=IntakeAction.STORE_UNPROCESSED,
                reason_code=PublicReasonCode.GENERIC_CANDIDATE_DEFERRED,
                retryable=reason_is_retryable(PublicReasonCode.GENERIC_CANDIDATE_DEFERRED),
            )

    if detected.format in process_formats:
        adapters = adapter_keys or {}
        return IntakeDecision(
            action=IntakeAction.PROCESS,
            reason_code=PublicReasonCode.PROCESSING_QUEUED,
            adapter_key=adapters.get(detected.format, detected.format),
            retryable=False,
        )

    if detected.family == "image":
        reason = PublicReasonCode.DECODER_UNAVAILABLE
    elif detected.family == "opaque":
        reason = PublicReasonCode.OPAQUE_SAFE_FALLBACK
    else:
        reason = PublicReasonCode.ADAPTER_UNAVAILABLE
    return IntakeDecision(
        action=IntakeAction.STORE_UNPROCESSED,
        reason_code=reason,
        retryable=reason_is_retryable(reason),
    )


def reason_is_retryable(reason: PublicReasonCode) -> bool:
    """Return the retryability fixed by the public reason table."""

    return reason in _RETRYABLE_REASONS


def _rejected(reason: PublicReasonCode) -> IntakeDecision:
    return IntakeDecision(
        action=IntakeAction.REJECT,
        reason_code=reason,
        retryable=reason_is_retryable(reason),
    )


def _evidence_rejection(evidence: tuple[str, ...]) -> PublicReasonCode | None:
    for item in evidence:
        token = item.removeprefix("risk:")
        try:
            reason = PublicReasonCode(token)
        except ValueError:
            continue
        if reason in _POLICY_REJECTIONS:
            return reason
    return None


_TABULAR_FAMILIES = frozenset({"table", "tabular", "spreadsheet"})
_TABULAR_FORMATS = frozenset({"csv", "tsv", "xlsx", "xls", "ods"})


def _normalize_intake_intent(value: IntakeIntent | str | None) -> IntakeIntent:
    if value is None:
        return IntakeIntent.LEGACY_UNSPECIFIED
    if isinstance(value, IntakeIntent):
        return value
    try:
        return IntakeIntent(value)
    except ValueError as error:
        raise ValueError(f"unsupported intake_intent: {value!r}") from error


def _is_tabular_format(detected: DetectedFormat) -> bool:
    return detected.family in _TABULAR_FAMILIES or detected.format in _TABULAR_FORMATS


def _is_bill_scan_format(detected: DetectedFormat) -> bool:
    return detected.family == "image" or detected.format == "pdf"
