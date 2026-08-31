from __future__ import annotations

import pytest

from clerksan.db.models import IntakeIntent
from clerksan.ingest.filetype import DetectedFormat
from clerksan.ingest.policy import (
    IntakeAction,
    IntakeDecision,
    PublicReasonCode,
    decide_intake,
)


def _detected(family: str, format_name: str) -> DetectedFormat:
    return DetectedFormat(
        family=family,
        format=format_name,
        canonical_mime="application/octet-stream",
        evidence=(f"magic:{format_name}",),
    )


def test_public_reason_code_values_match_the_phase_one_contract_exactly() -> None:
    assert {reason.value for reason in PublicReasonCode} == {
        "processing_queued",
        "mapping_required",
        "model_unavailable",
        "processing_failed",
        "legacy_outcome_unavailable",
        "adapter_unavailable",
        "decoder_unavailable",
        "opaque_safe_fallback",
        "invalid_host",
        "empty_file",
        "request_too_large",
        "multipart_limit_exceeded",
        "json_limit_exceeded",
        "upload_capacity_exceeded",
        "upload_too_large",
        "prohibited_audio",
        "prohibited_video",
        "prohibited_executable",
        "active_content",
        "encrypted_content",
        "inspection_ambiguous",
        "malformed_content",
        "resource_limit_exceeded",
        "idempotency_conflict",
        "internal_error",
        "sandbox_unavailable",
        "worker_capability_stale",
        "registry_mismatch",
        "local_data_needs_upgrade",
        "generic_candidate_deferred",
        "intake_intent_mismatch",
    }


def test_registered_structural_format_is_processable_deterministically() -> None:
    detected = _detected("image", "png")

    first = decide_intake(
        detected,
        process_formats={"png"},
        adapter_keys={"png": "image"},
    )
    second = decide_intake(
        detected,
        process_formats={"png"},
        adapter_keys={"png": "image"},
    )

    assert (
        first
        == second
        == IntakeDecision(
            action=IntakeAction.PROCESS,
            reason_code=PublicReasonCode.PROCESSING_QUEUED,
            adapter_key="image",
            retryable=False,
        )
    )


def test_safe_unregistered_formats_produce_dark_fallback_decisions() -> None:
    assert decide_intake(_detected("image", "heic")) == IntakeDecision(
        action=IntakeAction.STORE_UNPROCESSED,
        reason_code=PublicReasonCode.DECODER_UNAVAILABLE,
        adapter_key=None,
        retryable=True,
    )
    assert decide_intake(_detected("opaque", "unknown")) == IntakeDecision(
        action=IntakeAction.STORE_UNPROCESSED,
        reason_code=PublicReasonCode.OPAQUE_SAFE_FALLBACK,
        adapter_key=None,
        retryable=True,
    )
    assert decide_intake(_detected("document", "rtf")) == IntakeDecision(
        action=IntakeAction.STORE_UNPROCESSED,
        reason_code=PublicReasonCode.ADAPTER_UNAVAILABLE,
        adapter_key=None,
        retryable=True,
    )


@pytest.mark.parametrize(
    ("family", "reason"),
    [
        ("empty", PublicReasonCode.EMPTY_FILE),
        ("audio", PublicReasonCode.PROHIBITED_AUDIO),
        ("video", PublicReasonCode.PROHIBITED_VIDEO),
        ("executable", PublicReasonCode.PROHIBITED_EXECUTABLE),
        ("active", PublicReasonCode.ACTIVE_CONTENT),
        ("encrypted", PublicReasonCode.ENCRYPTED_CONTENT),
        ("ambiguous", PublicReasonCode.INSPECTION_AMBIGUOUS),
        ("malformed", PublicReasonCode.MALFORMED_CONTENT),
    ],
)
def test_unsafe_structural_families_are_rejected(
    family: str,
    reason: PublicReasonCode,
) -> None:
    decision = decide_intake(_detected(family, "fixture"), process_formats={"fixture"})

    assert decision == IntakeDecision(
        action=IntakeAction.REJECT,
        reason_code=reason,
        adapter_key=None,
        retryable=False,
    )


def test_explicit_limit_violation_wins_over_registry_availability() -> None:
    decision = decide_intake(
        _detected("image", "png"),
        process_formats={"png"},
        adapter_keys={"png": "image"},
        violation=PublicReasonCode.RESOURCE_LIMIT_EXCEEDED,
    )

    assert decision.action is IntakeAction.REJECT
    assert decision.reason_code is PublicReasonCode.RESOURCE_LIMIT_EXCEEDED
    assert decision.adapter_key is None
    assert decision.retryable is False


def test_registered_archive_is_processable_but_unregistered_container_is_rejected() -> None:
    registered = decide_intake(
        _detected("container", "zip"),
        process_formats={"zip"},
        adapter_keys={"zip": "archive"},
    )
    unavailable = decide_intake(_detected("container", "tar"))

    assert registered == IntakeDecision(
        action=IntakeAction.PROCESS,
        reason_code=PublicReasonCode.PROCESSING_QUEUED,
        adapter_key="archive",
        retryable=False,
    )
    assert unavailable == IntakeDecision(
        action=IntakeAction.REJECT,
        reason_code=PublicReasonCode.INSPECTION_AMBIGUOUS,
        adapter_key=None,
        retryable=False,
    )


def test_generic_table_requires_mapping_even_when_a_parser_is_registered() -> None:
    decision = decide_intake(
        _detected("tabular", "csv"),
        process_formats={"csv"},
        adapter_keys={"csv": "delimited"},
        intake_intent=IntakeIntent.GENERIC_FILE,
    )

    assert decision == IntakeDecision(
        action=IntakeAction.PROCESS,
        reason_code=PublicReasonCode.MAPPING_REQUIRED,
        adapter_key="delimited",
        retryable=False,
    )


@pytest.mark.parametrize("format_name", ["png", "pdf"])
def test_generic_raster_and_pdf_use_advertised_candidate_processing(
    format_name: str,
) -> None:
    family = "image" if format_name == "png" else "document"
    decision = decide_intake(
        _detected(family, format_name),
        process_formats={format_name},
        intake_intent="generic_file",
    )

    assert decision == IntakeDecision(
        action=IntakeAction.PROCESS,
        reason_code=PublicReasonCode.PROCESSING_QUEUED,
        adapter_key=format_name,
        retryable=False,
    )


def test_generic_raster_without_an_advertised_processor_is_preserved() -> None:
    assert decide_intake(
        _detected("image", "png"),
        intake_intent=IntakeIntent.GENERIC_FILE,
    ) == IntakeDecision(
        action=IntakeAction.STORE_UNPROCESSED,
        reason_code=PublicReasonCode.GENERIC_CANDIDATE_DEFERRED,
        adapter_key=None,
        retryable=True,
    )


@pytest.mark.parametrize("family, format_name", [("tabular", "csv"), ("document", "docx")])
def test_bill_scan_rejects_non_raster_and_non_pdf_before_persistence(
    family: str,
    format_name: str,
) -> None:
    decision = decide_intake(
        _detected(family, format_name),
        intake_intent=IntakeIntent.BILL_SCAN,
    )

    assert decision == IntakeDecision(
        action=IntakeAction.REJECT,
        reason_code=PublicReasonCode.INTAKE_INTENT_MISMATCH,
        adapter_key=None,
        retryable=False,
    )


def test_legacy_unspecified_preserves_existing_registry_behavior() -> None:
    assert decide_intake(
        _detected("image", "png"),
        process_formats={"png"},
        adapter_keys={"png": "image"},
        intake_intent=IntakeIntent.LEGACY_UNSPECIFIED,
    ) == IntakeDecision(
        action=IntakeAction.PROCESS,
        reason_code=PublicReasonCode.PROCESSING_QUEUED,
        adapter_key="image",
        retryable=False,
    )
