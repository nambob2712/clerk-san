"""Bounded asynchronous upload endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Header, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.api.deps import database_session, settings_from_request
from clerksan.api.schemas import DocumentOut, RawSourceAccepted, UploadAccepted
from clerksan.config import IntakeMode, Settings
from clerksan.db.models import ExecutionProfile, FileKind, IntakeIntent, SourceIntakeState
from clerksan.db.repositories import (
    DocumentRepo,
    SourceIntakeRepo,
    UploadIdempotencyOutcome,
)
from clerksan.ingest.activation import evaluate_universal_activation
from clerksan.ingest.capabilities import CapabilityRegistry, build_capability_registry
from clerksan.ingest.filetype import (
    MIME_BY_FILE_TYPE,
    DetectedFormat,
    FileType,
    UnsupportedFileError,
    detect_file_type,
    inspect_file,
)
from clerksan.ingest.jobs import enqueue
from clerksan.ingest.limits import IngestLimits, check_upload_size
from clerksan.ingest.parser_runner import (
    ParserRunner,
    ParserSandboxError,
    ReadOnlySource,
)
from clerksan.ingest.policy import (
    IntakeAction,
    IntakeDecision,
    PublicIntakeError,
    PublicReasonCode,
    decide_intake,
    reason_is_retryable,
)
from clerksan.ingest.storage_reconcile import (
    QuarantineReservation,
    async_storage_lock,
    finalize_reservation,
    publish_reserved_blob,
    reserve_quarantine,
)

router = APIRouter(tags=["ingest"])
logger = logging.getLogger(__name__)
_CHUNK_BYTES = 64 * 1024
_EXPLICIT_INTAKE_INTENTS = frozenset({IntakeIntent.GENERIC_FILE, IntakeIntent.BILL_SCAN})


@dataclass(frozen=True, slots=True)
class StagedUpload:
    reservation: QuarantineReservation
    sha256: str
    mime: str
    detected: DetectedFormat
    detected_type: FileType | None
    decision: IntakeDecision
    preflight_evidence: dict[str, Any] | None = None


def _safe_filename(filename: str | None) -> str:
    candidate = Path(filename or "upload").name.strip()
    return candidate or "upload"


async def _stage_upload(
    file: UploadFile,
    settings: Settings,
    *,
    intake_intent: IntakeIntent = IntakeIntent.LEGACY_UNSPECIFIED,
    registry: CapabilityRegistry | None = None,
    parser_runner: ParserRunner | None = None,
) -> StagedUpload:
    """Stream, hash, and validate one upload without publishing it."""

    limits = IngestLimits.from_settings(settings)
    reservation = reserve_quarantine(settings.storage_dir)
    digest = hashlib.sha256()
    received = 0

    try:
        with reservation.payload_path.open("wb") as output:
            while chunk := await file.read(_CHUNK_BYTES):
                received += len(chunk)
                check_upload_size(received, limits)
                digest.update(chunk)
                output.write(chunk)

        raw = reservation.payload_path.read_bytes()
        detected = inspect_file(
            raw,
            declared_name=_safe_filename(file.filename),
            declared_mime=file.content_type,
            limits=limits,
        )
        preflight_evidence: dict[str, Any] | None = None
        if settings.intake_mode is IntakeMode.UNIVERSAL:
            if registry is None or parser_runner is None or not registry.sandbox_verified:
                raise PublicIntakeError(PublicReasonCode.SANDBOX_UNAVAILABLE)
            decision = decide_intake(
                detected,
                frozenset(registry.process),
                adapter_keys=_adapter_keys(registry),
                intake_intent=intake_intent,
            )
            if decision.action is IntakeAction.REJECT:
                raise PublicIntakeError(decision.reason_code)
            try:
                detected_type = FileType(detected.format)
            except ValueError:
                detected_type = None
            if decision.action is IntakeAction.PROCESS:
                with reservation.payload_path.open("rb") as source_stream:
                    source = ReadOnlySource(
                        fd=source_stream.fileno(),
                        source_sha256=digest.hexdigest(),
                        filename=_safe_filename(file.filename),
                        mime_type=detected.canonical_mime,
                    )
                    try:
                        evidence = await asyncio.to_thread(
                            parser_runner.preflight,
                            source,
                            {
                                "family": detected.family,
                                "format": detected.format,
                                "canonical_mime": detected.canonical_mime,
                                "charset": detected.charset,
                                "evidence": list(detected.evidence),
                            },
                            limits,
                        )
                    except ParserSandboxError as error:
                        raise PublicIntakeError(PublicReasonCode.SANDBOX_UNAVAILABLE) from error
                preflight_evidence = dict(evidence)
        else:
            _reject_unsafe_legacy_content(detected)
            try:
                detected_type = detect_file_type(raw, _safe_filename(file.filename), limits=limits)
            except UnsupportedFileError:
                raise PublicIntakeError(_unsupported_legacy_reason(detected)) from None
            decision = IntakeDecision(
                action=IntakeAction.PROCESS,
                reason_code=PublicReasonCode.PROCESSING_QUEUED,
                adapter_key=detected_type.value,
            )
        return StagedUpload(
            reservation=reservation,
            sha256=digest.hexdigest(),
            mime=(
                MIME_BY_FILE_TYPE[detected_type]
                if detected_type is not None
                else detected.canonical_mime
            ),
            detected=detected,
            detected_type=detected_type,
            decision=decision,
            preflight_evidence=preflight_evidence,
        )
    except Exception:
        _discard_unpublished(reservation)
        raise
    finally:
        await file.close()


def _reject_unsafe_legacy_content(detected: DetectedFormat) -> None:
    reason = {
        "empty": PublicReasonCode.EMPTY_FILE,
        "audio": PublicReasonCode.PROHIBITED_AUDIO,
        "video": PublicReasonCode.PROHIBITED_VIDEO,
        "executable": PublicReasonCode.PROHIBITED_EXECUTABLE,
        "active": PublicReasonCode.ACTIVE_CONTENT,
        "encrypted": PublicReasonCode.ENCRYPTED_CONTENT,
        "malformed": PublicReasonCode.MALFORMED_CONTENT,
    }.get(detected.family)
    if reason is not None:
        raise PublicIntakeError(reason)


def _unsupported_legacy_reason(detected: DetectedFormat) -> PublicReasonCode:
    return {
        "audio": PublicReasonCode.PROHIBITED_AUDIO,
        "video": PublicReasonCode.PROHIBITED_VIDEO,
        "executable": PublicReasonCode.PROHIBITED_EXECUTABLE,
        "active": PublicReasonCode.ACTIVE_CONTENT,
        "encrypted": PublicReasonCode.ENCRYPTED_CONTENT,
        "malformed": PublicReasonCode.MALFORMED_CONTENT,
    }.get(detected.family, PublicReasonCode.INSPECTION_AMBIGUOUS)


def _parse_idempotency_key(value: str | None) -> UUID | None:
    if value is None:
        return None
    if len(value) != 36:
        raise PublicIntakeError(PublicReasonCode.MALFORMED_CONTENT)
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise PublicIntakeError(PublicReasonCode.MALFORMED_CONTENT) from error
    if str(parsed) != value.lower():
        raise PublicIntakeError(PublicReasonCode.MALFORMED_CONTENT)
    return parsed


def _parse_explicit_intake_intent(value: str | None) -> IntakeIntent | None:
    """Accept only Phase-1 user choices; omission is resolved by the caller."""

    if value is None:
        return None
    try:
        parsed = IntakeIntent(value)
    except ValueError as error:
        raise PublicIntakeError(PublicReasonCode.MALFORMED_CONTENT) from error
    if parsed not in _EXPLICIT_INTAKE_INTENTS:
        raise PublicIntakeError(PublicReasonCode.MALFORMED_CONTENT)
    return parsed


def _intent_digest(operation: str, **values: str) -> str:
    payload = {"operation": operation, **values}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_processing_components(settings: Settings) -> tuple[str, ...]:
    return tuple(sorted(f"model:{model}" for model in settings.required_models))


def _requirements_for_intake(
    settings: Settings,
    intent: IntakeIntent,
    staged: StagedUpload,
) -> tuple[str, ...]:
    if settings.intake_mode is IntakeMode.UNIVERSAL and intent is IntakeIntent.GENERIC_FILE:
        return ()
    return _required_processing_components(settings)


async def _runtime_intake_dependencies(
    request: Request,
    session: AsyncSession,
    settings: Settings,
) -> tuple[CapabilityRegistry, ParserRunner | None]:
    if settings.intake_mode is IntakeMode.LEGACY:
        return build_capability_registry(settings), None
    registry = getattr(request.app.state, "capability_registry", None)
    runner = getattr(request.app.state, "parser_runner", None)
    if (
        not isinstance(registry, CapabilityRegistry)
        or runner is None
        or not callable(getattr(runner, "preflight", None))
    ):
        raise PublicIntakeError(PublicReasonCode.SANDBOX_UNAVAILABLE)
    activation = await evaluate_universal_activation(session, settings, registry)
    if not activation.ready:
        raise PublicIntakeError(activation.reason_code or PublicReasonCode.SANDBOX_UNAVAILABLE)
    return registry, runner


async def _require_current_universal_activation(
    session: AsyncSession,
    settings: Settings,
    registry: CapabilityRegistry,
) -> None:
    if settings.intake_mode is not IntakeMode.UNIVERSAL:
        return
    activation = await evaluate_universal_activation(session, settings, registry)
    if not activation.ready:
        raise PublicIntakeError(activation.reason_code or PublicReasonCode.SANDBOX_UNAVAILABLE)


def _intake_creation_evidence(staged: StagedUpload) -> list[dict[str, Any]]:
    evidence = [{"kind": "structural", "value": item} for item in staged.detected.evidence]
    if staged.detected.charset is not None:
        evidence.append({"kind": "charset", "value": staged.detected.charset})
    if staged.preflight_evidence is not None:
        evidence.append(
            {
                "kind": "sandbox_preflight",
                "value": staged.preflight_evidence,
            }
        )
    return evidence


def _initial_intake_state(staged: StagedUpload) -> SourceIntakeState:
    if staged.decision.action is IntakeAction.STORE_UNPROCESSED:
        return SourceIntakeState.STORED_UNPROCESSED
    return SourceIntakeState.QUEUED


def _replay_reason(value: str | None) -> PublicReasonCode:
    try:
        return PublicReasonCode(value) if value else PublicReasonCode.PROCESSING_QUEUED
    except ValueError:
        return PublicReasonCode.PROCESSING_QUEUED


def _with_resolved_universal_intent(
    staged: StagedUpload,
    registry: CapabilityRegistry,
    intake_intent: IntakeIntent,
) -> StagedUpload:
    decision = decide_intake(
        staged.detected,
        frozenset(registry.process),
        adapter_keys=_adapter_keys(registry),
        intake_intent=intake_intent,
    )
    if decision.action is IntakeAction.REJECT:
        raise PublicIntakeError(decision.reason_code)
    return replace(staged, decision=decision)


def _adapter_keys(registry: CapabilityRegistry) -> dict[str, str]:
    return {
        format_key: (
            f"delimited.{format_key}"
            if format_key in {FileType.CSV.value, FileType.TSV.value}
            else format_key
        )
        for format_key in registry.process
    }


def _discard_unpublished(reservation: QuarantineReservation) -> None:
    try:
        finalize_reservation(reservation)
    except Exception:  # noqa: BLE001 - startup reconciliation owns failed cleanup
        logger.exception("unable to finalize an unpublished upload reservation")


def _finalize_committed(reservation: QuarantineReservation) -> None:
    try:
        finalize_reservation(reservation)
    except Exception:  # noqa: BLE001 - the committed source remains replayable
        logger.exception("unable to finalize a committed upload reservation")


async def _accepted_source_metadata(
    session: AsyncSession,
    document_id: UUID,
    source_version: int,
) -> tuple[UUID | None, UUID | None]:
    detail = await DocumentRepo(session).get(document_id)
    source = next(
        (
            item
            for item in detail["files"]
            if item["kind"] == FileKind.ORIGINAL.value and item["version"] == source_version
        ),
        None,
    )
    if source is None:
        return None, None
    intake = await SourceIntakeRepo(session).get_for_source(
        document_id,
        source["id"],
        source_version,
    )
    return source["id"], intake.id if intake is not None else None


@router.post("/documents", status_code=202, response_model=UploadAccepted)
async def upload_document(
    request: Request,
    file: UploadFile,
    intake_intent: str | None = Form(default=None),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> UploadAccepted:
    """Preserve one supported source, then enqueue legacy-compatible processing."""

    filename = _safe_filename(file.filename)
    resolved_intake_intent = (
        _parse_explicit_intake_intent(intake_intent) or IntakeIntent.LEGACY_UNSPECIFIED
    )
    key = _parse_idempotency_key(idempotency_key_header)
    intent_digest = _intent_digest(
        "create_document",
        filename=filename,
        intake_intent=resolved_intake_intent.value,
    )
    registry, parser_runner = await _runtime_intake_dependencies(request, session, settings)
    staged = await _stage_upload(
        file,
        settings,
        intake_intent=resolved_intake_intent,
        registry=registry,
        parser_runner=parser_runner,
    )
    async with async_storage_lock(settings.storage_dir, shared=True):
        try:
            await _require_current_universal_activation(session, settings, registry)
        except BaseException:
            _discard_unpublished(staged.reservation)
            raise
        intakes = SourceIntakeRepo(session)
        if key is not None:
            reservation = await intakes.reserve_upload_idempotency(
                key,
                staged.sha256,
                intent_digest,
            )
            if reservation.outcome is UploadIdempotencyOutcome.REPLAY:
                assert reservation.replay is not None
                _discard_unpublished(staged.reservation)
                return UploadAccepted(
                    document_id=reservation.replay.document_id,
                    status="uploaded",
                    duplicate_of=reservation.replay.duplicate_of_document_id,
                    source_file_id=reservation.replay.source_file_id,
                    source_intake_id=reservation.replay.intake_id,
                    job_id=reservation.replay.job_id,
                    reason_code=_replay_reason(reservation.replay.reason_code),
                    retryable=reason_is_retryable(_replay_reason(reservation.replay.reason_code)),
                )
            if reservation.outcome is UploadIdempotencyOutcome.CONFLICT:
                _discard_unpublished(staged.reservation)
                raise PublicIntakeError(PublicReasonCode.IDEMPOTENCY_CONFLICT)

        published = False
        try:
            blob = publish_reserved_blob(staged.reservation, staged.sha256)
            published = True
            requirements = _requirements_for_intake(settings, resolved_intake_intent, staged)
            documents = DocumentRepo(session)
            duplicate_of = await documents.find_by_sha256(staged.sha256)
            document_id = await documents.create_with_raw(
                filename=filename,
                content_path=blob.relative_path,
                sha256=staged.sha256,
                mime=staged.mime,
                duplicate_of_document_id=duplicate_of,
                upload_idempotency_key=key,
                intent_digest=intent_digest if key is not None else None,
                registry_digest=registry.registry_digest,
                capabilities_digest=registry.capabilities_digest,
                required_components=requirements,
                intake_intent=resolved_intake_intent,
                detected_family=staged.detected.family,
                detected_format=staged.detected.format,
                detection_evidence=_intake_creation_evidence(staged),
                policy_version=(
                    "universal-intake-v1"
                    if settings.intake_mode is IntakeMode.UNIVERSAL
                    else "legacy-compat-v1"
                ),
                intake_state=_initial_intake_state(staged),
                reason_code=staged.decision.reason_code.value,
                retryable=staged.decision.retryable,
                execution_profile=(
                    ExecutionProfile.UNIVERSAL_SANDBOXED
                    if settings.intake_mode is IntakeMode.UNIVERSAL
                    else ExecutionProfile.LEGACY_COMPAT
                ),
                sandbox_verified=settings.intake_mode is IntakeMode.UNIVERSAL,
            )
            source_file_id, source_intake_id = await _accepted_source_metadata(
                session, document_id, 1
            )
            if source_file_id is None or source_intake_id is None:
                raise RuntimeError("accepted source identity was not persisted transactionally")
            job_id = None
            if staged.decision.action is IntakeAction.PROCESS:
                job_id = await enqueue(
                    session,
                    job_type="process_document",
                    payload={
                        "document_id": str(document_id),
                        "source_file_id": str(source_file_id),
                        "source_intake_id": str(source_intake_id),
                        "source_version": 1,
                        "intake_intent": resolved_intake_intent.value,
                        "detected_format": staged.detected.format,
                        "adapter_key": staged.decision.adapter_key,
                    },
                    idempotency_key="process_document",
                    settings=settings,
                    registry_digest=registry.registry_digest,
                    capabilities_digest=registry.capabilities_digest,
                    required_components=requirements,
                    intake_intent=resolved_intake_intent,
                    capability_registry=registry,
                )
            await session.commit()
        except BaseException:
            # A published digest is never request-compensated. The grace-period,
            # reference-aware reconciler decides whether an uncommitted blob is orphaned.
            if not published:
                _discard_unpublished(staged.reservation)
            raise

        _finalize_committed(staged.reservation)
        return UploadAccepted(
            document_id=document_id,
            status="uploaded",
            duplicate_of=duplicate_of,
            source_file_id=source_file_id,
            source_intake_id=source_intake_id,
            job_id=job_id,
            reason_code=staged.decision.reason_code,
            retryable=staged.decision.retryable,
        )


@router.post(
    "/documents/{document_id}/original",
    status_code=202,
    response_model=RawSourceAccepted,
)
async def append_original_version(
    document_id: UUID,
    request: Request,
    file: UploadFile,
    intake_intent: str | None = Form(default=None),
    actor: str = Query(default="local-user", min_length=1, pattern=r".*\S.*"),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> RawSourceAccepted:
    """Preserve a replacement source as an appended version, then queue processing."""

    filename = _safe_filename(file.filename)
    cleaned_actor = actor.strip()
    requested_intake_intent = _parse_explicit_intake_intent(intake_intent)
    key = _parse_idempotency_key(idempotency_key_header)
    registry, parser_runner = await _runtime_intake_dependencies(request, session, settings)
    staged = await _stage_upload(
        file,
        settings,
        intake_intent=requested_intake_intent or IntakeIntent.LEGACY_UNSPECIFIED,
        registry=registry,
        parser_runner=parser_runner,
    )
    async with async_storage_lock(settings.storage_dir, shared=True):
        try:
            await _require_current_universal_activation(session, settings, registry)
        except BaseException:
            _discard_unpublished(staged.reservation)
            raise
        documents = DocumentRepo(session)
        try:
            resolved_intake_intent = await documents.resolve_append_intake_intent(
                document_id,
                requested_intake_intent,
            )
            if settings.intake_mode is IntakeMode.UNIVERSAL:
                staged = _with_resolved_universal_intent(staged, registry, resolved_intake_intent)
        except BaseException:
            _discard_unpublished(staged.reservation)
            raise
        intent_digest = _intent_digest(
            "append_original",
            document_id=str(document_id),
            filename=filename,
            intake_intent=resolved_intake_intent.value,
        )
        intakes = SourceIntakeRepo(session)
        if key is not None:
            reservation = await intakes.reserve_upload_idempotency(
                key,
                staged.sha256,
                intent_digest,
            )
            if reservation.outcome is UploadIdempotencyOutcome.REPLAY:
                assert reservation.replay is not None
                _discard_unpublished(staged.reservation)
                if reservation.replay.document_id != document_id:
                    raise PublicIntakeError(PublicReasonCode.IDEMPOTENCY_CONFLICT)
                return RawSourceAccepted(
                    document_id=document_id,
                    version=reservation.replay.source_version,
                    status="reprocess_queued",
                    job_id=reservation.replay.job_id,
                    duplicate_of=reservation.replay.duplicate_of_document_id,
                    source_file_id=reservation.replay.source_file_id,
                    source_intake_id=reservation.replay.intake_id,
                )
            if reservation.outcome is UploadIdempotencyOutcome.CONFLICT:
                _discard_unpublished(staged.reservation)
                raise PublicIntakeError(PublicReasonCode.IDEMPOTENCY_CONFLICT)

        published = False
        try:
            blob = publish_reserved_blob(staged.reservation, staged.sha256)
            published = True
            requirements = _requirements_for_intake(settings, resolved_intake_intent, staged)
            duplicate_of = await documents.find_by_sha256(staged.sha256)
            source = await documents.append_raw_source(
                document_id,
                filename=filename,
                content_path=blob.relative_path,
                sha256=staged.sha256,
                mime=staged.mime,
                actor=cleaned_actor,
                duplicate_of_document_id=duplicate_of,
                upload_idempotency_key=key,
                intent_digest=intent_digest if key is not None else None,
                registry_digest=registry.registry_digest,
                capabilities_digest=registry.capabilities_digest,
                required_components=requirements,
                intake_intent=resolved_intake_intent,
                detected_family=staged.detected.family,
                detected_format=staged.detected.format,
                detection_evidence=_intake_creation_evidence(staged),
                policy_version=(
                    "universal-intake-v1"
                    if settings.intake_mode is IntakeMode.UNIVERSAL
                    else "legacy-compat-v1"
                ),
                intake_state=_initial_intake_state(staged),
                reason_code=staged.decision.reason_code.value,
                retryable=staged.decision.retryable,
                execution_profile=(
                    ExecutionProfile.UNIVERSAL_SANDBOXED
                    if settings.intake_mode is IntakeMode.UNIVERSAL
                    else ExecutionProfile.LEGACY_COMPAT
                ),
                sandbox_verified=settings.intake_mode is IntakeMode.UNIVERSAL,
            )
            source_file_id, source_intake_id = await _accepted_source_metadata(
                session, document_id, source.version
            )
            if source_file_id is None or source_intake_id is None:
                raise RuntimeError("accepted source identity was not persisted transactionally")
            job_id = None
            if staged.decision.action is IntakeAction.PROCESS:
                job_id = await enqueue(
                    session,
                    job_type="process_document",
                    payload={
                        "document_id": str(document_id),
                        "source_file_id": str(source_file_id),
                        "source_intake_id": str(source_intake_id),
                        "source_version": source.version,
                        "intake_intent": resolved_intake_intent.value,
                        "detected_format": staged.detected.format,
                        "adapter_key": staged.decision.adapter_key,
                    },
                    idempotency_key=f"process_source:{source.version}:{source.sha256}",
                    settings=settings,
                    registry_digest=registry.registry_digest,
                    capabilities_digest=registry.capabilities_digest,
                    required_components=requirements,
                    intake_intent=resolved_intake_intent,
                    capability_registry=registry,
                )
            await session.commit()
        except BaseException:
            if not published:
                _discard_unpublished(staged.reservation)
            raise

        _finalize_committed(staged.reservation)
        return RawSourceAccepted(
            document_id=document_id,
            version=source.version,
            status="reprocess_queued",
            job_id=job_id,
            duplicate_of=duplicate_of,
            source_file_id=source_file_id,
            source_intake_id=source_intake_id,
        )


@router.get("/documents/{document_id}/status", response_model=DocumentOut)
async def document_status(
    document_id: UUID, session: AsyncSession = Depends(database_session)
) -> DocumentOut:
    return DocumentOut(**(await DocumentRepo(session).get(document_id)))
