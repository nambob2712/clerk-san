"""Capability-bound scheduling for exact-source intake actions."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.config import IntakeMode, Settings
from clerksan.db.models import ExecutionProfile, IntakeIntent, SourceIntake
from clerksan.db.repositories import ReprocessStateError
from clerksan.ingest.activation import evaluate_universal_activation
from clerksan.ingest.capabilities import CapabilityRegistry, build_capability_registry
from clerksan.ingest.filetype import DetectedFormat, FileType
from clerksan.ingest.policy import IntakeAction, PublicIntakeError, PublicReasonCode, decide_intake


@dataclass(frozen=True, slots=True)
class ExactSourceReprocessPlan:
    """Current execution evidence for one unchanged preserved source."""

    registry: CapabilityRegistry
    adapter_key: str | None
    detected_format: str | None
    required_components: tuple[str, ...]


async def plan_exact_source_reprocess(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    intake: SourceIntake,
) -> ExactSourceReprocessPlan:
    """Fail before lifecycle writes unless the current runtime can process the source."""

    if settings.intake_mode is IntakeMode.LEGACY:
        if intake.execution_profile is ExecutionProfile.UNIVERSAL_SANDBOXED:
            raise PublicIntakeError(PublicReasonCode.SANDBOX_UNAVAILABLE)
        registry = build_capability_registry(settings)
        return ExactSourceReprocessPlan(
            registry=registry,
            adapter_key=None,
            detected_format=None,
            required_components=_model_requirements(settings),
        )

    registry = getattr(request.app.state, "capability_registry", None)
    if not isinstance(registry, CapabilityRegistry) or not registry.sandbox_verified:
        raise PublicIntakeError(PublicReasonCode.SANDBOX_UNAVAILABLE)
    activation = await evaluate_universal_activation(session, settings, registry)
    if not activation.ready:
        raise PublicIntakeError(activation.reason_code or PublicReasonCode.SANDBOX_UNAVAILABLE)

    detected = _persisted_detection(intake)
    decision = decide_intake(
        detected,
        frozenset(registry.process),
        adapter_keys=_adapter_keys(registry),
        intake_intent=intake.intake_intent,
    )
    if decision.action is not IntakeAction.PROCESS or decision.adapter_key is None:
        raise ReprocessStateError(
            "the current sandbox registry does not advertise a safe processor for this source"
        )
    requirements = (
        () if intake.intake_intent is IntakeIntent.GENERIC_FILE else _model_requirements(settings)
    )
    return ExactSourceReprocessPlan(
        registry=registry,
        adapter_key=decision.adapter_key,
        detected_format=detected.format,
        required_components=requirements,
    )


def _persisted_detection(intake: SourceIntake) -> DetectedFormat:
    if not intake.detected_family or not intake.detected_format or not intake.canonical_mime:
        raise ReprocessStateError(
            "the preserved source has no structural detection evidence for universal reprocessing"
        )
    evidence: list[str] = []
    charset: str | None = None
    for item in intake.detection_evidence:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        value = item.get("value")
        if kind == "structural" and isinstance(value, str):
            evidence.append(value)
        elif kind == "charset" and isinstance(value, str) and charset is None:
            charset = value
    return DetectedFormat(
        family=intake.detected_family,
        format=intake.detected_format,
        canonical_mime=intake.canonical_mime,
        charset=charset,
        evidence=tuple(evidence),
    )


def _adapter_keys(registry: CapabilityRegistry) -> dict[str, str]:
    return {
        format_key: (
            f"delimited.{format_key}"
            if format_key in {FileType.CSV.value, FileType.TSV.value}
            else format_key
        )
        for format_key in registry.process
    }


def _model_requirements(settings: Settings) -> tuple[str, ...]:
    return tuple(sorted(f"model:{model}" for model in settings.required_models))
