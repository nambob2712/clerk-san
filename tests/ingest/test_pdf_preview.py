from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from clerksan.api.main import create_app
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.models import DocumentFile, FileKind
from clerksan.db.repositories import DocumentRepo
from clerksan.ingest.adapters.base import AdapterRegistry
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import (
    PDF_PREVIEW_MANIFEST_MIME,
    DocMetadata,
    ExtractedImage,
    NormalizedDocument,
    PdfPreviewManifest,
)
from clerksan.ingest.parser_artifacts import (
    PDF_PREVIEW_MANIFEST_MIME as PARSER_PREVIEW_MANIFEST_MIME,
)
from clerksan.ingest.parser_artifacts import (
    ArtifactRole,
    GeneratedArtifact,
    ParserArtifact,
    ParserRunResult,
    build_pdf_preview_manifest,
    descriptor_for_generated,
)
from clerksan.ingest.pipeline import (
    PipelineDependencies,
    _materialize_parser_artifacts,
    take_committed_artifact_reservations,
)
from clerksan.ingest.storage_reconcile import finalize_reservation
from clerksan.llm.ocr import OcrBlock, OcrResult


class _RecordingOcr:
    name = "recording-ocr"

    def __init__(self) -> None:
        self.inputs: list[bytes] = []

    async def ocr(self, image_bytes: bytes) -> OcrResult:
        self.inputs.append(image_bytes)
        return OcrResult(
            text="scanned page total 1200 yen",
            blocks=[OcrBlock(text="scanned page total 1200 yen", confidence=0.99)],
            engine=self.name,
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'preview.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )


def _png(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(output, format="PNG")
    return output.getvalue()


def _parser_artifact(
    generated: GeneratedArtifact, *, ordinal: int, source_sha256: str
) -> ParserArtifact:
    return ParserArtifact(
        descriptor=descriptor_for_generated(
            generated,
            ordinal=ordinal,
            source_sha256=source_sha256,
            seal_supported=False,
            sealed=False,
        ),
        data=generated.data,
    )


def _dependencies(settings: Settings, ocr: _RecordingOcr) -> PipelineDependencies:
    async def read_bytes(content_path: str) -> bytes:
        return (settings.storage_dir / content_path).read_bytes()

    async def unused_writer(_document_id, _encoded: bytes) -> str:
        raise AssertionError("normalized writer is outside this focused test")

    return PipelineDependencies(
        settings=settings,
        adapters=AdapterRegistry(),
        client=cast(Any, object()),
        models=cast(Any, object()),
        read_bytes=read_bytes,
        write_normalized=unused_writer,
        ocr=ocr,
    )


async def _persist_ready_pdf(settings: Settings) -> tuple[str, str, bytes, bytes]:
    raw_pdf = b"%PDF-1.7 raw source bytes never sent to OCR"
    source_sha256 = hashlib.sha256(raw_pdf).hexdigest()
    source_path = settings.storage_dir / "originals" / source_sha256
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(raw_pdf)
    native_page = _png("white")
    scanned_page = _png("gray")
    generated_pages = (
        GeneratedArtifact(
            role=ArtifactRole.PDF_PAGE,
            media_type="image/png",
            data=native_page,
            page_number=1,
            width=24,
            height=16,
            source_location="page:1",
            ocr_required=False,
        ),
        GeneratedArtifact(
            role=ArtifactRole.PDF_PAGE,
            media_type="image/png",
            data=scanned_page,
            page_number=2,
            width=24,
            height=16,
            source_location="page:2",
            ocr_required=True,
        ),
    )
    parser_manifest = build_pdf_preview_manifest(source_sha256, generated_pages)
    generated_manifest = GeneratedArtifact(
        role=ArtifactRole.PDF_PREVIEW_MANIFEST,
        media_type=PARSER_PREVIEW_MANIFEST_MIME,
        data=parser_manifest,
    )
    artifacts = tuple(
        _parser_artifact(page, ordinal=ordinal, source_sha256=source_sha256)
        for ordinal, page in enumerate((*generated_pages, generated_manifest), start=1)
    )
    normalized = NormalizedDocument(
        markdown_body="native text layer\n\n---\n\n",
        metadata=DocMetadata(
            filename="mixed.pdf",
            detected_type=FileType.PDF,
            sha256=source_sha256,
            family="document",
            canonical_mime="application/pdf",
            page_provenance=["text_layer", "ocr_required"],
            extra={
                "page_count": 2,
                "ocr_required": True,
                "ocr_required_pages": [2],
                "preview_status": "ready",
                "preview_page_count": 2,
                "preview_manifest_sha256": hashlib.sha256(parser_manifest).hexdigest(),
            },
        ),
        images=[
            ExtractedImage(
                sha256=hashlib.sha256(scanned_page).hexdigest(),
                content_path="artifact:pdf-page:2",
                width=24,
                height=16,
                source_location="page:2",
            )
        ],
        embeddable=True,
    )
    ocr = _RecordingOcr()
    async with get_session(settings) as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="mixed.pdf",
            content_path=source_path.relative_to(settings.storage_dir).as_posix(),
            sha256=source_sha256,
            mime="application/pdf",
        )
        source = await session.scalar(
            select(DocumentFile).where(
                DocumentFile.document_id == document_id,
                DocumentFile.kind == FileKind.ORIGINAL,
            )
        )
        assert source is not None
        materialized = await _materialize_parser_artifacts(
            session,
            document_id=document_id,
            source=source,
            adapter_key="pdf",
            result=ParserRunResult(normalized, artifacts),
            deps=_dependencies(settings, ocr),
        )
        assert materialized.markdown_body == (
            "native text layer\n\n---\n\nscanned page total 1200 yen"
        )
        assert materialized.metadata.page_provenance == ["text_layer", "ocr"]
        assert materialized.metadata.extra["ocr_required_pages"] == []
        assert ocr.inputs == [scanned_page]
        assert raw_pdf not in ocr.inputs

        page_rows = list(
            (
                await session.scalars(
                    select(DocumentFile)
                    .where(DocumentFile.kind == FileKind.PAGE_RENDER)
                    .order_by(DocumentFile.page_number)
                )
            ).all()
        )
        manifest_row = await session.scalar(
            select(DocumentFile).where(DocumentFile.mime == PDF_PREVIEW_MANIFEST_MIME)
        )
        assert [page.page_number for page in page_rows] == [1, 2]
        assert manifest_row is not None
        manifest = PdfPreviewManifest.model_validate_json(
            (settings.storage_dir / manifest_row.content_path).read_bytes()
        )
        assert [page.artifact_id for page in manifest.pages] == [page.id for page in page_rows]
        await session.commit()
        reservations = take_committed_artifact_reservations(session)
        for reservation in reservations:
            finalize_reservation(reservation)
        return str(document_id), str(source.id), native_page, scanned_page


def test_pdf_preview_uses_only_sanitized_ocr_and_serves_complete_exact_pages(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        document_id, source_file_id, native_page, scanned_page = asyncio.run(
            _persist_ready_pdf(settings)
        )
        source_sha256 = hashlib.sha256(b"%PDF-1.7 raw source bytes never sent to OCR").hexdigest()
        query = f"version=1&sha256={source_sha256}"
        manifest_response = client.get(
            f"/documents/{document_id}/sources/{source_file_id}/pdf-preview?{query}"
        )
        assert manifest_response.status_code == 200, manifest_response.text
        assert manifest_response.json()["page_count"] == 2
        assert manifest_response.headers["x-content-type-options"] == "nosniff"

        first = client.get(
            f"/documents/{document_id}/sources/{source_file_id}/pdf-preview/pages/1?{query}"
        )
        second = client.get(
            f"/documents/{document_id}/sources/{source_file_id}/pdf-preview/pages/2?{query}"
        )
        assert first.status_code == second.status_code == 200
        assert first.content == native_page
        assert second.content == scanned_page
        assert second.headers["content-type"].startswith("image/png")
        assert second.headers["x-content-type-options"] == "nosniff"

        raw = client.get(
            f"/documents/{document_id}/original?source_file_id={source_file_id}&{query}"
        )
        assert raw.status_code == 200
        assert raw.headers["content-disposition"].startswith("attachment;")

        mismatch = client.get(
            f"/documents/{document_id}/sources/{source_file_id}/pdf-preview?"
            f"version=1&sha256={'f' * 64}"
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "source_identity_mismatch"


def test_missing_pdf_page_never_exposes_a_partial_preview(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        document_id, source_file_id, _native_page, _scanned_page = asyncio.run(
            _persist_ready_pdf(settings)
        )
        source_sha256 = hashlib.sha256(b"%PDF-1.7 raw source bytes never sent to OCR").hexdigest()

        async def remove_second_page() -> None:
            async with get_session(settings) as session:
                page = await session.scalar(
                    select(DocumentFile).where(
                        DocumentFile.kind == FileKind.PAGE_RENDER,
                        DocumentFile.page_number == 2,
                    )
                )
                assert page is not None
                (settings.storage_dir / page.content_path).unlink()

        asyncio.run(remove_second_page())
        response = client.get(
            f"/documents/{document_id}/sources/{source_file_id}/pdf-preview?"
            f"version=1&sha256={source_sha256}"
        )
        assert response.status_code == 409
        assert response.json()["code"] == "pdf_preview_inconsistent"


def test_sanitized_image_is_the_only_image_ocr_input(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    raw_image = b"untrusted encoded source"
    source_sha256 = hashlib.sha256(raw_image).hexdigest()
    sanitized = _png("blue")
    generated = GeneratedArtifact(
        role=ArtifactRole.SANITIZED_IMAGE,
        media_type="image/png",
        data=sanitized,
        page_number=1,
        width=24,
        height=16,
        source_location="image:1",
        ocr_required=True,
    )
    normalized = NormalizedDocument(
        markdown_body="",
        metadata=DocMetadata(
            filename="scan.bmp",
            detected_type=FileType.BMP,
            sha256=source_sha256,
            family="image",
            canonical_mime="image/bmp",
            page_provenance=["ocr_required"],
            extra={"ocr_required": True, "ocr_required_pages": [1]},
        ),
        images=[
            ExtractedImage(
                sha256=hashlib.sha256(sanitized).hexdigest(),
                content_path="artifact:sanitized-image:1",
                width=24,
                height=16,
                source_location="image:1",
            )
        ],
        embeddable=False,
    )
    ocr = _RecordingOcr()

    async def run() -> None:
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        source_path = settings.storage_dir / "originals" / source_sha256
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(raw_image)
        async with get_session(settings) as session:
            document_id = await DocumentRepo(session).create_with_raw(
                filename="scan.bmp",
                content_path=source_path.relative_to(settings.storage_dir).as_posix(),
                sha256=source_sha256,
                mime="image/bmp",
            )
            source = await session.scalar(
                select(DocumentFile).where(
                    DocumentFile.document_id == document_id,
                    DocumentFile.kind == FileKind.ORIGINAL,
                )
            )
            assert source is not None
            materialized = await _materialize_parser_artifacts(
                session,
                document_id=document_id,
                source=source,
                adapter_key="bmp",
                result=ParserRunResult(
                    normalized,
                    (_parser_artifact(generated, ordinal=1, source_sha256=source_sha256),),
                ),
                deps=_dependencies(settings, ocr),
            )
            assert materialized.markdown_body == "scanned page total 1200 yen"
            assert materialized.metadata.page_provenance == ["ocr"]
            assert materialized.images[0].content_path.startswith("derivatives/")
            await session.commit()
            for reservation in take_committed_artifact_reservations(session):
                finalize_reservation(reservation)

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000"):
        asyncio.run(run())
    assert ocr.inputs == [sanitized]
    assert raw_image not in ocr.inputs
