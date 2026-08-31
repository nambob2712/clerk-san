"""Persist and OCR images embedded in bounded DOCX/XLSX archives."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.config import Settings, get_settings
from clerksan.db.models import DocumentFile, EmbeddedMedia, FileKind, Job
from clerksan.ingest.filetype import MIME_BY_FILE_TYPE, FileType, detect_file_type
from clerksan.ingest.jobs import enqueue, register_handler
from clerksan.ingest.limits import IngestLimits, safe_zip_members
from clerksan.ingest.normalized import NormalizedDocument
from clerksan.llm.client import ModelManager, ModelRole, OllamaClient
from clerksan.llm.ocr import OcrEngine, get_ocr_engine
from clerksan.storage import read_verified_artifact, resolve_storage_path, verify_artifact_file

ReadBytes = Callable[[str], Awaitable[bytes]]


class EmbeddedMediaSourceVersionSupersededError(RuntimeError):
    """An embedded-media job belongs to an original that is no longer current."""


@dataclass(slots=True)
class EmbeddedMediaDependencies:
    """The testable local seams used by an embedded-media worker job."""

    settings: Settings
    client: OllamaClient
    models: ModelManager
    engine: OcrEngine
    read_bytes: ReadBytes


def build_default_dependencies(settings: Settings | None = None) -> EmbeddedMediaDependencies:
    """Construct local OCR dependencies without importing the API process."""

    active_settings = settings or get_settings()
    client = OllamaClient(active_settings)
    return EmbeddedMediaDependencies(
        settings=active_settings,
        client=client,
        models=ModelManager(client, active_settings),
        engine=get_ocr_engine(active_settings, client),
        read_bytes=_storage_reader(active_settings.storage_dir),
    )


async def persist_embedded_media(
    session: AsyncSession,
    document_id: UUID,
    *,
    raw: bytes,
    normalized: NormalizedDocument,
    storage_dir: Path,
    limits: IngestLimits,
    source_version: int,
    settings: Settings | None = None,
) -> int:
    """Write source-linked archive images once and enqueue one durable OCR job each."""

    if source_version < 1:
        raise ValueError("source_version must be greater than zero")
    if not await _source_version_is_current(session, document_id, source_version):
        raise EmbeddedMediaSourceVersionSupersededError(
            "the source version was replaced before embedded media could be persisted"
        )
    source_file_id = await _source_file_id(session, document_id, source_version)
    if source_file_id is None:
        raise EmbeddedMediaSourceVersionSupersededError(
            "the exact immutable source is unavailable for embedded media"
        )
    blobs = _embedded_blobs(raw, normalized, limits)
    for image in normalized.images:
        if not image.content_path.startswith("embedded/sha256/"):
            continue
        blob = blobs.get(image.sha256)
        if blob is None:
            raise ValueError(f"embedded media {image.sha256} is missing from its source archive")
        target = storage_dir / image.content_path
        await asyncio.to_thread(_write_once, target, blob)
        mime = _image_mime(blob, image.source_location or image.content_path)
        record = await session.scalar(
            select(EmbeddedMedia)
            .where(
                EmbeddedMedia.document_id == document_id,
                EmbeddedMedia.source_version == source_version,
                EmbeddedMedia.sha256 == image.sha256,
            )
            .with_for_update()
        )
        if record is None:
            session.add(
                EmbeddedMedia(
                    document_id=document_id,
                    source_version=source_version,
                    sha256=image.sha256,
                    content_path=image.content_path,
                    mime=mime,
                    source_location=image.source_location or image.content_path,
                    width=image.width,
                    height=image.height,
                )
            )
        elif record.content_path != image.content_path or record.source_location != (
            image.source_location or image.content_path
        ):
            raise ValueError("embedded media metadata conflicts with its immutable source linkage")
        await enqueue(
            session,
            job_type="process_embedded_media",
            payload={
                "document_id": str(document_id),
                "source_file_id": str(source_file_id),
                "source_version": source_version,
                "media_sha256": image.sha256,
            },
            idempotency_key=f"embedded_media:{source_version}:{image.sha256}",
            settings=settings,
            required_components=_ocr_requirements(settings),
        )
    await session.flush()
    return len(blobs)


async def process_embedded_media(
    session: AsyncSession,
    payload: dict[str, object],
    *,
    dependencies: EmbeddedMediaDependencies | None = None,
) -> None:
    """OCR one immutable archive image; an already-written result is retry-safe."""

    document_id = _document_id(payload)
    job = await _job_for_payload(session, payload, document_id)
    if _job_completed(job):
        return
    sha256 = _sha256(payload.get("media_sha256"))
    source_version = _source_version(payload)
    if source_version is None:
        await _mark_job_completed(session, job, media_sha256=sha256, source_version=None)
        return
    owns_client = dependencies is None
    deps = dependencies or build_default_dependencies()
    try:
        expected_source_file_id = _source_file_id_from_payload(payload)
        actual_source_file_id = await _source_file_id(session, document_id, source_version)
        if expected_source_file_id is not None and actual_source_file_id != expected_source_file_id:
            await _mark_job_completed(
                session,
                job,
                media_sha256=sha256,
                source_version=source_version,
            )
            return
        if not await _source_version_is_current(session, document_id, source_version):
            await _mark_job_completed(
                session,
                job,
                media_sha256=sha256,
                source_version=source_version,
            )
            return
        media = await session.scalar(
            select(EmbeddedMedia)
            .where(
                EmbeddedMedia.document_id == document_id,
                EmbeddedMedia.source_version == source_version,
                EmbeddedMedia.sha256 == sha256,
            )
            .with_for_update()
        )
        if media is None:
            raise LookupError(f"embedded media {sha256} for document {document_id} does not exist")
        if media.ocr_text is not None:
            await _mark_job_completed(
                session,
                job,
                media_sha256=sha256,
                source_version=source_version,
            )
            return
        if deps.settings.ocr_engine.strip().lower() == "vision_llm":
            await deps.models.ensure_loaded(ModelRole.OCR)
        path = resolve_storage_path(deps.settings.storage_dir, media.content_path)
        raw = await read_verified_artifact(deps.read_bytes, str(path), media.sha256)
        result = await deps.engine.ocr(raw)
        media.ocr_text = result.text
        media.ocr_engine = result.engine or deps.engine.name
        await _mark_job_completed(
            session,
            job,
            media_sha256=sha256,
            source_version=source_version,
        )
    finally:
        if owns_client:
            await deps.client.aclose()


def _embedded_blobs(
    raw: bytes, normalized: NormalizedDocument, limits: IngestLimits
) -> dict[str, bytes]:
    detected = normalized.metadata.detected_type
    prefix = {
        FileType.DOCX: "word/media/",
        FileType.XLSX: "xl/media/",
    }.get(detected)
    if prefix is None:
        return {}
    expected = {
        image.source_location: image.sha256
        for image in normalized.images
        if image.content_path.startswith("embedded/sha256/") and image.source_location
    }
    if not expected:
        return {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            safe_zip_members(archive, limits)
            blobs: dict[str, bytes] = {}
            for member in archive.infolist():
                if not member.filename.startswith(prefix) or member.filename not in expected:
                    continue
                blob = archive.read(member)
                digest = hashlib.sha256(blob).hexdigest()
                if digest != expected[member.filename]:
                    raise ValueError("embedded media digest changed after adapter validation")
                blobs[digest] = blob
    except zipfile.BadZipFile as error:
        raise ValueError("invalid OOXML archive") from error
    if set(blobs) != set(expected.values()):
        raise ValueError("expected embedded media member was not found")
    return blobs


def _image_mime(blob: bytes, source_location: str) -> str:
    detected = detect_file_type(blob, source_location)
    if detected not in {FileType.JPEG, FileType.PNG, FileType.WEBP}:
        raise ValueError("embedded media must be a supported image format")
    return MIME_BY_FILE_TYPE[detected]


def _document_id(payload: dict[str, object]) -> UUID:
    try:
        return UUID(str(payload["document_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("payload.document_id must be a UUID") from error


def _source_version(payload: dict[str, object]) -> int | None:
    value = payload.get("source_version")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _source_file_id_from_payload(payload: dict[str, object]) -> UUID | None:
    value = payload.get("source_file_id")
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError("payload.source_file_id must be a UUID") from error


async def _source_file_id(
    session: AsyncSession, document_id: UUID, source_version: int
) -> UUID | None:
    return await session.scalar(
        select(DocumentFile.id).where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
            DocumentFile.version == source_version,
        )
    )


def _ocr_requirements(settings: Settings | None) -> tuple[str, ...]:
    if settings is None or settings.ocr_engine.strip().lower() != "vision_llm":
        return ()
    return (f"model:{settings.ocr_model}",)


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


def _sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("payload.media_sha256 must be a 64-character digest")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError("payload.media_sha256 must be lowercase hexadecimal")
    return value


async def _job_for_payload(
    session: AsyncSession, payload: dict[str, object], document_id: UUID
) -> Job | None:
    """Load the leased job so completed OCR survives a hard worker kill."""

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
    pipeline = job.payload.get("_pipeline") if job is not None else None
    return isinstance(pipeline, dict) and pipeline.get("completed") is True


async def _mark_job_completed(
    session: AsyncSession,
    job: Job | None,
    *,
    media_sha256: str,
    source_version: int | None,
) -> None:
    if job is None:
        return
    job.payload = {
        **job.payload,
        "_pipeline": {
            "completed": True,
            "embedded_media_sha256": media_sha256,
            "source_version": source_version,
        },
    }
    await session.flush()


def _storage_reader(storage_dir: Path) -> ReadBytes:
    async def read(content_path: str) -> bytes:
        path = resolve_storage_path(storage_dir, content_path)
        return await asyncio.to_thread(path.read_bytes)

    return read


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    expected_sha256 = hashlib.sha256(content).hexdigest()
    if path.exists():
        verify_artifact_file(path, expected_sha256)
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".embedded-", dir=path.parent)
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


register_handler("process_embedded_media", process_embedded_media)
