from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.config import Settings
from clerksan.db.models import Base, EmbeddedMedia, Job
from clerksan.db.repositories import DocumentRepo
from clerksan.ingest.embedded_media import (
    EmbeddedMediaDependencies,
    EmbeddedMediaSourceVersionSupersededError,
    persist_embedded_media,
    process_embedded_media,
)
from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits
from clerksan.ingest.normalized import DocMetadata, ExtractedImage, NormalizedDocument
from clerksan.llm.ocr import OcrResult
from clerksan.storage import ArtifactIntegrityError


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'embedded-media.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _png() -> bytes:
    image = Image.new("RGB", (60, 60), color="teal")
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def _docx_archive(image: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/media/image1.png", image)
    return output.getvalue()


def _normalized(image: bytes) -> NormalizedDocument:
    digest = hashlib.sha256(image).hexdigest()
    return NormalizedDocument(
        markdown_body="Embedded image report",
        metadata=DocMetadata(
            filename="report.docx",
            detected_type=FileType.DOCX,
            sha256="a" * 64,
        ),
        images=[
            ExtractedImage(
                sha256=digest,
                content_path=f"embedded/sha256/{digest}",
                width=60,
                height=60,
                source_location="word/media/image1.png",
            )
        ],
    )


class _Engine:
    name = "test-ocr"

    def __init__(self) -> None:
        self.calls = 0

    async def ocr(self, image_bytes: bytes) -> OcrResult:
        assert image_bytes.startswith(b"\x89PNG")
        self.calls += 1
        return OcrResult(text="embedded text", engine=self.name)


@pytest.mark.asyncio
async def test_embedded_media_is_source_linked_enqueued_once_and_ocr_is_retry_safe(
    session_factory, tmp_path: Path
) -> None:
    image = _png()
    archive = _docx_archive(image)
    normalized = _normalized(image)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'embedded-media.sqlite'}",
        storage_dir=tmp_path / "store",
        ocr_engine="paddleocr",
    )
    engine = _Engine()

    async def read_bytes(path: str) -> bytes:
        return Path(path).read_bytes()

    dependencies = EmbeddedMediaDependencies(
        settings=settings,
        client=cast(Any, object()),
        models=cast(Any, object()),
        engine=engine,
        read_bytes=read_bytes,
    )
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="report.docx",
            content_path="/tmp/report.docx",
            sha256="b" * 64,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        assert (
            await persist_embedded_media(
                session,
                document_id,
                raw=archive,
                normalized=normalized,
                storage_dir=settings.storage_dir,
                limits=IngestLimits.from_settings(settings),
                source_version=1,
            )
            == 1
        )
        assert (
            await persist_embedded_media(
                session,
                document_id,
                raw=archive,
                normalized=normalized,
                storage_dir=settings.storage_dir,
                limits=IngestLimits.from_settings(settings),
                source_version=1,
            )
            == 1
        )
        media = await session.scalar(select(EmbeddedMedia))
        jobs = (await session.scalars(select(Job).order_by(Job.created_at))).all()
        assert media is not None
        assert media.source_location == "word/media/image1.png"
        assert [job.job_type for job in jobs] == ["process_embedded_media"]

        payload = {
            "document_id": str(document_id),
            "source_version": 1,
            "media_sha256": media.sha256,
        }
        worker_payload = {**payload, "_job_id": str(jobs[0].id)}
        await process_embedded_media(session, worker_payload, dependencies=dependencies)
        await process_embedded_media(session, worker_payload, dependencies=dependencies)
        completed_job = await session.get(Job, jobs[0].id)
        assert completed_job is not None
        assert completed_job.payload["_pipeline"] == {
            "completed": True,
            "embedded_media_sha256": media.sha256,
            "source_version": 1,
        }

    assert media.ocr_text == "embedded text"
    assert media.ocr_engine == "test-ocr"
    assert engine.calls == 1


@pytest.mark.asyncio
async def test_embedded_media_rejects_corrupt_content_addressed_file_before_reuse(
    session_factory, tmp_path: Path
) -> None:
    image = _png()
    archive = _docx_archive(image)
    normalized = _normalized(image)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'embedded-media.sqlite'}",
        storage_dir=tmp_path / "store",
        ocr_engine="paddleocr",
    )
    corrupted_path = settings.storage_dir / normalized.images[0].content_path
    corrupted_path.parent.mkdir(parents=True)
    corrupted_path.write_bytes(b"corrupt embedded artifact")

    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="report.docx",
            content_path="/tmp/report.docx",
            sha256="b" * 64,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        with pytest.raises(ArtifactIntegrityError, match="artifact checksum mismatch"):
            await persist_embedded_media(
                session,
                document_id,
                raw=archive,
                normalized=normalized,
                storage_dir=settings.storage_dir,
                limits=IngestLimits.from_settings(settings),
                source_version=1,
            )

        assert (await session.scalars(select(EmbeddedMedia))).all() == []
        assert (await session.scalars(select(Job))).all() == []


@pytest.mark.asyncio
async def test_tampered_embedded_media_is_not_sent_to_ocr(session_factory, tmp_path: Path) -> None:
    image = _png()
    archive = _docx_archive(image)
    normalized = _normalized(image)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'embedded-media.sqlite'}",
        storage_dir=tmp_path / "store",
        ocr_engine="paddleocr",
    )
    engine = _Engine()

    async def read_bytes(path: str) -> bytes:
        return Path(path).read_bytes()

    dependencies = EmbeddedMediaDependencies(
        settings=settings,
        client=cast(Any, object()),
        models=cast(Any, object()),
        engine=engine,
        read_bytes=read_bytes,
    )
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="report.docx",
            content_path="/tmp/report.docx",
            sha256="b" * 64,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        await persist_embedded_media(
            session,
            document_id,
            raw=archive,
            normalized=normalized,
            storage_dir=settings.storage_dir,
            limits=IngestLimits.from_settings(settings),
            source_version=1,
        )
        media = await session.scalar(select(EmbeddedMedia))
        assert media is not None
        (settings.storage_dir / media.content_path).write_bytes(b"tampered image bytes")

        with pytest.raises(ArtifactIntegrityError, match="artifact checksum mismatch"):
            await process_embedded_media(
                session,
                {
                    "document_id": str(document_id),
                    "source_version": 1,
                    "media_sha256": media.sha256,
                },
                dependencies=dependencies,
            )

    assert engine.calls == 0
    assert media.ocr_text is None


@pytest.mark.asyncio
async def test_source_replacement_clears_embedded_media_and_ignores_stale_jobs(
    session_factory, tmp_path: Path
) -> None:
    image = _png()
    archive = _docx_archive(image)
    normalized = _normalized(image)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'embedded-media.sqlite'}",
        storage_dir=tmp_path / "store",
        ocr_engine="paddleocr",
    )
    engine = _Engine()

    async def read_bytes(path: str) -> bytes:
        return Path(path).read_bytes()

    dependencies = EmbeddedMediaDependencies(
        settings=settings,
        client=cast(Any, object()),
        models=cast(Any, object()),
        engine=engine,
        read_bytes=read_bytes,
    )
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="report.docx",
            content_path="/tmp/report-v1.docx",
            sha256="c" * 64,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        await persist_embedded_media(
            session,
            document_id,
            raw=archive,
            normalized=normalized,
            storage_dir=settings.storage_dir,
            limits=IngestLimits.from_settings(settings),
            source_version=1,
        )
        media = await session.scalar(select(EmbeddedMedia))
        assert media is not None
        stale_payload = {
            "document_id": str(document_id),
            "source_version": 1,
            "media_sha256": media.sha256,
        }
        replacement = await documents.append_raw_source(
            document_id,
            filename="report-v2.docx",
            content_path="/tmp/report-v2.docx",
            sha256="d" * 64,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            actor="reviewer",
        )

        assert replacement.version == 2
        assert (await session.scalars(select(EmbeddedMedia))).all() == []
        await process_embedded_media(session, stale_payload, dependencies=dependencies)
        with pytest.raises(
            EmbeddedMediaSourceVersionSupersededError, match="source version was replaced"
        ):
            await persist_embedded_media(
                session,
                document_id,
                raw=archive,
                normalized=normalized,
                storage_dir=settings.storage_dir,
                limits=IngestLimits.from_settings(settings),
                source_version=1,
            )

    assert engine.calls == 0
