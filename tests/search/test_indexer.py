from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.config import Settings
from clerksan.db.models import (
    Base,
    BatchLifecycle,
    DocumentClass,
    DocumentFile,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    FileKind,
    Job,
    RecordKind,
    SourceIntake,
    SourceIntakeState,
)
from clerksan.db.repositories import (
    DocumentRepo,
    ExtractionBatchRepo,
    ExtractionRepo,
    SourceIntakeRepo,
    VerifiedRepo,
)
from clerksan.ingest.filetype import FileType
from clerksan.ingest.jobs import enqueue
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.ingest.records import (
    CandidateDraft,
    CompositionLedger,
    StructuralDisposition,
    StructuralUnitDecision,
    build_candidate_key,
    value_fingerprint,
)
from clerksan.search.indexer import (
    IndexingError,
    SearchDependencies,
    SearchDomain,
    index_candidate_batch,
    index_document,
    search,
)


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'search.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'search.sqlite'}",
        embed_model="local-test-embed",
        embed_model_digest="sha256:test",
        embed_dim=2,
    )


def _dependencies(tmp_path: Path) -> SearchDependencies:
    async def read_bytes(path: str) -> bytes:
        return Path(path).read_bytes()

    async def embedder(texts, client, settings, *, purpose: str):
        del client, settings
        return [[1.0, 0.0] if "needle" in item.lower() else [0.0, 1.0] for item in texts]

    return SearchDependencies(
        settings=_settings(tmp_path),
        client=cast(Any, object()),
        read_bytes=read_bytes,
        embedder=embedder,
    )


async def _source_file_id(session, document_id, source_version: int):
    value = await session.scalar(
        select(DocumentFile.id).where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
            DocumentFile.version == source_version,
        )
    )
    assert value is not None
    return value


async def _document_with_normalized(
    session,
    tmp_path: Path,
    *,
    body: str,
    digest: str,
    doc_class: DocumentClass,
    normalized: NormalizedDocument | None = None,
):
    raw = tmp_path / f"{digest}.png"
    raw.write_bytes(b"raw")
    document_id = await DocumentRepo(session).create_with_raw(
        filename=f"{digest}.png",
        content_path=str(raw),
        sha256=digest,
        mime="image/png",
        doc_class=doc_class,
    )
    normalized_path = tmp_path / f"{digest}.normalized.json"
    normalized = normalized or NormalizedDocument(
        markdown_body=body,
        metadata=DocMetadata(filename="receipt.md", detected_type=FileType.MD, sha256=digest),
    )
    encoded = normalized.model_dump_json().encode()
    normalized_path.write_bytes(encoded)
    await DocumentRepo(session).add_artifact(
        document_id,
        kind=FileKind.NORMALIZED,
        content_path=str(normalized_path),
        sha256=hashlib.sha256(encoded).hexdigest(),
        mime="application/json",
        text_provenance="normalized_document:source_version:1",
        source_file_id=await _source_file_id(session, document_id, 1),
        source_version=1,
    )
    return document_id, normalized_path


@pytest.mark.asyncio
async def test_index_replaces_old_chunks_and_searches_locally(
    session_factory, tmp_path: Path
) -> None:
    dependencies = _dependencies(tmp_path)
    async with session_factory() as session:
        document_id, normalized_path = await _document_with_normalized(
            session,
            tmp_path,
            body="# Note\n\nneedle client gift receipt",
            digest="a" * 64,
            doc_class=DocumentClass.RECEIPT,
        )
        await index_document(session, {"document_id": str(document_id)}, dependencies=dependencies)
        await session.commit()

        hits = await search(
            session,
            "needle receipt",
            doc_class=DocumentClass.RECEIPT,
            dependencies=dependencies,
        )
        assert hits[0].document_id == document_id
        assert "needle" in hits[0].text

        replacement = NormalizedDocument(
            markdown_body="# Note\n\nreplacement text",
            metadata=DocMetadata(filename="receipt.md", detected_type=FileType.MD, sha256="a" * 64),
        )
        replacement_path = tmp_path / "replacement.normalized.json"
        replacement_encoded = replacement.model_dump_json().encode()
        replacement_path.write_bytes(replacement_encoded)
        await DocumentRepo(session).add_artifact(
            document_id,
            kind=FileKind.NORMALIZED,
            content_path=str(replacement_path),
            sha256=hashlib.sha256(replacement_encoded).hexdigest(),
            mime="application/json",
            source_file_id=await _source_file_id(session, document_id, 1),
            source_version=1,
        )
        await index_document(session, {"document_id": str(document_id)}, dependencies=dependencies)
        count = await session.scalar(
            text("SELECT count(*) FROM chunks WHERE document_id = :id"),
            {"id": document_id.hex},
        )
        await session.commit()

    assert count == 1


@pytest.mark.asyncio
async def test_index_worker_job_writes_a_durable_completion_marker(
    session_factory, tmp_path: Path
) -> None:
    dependencies = _dependencies(tmp_path)
    async with session_factory() as session:
        document_id, normalized_path = await _document_with_normalized(
            session,
            tmp_path,
            body="# Note\n\nneedle receipt",
            digest="a" * 64,
            doc_class=DocumentClass.RECEIPT,
        )
        normalized_sha256 = hashlib.sha256(normalized_path.read_bytes()).hexdigest()
        job_id = await enqueue(
            session,
            job_type="index_document",
            payload={
                "document_id": str(document_id),
                "source_version": 1,
                "normalized_sha256": normalized_sha256,
            },
            idempotency_key=f"index:1:{normalized_sha256}",
        )
        assert job_id is not None
        await index_document(
            session,
            {
                "document_id": str(document_id),
                "source_version": 1,
                "normalized_sha256": normalized_sha256,
                "_job_id": str(job_id),
            },
            dependencies=dependencies,
        )
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.payload["_pipeline"] == {
            "completed": True,
            "indexed_source_version": 1,
            "chunks_replaced": True,
        }


@pytest.mark.asyncio
async def test_tampered_normalized_artifact_is_not_embedded_or_indexed(
    session_factory, tmp_path: Path
) -> None:
    dependencies = _dependencies(tmp_path)
    embedded = False

    async def embedder(texts, client, settings, *, purpose: str):
        nonlocal embedded
        del texts, client, settings, purpose
        embedded = True
        return []

    dependencies.embedder = embedder
    async with session_factory() as session:
        document_id, normalized_path = await _document_with_normalized(
            session,
            tmp_path,
            body="# Note\n\ntrusted source text",
            digest="f" * 64,
            doc_class=DocumentClass.RECEIPT,
        )
        tampered = NormalizedDocument(
            markdown_body="# Note\n\ntampered searchable text",
            metadata=DocMetadata(filename="receipt.md", detected_type=FileType.MD, sha256="f" * 64),
        )
        normalized_path.write_text(tampered.model_dump_json(), encoding="utf-8")

        with pytest.raises(IndexingError, match="normalized artifact .* unreadable"):
            await index_document(
                session, {"document_id": str(document_id)}, dependencies=dependencies
            )

        chunk_count = await session.scalar(
            text("SELECT count(*) FROM chunks WHERE document_id = :id"),
            {"id": document_id.hex},
        )

    assert embedded is False
    assert chunk_count == 0


@pytest.mark.asyncio
async def test_index_uses_sheet_discovery_descriptions_without_embedding_spreadsheet_rows(
    session_factory, tmp_path: Path
) -> None:
    dependencies = _dependencies(tmp_path)
    spreadsheet = NormalizedDocument(
        markdown_body="Workbook summary only",
        metadata=DocMetadata(
            filename="expenses.xlsx",
            detected_type=FileType.XLSX,
            sha256="b" * 64,
            extra={
                "sheet_descriptions": [
                    "Workbook expenses.xlsx; sheet July. Columns: needle vendor, amount."
                ]
            },
        ),
        embeddable=False,
    )
    async with session_factory() as session:
        document_id, _ = await _document_with_normalized(
            session,
            tmp_path,
            body=spreadsheet.markdown_body,
            digest="b" * 64,
            doc_class=DocumentClass.OTHER,
            normalized=spreadsheet,
        )
        await index_document(session, {"document_id": str(document_id)}, dependencies=dependencies)
        await session.commit()

        hits = await search(session, "needle", dependencies=dependencies)

    assert hits[0].document_id == document_id
    assert hits[0].text == spreadsheet.metadata.extra["sheet_descriptions"][0]


@pytest.mark.asyncio
async def test_date_filtered_search_excludes_a_superseded_verified_version(
    session_factory, tmp_path: Path
) -> None:
    dependencies = _dependencies(tmp_path)
    async with session_factory() as session:
        document_id, _ = await _document_with_normalized(
            session,
            tmp_path,
            body="# Note\n\nneedle receipt",
            digest="c" * 64,
            doc_class=DocumentClass.RECEIPT,
        )
        extractions = ExtractionRepo(session)
        first_id = await extractions.add(
            document_id,
            payload={
                "transaction_date": {"value": "2026-01-10"},
                "total_amount": {"value": 1000},
                "counterparty": {"value": "North Cafe"},
            },
            field_confidences={},
            model_name="first",
            prompt_version="first",
            actor="worker",
        )
        await VerifiedRepo(session).promote(first_id, 1, corrections={}, reviewer="reviewer")
        second_id = await extractions.add(
            document_id,
            payload={
                "transaction_date": {"value": "2026-05-10"},
                "total_amount": {"value": 1000},
                "counterparty": {"value": "North Cafe"},
            },
            field_confidences={},
            model_name="second",
            prompt_version="second",
            actor="worker",
        )
        await VerifiedRepo(session).promote(second_id, 1, corrections={}, reviewer="reviewer")
        await index_document(session, {"document_id": str(document_id)}, dependencies=dependencies)
        await session.commit()

        current = await search(
            session,
            "needle",
            date_from=dt.date(2026, 5, 1),
            date_to=dt.date(2026, 5, 31),
            dependencies=dependencies,
        )
        superseded = await search(
            session,
            "needle",
            date_from=dt.date(2026, 1, 1),
            date_to=dt.date(2026, 1, 31),
            dependencies=dependencies,
        )

    assert [hit.document_id for hit in current] == [document_id]
    assert superseded == []


@pytest.mark.asyncio
async def test_replacing_a_source_removes_old_chunks_until_the_new_source_is_indexed(
    session_factory, tmp_path: Path
) -> None:
    dependencies = _dependencies(tmp_path)
    async with session_factory() as session:
        document_id, _ = await _document_with_normalized(
            session,
            tmp_path,
            body="# Original\n\nold source needle",
            digest="d" * 64,
            doc_class=DocumentClass.RECEIPT,
        )
        await index_document(session, {"document_id": str(document_id)}, dependencies=dependencies)
        await session.commit()

        assert await search(session, "old source needle", dependencies=dependencies)

        documents = DocumentRepo(session)
        replacement_source = await documents.append_raw_source(
            document_id,
            filename="replacement.png",
            content_path=str(tmp_path / "replacement.png"),
            sha256="e" * 64,
            mime="image/png",
            actor="reviewer",
        )
        await session.commit()

        assert await search(session, "old source needle", dependencies=dependencies) == []

        replacement = NormalizedDocument(
            markdown_body="# Replacement\n\nnew source needle",
            metadata=DocMetadata(
                filename="replacement.png",
                detected_type=FileType.PNG,
                sha256="e" * 64,
            ),
        )
        encoded = replacement.model_dump_json().encode()
        normalized_path = tmp_path / "replacement.normalized.json"
        normalized_path.write_bytes(encoded)
        normalized_digest = hashlib.sha256(encoded).hexdigest()
        await documents.add_artifact(
            document_id,
            kind=FileKind.NORMALIZED,
            content_path=str(normalized_path),
            sha256=normalized_digest,
            mime="application/json",
            text_provenance=(f"normalized_document:source_version:{replacement_source.version}"),
            source_file_id=await _source_file_id(session, document_id, replacement_source.version),
            source_version=replacement_source.version,
        )
        await index_document(
            session,
            {
                "document_id": str(document_id),
                "source_version": replacement_source.version,
                "normalized_sha256": normalized_digest,
            },
            dependencies=dependencies,
        )
        await session.commit()

        detail = await documents.get(document_id)
        hits = await search(session, "new source needle", dependencies=dependencies)

    originals = [file for file in detail["files"] if file["kind"] == FileKind.ORIGINAL.value]
    assert [file["version"] for file in originals] == [1, replacement_source.version]
    assert [hit.text for hit in hits] == ["new source needle"]


@pytest.mark.asyncio
async def test_delayed_old_index_job_cannot_replace_newer_source_chunks(
    session_factory, tmp_path: Path
) -> None:
    first_raw = b"first raw source"
    replacement_raw = b"replacement raw source"
    first_path = tmp_path / "first.png"
    replacement_path = tmp_path / "replacement.png"
    first_path.write_bytes(first_raw)
    replacement_path.write_bytes(replacement_raw)
    first_digest = hashlib.sha256(first_raw).hexdigest()
    replacement_digest = hashlib.sha256(replacement_raw).hexdigest()
    first_normalized = NormalizedDocument(
        markdown_body="old source needle",
        metadata=DocMetadata(filename="first.png", detected_type=FileType.PNG, sha256=first_digest),
    )
    first_encoded = first_normalized.model_dump_json().encode()
    first_normalized_path = tmp_path / "first.normalized.json"
    first_normalized_path.write_bytes(first_encoded)
    first_normalized_digest = hashlib.sha256(first_encoded).hexdigest()

    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="first.png",
            content_path=str(first_path),
            sha256=first_digest,
            mime="image/png",
        )
        await documents.add_artifact(
            document_id,
            kind=FileKind.NORMALIZED,
            content_path=str(first_normalized_path),
            sha256=first_normalized_digest,
            mime="application/json",
            text_provenance="normalized_document:source_version:1",
            source_file_id=await _source_file_id(session, document_id, 1),
            source_version=1,
        )
        await session.commit()

    base_dependencies = _dependencies(tmp_path)
    old_read_started = asyncio.Event()
    allow_old_read = asyncio.Event()

    async def delayed_old_read(path: str) -> bytes:
        if Path(path) == first_normalized_path:
            old_read_started.set()
            await allow_old_read.wait()
        return await base_dependencies.read_bytes(path)

    old_dependencies = SearchDependencies(
        settings=base_dependencies.settings,
        client=base_dependencies.client,
        read_bytes=delayed_old_read,
        embedder=base_dependencies.embedder,
    )

    async def run_old_job() -> None:
        async with session_factory() as session:
            async with session.begin():
                await index_document(
                    session,
                    {
                        "document_id": str(document_id),
                        "source_version": 1,
                        "normalized_sha256": first_normalized_digest,
                    },
                    dependencies=old_dependencies,
                )

    old_job = asyncio.create_task(run_old_job())
    try:
        await asyncio.wait_for(old_read_started.wait(), timeout=5)

        replacement_normalized = NormalizedDocument(
            markdown_body="new source needle",
            metadata=DocMetadata(
                filename="replacement.png",
                detected_type=FileType.PNG,
                sha256=replacement_digest,
            ),
        )
        replacement_encoded = replacement_normalized.model_dump_json().encode()
        replacement_normalized_path = tmp_path / "replacement.normalized.json"
        replacement_normalized_path.write_bytes(replacement_encoded)
        replacement_normalized_digest = hashlib.sha256(replacement_encoded).hexdigest()
        async with session_factory() as session:
            async with session.begin():
                documents = DocumentRepo(session)
                replacement = await documents.append_raw_source(
                    document_id,
                    filename="replacement.png",
                    content_path=str(replacement_path),
                    sha256=replacement_digest,
                    mime="image/png",
                    actor="reviewer",
                )
                await documents.add_artifact(
                    document_id,
                    kind=FileKind.NORMALIZED,
                    content_path=str(replacement_normalized_path),
                    sha256=replacement_normalized_digest,
                    mime="application/json",
                    text_provenance=(f"normalized_document:source_version:{replacement.version}"),
                    source_file_id=await _source_file_id(session, document_id, replacement.version),
                    source_version=replacement.version,
                )

        async with session_factory() as session:
            async with session.begin():
                await index_document(
                    session,
                    {
                        "document_id": str(document_id),
                        "source_version": replacement.version,
                        "normalized_sha256": replacement_normalized_digest,
                    },
                    dependencies=base_dependencies,
                )

        allow_old_read.set()
        await asyncio.wait_for(old_job, timeout=5)
    finally:
        allow_old_read.set()
        if not old_job.done():
            await old_job

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT text FROM chunks WHERE document_id = :id ORDER BY seq"),
                    {"id": document_id.hex},
                )
            )
            .scalars()
            .all()
        )

    assert rows == ["new source needle"]


@pytest.mark.asyncio
async def test_candidate_chunks_stay_hidden_until_active_and_keep_domains_separate(
    session_factory, tmp_path: Path
) -> None:
    dependencies = _dependencies(tmp_path)
    async with session_factory() as session:
        document_id, normalized_path = await _document_with_normalized(
            session,
            tmp_path,
            body="# Note\n\nneedle generic policy text",
            digest="7" * 64,
            doc_class=DocumentClass.OTHER,
        )
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == document_id)
        )
        assert intake is not None
        await SourceIntakeRepo(session).transition(
            intake.id,
            expected_version=intake.version,
            state=SourceIntakeState.PROCESSING,
            actor="worker",
        )
        payload = {"content_markdown": "needle generic policy text"}
        fingerprint = value_fingerprint(payload)
        locator = "document:body"
        candidate_key = build_candidate_key(
            source_sha256=intake.source_sha256,
            source_locator=locator,
            candidate_ordinal=1,
            normalized_item_hash=fingerprint,
            record_kind=RecordKind.GENERIC_DOCUMENT,
            financial_subtype=None,
            mapping_version=1,
        )
        candidate = CandidateDraft(
            candidate_ordinal=1,
            candidate_key=candidate_key,
            record_kind=RecordKind.GENERIC_DOCUMENT,
            financial_subtype=None,
            payload=payload,
            confidences={},
            source_locator=locator,
            row_fingerprint=fingerprint,
        )
        ledger = CompositionLedger(
            (
                StructuralUnitDecision(
                    unit_id=locator,
                    locator=locator,
                    content_digest=fingerprint,
                    disposition=StructuralDisposition.RESIDUAL_GENERIC_CANDIDATE,
                    candidate_key=candidate_key,
                ),
            )
        )
        normalized_sha256 = hashlib.sha256(normalized_path.read_bytes()).hexdigest()
        summary = await ExtractionBatchRepo(session).add_candidate_batch(
            document_id,
            source_intake_id=intake.id,
            source_file_id=intake.source_file_id,
            source_version=intake.source_version,
            source_sha256=intake.source_sha256,
            normalized_sha256=normalized_sha256,
            structure_fingerprint="8" * 64,
            candidates=(candidate,),
            ledger=ledger,
            producer="test",
            producer_version="1",
            origin="generic_document",
            idempotency_key="candidate-search",
        )
        await index_candidate_batch(
            session,
            {
                "document_id": str(document_id),
                "batch_id": str(summary.id),
                "source_file_id": str(intake.source_file_id),
                "source_version": intake.source_version,
                "normalized_sha256": normalized_sha256,
            },
            dependencies=dependencies,
        )
        await session.commit()

        assert await search(session, "needle", dependencies=dependencies) == []

        batch = await session.get(ExtractionBatch, summary.id)
        extracted = await session.scalar(
            select(ExtractedRecord).where(ExtractedRecord.batch_id == summary.id)
        )
        assert batch is not None and extracted is not None
        batch.lifecycle = BatchLifecycle.ACTIVE
        extracted.status = ExtractionStatus.APPROVED
        await session.flush()
        with pytest.raises(IndexingError, match="no longer indexable"):
            await index_candidate_batch(
                session,
                {
                    "document_id": str(document_id),
                    "batch_id": str(summary.id),
                    "source_file_id": str(intake.source_file_id),
                    "source_version": intake.source_version,
                    "normalized_sha256": normalized_sha256,
                },
                dependencies=dependencies,
            )

        generic_hits = await search(
            session,
            "needle",
            domain=SearchDomain.GENERIC,
            dependencies=dependencies,
        )
        financial_hits = await search(
            session,
            "needle",
            domain=SearchDomain.FINANCIAL,
            dependencies=dependencies,
        )

    assert [hit.document_id for hit in generic_hits] == [document_id]
    assert financial_hits == []
