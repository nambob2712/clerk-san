from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.config import Settings
from clerksan.db.models import (
    Base,
    DocumentClass,
    DocumentFile,
    DocumentStatus,
    ExtractedRecord,
    ExtractionStatus,
    FileKind,
    Job,
    SpreadsheetRow,
)
from clerksan.db.repositories import DocumentRepo
from clerksan.extract.classifier import ClassificationResult
from clerksan.extract.schemas import FieldValue, ReceiptExtraction
from clerksan.ingest.adapters.base import AdapterRegistry
from clerksan.ingest.filetype import FileType
from clerksan.ingest.jobs import enqueue
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.ingest.pipeline import (
    PipelineDependencies,
    _normalized_writer,
    build_default_dependencies,
    process_document,
    rebuild_format_derivatives,
)
from clerksan.storage import ArtifactIntegrityError


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pipeline.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class FakePngAdapter:
    supported_types = (FileType.PNG,)

    def __init__(self) -> None:
        self.calls = 0
        self.raw_inputs: list[bytes] = []

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        self.calls += 1
        self.raw_inputs.append(raw)
        assert raw.startswith(b"\x89PNG")
        return NormalizedDocument(markdown_body="領収書 合計 1200円", metadata=meta)


class FakeXlsxAdapter:
    supported_types = (FileType.XLSX,)

    def __init__(self) -> None:
        self.calls = 0

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        assert raw.startswith(b"PK")
        self.calls += 1
        from clerksan.ingest.normalized import ExtractedTable

        return NormalizedDocument(
            markdown_body="Workbook expenses.xlsx; sheet July.",
            metadata=meta,
            tables=[
                ExtractedTable(
                    header=["date", "amount"],
                    rows=[["2026-07-01", "1200"], ["2026-07-02", "500.50"]],
                    source_location="sheet:July:table:1",
                )
            ],
        )


def _minimal_xlsx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet.main+xml"/>'
                "</Types>"
            ),
        )
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return output.getvalue()


def _dependencies(
    tmp_path: Path, adapter: FakePngAdapter | FakeXlsxAdapter
) -> PipelineDependencies:
    registry = AdapterRegistry()
    registry.register(adapter)

    async def read_bytes(path: str) -> bytes:
        return Path(path).read_bytes()

    async def write_normalized(document_id, encoded: bytes) -> str:
        digest = hashlib.sha256(encoded).hexdigest()
        target = tmp_path / f"{document_id}-{digest}.normalized.json"
        target.write_bytes(encoded)
        return str(target)

    async def classifier(document, client, models) -> ClassificationResult:
        del document, client, models
        from clerksan.db.models import DocumentClass

        return ClassificationResult(label=DocumentClass.RECEIPT, confidence=0.99, method="fake")

    async def extractor(document, doc_class, client, models) -> ReceiptExtraction:
        del document, doc_class, client, models
        return ReceiptExtraction(
            transaction_date=FieldValue(value=date(2026, 7, 13), confidence=0.98),
            total_amount=FieldValue(value=1200.0, confidence=0.98),
            counterparty=FieldValue(value="サンプル商店", confidence=0.99),
            currency=FieldValue(value="JPY", confidence=0.95),
        )

    return PipelineDependencies(
        settings=Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'pipeline.sqlite'}"),
        adapters=registry,
        client=cast(Any, object()),
        models=cast(Any, object()),
        read_bytes=read_bytes,
        write_normalized=write_normalized,
        classifier=classifier,
        extractor=extractor,
        actor="worker-test",
        model_name="fake-local-model",
        prompt_version="test",
    )


@pytest.mark.asyncio
async def test_normalized_content_addressed_writer_rejects_corrupt_existing_file(
    tmp_path: Path,
) -> None:
    document_id = uuid4()
    encoded = b'{"markdown_body":"trusted"}'
    digest = hashlib.sha256(encoded).hexdigest()
    target = tmp_path / "normalized" / str(document_id) / f"{digest}.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt normalized artifact")

    with pytest.raises(ArtifactIntegrityError, match="artifact checksum mismatch"):
        await _normalized_writer(tmp_path)(document_id, encoded)


@pytest.mark.asyncio
async def test_format_derivative_rebuild_restages_without_appending_an_extraction(
    session_factory, tmp_path: Path
) -> None:
    raw = _minimal_xlsx()
    source = tmp_path / "expenses.xlsx"
    source.write_bytes(raw)
    adapter = FakeXlsxAdapter()
    dependencies = _dependencies(tmp_path, adapter)

    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="expenses.xlsx",
            content_path=str(source),
            sha256=hashlib.sha256(raw).hexdigest(),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        job_id = await enqueue(
            session,
            job_type="rebuild_format_derivatives",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="format-derivatives:0013:1",
        )
        assert job_id is not None
        await rebuild_format_derivatives(
            session,
            {
                "document_id": str(document_id),
                "source_version": 1,
                "_job_id": str(job_id),
            },
            dependencies=dependencies,
        )
        rows = (
            await session.scalars(
                select(SpreadsheetRow).where(SpreadsheetRow.document_id == document_id)
            )
        ).all()
        records = (
            await session.scalars(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).all()
        document = await DocumentRepo(session).get(document_id)
        completed_job = await session.get(Job, job_id)
        assert completed_job is not None
        await DocumentRepo(session).append_raw_source(
            document_id,
            filename="expenses-v2.xlsx",
            content_path=str(tmp_path / "expenses-v2.xlsx"),
            sha256="f" * 64,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            actor="migration-test",
        )
        await rebuild_format_derivatives(
            session,
            {"document_id": str(document_id), "source_version": 1},
            dependencies=dependencies,
        )
        stale_rows = (
            await session.scalars(
                select(SpreadsheetRow).where(SpreadsheetRow.document_id == document_id)
            )
        ).all()

    assert adapter.calls == 1
    assert len(rows) == 2
    assert {row.source_version for row in rows} == {1}
    assert records == []
    assert document["status"] == DocumentStatus.UPLOADED.value
    assert stale_rows == []
    assert completed_job.payload["_pipeline"] == {
        "completed": True,
        "format_derivatives_rebuilt_for_source_version": 1,
    }


@pytest.mark.asyncio
async def test_default_pipeline_registers_all_supported_local_upload_adapters(
    tmp_path: Path,
) -> None:
    dependencies = build_default_dependencies(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'pipeline.sqlite'}",
            storage_dir=tmp_path / "store",
        )
    )
    try:
        for file_type in (FileType.DOCX, FileType.XLSX, FileType.MD, FileType.PDF, FileType.PNG):
            assert dependencies.adapters.get(file_type)
    finally:
        await dependencies.client.aclose()


@pytest.mark.asyncio
async def test_tampered_original_stops_before_adapter_or_extraction(
    session_factory, tmp_path: Path
) -> None:
    original = b"\x89PNG\r\n\x1a\noriginal-receipt"
    raw = tmp_path / "receipt.png"
    raw.write_bytes(original)
    adapter = FakePngAdapter()
    dependencies = _dependencies(tmp_path, adapter)

    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path=str(raw),
            sha256=hashlib.sha256(original).hexdigest(),
            mime="image/png",
        )
        raw.write_bytes(b"\x89PNG\r\n\x1a\ntampered-receipt")

        with pytest.raises(ArtifactIntegrityError, match="artifact checksum mismatch"):
            await process_document(
                session,
                {"document_id": str(document_id)},
                dependencies=dependencies,
            )

        records = (
            await session.scalars(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).all()

    assert adapter.calls == 0
    assert records == []


@pytest.mark.asyncio
async def test_tampered_original_stops_reprocess_before_cached_normalized_extraction(
    session_factory, tmp_path: Path
) -> None:
    original = b"\x89PNG\r\n\x1a\noriginal-receipt"
    raw = tmp_path / "receipt.png"
    raw.write_bytes(original)
    adapter = FakePngAdapter()
    dependencies = _dependencies(tmp_path, adapter)

    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path=str(raw),
            sha256=hashlib.sha256(original).hexdigest(),
            mime="image/png",
        )
        await process_document(
            session,
            {"document_id": str(document_id)},
            dependencies=dependencies,
        )
        await session.commit()
        raw.write_bytes(b"\x89PNG\r\n\x1a\ntampered-receipt")

        with pytest.raises(ArtifactIntegrityError, match="artifact checksum mismatch"):
            await process_document(
                session,
                {"document_id": str(document_id)},
                dependencies=dependencies,
            )

        records = (
            await session.scalars(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).all()

    assert adapter.calls == 1
    assert len(records) == 1


@pytest.mark.asyncio
async def test_tampered_cached_normalized_stops_reprocess_before_extraction(
    session_factory, tmp_path: Path
) -> None:
    original = b"\x89PNG\r\n\x1a\noriginal-receipt"
    raw = tmp_path / "receipt.png"
    raw.write_bytes(original)
    adapter = FakePngAdapter()
    dependencies = _dependencies(tmp_path, adapter)

    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path=str(raw),
            sha256=hashlib.sha256(original).hexdigest(),
            mime="image/png",
        )
        await process_document(
            session,
            {"document_id": str(document_id)},
            dependencies=dependencies,
        )
        await session.commit()
        cached = await session.scalar(
            select(DocumentFile).where(
                DocumentFile.document_id == document_id,
                DocumentFile.kind == FileKind.NORMALIZED,
            )
        )
        assert cached is not None
        Path(cached.content_path).write_bytes(b'{"markdown_body":"tampered"}')

        with pytest.raises(ArtifactIntegrityError, match="artifact checksum mismatch"):
            await process_document(
                session,
                {"document_id": str(document_id)},
                dependencies=dependencies,
            )

        records = (
            await session.scalars(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).all()

    assert adapter.calls == 1
    assert len(records) == 1


@pytest.mark.asyncio
async def test_reprocess_reuses_normalized_artifact_and_supersedes_prior_pending(
    session_factory, tmp_path: Path
) -> None:
    raw = tmp_path / "receipt.png"
    raw.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-receipt")
    adapter = FakePngAdapter()
    dependencies = _dependencies(tmp_path, adapter)

    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path=str(raw),
            sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
            mime="image/png",
        )
        await process_document(
            session,
            {"document_id": str(document_id)},
            dependencies=dependencies,
        )
        await session.commit()

        await process_document(
            session,
            {"document_id": str(document_id)},
            dependencies=dependencies,
        )
        await session.commit()
        rows = (
            await session.scalars(
                select(ExtractedRecord)
                .where(ExtractedRecord.document_id == document_id)
                .order_by(ExtractedRecord.created_at.asc())
            )
        ).all()
        artifacts = (
            await session.scalars(
                select(DocumentFile)
                .where(DocumentFile.document_id == document_id)
                .order_by(DocumentFile.version.asc())
            )
        ).all()
        jobs = (
            await session.scalars(
                select(Job).where(Job.document_id == document_id).order_by(Job.created_at.asc())
            )
        ).all()
        document = await DocumentRepo(session).get(document_id)

    assert adapter.calls == 1
    assert len(rows) == 2
    assert rows[0].status is ExtractionStatus.SUPERSEDED
    assert rows[1].status is ExtractionStatus.PENDING_REVIEW
    assert document["status"] == DocumentStatus.IN_REVIEW.value
    assert artifacts[-1].kind.value == "normalized"
    assert [job.job_type for job in jobs] == ["index_document"]
    assert jobs[0].payload["source_version"] == 1
    assert len(jobs[0].payload["normalized_sha256"]) == 64
    assert jobs[0].idempotency_key.startswith("index:1:")


@pytest.mark.asyncio
async def test_same_completed_job_does_not_append_a_duplicate_extraction(
    session_factory, tmp_path: Path
) -> None:
    raw = tmp_path / "receipt.png"
    raw.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-receipt")
    adapter = FakePngAdapter()
    dependencies = _dependencies(tmp_path, adapter)

    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="receipt.png",
            content_path=str(raw),
            sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
            mime="image/png",
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id)},
            idempotency_key="initial",
        )
        assert job_id is not None
        payload = {"document_id": str(document_id), "_job_id": str(job_id)}
        await process_document(session, payload, dependencies=dependencies)
        await session.commit()

        await process_document(session, payload, dependencies=dependencies)
        await session.commit()
        records = (
            await session.scalars(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).all()
        job = await session.get(Job, job_id)

    assert len(records) == 1
    assert adapter.calls == 1
    assert job is not None
    assert job.payload["_pipeline"]["completed"] is True


@pytest.mark.asyncio
async def test_obsolete_source_job_skips_output_and_next_job_uses_replacement_source(
    session_factory, tmp_path: Path
) -> None:
    first_raw = b"\x89PNG\r\n\x1a\nfirst-source"
    replacement_raw = b"\x89PNG\r\n\x1a\nreplacement-source"
    first_path = tmp_path / "first.png"
    replacement_path = tmp_path / "replacement.png"
    first_path.write_bytes(first_raw)
    replacement_path.write_bytes(replacement_raw)
    adapter = FakePngAdapter()
    dependencies = _dependencies(tmp_path, adapter)

    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="first.png",
            content_path=str(first_path),
            sha256=hashlib.sha256(first_raw).hexdigest(),
            mime="image/png",
        )
        first_job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="source-one",
        )
        assert first_job_id is not None
        base_classifier = dependencies.classifier

        async def replace_source_after_normalization(document, client, models):
            del document, client, models
            replacement = await documents.append_raw_source(
                document_id,
                filename="replacement.png",
                content_path=str(replacement_path),
                sha256=hashlib.sha256(replacement_raw).hexdigest(),
                mime="image/png",
                actor="reviewer",
            )
            return ClassificationResult(
                label=DocumentClass.RECEIPT,
                confidence=0.99,
                method=f"replacement-{replacement.version}",
            )

        dependencies.classifier = replace_source_after_normalization
        await process_document(
            session,
            {
                "document_id": str(document_id),
                "source_version": 1,
                "_job_id": str(first_job_id),
            },
            dependencies=dependencies,
        )
        await session.commit()

        first_job = await session.get(Job, first_job_id)
        stale_records = (
            await session.scalars(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).all()
        replacement_source = next(
            file
            for file in (await DocumentRepo(session).get(document_id))["files"]
            if file["kind"] == "original" and file["source_filename"] == "replacement.png"
        )
        assert first_job is not None
        assert first_job.payload["_pipeline"]["skipped"] == "source_version_replaced"
        assert (
            first_job.payload["_pipeline"]["current_source_version"]
            == replacement_source["version"]
        )
        assert stale_records == []

        dependencies.classifier = base_classifier
        replacement_job_id = await enqueue(
            session,
            job_type="process_document",
            payload={
                "document_id": str(document_id),
                "source_version": replacement_source["version"],
            },
            idempotency_key="replacement-source",
        )
        assert replacement_job_id is not None
        await process_document(
            session,
            {
                "document_id": str(document_id),
                "source_version": replacement_source["version"],
                "_job_id": str(replacement_job_id),
            },
            dependencies=dependencies,
        )
        await session.commit()
        records = (
            await session.scalars(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).all()

    assert adapter.raw_inputs == [first_raw, replacement_raw]
    assert len(records) == 1
    assert records[0].source_version == replacement_source["version"]
