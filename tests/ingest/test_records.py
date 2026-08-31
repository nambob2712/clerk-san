from __future__ import annotations

import pytest

from clerksan.db.models import FinancialSubtype, RecordKind
from clerksan.ingest.records import (
    CandidateDraft,
    CandidateDraftError,
    CompositionLedger,
    StructuralDisposition,
    StructuralUnitDecision,
    build_candidate_key,
    value_fingerprint,
)


def _key(ordinal: int, kind: RecordKind = RecordKind.FINANCIAL) -> str:
    return build_candidate_key(
        source_sha256="a" * 64,
        source_locator="table:1/row:1",
        candidate_ordinal=ordinal,
        normalized_item_hash=value_fingerprint({"amount": "100"}),
        record_kind=kind,
        financial_subtype=(FinancialSubtype.TRANSACTION if kind is RecordKind.FINANCIAL else None),
        mapping_version=1,
    )


def test_candidate_identity_keeps_repeated_rows_distinct_by_ordinal() -> None:
    assert _key(1) != _key(2)


def test_candidate_record_kind_enforces_financial_subtype_boundary() -> None:
    with pytest.raises(CandidateDraftError, match="require a subtype"):
        CandidateDraft(
            candidate_ordinal=1,
            candidate_key="a" * 64,
            record_kind=RecordKind.FINANCIAL,
            financial_subtype=None,
            payload={},
            confidences={},
            source_locator="table:1/row:1",
            row_fingerprint="b" * 64,
        )
    with pytest.raises(CandidateDraftError, match="forbid"):
        build_candidate_key(
            source_sha256="a" * 64,
            source_locator="document:1",
            candidate_ordinal=1,
            normalized_item_hash="b" * 64,
            record_kind=RecordKind.GENERIC_DOCUMENT,
            financial_subtype=FinancialSubtype.OTHER_FINANCIAL,
            mapping_version=1,
        )


def test_composition_ledger_assigns_each_structural_unit_once() -> None:
    candidate_key = _key(1)
    decision = StructuralUnitDecision(
        unit_id="table:1/row:1",
        locator="table:1/row:1",
        content_digest="c" * 64,
        disposition=StructuralDisposition.MAPPED_CANDIDATE,
        candidate_key=candidate_key,
    )
    ledger = CompositionLedger((decision,))
    assert ledger.reconciliation_counts["mapped_candidate"] == 1

    with pytest.raises(CandidateDraftError, match="assigned more than once"):
        CompositionLedger((decision, decision))


def test_ignored_or_failed_unit_requires_an_explicit_reason() -> None:
    with pytest.raises(CandidateDraftError, match="require a reason"):
        StructuralUnitDecision(
            unit_id="table:1/row:2",
            locator="table:1/row:2",
            content_digest="d" * 64,
            disposition=StructuralDisposition.EXPLICIT_IGNORE,
        )
