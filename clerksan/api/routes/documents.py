"""Verified-history, detail, and combined-condition search endpoints."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.api.deps import database_session, settings_from_request
from clerksan.api.intake_actions import plan_exact_source_reprocess
from clerksan.api.schemas import DerivativeRetryAccepted, DocumentOut, Page, ReprocessAccepted
from clerksan.config import Settings
from clerksan.db.audit import read_history
from clerksan.db.models import (
    Document,
    DocumentClass,
    DocumentFile,
    DocumentStatus,
    ExtractedRecord,
    FileKind,
    RecurringBill,
    VerifiedRecord,
)
from clerksan.db.repositories import DocumentRepo, SourceIntakeRepo
from clerksan.ingest.jobs import enqueue
from clerksan.ingest.limits import IngestLimits
from clerksan.ingest.normalized import (
    PDF_PREVIEW_MANIFEST_MIME,
    PdfPreviewManifest,
    PdfPreviewStatus,
    canonical_digest,
)
from clerksan.storage import (
    ArtifactIntegrityError,
    StoragePathError,
    resolve_storage_path,
    sha256_file,
)

router = APIRouter(tags=["documents"])


async def _document_with_history(session: AsyncSession, document_id: UUID) -> dict:
    document = await DocumentRepo(session).get(document_id)
    history_by_id: dict[int, dict[str, Any]] = {}
    for table, statement in (
        ("documents", select(Document.id).where(Document.id == document_id)),
        (
            "document_files",
            select(DocumentFile.id).where(DocumentFile.document_id == document_id),
        ),
        (
            "extracted_records",
            select(ExtractedRecord.id).where(ExtractedRecord.document_id == document_id),
        ),
        (
            "verified_records",
            select(VerifiedRecord.id).where(VerifiedRecord.document_id == document_id),
        ),
        (
            "recurring_bills",
            select(RecurringBill.id).where(RecurringBill.document_id == document_id),
        ),
    ):
        record_ids = (await session.scalars(statement)).all()
        for record_id in record_ids:
            for entry in await read_history(session, table=table, row_pk=str(record_id)):
                history_by_id.setdefault(entry["id"], entry)
    document["audit_history"] = sorted(
        history_by_id.values(),
        key=lambda entry: (entry["at"], entry["id"]),
        reverse=True,
    )
    return document


@router.get("/documents", response_model=Page[DocumentOut])
async def list_documents(
    doc_class: DocumentClass | None = None,
    status: DocumentStatus | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    counterparty: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(database_session),
) -> Page[DocumentOut]:
    documents = await DocumentRepo(session).list(
        doc_class=doc_class,
        status=status,
        date_from=date_from,
        date_to=date_to,
        amount_min=amount_min,
        amount_max=amount_max,
        counterparty=counterparty,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[DocumentOut(**document) for document in documents],
        limit=limit,
        offset=offset,
    )


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: UUID, session: AsyncSession = Depends(database_session)
) -> DocumentOut:
    return DocumentOut(**(await _document_with_history(session, document_id)))


@router.post(
    "/documents/{document_id}/reprocess",
    status_code=202,
    response_model=ReprocessAccepted,
)
async def reprocess_document(
    request: Request,
    document_id: UUID,
    actor: str = Query(default="local-user", min_length=1, pattern=r".*\S.*"),
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> ReprocessAccepted:
    """Queue exactly one source-bound reprocess after rejection or from verified history."""

    documents = DocumentRepo(session)
    intake = await documents.lock_current_reprocess_intake(document_id)
    execution = await plan_exact_source_reprocess(
        request,
        session,
        settings,
        intake,
    )
    target = await documents.prepare_reprocess(document_id, actor=actor.strip())
    detail = await documents.get(document_id)
    source = next(
        (
            item
            for item in detail["files"]
            if item["kind"] == "original" and item["version"] == target.original_version
        ),
        None,
    )
    if source is None:
        raise RuntimeError("reprocess target has no exact immutable source")
    current_intake = await SourceIntakeRepo(session).get_for_source(
        document_id,
        source["id"],
        target.original_version,
    )
    if current_intake is None or current_intake.id != intake.id:
        raise RuntimeError("reprocess target has no source intake projection")
    job_id = await enqueue(
        session,
        job_type="process_document",
        payload={
            "document_id": str(document_id),
            "source_file_id": str(source["id"]),
            "source_intake_id": str(current_intake.id),
            "source_version": target.original_version,
            **(
                {
                    "detected_format": execution.detected_format,
                    "adapter_key": execution.adapter_key,
                }
                if execution.detected_format is not None
                else {}
            ),
        },
        idempotency_key=target.idempotency_key,
        settings=settings,
        registry_digest=execution.registry.registry_digest,
        capabilities_digest=execution.registry.capabilities_digest,
        required_components=execution.required_components,
        intake_intent=current_intake.intake_intent,
        capability_registry=execution.registry,
    )
    return ReprocessAccepted(
        document_id=document_id,
        original_version=target.original_version,
        status="queued" if job_id is not None else "already_queued",
        job_id=job_id,
    )


@router.post(
    "/documents/{document_id}/retry-derivatives",
    status_code=202,
    response_model=DerivativeRetryAccepted,
)
async def retry_document_derivatives(
    document_id: UUID,
    session: AsyncSession = Depends(database_session),
) -> DerivativeRetryAccepted:
    """Retry terminal current-source indexing/OCR without changing review state."""

    target = await DocumentRepo(session).retry_current_derivatives(document_id)
    return DerivativeRetryAccepted(
        document_id=document_id,
        original_version=target.original_version,
        status="queued" if target.queued_job_ids else "nothing_to_retry",
        job_ids=list(target.queued_job_ids),
    )


@router.get("/documents/{document_id}/original", response_class=FileResponse)
async def open_original(
    document_id: UUID,
    version: int | None = Query(default=None, ge=1),
    source_file_id: UUID | None = Query(default=None),
    sha256: str | None = Query(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> FileResponse:
    """Serve one immutable source version without treating it as active web content."""

    document = await DocumentRepo(session).get(document_id)
    original = _original_for_identity(
        document["files"], version=version, source_file_id=source_file_id, sha256=sha256
    )
    if original is None:
        raise HTTPException(status_code=404, detail="Original artifact is unavailable")
    try:
        path = resolve_storage_path(settings.storage_dir, str(original["content_path"]))
    except StoragePathError as error:
        raise HTTPException(
            status_code=404, detail="Original artifact is outside local storage"
        ) from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Original artifact is missing")
    actual_sha256 = await asyncio.to_thread(sha256_file, path)
    if actual_sha256 != original["sha256"]:
        raise HTTPException(status_code=409, detail="Original artifact checksum mismatch")
    mime = str(original["mime"])
    return FileResponse(
        path,
        media_type=mime,
        filename=str(original["source_filename"]),
        content_disposition_type="inline" if _is_inline_preview(mime) else "attachment",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'; sandbox",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/documents/{document_id}/sources/{source_file_id}/pdf-preview",
    response_model=PdfPreviewManifest,
)
async def get_pdf_preview_manifest(
    document_id: UUID,
    source_file_id: UUID,
    version: int = Query(ge=1),
    sha256: str = Query(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> Any:
    """Return a complete inert page manifest for one exact immutable PDF source."""

    try:
        manifest, _ = await _load_pdf_preview(
            session,
            settings,
            document_id=document_id,
            source_file_id=source_file_id,
            source_version=version,
            source_sha256=sha256,
        )
    except _PdfPreviewRouteError as error:
        return error.response()
    return JSONResponse(
        content=manifest.model_dump(mode="json"),
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/documents/{document_id}/sources/{source_file_id}/pdf-preview/pages/{page_number}",
    response_class=FileResponse,
    responses={
        200: {
            "description": "One sanitized, manifest-bound PDF page image.",
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def get_pdf_preview_page(
    document_id: UUID,
    source_file_id: UUID,
    page_number: int,
    version: int = Query(ge=1),
    sha256: str = Query(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> Any:
    """Serve one manifest-linked sanitized PNG page, never the raw PDF bytes."""

    if page_number < 1:
        return _PdfPreviewRouteError(
            404,
            "pdf_preview_page_not_found",
            "PDF preview page is unavailable.",
        ).response()
    try:
        manifest, page_files = await _load_pdf_preview(
            session,
            settings,
            document_id=document_id,
            source_file_id=source_file_id,
            source_version=version,
            source_sha256=sha256,
        )
        descriptor = next(
            (page for page in manifest.pages if page.page_number == page_number),
            None,
        )
        artifact = page_files.get(page_number)
        if descriptor is None or artifact is None:
            raise _PdfPreviewRouteError(
                404,
                "pdf_preview_page_not_found",
                "PDF preview page is unavailable.",
            )
        path = await asyncio.to_thread(_verified_preview_path, settings, artifact)
    except _PdfPreviewRouteError as error:
        return error.response()
    return FileResponse(
        path,
        media_type="image/png",
        filename=f"page-{page_number}.png",
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


class _PdfPreviewRouteError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={"code": self.code, "message": self.message, "detail": self.detail},
            headers={"Cache-Control": "private, no-store"},
        )


async def _load_pdf_preview(
    session: AsyncSession,
    settings: Settings,
    *,
    document_id: UUID,
    source_file_id: UUID,
    source_version: int,
    source_sha256: str,
) -> tuple[PdfPreviewManifest, dict[int, DocumentFile]]:
    source = await session.scalar(
        select(DocumentFile).where(
            DocumentFile.document_id == document_id,
            DocumentFile.id == source_file_id,
            DocumentFile.kind == FileKind.ORIGINAL,
        )
    )
    if source is None:
        raise _PdfPreviewRouteError(
            404,
            "pdf_preview_not_found",
            "PDF preview source is unavailable.",
        )
    if source.version != source_version or source.sha256 != source_sha256:
        raise _PdfPreviewRouteError(
            409,
            "source_identity_mismatch",
            "PDF preview request does not match the immutable source identity.",
        )
    manifests = list(
        (
            await session.scalars(
                select(DocumentFile).where(
                    DocumentFile.document_id == document_id,
                    DocumentFile.source_file_id == source_file_id,
                    DocumentFile.source_version == source_version,
                    DocumentFile.mime == PDF_PREVIEW_MANIFEST_MIME,
                )
            )
        ).all()
    )
    if not manifests:
        raise _PdfPreviewRouteError(
            404,
            "pdf_preview_not_found",
            "PDF preview is unavailable for this source.",
        )
    if len(manifests) != 1:
        raise _PdfPreviewRouteError(
            409,
            "pdf_preview_inconsistent",
            "PDF preview manifest is inconsistent.",
        )
    manifest_file = manifests[0]
    try:
        encoded = await asyncio.to_thread(_read_verified_preview, settings, manifest_file)
        manifest = PdfPreviewManifest.model_validate_json(encoded)
    except (
        ArtifactIntegrityError,
        OSError,
        StoragePathError,
        ValidationError,
        ValueError,
    ) as error:
        raise _PdfPreviewRouteError(
            409,
            "pdf_preview_inconsistent",
            "PDF preview manifest failed integrity validation.",
        ) from error
    if (
        manifest.document_id != document_id
        or manifest.source_file_id != source_file_id
        or manifest.source_version != source_version
        or manifest.source_sha256 != source_sha256
    ):
        raise _PdfPreviewRouteError(
            409,
            "pdf_preview_inconsistent",
            "PDF preview manifest does not match its immutable source.",
        )
    manifest_identity = manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    if canonical_digest(manifest_identity) != manifest.manifest_sha256:
        raise _PdfPreviewRouteError(
            409,
            "pdf_preview_inconsistent",
            "PDF preview manifest digest is invalid.",
        )
    limits = IngestLimits.from_settings(settings)
    if manifest.page_count > limits.max_pdf_pages:
        raise _PdfPreviewRouteError(
            409,
            "pdf_preview_inconsistent",
            "PDF preview page count exceeds the configured limit.",
        )
    if manifest.status is PdfPreviewStatus.UNAVAILABLE:
        raise _PdfPreviewRouteError(
            409,
            "pdf_preview_unavailable",
            "A safe PDF preview could not be generated.",
            {"reason": manifest.unavailable_reason},
        )
    page_rows = list(
        (
            await session.scalars(
                select(DocumentFile)
                .where(
                    DocumentFile.document_id == document_id,
                    DocumentFile.source_file_id == source_file_id,
                    DocumentFile.source_version == source_version,
                    DocumentFile.kind == FileKind.PAGE_RENDER,
                )
                .order_by(DocumentFile.page_number.asc())
            )
        ).all()
    )
    page_files = {int(page.page_number): page for page in page_rows if page.page_number is not None}
    if list(page_files) != list(range(1, manifest.page_count + 1)):
        raise _PdfPreviewRouteError(
            409,
            "pdf_preview_inconsistent",
            "PDF preview page set is incomplete.",
        )
    for descriptor in manifest.pages:
        artifact = page_files.get(descriptor.page_number)
        if (
            artifact is None
            or artifact.id != descriptor.artifact_id
            or artifact.sha256 != descriptor.sha256
            or artifact.mime != descriptor.mime
        ):
            raise _PdfPreviewRouteError(
                409,
                "pdf_preview_inconsistent",
                "PDF preview page metadata is inconsistent.",
            )
        try:
            await asyncio.to_thread(_verified_preview_path, settings, artifact)
        except (ArtifactIntegrityError, OSError, StoragePathError) as error:
            raise _PdfPreviewRouteError(
                409,
                "pdf_preview_inconsistent",
                "PDF preview page failed integrity validation.",
            ) from error
    return manifest, page_files


def _verified_preview_path(settings: Settings, artifact: DocumentFile) -> Path:
    path = resolve_storage_path(settings.storage_dir, artifact.content_path)
    if not path.is_file():
        raise OSError("preview artifact is missing")
    actual_sha256 = sha256_file(path)
    if actual_sha256 != artifact.sha256:
        raise ArtifactIntegrityError("artifact checksum mismatch")
    return path


def _read_verified_preview(settings: Settings, artifact: DocumentFile) -> bytes:
    return _verified_preview_path(settings, artifact).read_bytes()


def _latest_original(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    originals = [file for file in files if file["kind"] == "original"]
    return max(
        originals,
        key=lambda file: (int(file["version"]), str(file["id"])),
        default=None,
    )


def _original_for_identity(
    files: list[dict[str, Any]],
    *,
    version: int | None,
    source_file_id: UUID | None,
    sha256: str | None,
) -> dict[str, Any] | None:
    """Resolve a preserved original by the extraction-bound identity, never by recency alone."""

    if version is None and source_file_id is None and sha256 is None:
        return _latest_original(files)
    candidates = [file for file in files if file["kind"] == "original"]
    for candidate in candidates:
        if version is not None and int(candidate["version"]) != version:
            continue
        if source_file_id is not None and str(candidate["id"]) != str(source_file_id):
            continue
        if sha256 is not None and str(candidate["sha256"]) != sha256:
            continue
        return candidate
    return None


def _is_inline_preview(mime: str) -> bool:
    """Limit in-browser inspection to media types that do not execute document markup."""

    return mime in {"image/jpeg", "image/png", "image/webp"}
