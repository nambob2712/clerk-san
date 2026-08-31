"""Provider-neutral candidate drafts and complete structural reconciliation."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from clerksan.db.models import FinancialSubtype, RecordKind
from clerksan.ingest.normalized import canonical_digest, canonical_json


class CandidateDraftError(ValueError):
    """A draft would violate candidate identity or record-kind invariants."""


class StructuralDisposition(enum.StrEnum):
    MAPPED_CANDIDATE = "mapped_candidate"
    RESIDUAL_GENERIC_CANDIDATE = "residual_generic_candidate"
    EXPLICIT_IGNORE = "explicit_ignore"
    BLANK = "blank"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    candidate_ordinal: int
    candidate_key: str
    record_kind: RecordKind
    financial_subtype: FinancialSubtype | None
    payload: dict[str, Any]
    confidences: dict[str, float]
    source_locator: str
    row_fingerprint: str
    validation_issues: tuple[str, ...] = ()
    evidence_group_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.candidate_ordinal < 1:
            raise CandidateDraftError("candidate ordinal must be positive")
        _require_digest(self.candidate_key, "candidate_key")
        _require_digest(self.row_fingerprint, "row_fingerprint")
        if not self.source_locator.strip():
            raise CandidateDraftError("source locator must not be blank")
        if self.record_kind is RecordKind.FINANCIAL and self.financial_subtype is None:
            raise CandidateDraftError("financial candidates require a subtype")
        if self.record_kind is RecordKind.GENERIC_DOCUMENT and self.financial_subtype is not None:
            raise CandidateDraftError("generic candidates forbid a financial subtype")
        if any(not issue.strip() for issue in self.validation_issues):
            raise CandidateDraftError("validation issues must not contain blanks")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(self, "confidences", MappingProxyType(dict(self.confidences)))


@dataclass(frozen=True, slots=True)
class StructuralUnitDecision:
    unit_id: str
    locator: str
    content_digest: str
    disposition: StructuralDisposition
    candidate_key: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.unit_id.strip() or not self.locator.strip():
            raise CandidateDraftError("structural unit identity must not be blank")
        _require_digest(self.content_digest, "content_digest")
        candidate_dispositions = {
            StructuralDisposition.MAPPED_CANDIDATE,
            StructuralDisposition.RESIDUAL_GENERIC_CANDIDATE,
        }
        if self.disposition in candidate_dispositions:
            if self.candidate_key is None:
                raise CandidateDraftError("candidate disposition requires candidate_key")
            _require_digest(self.candidate_key, "candidate_key")
        elif self.candidate_key is not None:
            raise CandidateDraftError("non-candidate disposition cannot bind a candidate")
        if self.disposition in {
            StructuralDisposition.EXPLICIT_IGNORE,
            StructuralDisposition.PARSE_ERROR,
        } and not (self.reason and self.reason.strip()):
            raise CandidateDraftError("ignored/error structural units require a reason")


@dataclass(frozen=True, slots=True)
class CompositionLedger:
    decisions: tuple[StructuralUnitDecision, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        unit_ids: set[str] = set()
        ranges: set[tuple[str, str]] = set()
        candidate_keys: set[str] = set()
        for decision in self.decisions:
            if decision.unit_id in unit_ids:
                raise CandidateDraftError("a structural unit is assigned more than once")
            unit_ids.add(decision.unit_id)
            identity = (decision.locator, decision.content_digest)
            if identity in ranges:
                raise CandidateDraftError("overlapping structural locator/digest assignment")
            ranges.add(identity)
            if decision.candidate_key is not None:
                if decision.candidate_key in candidate_keys:
                    raise CandidateDraftError(
                        "a candidate cannot consume multiple structural units"
                    )
                candidate_keys.add(decision.candidate_key)

    @property
    def reconciliation_counts(self) -> dict[str, int]:
        counts = {disposition.value: 0 for disposition in StructuralDisposition}
        for decision in self.decisions:
            counts[decision.disposition.value] += 1
        return counts


def canonical_value_json(value: Any) -> str:
    """Serialize normalized values deterministically for fingerprints."""

    try:
        return canonical_json(value)
    except ValueError as error:
        raise CandidateDraftError("candidate value is not canonical JSON") from error


def value_fingerprint(value: Any) -> str:
    try:
        return canonical_digest(value)
    except ValueError as error:
        raise CandidateDraftError("candidate value is not canonical JSON") from error


def build_candidate_key(
    *,
    source_sha256: str,
    source_locator: str,
    candidate_ordinal: int,
    normalized_item_hash: str,
    record_kind: RecordKind,
    financial_subtype: FinancialSubtype | None,
    mapping_version: int,
) -> str:
    _require_digest(source_sha256, "source_sha256")
    _require_digest(normalized_item_hash, "normalized_item_hash")
    if candidate_ordinal < 1 or mapping_version < 1 or not source_locator.strip():
        raise CandidateDraftError("candidate identity inputs must be positive and nonblank")
    if record_kind is RecordKind.FINANCIAL and financial_subtype is None:
        raise CandidateDraftError("financial candidate identity requires subtype")
    if record_kind is RecordKind.GENERIC_DOCUMENT and financial_subtype is not None:
        raise CandidateDraftError("generic candidate identity forbids subtype")
    payload = {
        "candidate_ordinal": candidate_ordinal,
        "financial_subtype": financial_subtype.value if financial_subtype else None,
        "mapping_version": mapping_version,
        "normalized_item_hash": normalized_item_hash,
        "record_kind": record_kind.value,
        "source_locator": source_locator,
        "source_sha256": source_sha256,
    }
    return value_fingerprint(payload)


def _require_digest(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise CandidateDraftError(f"{field_name} must be a lowercase SHA-256 digest")
