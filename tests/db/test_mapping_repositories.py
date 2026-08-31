from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from clerksan.db.models import (
    Base,
    Document,
    DocumentFile,
    DocumentStatus,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    FileKind,
    FinancialSubtype,
    IntakeIntent,
    RecordKind,
    SchemaMapping,
    SourceIntake,
    SourceIntakeState,
    VerifiedRecord,
)
from clerksan.db.repositories import (
    ExtractionBatchRepo,
    MappingConflictError,
    MappingSetRepo,
    SchemaMappingRepo,
)
from clerksan.ingest.filetype import FileType
from clerksan.ingest.mapping import (
    FieldRule,
    MappingContract,
    MappingSetContract,
    MappingSetEntryContract,
    apply_mapping_set,
)
from clerksan.ingest.normalized import (
    DocMetadata,
    ExtractedTable,
    NormalizedDocument,
    canonical_digest,
)
from clerksan.ingest.records import (
    CandidateDraft,
    CompositionLedger,
    StructuralDisposition,
    StructuralUnitDecision,
    build_candidate_key,
    value_fingerprint,
)
from clerksan.ingest.staging import build_staged_structure


async def _session(tmp_path) -> tuple[AsyncSession, object]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mapping.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    return session, engine


async def _source(session: AsyncSession):
    document = Document(
        source_filename="records.csv",
        status=DocumentStatus.UPLOADED,
    )
    original = DocumentFile(
        document=document,
        version=1,
        kind=FileKind.ORIGINAL,
        content_path="originals/a.csv",
        sha256="a" * 64,
        mime="text/csv",
        source_filename="records.csv",
    )
    session.add_all((document, original))
    await session.flush()
    intake = SourceIntake(
        document_id=document.id,
        source_file_id=original.id,
        source_version=1,
        source_sha256=original.sha256,
        intake_intent=IntakeIntent.GENERIC_FILE,
        state=SourceIntakeState.NEEDS_MAPPING,
    )
    session.add(intake)
    await session.flush()
    return document, original, intake


def _staged(source_file_id, *, rows=None):
    normalized = NormalizedDocument(
        markdown_body="",
        metadata=DocMetadata(
            filename="records.csv",
            detected_type=FileType.CSV,
            sha256="a" * 64,
        ),
        tables=[
            ExtractedTable(
                header=["date", "amount"],
                rows=rows or [["2026-08-01", "100"], ["2026-08-02", "200"]],
                source_location="transactions",
            )
        ],
        embeddable=False,
    )
    return build_staged_structure(
        normalized,
        source_file_id=source_file_id,
        source_version=1,
    )


def _mapping(staged, *, mapping_id=None):
    descriptor = staged.tables[0].descriptor
    return MappingContract(
        mapping_id=mapping_id or uuid4(),
        table_locator=descriptor.table_locator,
        schema_fingerprint=descriptor.schema_fingerprint,
        record_kind=RecordKind.FINANCIAL,
        financial_subtype=FinancialSubtype.TRANSACTION,
        field_rules=(
            FieldRule(target_field="date", source_columns=("date",)),
            FieldRule(target_field="amount", source_columns=("amount",)),
        ),
        required_fields=("date", "amount"),
    )


async def _persist_set(session, document, original, intake, staged, mapping):
    persisted = await SchemaMappingRepo(session).create(
        document.id,
        mapping,
        created_by="reviewer",
        source_intake_id=intake.id,
        source_file_id=original.id,
        source_version=1,
        source_sha256=original.sha256,
    )
    assert persisted.mapping_digest == mapping.contract_digest
    contract = MappingSetContract(
        source_file_id=original.id,
        source_version=1,
        structure_fingerprint=staged.structure_fingerprint,
        entries=(
            MappingSetEntryContract(
                table_locator=mapping.table_locator,
                schema_fingerprint=mapping.schema_fingerprint,
                mapping=mapping,
            ),
        ),
        created_by="reviewer",
    )
    return await MappingSetRepo(session).create(
        document.id,
        contract,
        source_intake_id=intake.id,
        source_sha256=original.sha256,
    )


@pytest.mark.asyncio
async def test_mapping_set_and_batch_are_immutable_source_bound_and_replay_safe(tmp_path) -> None:
    session, engine = await _session(tmp_path)
    try:
        document, original, intake = await _source(session)
        staged = _staged(original.id)
        mapping = _mapping(staged)
        mapping_set = await _persist_set(session, document, original, intake, staged, mapping)
        application = apply_mapping_set(
            mapping_set.contract,
            staged,
            source_sha256=original.sha256,
        )

        first = await ExtractionBatchRepo(session).add_mapping_batch(
            document.id,
            source_intake_id=intake.id,
            source_file_id=original.id,
            source_version=1,
            source_sha256=original.sha256,
            normalized_sha256=staged.normalized_sha256,
            structure_fingerprint=staged.structure_fingerprint,
            mapping_set=mapping_set,
            application=application,
            producer="mapping-engine",
            producer_version="1",
            origin="confirmed_mapping",
            idempotency_key="job-1",
        )
        replay = await ExtractionBatchRepo(session).add_mapping_batch(
            document.id,
            source_intake_id=intake.id,
            source_file_id=original.id,
            source_version=1,
            source_sha256=original.sha256,
            normalized_sha256=staged.normalized_sha256,
            structure_fingerprint=staged.structure_fingerprint,
            mapping_set=mapping_set,
            application=application,
            producer="mapping-engine",
            producer_version="1",
            origin="confirmed_mapping",
            idempotency_key="job-1",
        )

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.id == first.id
        assert first.candidate_count == 2
        assert await session.scalar(select(func.count(ExtractionBatch.id))) == 1
        candidates = list(
            (
                await session.scalars(
                    select(ExtractedRecord).order_by(ExtractedRecord.candidate_ordinal)
                )
            ).all()
        )
        assert [candidate.status for candidate in candidates] == [
            ExtractionStatus.PENDING_REVIEW,
            ExtractionStatus.PENDING_REVIEW,
        ]
        assert candidates[0].candidate_key != candidates[1].candidate_key
        assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
        assert document.status is DocumentStatus.UPLOADED
        assert intake.state is SourceIntakeState.PROCESSED
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_batch_idempotency_key_rejects_changed_normalized_digest(tmp_path) -> None:
    session, engine = await _session(tmp_path)
    try:
        document, original, intake = await _source(session)
        staged = _staged(original.id)
        mapping_set = await _persist_set(
            session, document, original, intake, staged, _mapping(staged)
        )
        application = apply_mapping_set(
            mapping_set.contract,
            staged,
            source_sha256=original.sha256,
        )
        repo = ExtractionBatchRepo(session)
        kwargs = dict(
            source_intake_id=intake.id,
            source_file_id=original.id,
            source_version=1,
            source_sha256=original.sha256,
            structure_fingerprint=staged.structure_fingerprint,
            mapping_set=mapping_set,
            application=application,
            producer="mapping-engine",
            producer_version="1",
            origin="confirmed_mapping",
            idempotency_key="same-job",
        )
        await repo.add_mapping_batch(
            document.id,
            normalized_sha256=staged.normalized_sha256,
            **kwargs,
        )
        with pytest.raises(MappingConflictError, match="different input"):
            await repo.add_mapping_batch(
                document.id,
                normalized_sha256="f" * 64,
                **kwargs,
            )
        assert await session.scalar(select(func.count(ExtractionBatch.id))) == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_mapping_mutation_rejects_a_superseded_source_before_write(tmp_path) -> None:
    session, engine = await _session(tmp_path)
    try:
        document, original, intake = await _source(session)
        staged = _staged(original.id)
        replacement = DocumentFile(
            document_id=document.id,
            version=2,
            kind=FileKind.ORIGINAL,
            content_path="originals/b.csv",
            sha256="b" * 64,
            mime="text/csv",
            source_filename="new.csv",
        )
        session.add(replacement)
        await session.flush()

        with pytest.raises(MappingConflictError, match="source changed"):
            await SchemaMappingRepo(session).create(
                document.id,
                _mapping(staged),
                created_by="reviewer",
                source_intake_id=intake.id,
                source_file_id=original.id,
                source_version=1,
                source_sha256=original.sha256,
            )
        assert await session.scalar(select(func.count(SchemaMapping.id))) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.parametrize("candidate_count", [0, 1])
@pytest.mark.asyncio
async def test_batch_persists_zero_or_one_candidate_without_authority(
    tmp_path, candidate_count: int
) -> None:
    session, engine = await _session(tmp_path)
    try:
        document, original, intake = await _source(session)
        staged = _staged(original.id, rows=[["2026-08-01", "100"]])
        descriptor = staged.tables[0].descriptor
        if candidate_count == 0:
            contract = MappingSetContract(
                source_file_id=original.id,
                source_version=1,
                structure_fingerprint=staged.structure_fingerprint,
                entries=(
                    MappingSetEntryContract(
                        table_locator=descriptor.table_locator,
                        schema_fingerprint=descriptor.schema_fingerprint,
                        ignore_reason="explicitly outside this import",
                    ),
                ),
                created_by="reviewer",
            )
            mapping_set = await MappingSetRepo(session).create(
                document.id,
                contract,
                source_intake_id=intake.id,
                source_sha256=original.sha256,
            )
        else:
            mapping_set = await _persist_set(
                session,
                document,
                original,
                intake,
                staged,
                _mapping(staged),
            )
        application = apply_mapping_set(
            mapping_set.contract,
            staged,
            source_sha256=original.sha256,
        )
        summary = await ExtractionBatchRepo(session).add_mapping_batch(
            document.id,
            source_intake_id=intake.id,
            source_file_id=original.id,
            source_version=1,
            source_sha256=original.sha256,
            normalized_sha256=staged.normalized_sha256,
            structure_fingerprint=staged.structure_fingerprint,
            mapping_set=mapping_set,
            application=application,
            producer="mapping-engine",
            producer_version="1",
            origin="confirmed_mapping",
            idempotency_key=f"batch-{candidate_count}",
        )
        assert summary.candidate_count == candidate_count
        assert await session.scalar(select(func.count(ExtractedRecord.id))) == candidate_count
        assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
        assert document.status is DocumentStatus.UPLOADED
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.parametrize("candidate_count", [0, 1, 2])
@pytest.mark.asyncio
async def test_direct_candidate_batch_persists_zero_one_or_many_with_replay(
    tmp_path, candidate_count: int
) -> None:
    session, engine = await _session(tmp_path)
    try:
        document, original, intake = await _source(session)
        intake.state = SourceIntakeState.PROCESSING
        intake.version += 1
        payloads = tuple(
            {"title": f"section {ordinal}", "text": f"content {ordinal}"}
            for ordinal in range(1, candidate_count + 1)
        )
        candidates: list[CandidateDraft] = []
        decisions: list[StructuralUnitDecision] = []
        for ordinal, payload in enumerate(payloads, start=1):
            locator = f"section:{ordinal}"
            fingerprint = value_fingerprint(payload)
            key = build_candidate_key(
                source_sha256=original.sha256,
                source_locator=locator,
                candidate_ordinal=ordinal,
                normalized_item_hash=fingerprint,
                record_kind=RecordKind.GENERIC_DOCUMENT,
                financial_subtype=None,
                mapping_version=1,
            )
            candidates.append(
                CandidateDraft(
                    candidate_ordinal=ordinal,
                    candidate_key=key,
                    record_kind=RecordKind.GENERIC_DOCUMENT,
                    financial_subtype=None,
                    payload=payload,
                    confidences={},
                    source_locator=locator,
                    row_fingerprint=fingerprint,
                )
            )
            decisions.append(
                StructuralUnitDecision(
                    unit_id=locator,
                    locator=locator,
                    content_digest=fingerprint,
                    disposition=StructuralDisposition.RESIDUAL_GENERIC_CANDIDATE,
                    candidate_key=key,
                )
            )
        ledger = CompositionLedger(tuple(decisions))
        normalized_sha256 = canonical_digest({"payloads": payloads})
        structure_fingerprint = canonical_digest(
            {"locators": [decision.locator for decision in decisions]}
        )
        repo = ExtractionBatchRepo(session)
        kwargs = dict(
            source_intake_id=intake.id,
            source_file_id=original.id,
            source_version=1,
            source_sha256=original.sha256,
            normalized_sha256=normalized_sha256,
            structure_fingerprint=structure_fingerprint,
            candidates=tuple(candidates),
            ledger=ledger,
            producer="structural-parser",
            producer_version="1",
            origin="generic_document",
            idempotency_key=f"direct-{candidate_count}",
        )
        first = await repo.add_candidate_batch(document.id, **kwargs)
        replay = await repo.add_candidate_batch(document.id, **kwargs)
        if candidates:
            changed = (replace(candidates[0], payload={"title": "changed"}), *candidates[1:])
            with pytest.raises(MappingConflictError, match="different input"):
                await repo.add_candidate_batch(
                    document.id,
                    **{**kwargs, "candidates": changed},
                )

        assert first.candidate_count == candidate_count
        assert first.replayed is False
        assert replay.id == first.id
        assert replay.replayed is True
        assert first.mapping_set_id is None
        assert await session.scalar(select(func.count(ExtractionBatch.id))) == 1
        assert await session.scalar(select(func.count(ExtractedRecord.id))) == candidate_count
        assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
        assert intake.state is SourceIntakeState.PROCESSED
    finally:
        await session.close()
        await engine.dispose()
