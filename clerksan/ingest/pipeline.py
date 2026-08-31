"""The dependency-injected document-processing handler used by the worker.

One invocation preserves the existing original, reuses a normalized artifact when
available, and appends one immutable extraction for a deliberate reprocess.  A
completed job records its output id inside its own durable payload, so a lease retry
after the transaction committed cannot append a second extraction accidentally.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.config import Settings, get_settings
from clerksan.db.models import (
    Document,
    DocumentClass,
    DocumentFile,
    DocumentStatus,
    DuplicateFlag,
    ExecutionProfile,
    FileKind,
    FinancialSubtype,
    IntakeIntent,
    Job,
    RecordKind,
    SourceIntakeState,
)
from clerksan.db.repositories import (
    DocumentRepo,
    ExtractionBatchRepo,
    ExtractionBatchSummary,
    ExtractionRepo,
    SourceIntakeRepo,
    SourceVersionSupersededError,
)
from clerksan.extract.classifier import ClassificationResult, classify
from clerksan.extract.extractor import PROMPT_VERSION, extract
from clerksan.ingest.adapters.base import AdapterRegistry
from clerksan.ingest.capabilities import CapabilityRegistry
from clerksan.ingest.embedded_media import (
    EmbeddedMediaSourceVersionSupersededError,
    persist_embedded_media,
)
from clerksan.ingest.filetype import FileType, detect_file_type
from clerksan.ingest.jobs import register_handler
from clerksan.ingest.limits import IngestLimits
from clerksan.ingest.normalized import (
    PDF_PREVIEW_MANIFEST_MIME,
    DocMetadata,
    NormalizedDocument,
    PdfPreviewManifest,
    PdfPreviewPageDescriptor,
    PdfPreviewStatus,
    canonical_digest,
    canonical_json,
    canonical_locator,
)
from clerksan.ingest.parser_artifacts import ArtifactRole, ParserArtifact, ParserRunResult
from clerksan.ingest.parser_runner import AdapterContext, ParserRunner, ReadOnlySource
from clerksan.ingest.records import (
    CandidateDraft,
    CompositionLedger,
    StructuralDisposition,
    StructuralUnitDecision,
    build_candidate_key,
    value_fingerprint,
)
from clerksan.ingest.staging import (
    StagedSourceVersionSupersededError,
    stage_spreadsheet_rows,
    stage_tabular_rows,
)
from clerksan.ingest.storage_reconcile import (
    QuarantineReservation,
    publish_reserved_blob,
    reserve_quarantine,
)
from clerksan.llm.client import ModelManager, ModelRole, OllamaClient
from clerksan.llm.ocr import OcrEngine, OcrResult, get_ocr_engine
from clerksan.storage import read_verified_artifact, resolve_storage_path, verify_artifact_file

ReadBytes = Callable[[str], Awaitable[bytes]]
WriteNormalized = Callable[[UUID, bytes], Awaitable[str]]
Classifier = Callable[
    [NormalizedDocument, OllamaClient, ModelManager], Awaitable[ClassificationResult]
]
Extractor = Callable[
    [NormalizedDocument, DocumentClass, OllamaClient, ModelManager], Awaitable[Any]
]


class _ModelManagedOcr:
    """Load the selected vision model before dispatching its OCR request."""

    def __init__(self, engine: OcrEngine, models: ModelManager) -> None:
        self._engine = engine
        self._models = models
        self.name = engine.name

    async def ocr(self, image_bytes: bytes) -> OcrResult:
        await self._models.ensure_loaded(ModelRole.OCR)
        return await self._engine.ocr(image_bytes)


_CACHED_DEFAULT_DEPENDENCIES: dict[tuple[str, str, str, str], PipelineDependencies] = {}
_ARTIFACT_RESERVATIONS_KEY = "clerksan.pipeline.artifact_reservations"


@dataclass(frozen=True, slots=True)
class _PersistedParserPage:
    artifact_id: UUID
    artifact: ParserArtifact
    content_path: str
    ocr_result: OcrResult | None


@dataclass(slots=True)
class PipelineDependencies:
    """All external document-processing seams, supplied explicitly in tests and apps."""

    settings: Settings
    adapters: AdapterRegistry
    client: OllamaClient
    models: ModelManager
    read_bytes: ReadBytes
    write_normalized: WriteNormalized
    classifier: Classifier = classify
    extractor: Extractor = extract
    actor: str = "worker"
    model_name: str | None = None
    prompt_version: str = PROMPT_VERSION
    parser_runner: ParserRunner | None = None
    capability_registry: CapabilityRegistry | None = None
    ocr: OcrEngine | None = None


def build_default_dependencies(
    settings: Settings | None = None,
    *,
    parser_runner: ParserRunner | None = None,
    capability_registry: CapabilityRegistry | None = None,
) -> PipelineDependencies:
    """Construct production dependencies at the worker boundary, not at import time."""

    active_settings = settings or get_settings()
    client = OllamaClient(active_settings)
    models = ModelManager(client, active_settings)
    adapters = AdapterRegistry()
    from clerksan.ingest.adapters.docx import DocxAdapter
    from clerksan.ingest.adapters.image import ImageAdapter
    from clerksan.ingest.adapters.markdown import MarkdownAdapter
    from clerksan.ingest.adapters.pdf import PdfAdapter
    from clerksan.ingest.adapters.xlsx import XlsxAdapter

    ocr: OcrEngine = get_ocr_engine(active_settings, client)
    if active_settings.ocr_engine.strip().lower() == "vision_llm":
        ocr = _ModelManagedOcr(ocr, models)
    limits = IngestLimits.from_settings(active_settings)
    adapters.register(ImageAdapter(ocr, limits=limits))
    adapters.register(PdfAdapter(ocr, active_settings, limits=limits))
    adapters.register(MarkdownAdapter())
    adapters.register(DocxAdapter(limits=limits))
    adapters.register(XlsxAdapter(limits=limits))
    return PipelineDependencies(
        settings=active_settings,
        adapters=adapters,
        client=client,
        models=models,
        read_bytes=_storage_reader(active_settings.storage_dir),
        write_normalized=_normalized_writer(active_settings.storage_dir),
        model_name=active_settings.extract_model,
        parser_runner=parser_runner,
        capability_registry=capability_registry,
        ocr=ocr,
    )


def _default_dependencies(settings: Settings | None = None) -> PipelineDependencies:
    """Reuse the local HTTP client across worker jobs with the same runtime settings."""

    active_settings = settings or get_settings()
    key = (
        active_settings.database_url,
        active_settings.ollama_url,
        str(active_settings.storage_dir),
        active_settings.ocr_engine,
    )
    cached = _CACHED_DEFAULT_DEPENDENCIES.get(key)
    if cached is None:
        cached = build_default_dependencies(active_settings)
        _CACHED_DEFAULT_DEPENDENCIES[key] = cached
    return cached


async def process_document(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    dependencies: PipelineDependencies | None = None,
) -> None:
    """Adapt, classify, extract, and submit one document to universal review.

    ``_job_id`` is supplied only by the worker.  It makes handler retries idempotent
    without making a new, intentionally enqueued reprocess job a no-op.
    """

    document_id = _document_id(payload)
    deps = dependencies or _default_dependencies()
    job = await _job_for_payload(session, payload, document_id)
    if _job_completed(job):
        return

    documents = DocumentRepo(session)
    document = await documents.get(document_id)
    original = _latest_original_detail(document["files"])
    if original is None:
        raise RuntimeError(f"document {document_id} has no original artifact")
    requested_source_version = _source_version(payload)
    source_version = requested_source_version or int(original["version"])
    if source_version != original["version"]:
        await _mark_job_skipped(job, source_version, int(original["version"]))
        return
    await _mark_source_intake_processing(
        session,
        document_id=document_id,
        source_file_id=UUID(str(original["id"])),
        source_version=source_version,
        actor=deps.actor,
    )
    universal = _universal_execution(payload)
    intake_intent = _payload_intake_intent(payload)
    extraction_id: UUID | None = None
    batch_id: UUID | None = None
    try:
        normalized = (
            await _load_or_create_universal_normalized(
                session,
                document,
                deps,
                source_version,
                payload,
            )
            if universal
            else await _load_or_create_normalized(session, document, deps, source_version)
        )
        if not await _source_version_is_current(session, document_id, source_version):
            await _mark_job_skipped_for_current_source(session, job, source_version, document_id)
            return
        if universal and intake_intent is IntakeIntent.GENERIC_FILE:
            if normalized.tables:
                await stage_tabular_rows(
                    document_id,
                    UUID(str(original["id"])),
                    source_version,
                    normalized.tables,
                    session,
                )
                await documents.set_status(
                    document_id,
                    DocumentStatus.NORMALIZED,
                    source_version=source_version,
                )
                await _mark_source_intake_outcome(
                    session,
                    document_id=document_id,
                    source_file_id=UUID(str(original["id"])),
                    source_version=source_version,
                    state=SourceIntakeState.NEEDS_MAPPING,
                    reason_code="mapping_required",
                    retryable=False,
                    actor=deps.actor,
                )
                await _mark_pipeline_outcome(
                    session,
                    job,
                    outcome="needs_mapping",
                    normalized=normalized,
                )
                return
            batch = await _persist_generic_candidate_batch(
                session,
                document_id=document_id,
                source_file_id=UUID(str(original["id"])),
                source_version=source_version,
                source_sha256=str(original["sha256"]),
                normalized=normalized,
                job=job,
            )
            if batch.candidate_count:
                await _enqueue_candidate_index(
                    session,
                    batch=batch,
                    intake_intent=intake_intent,
                    deps=deps,
                )
            await documents.set_status(
                document_id,
                DocumentStatus.IN_REVIEW,
                source_version=source_version,
            )
            await _mark_pipeline_outcome(
                session,
                job,
                outcome="in_review",
                normalized=normalized,
                batch_id=batch.id,
            )
            return
        await stage_spreadsheet_rows(
            session,
            document_id,
            normalized,
            source_version=source_version,
        )
        if not universal and normalized.metadata.detected_type in {
            FileType.DOCX,
            FileType.XLSX,
        }:
            await persist_embedded_media(
                session,
                document_id,
                raw=await _original_bytes(document, deps, source_version),
                normalized=normalized,
                storage_dir=deps.settings.storage_dir,
                limits=IngestLimits.from_settings(deps.settings),
                source_version=source_version,
                settings=deps.settings,
            )
        if not await _source_version_is_current(session, document_id, source_version):
            await _mark_job_skipped_for_current_source(session, job, source_version, document_id)
            return
        await documents.set_status(
            document_id,
            DocumentStatus.NORMALIZED,
            source_version=source_version,
        )

        classification = await deps.classifier(normalized, deps.client, deps.models)
        if not await _source_version_is_current(session, document_id, source_version):
            await _mark_job_skipped_for_current_source(session, job, source_version, document_id)
            return
        doc_class = _document_class(classification)
        orm_document = await _locked_document_for_source(session, document_id, source_version)
        orm_document.document_class = doc_class
        orm_document.status = DocumentStatus.EXTRACTED

        extraction = await deps.extractor(normalized, doc_class, deps.client, deps.models)
        if not await _source_version_is_current(session, document_id, source_version):
            await _mark_job_skipped_for_current_source(session, job, source_version, document_id)
            return
        extracted_payload = _extraction_payload(extraction)
        if universal:
            batch = await _persist_financial_candidate_batch(
                session,
                document_id=document_id,
                source_file_id=UUID(str(original["id"])),
                source_version=source_version,
                source_sha256=str(original["sha256"]),
                normalized=normalized,
                document_class=doc_class,
                extracted_payload=extracted_payload,
                field_confidences=_field_confidences(extracted_payload),
                producer=deps.model_name or deps.settings.extract_model,
                producer_version=deps.prompt_version,
                job=job,
            )
            batch_id = batch.id
            await _enqueue_candidate_index(
                session,
                batch=batch,
                intake_intent=intake_intent,
                deps=deps,
            )
        else:
            extraction_id = await ExtractionRepo(session).add(
                document_id,
                payload=extracted_payload,
                field_confidences=_field_confidences(extracted_payload),
                model_name=deps.model_name or deps.settings.extract_model,
                prompt_version=deps.prompt_version,
                actor=deps.actor,
                source_version=source_version,
            )
    except (
        EmbeddedMediaSourceVersionSupersededError,
        SourceVersionSupersededError,
        StagedSourceVersionSupersededError,
    ):
        await _mark_job_skipped_for_current_source(session, job, source_version, document_id)
        return
    try:
        await documents.set_status(
            document_id,
            DocumentStatus.IN_REVIEW,
            source_version=source_version,
        )
    except SourceVersionSupersededError:
        await _mark_job_skipped_for_current_source(session, job, source_version, document_id)
        return
    await check_duplicates(
        session,
        document_id,
        read_bytes=_verified_duplicate_reader(session, deps),
    )
    normalized_digest = hashlib.sha256(normalized.model_dump_json().encode("utf-8")).hexdigest()
    if not universal:
        from clerksan.ingest.jobs import enqueue

        await enqueue(
            session,
            job_type="index_document",
            payload={
                "document_id": str(document_id),
                "source_file_id": str(original["id"]),
                "source_version": source_version,
                "normalized_sha256": normalized_digest,
            },
            idempotency_key=f"index:{source_version}:{normalized_digest}",
            settings=deps.settings,
            required_components=_index_requirements(deps.settings),
            intake_intent=intake_intent,
            capability_registry=deps.capability_registry,
        )
    await _mark_source_intake_processed(
        session,
        document_id=document_id,
        source_file_id=UUID(str(original["id"])),
        source_version=source_version,
        actor=deps.actor,
    )
    if job is not None:
        result_identity = (
            {"batch_id": str(batch_id)}
            if batch_id is not None
            else {"extraction_id": str(extraction_id)}
        )
        job.payload = {
            **job.payload,
            "_pipeline": {"completed": True, **result_identity},
        }
        await session.flush()


async def rebuild_format_derivatives(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    dependencies: PipelineDependencies | None = None,
) -> None:
    """Rebuild only mutable OOXML projections for a current preserved source.

    The migration-created maintenance job deliberately does not change the document
    lifecycle or append an extraction: it restores spreadsheet rows and embedded-media
    OCR from an immutable original whose prior derived rows lacked source provenance.
    """

    document_id = _document_id(payload)
    deps = dependencies or _default_dependencies()
    job = await _job_for_payload(session, payload, document_id)
    if _job_completed(job):
        return
    document = await DocumentRepo(session).get(document_id)
    original = _latest_original_detail(document["files"])
    if original is None:
        await _mark_format_rebuild_skipped(session, job, "source_missing")
        return
    source_version = _source_version(payload) or int(original["version"])
    if source_version != original["version"]:
        await _mark_job_skipped(job, source_version, int(original["version"]))
        return
    try:
        normalized = await _load_or_create_normalized(session, document, deps, source_version)
        if not await _source_version_is_current(session, document_id, source_version):
            await _mark_job_skipped_for_current_source(session, job, source_version, document_id)
            return
        await stage_spreadsheet_rows(
            session,
            document_id,
            normalized,
            source_version=source_version,
        )
        if normalized.metadata.detected_type in {FileType.DOCX, FileType.XLSX}:
            await persist_embedded_media(
                session,
                document_id,
                raw=await _original_bytes(document, deps, source_version),
                normalized=normalized,
                storage_dir=deps.settings.storage_dir,
                limits=IngestLimits.from_settings(deps.settings),
                source_version=source_version,
                settings=deps.settings,
            )
    except (
        EmbeddedMediaSourceVersionSupersededError,
        SourceVersionSupersededError,
        StagedSourceVersionSupersededError,
    ):
        await _mark_job_skipped_for_current_source(session, job, source_version, document_id)
        return
    if job is not None:
        job.payload = {
            **job.payload,
            "_pipeline": {
                "completed": True,
                "format_derivatives_rebuilt_for_source_version": source_version,
            },
        }
        await session.flush()


def _universal_execution(payload: dict[str, Any]) -> bool:
    profile = payload.get("_execution_profile", ExecutionProfile.LEGACY_COMPAT.value)
    sandbox_verified = payload.get("_sandbox_verified", False)
    universal = profile == ExecutionProfile.UNIVERSAL_SANDBOXED.value
    if universal != (sandbox_verified is True):
        raise RuntimeError("job execution profile and sandbox evidence do not agree")
    return universal


def _payload_intake_intent(payload: dict[str, Any]) -> IntakeIntent:
    value = payload.get(
        "_intake_intent",
        payload.get("intake_intent", IntakeIntent.LEGACY_UNSPECIFIED.value),
    )
    try:
        return value if isinstance(value, IntakeIntent) else IntakeIntent(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("process job has no valid immutable intake intent") from error


async def _load_or_create_universal_normalized(
    session: AsyncSession,
    document: dict[str, Any],
    deps: PipelineDependencies,
    source_version: int,
    payload: dict[str, Any],
) -> NormalizedDocument:
    runner = deps.parser_runner
    registry = deps.capability_registry
    if runner is None or registry is None or not registry.sandbox_verified:
        raise RuntimeError("universal processing requires the verified parser sidecar")
    if (
        payload.get("_registry_digest") != registry.registry_digest
        or payload.get("_capabilities_digest") != registry.capabilities_digest
    ):
        raise RuntimeError("universal job capability evidence is stale")

    files = (
        await session.scalars(
            select(DocumentFile)
            .where(DocumentFile.document_id == document["id"])
            .order_by(DocumentFile.version.asc())
        )
    ).all()
    original = next(
        (
            item
            for item in files
            if item.kind is FileKind.ORIGINAL and item.version == source_version
        ),
        None,
    )
    if original is None or not await _source_version_is_current(
        session, document["id"], source_version
    ):
        raise SourceVersionSupersededError("the requested source version is no longer current")
    adapter_key = payload.get("adapter_key")
    detected_format = payload.get("detected_format")
    if not isinstance(adapter_key, str) or not adapter_key.strip():
        raise RuntimeError("universal job has no bound adapter key")
    if not isinstance(detected_format, str) or detected_format not in registry.process:
        raise RuntimeError("universal job format is not advertised by its registry")
    ocr_name = deps.ocr.name if deps.ocr is not None else "none"
    provenance = (
        f"universal_normalized:v2:source_version:{source_version}:"
        f"registry:{registry.registry_digest}:adapter:{adapter_key}:ocr:{ocr_name}"
    )
    existing = next(
        (
            item
            for item in reversed(files)
            if item.kind is FileKind.NORMALIZED
            and item.source_file_id == original.id
            and item.source_version == source_version
            and item.text_provenance == provenance
        ),
        None,
    )
    if existing is not None:
        encoded = await read_verified_artifact(
            deps.read_bytes, existing.content_path, existing.sha256
        )
        return NormalizedDocument.model_validate_json(encoded)

    source_path = resolve_storage_path(deps.settings.storage_dir, original.content_path)
    await asyncio.to_thread(verify_artifact_file, source_path, original.sha256)
    with source_path.open("rb") as source_stream:
        result = await runner.run_with_artifacts(
            adapter_key,
            ReadOnlySource(
                fd=source_stream.fileno(),
                source_sha256=original.sha256,
                source_id=str(original.id),
                source_version=source_version,
                filename=original.source_filename,
                mime_type=original.mime,
            ),
            AdapterContext(
                adapter_key=adapter_key,
                policy_version="universal-intake-v1",
                registry_digest=registry.registry_digest,
                metadata={
                    "detected_type": detected_format,
                    "canonical_mime": original.mime,
                },
            ),
            IngestLimits.from_settings(deps.settings),
        )
    normalized = result.normalized
    if normalized.metadata.sha256 != original.sha256:
        raise RuntimeError("normalized output is not bound to the immutable source digest")
    normalized = await _materialize_parser_artifacts(
        session,
        document_id=document["id"],
        source=original,
        adapter_key=adapter_key,
        result=result,
        deps=deps,
    )
    return await _persist_normalized_artifact(
        session,
        document_id=document["id"],
        source=original,
        normalized=normalized,
        provenance=provenance,
        deps=deps,
    )


async def _materialize_parser_artifacts(
    session: AsyncSession,
    *,
    document_id: UUID,
    source: DocumentFile,
    adapter_key: str,
    result: ParserRunResult,
    deps: PipelineDependencies,
) -> NormalizedDocument:
    """Persist trusted sidecar derivatives and OCR only their sanitized bytes."""

    normalized = result.normalized
    if adapter_key == "pdf":
        return await _materialize_pdf_artifacts(
            session,
            document_id=document_id,
            source=source,
            normalized=normalized,
            artifacts=result.artifacts,
            deps=deps,
        )
    image_artifacts = tuple(
        artifact
        for artifact in result.artifacts
        if artifact.descriptor.role is ArtifactRole.SANITIZED_IMAGE
    )
    if not image_artifacts:
        if result.artifacts:
            raise RuntimeError("parser returned an unexpected derivative artifact set")
        return normalized
    if len(image_artifacts) != 1 or len(result.artifacts) != 1:
        raise RuntimeError("image parser artifact set is not singular")
    persisted = await _persist_parser_page(
        session,
        document_id=document_id,
        source=source,
        artifact=image_artifacts[0],
        deps=deps,
    )
    if persisted.ocr_result is None:
        raise RuntimeError("sanitized image was not passed through the configured OCR engine")
    return _normalized_after_image_ocr(normalized, persisted)


async def _materialize_pdf_artifacts(
    session: AsyncSession,
    *,
    document_id: UUID,
    source: DocumentFile,
    normalized: NormalizedDocument,
    artifacts: tuple[ParserArtifact, ...],
    deps: PipelineDependencies,
) -> NormalizedDocument:
    extra = normalized.metadata.extra
    page_count = extra.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise RuntimeError("PDF normalized output has no valid page count")
    preview_status = extra.get("preview_status")
    if preview_status == PdfPreviewStatus.UNAVAILABLE.value:
        required_pages = extra.get("ocr_required_pages")
        if isinstance(required_pages, list) and required_pages:
            raise RuntimeError(
                "PDF pages require OCR but no complete sanitized page set is available"
            )
        await _persist_pdf_preview_manifest(
            session,
            document_id=document_id,
            source=source,
            page_count=page_count,
            pages=(),
            unavailable_reason=str(extra.get("preview_unavailable_reason") or "render_unavailable"),
            deps=deps,
        )
        return normalized
    if preview_status != PdfPreviewStatus.READY.value:
        raise RuntimeError("PDF normalized output has no explicit preview outcome")

    page_artifacts = tuple(
        artifact for artifact in artifacts if artifact.descriptor.role is ArtifactRole.PDF_PAGE
    )
    parser_manifests = tuple(
        artifact
        for artifact in artifacts
        if artifact.descriptor.role is ArtifactRole.PDF_PREVIEW_MANIFEST
    )
    if len(page_artifacts) != page_count or len(parser_manifests) != 1:
        raise RuntimeError("PDF parser artifact set is incomplete")
    persisted_pages: list[_PersistedParserPage] = []
    for artifact in page_artifacts:
        persisted_pages.append(
            await _persist_parser_page(
                session,
                document_id=document_id,
                source=source,
                artifact=artifact,
                deps=deps,
            )
        )
    materialized = _normalized_after_pdf_ocr(normalized, tuple(persisted_pages))
    await _persist_pdf_preview_manifest(
        session,
        document_id=document_id,
        source=source,
        page_count=page_count,
        pages=tuple(persisted_pages),
        unavailable_reason=None,
        deps=deps,
    )
    return materialized


async def _persist_parser_page(
    session: AsyncSession,
    *,
    document_id: UUID,
    source: DocumentFile,
    artifact: ParserArtifact,
    deps: PipelineDependencies,
) -> _PersistedParserPage:
    descriptor = artifact.descriptor
    if descriptor.role not in {ArtifactRole.SANITIZED_IMAGE, ArtifactRole.PDF_PAGE}:
        raise RuntimeError("only sanitized raster artifacts may become page renders")
    if descriptor.page_number is None:
        raise RuntimeError("sanitized raster artifact has no page number")
    reservation, content_path = await asyncio.to_thread(
        _publish_artifact_bytes,
        deps.settings.storage_dir,
        artifact.data,
        descriptor.sha256,
    )
    _remember_artifact_reservation(session, reservation)
    ocr_result: OcrResult | None = None
    if descriptor.ocr_required:
        if deps.ocr is None:
            raise RuntimeError("sanitized artifact requires OCR but no OCR engine is configured")
        ocr_result = await deps.ocr.ocr(artifact.data)
        if not ocr_result.text.strip():
            raise RuntimeError("OCR returned no text for a required sanitized artifact")
    provenance = (
        f"sandboxed_{descriptor.role.value}:source:{source.id}:"
        f"version:{source.version}:sha256:{descriptor.sha256}"
    )
    try:
        async with session.begin_nested():
            artifact_id = await DocumentRepo(session).add_artifact(
                document_id,
                kind=FileKind.PAGE_RENDER,
                content_path=content_path,
                sha256=descriptor.sha256,
                mime=descriptor.media_type,
                source_file_id=source.id,
                source_version=source.version,
                page_number=descriptor.page_number,
                ocr_text=ocr_result.text.strip() if ocr_result is not None else None,
                text_provenance=provenance,
            )
    except IntegrityError:
        winner = await session.scalar(
            select(DocumentFile).where(
                DocumentFile.kind == FileKind.PAGE_RENDER,
                DocumentFile.source_file_id == source.id,
                DocumentFile.source_version == source.version,
                DocumentFile.page_number == descriptor.page_number,
            )
        )
        if winner is None:
            raise
        if winner.sha256 != descriptor.sha256 or winner.mime != descriptor.media_type:
            raise RuntimeError("persisted page slot does not match the parser artifact")
        await read_verified_artifact(deps.read_bytes, winner.content_path, winner.sha256)
        artifact_id = winner.id
        content_path = winner.content_path
    return _PersistedParserPage(
        artifact_id=artifact_id,
        artifact=artifact,
        content_path=content_path,
        ocr_result=ocr_result,
    )


def _normalized_after_image_ocr(
    normalized: NormalizedDocument, page: _PersistedParserPage
) -> NormalizedDocument:
    result = page.ocr_result
    if result is None:
        raise RuntimeError("image OCR result is unavailable")
    extra = dict(normalized.metadata.extra)
    extra.update(
        {
            "ocr_required": False,
            "ocr_required_pages": [],
            "ocr_completed_pages": [1],
            "ocr_engine": result.engine or "unknown",
            "ocr_confidence_is_self_reported": result.confidence_is_self_reported,
        }
    )
    image = normalized.images[0].model_copy(update={"content_path": page.content_path})
    return normalized.model_copy(
        update={
            "markdown_body": result.text.strip(),
            "metadata": normalized.metadata.model_copy(
                update={"page_provenance": ["ocr"], "extra": extra}
            ),
            "images": [image],
            "embeddable": True,
        }
    )


def _normalized_after_pdf_ocr(
    normalized: NormalizedDocument, pages: tuple[_PersistedParserPage, ...]
) -> NormalizedDocument:
    separator = "\n\n---\n\n"
    bodies = normalized.markdown_body.split(separator)
    provenance = list(normalized.metadata.page_provenance)
    if len(bodies) != len(pages) or len(provenance) != len(pages):
        raise RuntimeError("PDF text, provenance, and page artifact counts do not agree")
    completed: list[int] = []
    self_reported = False
    engine_names: set[str] = set()
    content_paths: dict[str, str] = {}
    for persisted in pages:
        descriptor = persisted.artifact.descriptor
        if descriptor.page_number is None or descriptor.source_location is None:
            raise RuntimeError("PDF page artifact has incomplete source linkage")
        content_paths[descriptor.source_location] = persisted.content_path
        if not descriptor.ocr_required:
            continue
        page_index = descriptor.page_number - 1
        if provenance[page_index] != "ocr_required" or bodies[page_index].strip():
            raise RuntimeError("PDF OCR target does not match an exact blank source page")
        result = persisted.ocr_result
        if result is None or not result.text.strip():
            raise RuntimeError("PDF OCR result is unavailable for a required page")
        bodies[page_index] = result.text.strip()
        provenance[page_index] = "ocr"
        completed.append(descriptor.page_number)
        self_reported = self_reported or result.confidence_is_self_reported
        engine_names.add(result.engine or "unknown")
    images = [
        image.model_copy(update={"content_path": content_paths[image.source_location]})
        for image in normalized.images
        if image.source_location in content_paths
    ]
    if len(images) != len(normalized.images):
        raise RuntimeError("PDF normalized image linkage is incomplete after persistence")
    extra = dict(normalized.metadata.extra)
    extra.update(
        {
            "ocr_required": False,
            "ocr_required_pages": [],
            "ocr_completed_pages": completed,
            "ocr_engines": sorted(engine_names),
            "ocr_confidence_is_self_reported": self_reported,
        }
    )
    markdown_body = separator.join(bodies)
    return normalized.model_copy(
        update={
            "markdown_body": markdown_body,
            "metadata": normalized.metadata.model_copy(
                update={"page_provenance": provenance, "extra": extra}
            ),
            "images": images,
            "embeddable": bool("".join(bodies).strip()),
        }
    )


async def _persist_pdf_preview_manifest(
    session: AsyncSession,
    *,
    document_id: UUID,
    source: DocumentFile,
    page_count: int,
    pages: tuple[_PersistedParserPage, ...],
    unavailable_reason: str | None,
    deps: PipelineDependencies,
) -> PdfPreviewManifest:
    status = PdfPreviewStatus.READY if pages else PdfPreviewStatus.UNAVAILABLE
    page_descriptors = [
        PdfPreviewPageDescriptor(
            page_number=page.artifact.descriptor.page_number,
            artifact_id=page.artifact_id,
            sha256=page.artifact.descriptor.sha256,
            mime=page.artifact.descriptor.media_type,
            width=page.artifact.descriptor.width,
            height=page.artifact.descriptor.height,
            byte_size=page.artifact.descriptor.byte_size,
        )
        for page in pages
    ]
    identity = {
        "schema_version": 1,
        "document_id": str(document_id),
        "source_file_id": str(source.id),
        "source_version": source.version,
        "source_sha256": source.sha256,
        "page_count": page_count,
        "status": status.value,
        "pages": [page.model_dump(mode="json") for page in page_descriptors],
        "unavailable_reason": unavailable_reason,
    }
    manifest = PdfPreviewManifest(
        **identity,
        manifest_sha256=canonical_digest(identity),
    )
    encoded = canonical_json(manifest.model_dump(mode="json")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    reservation, content_path = await asyncio.to_thread(
        _publish_artifact_bytes,
        deps.settings.storage_dir,
        encoded,
        digest,
    )
    _remember_artifact_reservation(session, reservation)
    provenance = f"pdf_preview_manifest:v1:source:{source.id}:version:{source.version}"
    try:
        async with session.begin_nested():
            await DocumentRepo(session).add_artifact(
                document_id,
                kind=FileKind.NORMALIZED,
                content_path=content_path,
                sha256=digest,
                mime=PDF_PREVIEW_MANIFEST_MIME,
                source_file_id=source.id,
                source_version=source.version,
                text_provenance=provenance,
            )
    except IntegrityError:
        winner = await session.scalar(
            select(DocumentFile).where(
                DocumentFile.source_file_id == source.id,
                DocumentFile.source_version == source.version,
                DocumentFile.mime == PDF_PREVIEW_MANIFEST_MIME,
            )
        )
        if winner is None:
            raise
        winner_bytes = await read_verified_artifact(
            deps.read_bytes, winner.content_path, winner.sha256
        )
        winner_manifest = PdfPreviewManifest.model_validate_json(winner_bytes)
        if winner_manifest != manifest:
            raise RuntimeError("persisted PDF preview manifest does not match its page set")
        return winner_manifest
    return manifest


def _publish_artifact_bytes(
    storage_dir: Path, content: bytes, expected_sha256: str
) -> tuple[QuarantineReservation, str]:
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise RuntimeError("artifact bytes do not match their declared digest")
    reservation = reserve_quarantine(storage_dir)
    with reservation.payload_path.open("wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    published = publish_reserved_blob(
        reservation,
        expected_sha256,
        namespace="derivatives",
    )
    return reservation, published.relative_path


def _remember_artifact_reservation(
    session: AsyncSession, reservation: QuarantineReservation
) -> None:
    reservations = session.info.setdefault(_ARTIFACT_RESERVATIONS_KEY, [])
    if not isinstance(reservations, list):
        raise RuntimeError("pipeline artifact reservation state is invalid")
    reservations.append(reservation)


def take_committed_artifact_reservations(
    session: AsyncSession,
) -> tuple[QuarantineReservation, ...]:
    """Detach reservations only after the caller's transaction has committed."""

    reservations = session.info.pop(_ARTIFACT_RESERVATIONS_KEY, [])
    if not isinstance(reservations, list) or not all(
        isinstance(item, QuarantineReservation) for item in reservations
    ):
        raise RuntimeError("pipeline artifact reservation state is invalid")
    return tuple(reservations)


async def _load_or_create_normalized(
    session: AsyncSession,
    document: dict[str, Any],
    deps: PipelineDependencies,
    source_version: int,
) -> NormalizedDocument:
    # Query artifacts directly instead of relying on an ORM relationship cached before a
    # previous retry committed; reruns must see the normalized artifact it created.
    files = (
        await session.scalars(
            select(DocumentFile)
            .where(DocumentFile.document_id == document["id"])
            .order_by(DocumentFile.version.asc())
        )
    ).all()
    original = next(
        (
            item
            for item in files
            if item.kind is FileKind.ORIGINAL and item.version == source_version
        ),
        None,
    )
    if original is None or not await _source_version_is_current(
        session, document["id"], source_version
    ):
        raise SourceVersionSupersededError("the requested source version is no longer current")
    raw = await read_verified_artifact(deps.read_bytes, original.content_path, original.sha256)
    provenance = _normalized_provenance(original.version)
    existing = next(
        (
            item
            for item in reversed(files)
            if item.kind is FileKind.NORMALIZED
            and item.source_file_id == original.id
            and item.source_version == source_version
            and item.text_provenance == provenance
        ),
        None,
    )
    if existing is not None:
        encoded = await read_verified_artifact(
            deps.read_bytes, existing.content_path, existing.sha256
        )
        return NormalizedDocument.model_validate_json(encoded)

    detected_type = detect_file_type(raw, original.source_filename)
    metadata = DocMetadata(
        filename=original.source_filename,
        detected_type=detected_type,
        sha256=original.sha256,
        extra={"content_path": original.content_path},
    )
    normalized = await deps.adapters.get(detected_type).adapt(raw, metadata)
    return await _persist_normalized_artifact(
        session,
        document_id=document["id"],
        source=original,
        normalized=normalized,
        provenance=provenance,
        deps=deps,
    )


async def _persist_normalized_artifact(
    session: AsyncSession,
    *,
    document_id: UUID,
    source: DocumentFile,
    normalized: NormalizedDocument,
    provenance: str,
    deps: PipelineDependencies,
) -> NormalizedDocument:
    encoded = normalized.model_dump_json().encode("utf-8")
    content_path = await deps.write_normalized(document_id, encoded)
    try:
        async with session.begin_nested():
            await DocumentRepo(session).add_artifact(
                document_id,
                kind=FileKind.NORMALIZED,
                content_path=content_path,
                sha256=hashlib.sha256(encoded).hexdigest(),
                mime="application/json",
                ocr_text=normalized.markdown_body,
                text_provenance=provenance,
                source_file_id=source.id,
                source_version=source.version,
            )
    except IntegrityError:
        # Another worker completed this deterministic stage while this job was adapting
        # the same original.  Consume the persisted winner rather than create a second
        # artifact or let a safe reprocess fail on its uniqueness guard.
        winner = await session.scalar(
            select(DocumentFile)
            .where(
                DocumentFile.document_id == document_id,
                DocumentFile.kind == FileKind.NORMALIZED,
                DocumentFile.source_file_id == source.id,
                DocumentFile.source_version == source.version,
                DocumentFile.text_provenance == provenance,
            )
            .order_by(DocumentFile.version.desc())
            .limit(1)
        )
        if winner is None:
            raise
        encoded = await read_verified_artifact(deps.read_bytes, winner.content_path, winner.sha256)
        return NormalizedDocument.model_validate_json(encoded)
    return normalized


async def _job_for_payload(
    session: AsyncSession, payload: dict[str, Any], document_id: UUID
) -> Job | None:
    """Load the handler's job without retaining a row lock during model work.

    The worker lease already provides exclusive handling. The job is updated only
    with the idempotency marker at the end of the handler transaction, so a lease
    heartbeat can renew independently while OCR and extraction are running.
    """

    job_value = payload.get("_job_id")
    if job_value is None:
        return None
    try:
        job_id = UUID(str(job_value))
    except (TypeError, ValueError) as error:
        raise ValueError("_job_id must be a UUID") from error
    job = await session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise LookupError(f"job {job_id} no longer exists")
    if job.document_id != document_id:
        raise ValueError("job document_id does not match payload.document_id")
    return job


def _job_completed(job: Job | None) -> bool:
    if job is None:
        return False
    state = job.payload.get("_pipeline")
    return isinstance(state, dict) and state.get("completed") is True


def _document_id(payload: dict[str, Any]) -> UUID:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a JSON object")
    try:
        return UUID(str(payload["document_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("payload.document_id must be a UUID") from error


def _source_version(payload: dict[str, Any]) -> int | None:
    value = payload.get("source_version")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("payload.source_version must be a positive integer")
    try:
        version = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("payload.source_version must be a positive integer") from error
    if version < 1 or str(version) != str(value).strip():
        raise ValueError("payload.source_version must be a positive integer")
    return version


def _document_class(classification: ClassificationResult) -> DocumentClass:
    label = classification.label
    if isinstance(label, DocumentClass):
        return label
    try:
        return DocumentClass(str(label))
    except ValueError as error:
        raise ValueError(f"classifier returned an unsupported document class: {label!r}") from error


def _extraction_payload(extraction: Any) -> dict[str, Any]:
    if hasattr(extraction, "model_dump"):
        payload = extraction.model_dump(mode="json")
    elif isinstance(extraction, dict):
        payload = extraction
    else:
        raise TypeError("extractor must return a Pydantic model or JSON object")
    if not isinstance(payload, dict):
        raise TypeError("extractor payload must be a JSON object")
    return payload


def _field_confidences(payload: dict[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            confidence = value.get("confidence")
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
                values[path] = float(confidence)
            for key, child in value.items():
                if key not in {"value", "confidence", "source_span"}:
                    visit(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    for key, value in payload.items():
        visit(value, key)
    return values


async def _persist_generic_candidate_batch(
    session: AsyncSession,
    *,
    document_id: UUID,
    source_file_id: UUID,
    source_version: int,
    source_sha256: str,
    normalized: NormalizedDocument,
    job: Job | None,
) -> ExtractionBatchSummary:
    intake = await SourceIntakeRepo(session).get_for_source(
        document_id, source_file_id, source_version
    )
    if intake is None:
        raise RuntimeError("the exact immutable source has no intake projection")
    body = normalized.markdown_body.strip()
    locator = canonical_locator("document", "body")
    candidates: tuple[CandidateDraft, ...]
    ledger: CompositionLedger
    if body:
        candidate_payload = {
            "title": normalized.metadata.filename,
            "content_markdown": body,
            "detected_type": normalized.metadata.detected_type.value,
            "canonical_mime": normalized.metadata.canonical_mime,
        }
        item_hash = value_fingerprint(candidate_payload)
        candidate_key = build_candidate_key(
            source_sha256=source_sha256,
            source_locator=locator,
            candidate_ordinal=1,
            normalized_item_hash=item_hash,
            record_kind=RecordKind.GENERIC_DOCUMENT,
            financial_subtype=None,
            mapping_version=1,
        )
        candidates = (
            CandidateDraft(
                candidate_ordinal=1,
                candidate_key=candidate_key,
                record_kind=RecordKind.GENERIC_DOCUMENT,
                financial_subtype=None,
                payload=candidate_payload,
                confidences={},
                source_locator=locator,
                row_fingerprint=item_hash,
                evidence_group_keys=tuple(normalized.metadata.page_provenance),
            ),
        )
        ledger = CompositionLedger(
            (
                StructuralUnitDecision(
                    unit_id=locator,
                    locator=locator,
                    content_digest=value_fingerprint(body),
                    disposition=StructuralDisposition.RESIDUAL_GENERIC_CANDIDATE,
                    candidate_key=candidate_key,
                ),
            )
        )
    else:
        candidates = ()
        ledger = CompositionLedger()
    normalized_sha256 = _normalized_sha256(normalized)
    return await ExtractionBatchRepo(session).add_candidate_batch(
        document_id,
        source_intake_id=intake.id,
        source_file_id=source_file_id,
        source_version=source_version,
        source_sha256=source_sha256,
        normalized_sha256=normalized_sha256,
        structure_fingerprint=_normalized_structure_fingerprint(normalized),
        candidates=candidates,
        ledger=ledger,
        producer="structural-parser",
        producer_version="1",
        origin="generic_document",
        idempotency_key=_candidate_batch_key(job, "generic", normalized_sha256),
        producer_job_id=job.id if job is not None else None,
    )


async def _persist_financial_candidate_batch(
    session: AsyncSession,
    *,
    document_id: UUID,
    source_file_id: UUID,
    source_version: int,
    source_sha256: str,
    normalized: NormalizedDocument,
    document_class: DocumentClass,
    extracted_payload: dict[str, Any],
    field_confidences: dict[str, float],
    producer: str,
    producer_version: str,
    job: Job | None,
) -> ExtractionBatchSummary:
    intake = await SourceIntakeRepo(session).get_for_source(
        document_id, source_file_id, source_version
    )
    if intake is None:
        raise RuntimeError("the exact immutable source has no intake projection")
    locator = canonical_locator("document", "body")
    item_hash = value_fingerprint(extracted_payload)
    subtype = _financial_subtype(document_class)
    candidate_key = build_candidate_key(
        source_sha256=source_sha256,
        source_locator=locator,
        candidate_ordinal=1,
        normalized_item_hash=item_hash,
        record_kind=RecordKind.FINANCIAL,
        financial_subtype=subtype,
        mapping_version=1,
    )
    candidate = CandidateDraft(
        candidate_ordinal=1,
        candidate_key=candidate_key,
        record_kind=RecordKind.FINANCIAL,
        financial_subtype=subtype,
        payload=extracted_payload,
        confidences=field_confidences,
        source_locator=locator,
        row_fingerprint=item_hash,
        evidence_group_keys=tuple(normalized.metadata.page_provenance),
    )
    ledger = CompositionLedger(
        (
            StructuralUnitDecision(
                unit_id=locator,
                locator=locator,
                content_digest=value_fingerprint(normalized.markdown_body),
                disposition=StructuralDisposition.MAPPED_CANDIDATE,
                candidate_key=candidate_key,
            ),
        )
    )
    normalized_sha256 = _normalized_sha256(normalized)
    return await ExtractionBatchRepo(session).add_candidate_batch(
        document_id,
        source_intake_id=intake.id,
        source_file_id=source_file_id,
        source_version=source_version,
        source_sha256=source_sha256,
        normalized_sha256=normalized_sha256,
        structure_fingerprint=_normalized_structure_fingerprint(normalized),
        candidates=(candidate,),
        ledger=ledger,
        producer=producer,
        producer_version=producer_version,
        origin="financial_extraction",
        idempotency_key=_candidate_batch_key(job, "financial", normalized_sha256),
        producer_job_id=job.id if job is not None else None,
    )


def _financial_subtype(document_class: DocumentClass) -> FinancialSubtype:
    return {
        DocumentClass.RECEIPT: FinancialSubtype.RECEIPT,
        DocumentClass.INVOICE: FinancialSubtype.INVOICE,
        DocumentClass.BILL: FinancialSubtype.BILL,
        DocumentClass.RECURRING_BILL: FinancialSubtype.RECURRING_BILL,
        DocumentClass.QUOTE: FinancialSubtype.QUOTE,
        DocumentClass.OTHER: FinancialSubtype.OTHER_FINANCIAL,
    }[document_class]


def _normalized_sha256(normalized: NormalizedDocument) -> str:
    return hashlib.sha256(normalized.model_dump_json().encode("utf-8")).hexdigest()


def _normalized_structure_fingerprint(normalized: NormalizedDocument) -> str:
    return canonical_digest(
        {
            "body_sha256": value_fingerprint(normalized.markdown_body),
            "images": [image.source_location for image in normalized.images],
            "tables": [table.source_location for table in normalized.tables],
        }
    )


def _candidate_batch_key(job: Job | None, origin: str, normalized_sha256: str) -> str:
    return f"job:{job.id}" if job is not None else f"{origin}:{normalized_sha256}"


def _storage_reader(storage_dir: Path) -> ReadBytes:
    async def read(content_path: str) -> bytes:
        path = _storage_path(storage_dir, content_path)
        return await asyncio.to_thread(path.read_bytes)

    return read


async def _original_bytes(
    document: dict[str, Any], deps: PipelineDependencies, source_version: int
) -> bytes:
    original = next(
        (
            file
            for file in document["files"]
            if file["kind"] == FileKind.ORIGINAL.value and file["version"] == source_version
        ),
        None,
    )
    if original is None:
        raise RuntimeError(f"document {document['id']} has no original artifact")
    return await read_verified_artifact(
        deps.read_bytes, str(original["content_path"]), str(original["sha256"])
    )


def _verified_duplicate_reader(session: AsyncSession, deps: PipelineDependencies) -> ReadBytes:
    """Give duplicate detection only checksum-verified persisted image bytes."""

    async def read(content_path: str) -> bytes:
        artifact = await session.scalar(
            select(DocumentFile)
            .where(DocumentFile.content_path == content_path)
            .order_by(DocumentFile.version.desc())
            .limit(1)
        )
        if artifact is None:
            raise OSError(f"persisted artifact {content_path!r} does not exist")
        return await read_verified_artifact(deps.read_bytes, artifact.content_path, artifact.sha256)

    return read


def _latest_original_detail(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    originals = [file for file in files if file["kind"] == FileKind.ORIGINAL.value]
    return max(
        originals,
        key=lambda file: (int(file["version"]), str(file["id"])),
        default=None,
    )


def _normalized_provenance(source_version: int) -> str:
    return f"normalized_document:source_version:{source_version}"


def _index_requirements(settings: Settings) -> tuple[str, ...]:
    if not settings.embed_model:
        return ()
    return (f"model:{settings.embed_model}",)


async def _enqueue_candidate_index(
    session: AsyncSession,
    *,
    batch: ExtractionBatchSummary,
    intake_intent: IntakeIntent,
    deps: PipelineDependencies,
) -> None:
    """Queue hidden candidate chunks; activation alone makes them visible."""

    from clerksan.ingest.jobs import enqueue

    await enqueue(
        session,
        job_type="index_candidate_batch",
        payload={
            "document_id": str(batch.document_id),
            "batch_id": str(batch.id),
            "source_intake_id": str(batch.source_intake_id),
            "source_file_id": str(batch.source_file_id),
            "source_version": batch.source_version,
            "normalized_sha256": batch.normalized_sha256,
        },
        idempotency_key=f"candidate-index:{batch.id}:{batch.normalized_sha256}",
        settings=deps.settings,
        required_components=_index_requirements(deps.settings),
        intake_intent=intake_intent,
        capability_registry=deps.capability_registry,
    )


async def _mark_source_intake_processing(
    session: AsyncSession,
    *,
    document_id: UUID,
    source_file_id: UUID,
    source_version: int,
    actor: str,
) -> None:
    intakes = SourceIntakeRepo(session)
    intake = await intakes.get_for_source(document_id, source_file_id, source_version)
    if intake is None:
        raise RuntimeError("the exact immutable source has no intake projection")

    version = intake.version
    state = intake.state
    if state in {SourceIntakeState.PROCESSED, SourceIntakeState.FAILED}:
        version = await intakes.transition(
            intake.id,
            expected_version=version,
            state=SourceIntakeState.QUEUED,
            actor=actor,
            reason_code="processing_queued",
        )
        state = SourceIntakeState.QUEUED
    if state in {
        SourceIntakeState.QUEUED,
        SourceIntakeState.NEEDS_MAPPING,
        SourceIntakeState.STORED_UNPROCESSED,
    }:
        await intakes.transition(
            intake.id,
            expected_version=version,
            state=SourceIntakeState.PROCESSING,
            actor=actor,
            reason_code="processing_queued",
        )
        return
    if state is not SourceIntakeState.PROCESSING:
        raise RuntimeError(f"source intake cannot start processing from {state.value}")


async def _mark_source_intake_processed(
    session: AsyncSession,
    *,
    document_id: UUID,
    source_file_id: UUID,
    source_version: int,
    actor: str,
) -> None:
    intakes = SourceIntakeRepo(session)
    intake = await intakes.get_for_source(document_id, source_file_id, source_version)
    if intake is None:
        raise RuntimeError("the exact immutable source has no intake projection")
    if intake.state is SourceIntakeState.PROCESSED:
        return
    if intake.state not in {SourceIntakeState.QUEUED, SourceIntakeState.PROCESSING}:
        raise RuntimeError(f"source intake cannot finish processing from {intake.state.value}")
    await intakes.transition(
        intake.id,
        expected_version=intake.version,
        state=SourceIntakeState.PROCESSED,
        actor=actor,
        reason_code=None,
    )


async def _mark_source_intake_outcome(
    session: AsyncSession,
    *,
    document_id: UUID,
    source_file_id: UUID,
    source_version: int,
    state: SourceIntakeState,
    reason_code: str,
    retryable: bool,
    actor: str,
) -> None:
    intakes = SourceIntakeRepo(session)
    intake = await intakes.get_for_source(document_id, source_file_id, source_version)
    if intake is None:
        raise RuntimeError("the exact immutable source has no intake projection")
    if intake.state is state and intake.reason_code == reason_code:
        return
    if intake.state not in {SourceIntakeState.QUEUED, SourceIntakeState.PROCESSING}:
        raise RuntimeError(f"source intake cannot record {state.value} from {intake.state.value}")
    await intakes.transition(
        intake.id,
        expected_version=intake.version,
        state=state,
        actor=actor,
        reason_code=reason_code,
        retryable=retryable,
    )


async def _mark_pipeline_outcome(
    session: AsyncSession,
    job: Job | None,
    *,
    outcome: str,
    normalized: NormalizedDocument,
    batch_id: UUID | None = None,
) -> None:
    if job is None:
        return
    job.payload = {
        **job.payload,
        "_pipeline": {
            "completed": True,
            "outcome": outcome,
            "normalized_sha256": hashlib.sha256(
                normalized.model_dump_json().encode("utf-8")
            ).hexdigest(),
            **({"batch_id": str(batch_id)} if batch_id is not None else {}),
        },
    }
    await session.flush()


async def _mark_job_skipped(
    job: Job | None, requested_source_version: int, current_source_version: int
) -> None:
    if job is None:
        return
    job.payload = {
        **job.payload,
        "_pipeline": {
            "completed": True,
            "skipped": "source_version_replaced",
            "requested_source_version": requested_source_version,
            "current_source_version": current_source_version,
        },
    }


async def _mark_format_rebuild_skipped(session: AsyncSession, job: Job | None, reason: str) -> None:
    """Record a terminal maintenance no-op before the worker's separate done write."""

    if job is None:
        return
    job.payload = {
        **job.payload,
        "_pipeline": {"completed": True, "skipped": reason},
    }
    await session.flush()


async def _source_version_is_current(
    session: AsyncSession, document_id: UUID, source_version: int
) -> bool:
    current = await session.scalar(
        select(DocumentFile.version)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
        )
        .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
        .limit(1)
    )
    return current == source_version


async def _locked_document_for_source(
    session: AsyncSession, document_id: UUID, source_version: int
) -> Document:
    document = await session.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    if document is None:
        raise LookupError(f"document {document_id} no longer exists")
    if not await _source_version_is_current(session, document_id, source_version):
        raise SourceVersionSupersededError(
            "the source version was replaced before classification could be recorded"
        )
    return document


async def _mark_job_skipped_for_current_source(
    session: AsyncSession,
    job: Job | None,
    requested_source_version: int,
    document_id: UUID,
) -> None:
    current = await session.scalar(
        select(DocumentFile.version)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
        )
        .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
        .limit(1)
    )
    await _mark_job_skipped(job, requested_source_version, current or requested_source_version)


def _normalized_writer(storage_dir: Path) -> WriteNormalized:
    async def write(document_id: UUID, encoded: bytes) -> str:
        digest = hashlib.sha256(encoded).hexdigest()
        relative = Path("normalized") / str(document_id) / f"{digest}.json"
        target = storage_dir / relative
        await asyncio.to_thread(_write_once, target, encoded)
        return relative.as_posix()

    return write


def _storage_path(storage_dir: Path, content_path: str) -> Path:
    return resolve_storage_path(storage_dir, content_path)


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    expected_sha256 = hashlib.sha256(content).hexdigest()
    if path.exists():
        verify_artifact_file(path, expected_sha256)
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".normalized-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            verify_artifact_file(path, expected_sha256)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


async def check_duplicates(
    session: AsyncSession, document_id: UUID, *, read_bytes: ReadBytes | None = None
) -> None:
    """Persist non-destructive duplicate evidence for the universal review queue."""

    from clerksan.dedupe.detector import find_duplicates

    candidates = await find_duplicates(session, document_id, read_bytes=read_bytes)
    for candidate in candidates:
        suspect_id = UUID(str(candidate["document_id"]))
        flag = await session.scalar(
            select(DuplicateFlag)
            .where(
                DuplicateFlag.document_id == document_id,
                DuplicateFlag.suspected_document_id == suspect_id,
            )
            .with_for_update()
        )
        if flag is None:
            session.add(
                DuplicateFlag(
                    document_id=document_id,
                    suspected_document_id=suspect_id,
                    reason=str(candidate["reason"]),
                    score=float(candidate["score"]),
                    evidence=dict(candidate["evidence"]),
                )
            )
        else:
            flag.reason = str(candidate["reason"])
            flag.score = float(candidate["score"])
            flag.evidence = dict(candidate["evidence"])
    await session.flush()


register_handler("process_document", process_document)
register_handler("rebuild_format_derivatives", rebuild_format_derivatives)
