from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db import repositories
from clerksan.db.audit import audit_actor, read_history
from clerksan.db.models import (
    AuditEntry,
    Base,
    BatchLifecycle,
    CandidateReviewDecision,
    Document,
    DocumentFile,
    DocumentStatus,
    ExecutionProfile,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    FileKind,
    FinancialSubtype,
    IntakeIntent,
    Job,
    JobStatus,
    RecordKind,
    SourceIntake,
    SourceIntakeState,
    UploadIdempotencyReservation,
    VerifiedRecord,
    WorkerCapabilityLease,
)
from clerksan.db.repositories import (
    DocumentRepo,
    ExtractionRepo,
    RawSourceVersionError,
    ReviewBatchRepo,
    SourceIntakeRepo,
    SourceIntakeValidationError,
    SourceVersionSupersededError,
    StaleExtractionError,
    StaleSourceIntakeError,
    UploadIdempotencyConflictError,
    UploadIdempotencyOutcome,
    VerifiedRepo,
    WorkerCapabilityLeaseRepo,
)
from clerksan.db.sqlite_schema import upgrade_sqlite_demo_schema
from clerksan.ingest.jobs import enqueue


def _payload(*, amount: float = 1200.0, counterparty: str = "サンプル商店") -> dict:
    return {
        "transaction_date": {
            "value": "2026-07-13",
            "confidence": 0.98,
            "source_span": "2026年7月13日",
        },
        "total_amount": {"value": amount, "confidence": 0.98, "source_span": "合計"},
        "counterparty": {"value": counterparty, "confidence": 0.99, "source_span": counterparty},
        "currency": {"value": "JPY", "confidence": 0.95},
        "expense_category": {"value": "会議費", "confidence": 0.70},
        "registration_number": {"value": "T8700110005901", "confidence": 0.99},
        "tax_8_amount": {"value": None, "confidence": 0.9},
        "tax_10_amount": {"value": 109.0, "confidence": 0.92},
    }


async def _current_original(session, document_id: UUID) -> DocumentFile:
    original = await session.scalar(
        select(DocumentFile)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
        )
        .order_by(DocumentFile.version.desc())
        .limit(1)
    )
    assert original is not None
    return original


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repository.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_promote_creates_verified_record_and_changes_lifecycle(session_factory) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="receipt.png",
            content_path="/tmp/receipt.png",
            sha256="a" * 64,
            mime="image/png",
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload=_payload(),
            field_confidences={"transaction_date": 0.98, "total_amount": 0.98},
            model_name="rule-based-demo",
            prompt_version="test",
            actor="worker",
        )
        verified_id = await VerifiedRepo(session).promote(
            extraction_id,
            1,
            corrections={"total_amount": 1250, "expense_category": "接待交際費"},
            reviewer="reviewer",
        )
        await session.commit()

        document = await documents.get(document_id)
        assert document["status"] == DocumentStatus.VERIFIED.value
        assert document["verified"]["id"] == verified_id
        assert str(document["verified"]["total_amount"]) == "1250.00"
        assert document["verified"]["category"] == "接待交際費"

        extraction = await session.get(ExtractedRecord, extraction_id)
        assert extraction is not None
        assert extraction.status is ExtractionStatus.APPROVED
        assert extraction.version == 2


@pytest.mark.asyncio
async def test_reprocessing_supersedes_pending_extraction_and_stale_approval_fails(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="receipt.png",
            content_path="/tmp/receipt.png",
            sha256="b" * 64,
            mime="image/png",
        )
        extractions = ExtractionRepo(session)
        first_id = await extractions.add(
            document_id,
            payload=_payload(),
            field_confidences={},
            model_name="model-a",
            prompt_version="one",
            actor="worker",
        )
        second_id = await extractions.add(
            document_id,
            payload=_payload(amount=1300),
            field_confidences={},
            model_name="model-b",
            prompt_version="two",
            actor="worker",
        )

        first = await session.get(ExtractedRecord, first_id)
        assert first is not None
        assert first.status is ExtractionStatus.SUPERSEDED
        assert first.version == 2
        with pytest.raises(StaleExtractionError):
            await VerifiedRepo(session).promote(first_id, 1, corrections={}, reviewer="reviewer")

        verified_id = await VerifiedRepo(session).promote(
            second_id, 1, corrections={}, reviewer="reviewer"
        )
        assert verified_id
        await session.commit()


@pytest.mark.asyncio
async def test_reprocessing_replaces_an_approved_extraction_only_after_new_approval(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="receipt.png",
            content_path="/tmp/reprocessed.png",
            sha256="d" * 64,
            mime="image/png",
        )
        extractions = ExtractionRepo(session)
        first_id = await extractions.add(
            document_id,
            payload=_payload(amount=1200),
            field_confidences={},
            model_name="model-a",
            prompt_version="one",
            actor="worker",
        )
        old_verified_id = await VerifiedRepo(session).promote(
            first_id, 1, corrections={}, reviewer="reviewer"
        )
        replacement_id = await extractions.add(
            document_id,
            payload=_payload(amount=1300),
            field_confidences={},
            model_name="model-b",
            prompt_version="two",
            actor="worker",
        )
        pending_document = await documents.get(document_id)
        pending_rows = await VerifiedRepo(session).range_query(counterparty="サンプル商店")
        replacement_verified_id = await VerifiedRepo(session).promote(
            replacement_id, 1, corrections={}, reviewer="replacement-reviewer"
        )
        completed_document = await documents.get(document_id)
        active_rows = await VerifiedRepo(session).range_query(counterparty="サンプル商店")
        previous = await session.get(ExtractedRecord, first_id)

    assert previous is not None
    assert previous.status is ExtractionStatus.SUPERSEDED
    assert previous.version == 3
    assert pending_document["status"] == DocumentStatus.IN_REVIEW.value
    assert pending_document["verified"]["id"] == old_verified_id
    assert pending_document["extracted"]["id"] == replacement_id
    assert pending_document["extracted"]["status"] == ExtractionStatus.PENDING_REVIEW.value
    assert old_verified_id in {row["id"] for row in pending_rows}
    assert completed_document["status"] == DocumentStatus.VERIFIED.value
    assert completed_document["verified"]["id"] == replacement_verified_id
    assert old_verified_id not in {row["id"] for row in active_rows}


@pytest.mark.asyncio
async def test_replacement_source_keeps_extraction_provenance_and_prefers_pending_review(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="first.png",
            content_path="originals/first.png",
            sha256="7" * 64,
            mime="image/png",
        )
        extractions = ExtractionRepo(session)
        first_id = await extractions.add(
            document_id,
            payload=_payload(amount=1200),
            field_confidences={},
            model_name="model-a",
            prompt_version="one",
            actor="worker",
        )
        old_verified_id = await VerifiedRepo(session).promote(
            first_id, 1, corrections={}, reviewer="reviewer"
        )
        replacement_source = await documents.append_raw_source(
            document_id,
            filename="second.png",
            content_path="originals/second.png",
            sha256="8" * 64,
            mime="image/png",
            actor="reviewer",
        )
        second_id = await extractions.add(
            document_id,
            payload=_payload(amount=1300),
            field_confidences={},
            model_name="model-b",
            prompt_version="two",
            actor="worker",
            source_version=replacement_source.version,
        )
        same_timestamp = dt.datetime(2026, 7, 14, 12, tzinfo=dt.UTC)
        first = await session.get(ExtractedRecord, first_id)
        second = await session.get(ExtractedRecord, second_id)
        assert first is not None and second is not None
        first.created_at = same_timestamp
        second.created_at = same_timestamp
        await session.flush()
        detail = await documents.get(document_id)
        latest = await extractions.latest_for(document_id)
        source_file = await session.scalar(
            select(DocumentFile).where(
                DocumentFile.document_id == document_id,
                DocumentFile.kind == FileKind.ORIGINAL,
                DocumentFile.version == replacement_source.version,
            )
        )

    assert source_file is not None
    assert detail["extracted"]["id"] == second_id
    assert latest is not None
    assert latest["id"] == second_id
    assert detail["extracted"]["status"] == ExtractionStatus.PENDING_REVIEW.value
    assert detail["extracted"]["source_file_id"] == source_file.id
    assert detail["extracted"]["source_version"] == replacement_source.version
    assert detail["verified"]["id"] == old_verified_id
    originals = [file for file in detail["files"] if file["kind"] == FileKind.ORIGINAL.value]
    assert [(file["version"], file["source_filename"]) for file in originals] == [
        (1, "first.png"),
        (2, "second.png"),
    ]


@pytest.mark.asyncio
async def test_combined_verified_range_query(session_factory) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        verified = VerifiedRepo(session)
        for index, amount in enumerate((900.0, 1200.0, 2100.0)):
            document_id = await documents.create_with_raw(
                filename=f"receipt-{index}.png",
                content_path=f"/tmp/receipt-{index}.png",
                sha256=f"{index + 1:064x}",
                mime="image/png",
            )
            extraction_id = await ExtractionRepo(session).add(
                document_id,
                payload=_payload(amount=amount),
                field_confidences={},
                model_name="demo",
                prompt_version="one",
                actor="worker",
            )
            await verified.promote(extraction_id, 1, corrections={}, reviewer="reviewer")

        rows = await verified.range_query(
            amount_min=1000, amount_max=2000, counterparty="サンプル商店"
        )
        assert [str(row["total_amount"]) for row in rows] == ["1200.00"]


@pytest.mark.asyncio
async def test_verified_range_query_hides_a_non_active_batch(session_factory) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="inactive.csv",
            content_path="/tmp/inactive.csv",
            sha256="6" * 64,
            mime="text/csv",
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload=_payload(counterparty="Inactive Batch"),
            field_confidences={},
            model_name="demo",
            prompt_version="one",
            actor="worker",
        )
        await VerifiedRepo(session).promote(extraction_id, 1, corrections={}, reviewer="reviewer")
        source = await _current_original(session, document_id)
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.source_file_id == source.id)
        )
        assert intake is not None
        batch = ExtractionBatch(
            source_intake_id=intake.id,
            document_id=document_id,
            source_file_id=source.id,
            source_version=source.version,
            source_sha256=source.sha256,
            normalized_sha256="7" * 64,
            structure_fingerprint="8" * 64,
            producer="test",
            producer_version="1",
            origin="test",
            intake_intent=intake.intake_intent,
            lifecycle=BatchLifecycle.OPEN,
            idempotency_key="inactive-range-query",
            candidate_count=1,
            reconciliation_counts={
                "mapped_candidate": 1,
                "residual_generic_candidate": 0,
                "explicit_ignore": 0,
                "blank": 0,
                "parse_error": 0,
            },
            reconciliation_digest="9" * 64,
        )
        session.add(batch)
        await session.flush()
        extraction = await session.get(ExtractedRecord, extraction_id)
        assert extraction is not None
        extraction.batch_id = batch.id
        extraction.candidate_ordinal = 1
        extraction.candidate_key = "a" * 64
        extraction.record_kind = RecordKind.FINANCIAL
        extraction.financial_subtype = FinancialSubtype.TRANSACTION
        extraction.source_file_id = source.id
        extraction.source_version = source.version
        extraction.source_locator = "table:1/row:1"
        extraction.row_fingerprint = "b" * 64
        extraction.validation_issues = []
        extraction.evidence_group_keys = []
        await session.flush()

        rows = await VerifiedRepo(session).range_query(counterparty="Inactive Batch")

    assert rows == []


@pytest.mark.asyncio
async def test_sqlite_demo_actor_context_restores_and_history_is_paginated(session_factory) -> None:
    async with session_factory() as session:
        async with audit_actor(session, "local-demo"):
            assert session.info["clerksan.actor"] == "local-demo"
        assert "clerksan.actor" not in session.info

        session.add_all(
            (
                AuditEntry(
                    actor="local-demo",
                    table_name="verified_records",
                    row_pk="record-1",
                    action="UPDATE",
                    field="total_amount",
                    old_value="100",
                    new_value="120",
                ),
                AuditEntry(
                    actor="local-demo",
                    table_name="verified_records",
                    row_pk="record-1",
                    action="UPDATE",
                    field="category",
                    old_value='"A"',
                    new_value='"B"',
                ),
            )
        )
        await session.flush()
        history = await read_history(
            session, table="verified_records", row_pk="record-1", limit=1, offset=0
        )
        assert len(history) == 1
        assert history[0]["table_name"] == "verified_records"


@pytest.mark.asyncio
async def test_repository_does_not_write_audit_log_directly(session_factory) -> None:
    async with session_factory() as session:
        assert (await session.scalars(select(AuditEntry))).all() == []


@pytest.mark.asyncio
async def test_concurrent_approvals_promote_one_verified_record(session_factory) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="concurrent.png",
            content_path="/tmp/concurrent.png",
            sha256="9" * 64,
            mime="image/png",
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload=_payload(),
            field_confidences={},
            model_name="model",
            prompt_version="one",
            actor="worker",
        )
        await session.commit()

    ready = asyncio.Barrier(2)

    async def approve_once(reviewer: str) -> tuple[str, object | None]:
        async with session_factory() as session:
            await ready.wait()
            try:
                verified_id = await VerifiedRepo(session).promote(
                    extraction_id, 1, corrections={}, reviewer=reviewer
                )
                await session.commit()
                return "approved", verified_id
            except StaleExtractionError:
                await session.rollback()
                return "stale", None

    outcomes = await asyncio.gather(approve_once("reviewer-a"), approve_once("reviewer-b"))

    assert [outcome[0] for outcome in outcomes].count("approved") == 1
    assert [outcome[0] for outcome in outcomes].count("stale") == 1
    async with session_factory() as session:
        verified = (await session.scalars(select(VerifiedRecord))).all()
        extraction = await session.get(ExtractedRecord, extraction_id)
    assert len(verified) == 1
    assert extraction is not None
    assert extraction.status is ExtractionStatus.APPROVED
    assert extraction.version == 2


@pytest.mark.asyncio
async def test_source_replacement_wins_over_a_stale_approval(session_factory, monkeypatch) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="before.png",
            content_path="/tmp/before.png",
            sha256="a" * 64,
            mime="image/png",
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload=_payload(),
            field_confidences={},
            model_name="model",
            prompt_version="one",
            actor="worker",
        )
        await session.commit()

    source_claimed = asyncio.Event()
    allow_source_commit = asyncio.Event()
    approval_ready = asyncio.Event()
    allow_approval_transition = asyncio.Event()
    original_supersede = repositories._supersede_pending_extractions
    original_approve = repositories._approve_pending_extraction

    async def hold_after_supersede(*args, **kwargs):
        result = await original_supersede(*args, **kwargs)
        source_claimed.set()
        await allow_source_commit.wait()
        return result

    async def hold_before_approval(*args, **kwargs):
        approval_ready.set()
        await allow_approval_transition.wait()
        return await original_approve(*args, **kwargs)

    monkeypatch.setattr(repositories, "_supersede_pending_extractions", hold_after_supersede)
    monkeypatch.setattr(repositories, "_approve_pending_extraction", hold_before_approval)

    async def replace_source() -> None:
        async with session_factory() as session:
            await DocumentRepo(session).append_raw_source(
                document_id,
                filename="after.png",
                content_path="/tmp/after.png",
                sha256="b" * 64,
                mime="image/png",
                actor="reviewer",
            )
            await session.commit()

    async def approve_stale() -> None:
        async with session_factory() as session:
            with pytest.raises(StaleExtractionError):
                await VerifiedRepo(session).promote(
                    extraction_id, 1, corrections={}, reviewer="reviewer"
                )
            await session.rollback()

    replacement = asyncio.create_task(replace_source())
    await source_claimed.wait()
    approval = asyncio.create_task(approve_stale())
    await approval_ready.wait()
    allow_source_commit.set()
    await replacement
    allow_approval_transition.set()
    await approval

    async with session_factory() as session:
        detail = await DocumentRepo(session).get(document_id)
        extraction = await session.get(ExtractedRecord, extraction_id)
        verified = (await session.scalars(select(VerifiedRecord))).all()
    assert extraction is not None
    assert extraction.status is ExtractionStatus.SUPERSEDED
    assert detail["status"] == DocumentStatus.NEEDS_REPROCESS.value
    assert [file["version"] for file in detail["files"] if file["kind"] == "original"] == [1, 2]
    assert verified == []


@pytest.mark.asyncio
async def test_concurrent_source_appends_return_a_typed_conflict(session_factory) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="first.png",
            content_path="/tmp/first.png",
            sha256="c" * 64,
            mime="image/png",
        )
        await session.commit()

    ready = asyncio.Barrier(2)

    async def append_once(filename: str, checksum: str) -> str:
        async with session_factory() as session:
            await ready.wait()
            try:
                await DocumentRepo(session).append_raw_source(
                    document_id,
                    filename=filename,
                    content_path=f"/tmp/{filename}",
                    sha256=checksum,
                    mime="image/png",
                    actor="reviewer",
                )
                await session.commit()
                return "appended"
            except RawSourceVersionError:
                await session.rollback()
                return "conflict"

    outcomes = await asyncio.gather(
        append_once("second.png", "d" * 64),
        append_once("third.png", "e" * 64),
    )

    assert sorted(outcomes) == ["appended", "conflict"]
    async with session_factory() as session:
        detail = await DocumentRepo(session).get(document_id)
    assert [file["version"] for file in detail["files"] if file["kind"] == "original"] == [1, 2]


@pytest.mark.asyncio
async def test_reprocess_targets_current_source_after_approved_history_and_rejection(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="reprocess.png",
            content_path="/tmp/reprocess.png",
            sha256="f" * 64,
            mime="image/png",
        )
        first_id = await ExtractionRepo(session).add(
            document_id,
            payload=_payload(amount=1000),
            field_confidences={},
            model_name="model-a",
            prompt_version="one",
            actor="worker",
        )
        await VerifiedRepo(session).promote(first_id, 1, corrections={}, reviewer="reviewer")

        original = await _current_original(session, document_id)
        intake = await SourceIntakeRepo(session).get_for_source(
            document_id, original.id, original.version
        )
        assert intake is not None
        intake_id = intake.id
        await SourceIntakeRepo(session).transition(
            intake_id,
            expected_version=1,
            state=SourceIntakeState.PROCESSED,
            actor="worker",
        )

        first_reprocess = await documents.prepare_reprocess(document_id, actor="reviewer")
        repeated_reprocess = await documents.prepare_reprocess(document_id, actor="reviewer")
        replacement_id = await ExtractionRepo(session).add(
            document_id,
            payload=_payload(amount=1200),
            field_confidences={},
            model_name="model-b",
            prompt_version="two",
            actor="worker",
            source_version=1,
        )
        await ExtractionRepo(session).reject(
            replacement_id, reason="needs another pass", reviewer="reviewer"
        )
        rejected_reprocess = await documents.prepare_reprocess(document_id, actor="reviewer")
        requeued_intake = await session.get(SourceIntake, intake_id)
        intake_count = await session.scalar(
            select(func.count())
            .select_from(SourceIntake)
            .where(SourceIntake.document_id == document_id)
        )

    assert first_reprocess.lifecycle_id == first_id
    assert repeated_reprocess.lifecycle_id == first_id
    assert rejected_reprocess.lifecycle_id == replacement_id
    assert intake_count == 1
    assert requeued_intake is not None
    assert requeued_intake.id == intake_id
    assert requeued_intake.state is SourceIntakeState.QUEUED
    assert requeued_intake.version == 3


@pytest.mark.asyncio
async def test_raw_source_writes_create_one_exact_legacy_companion_intake(
    session_factory,
) -> None:
    async with session_factory() as session:
        duplicate_of = await DocumentRepo(session).create_with_raw(
            filename="known.png",
            content_path="originals/known.png",
            sha256="1" * 64,
            mime="image/png",
        )
        key = uuid4()
        document_id = await DocumentRepo(session).create_with_raw(
            filename="first.png",
            content_path="originals/first.png",
            sha256="2" * 64,
            mime="image/png",
            duplicate_of_document_id=duplicate_of,
            upload_idempotency_key=key,
            intent_digest="3" * 64,
            registry_digest="4" * 64,
            capabilities_digest="5" * 64,
            required_components=["ocr", "parser"],
        )
        replacement = await DocumentRepo(session).append_raw_source(
            document_id,
            filename="second.png",
            content_path="originals/second.png",
            sha256="6" * 64,
            mime="image/png",
            actor="reviewer",
        )
        await session.commit()

    async with session_factory() as session:
        originals = (
            await session.scalars(
                select(DocumentFile)
                .where(
                    DocumentFile.document_id == document_id,
                    DocumentFile.kind == FileKind.ORIGINAL,
                )
                .order_by(DocumentFile.version)
            )
        ).all()
        intakes = (
            await session.scalars(
                select(SourceIntake)
                .where(SourceIntake.document_id == document_id)
                .order_by(SourceIntake.source_version)
            )
        ).all()

    assert replacement.version == 2
    assert len(originals) == len(intakes) == 2
    assert [
        (intake.source_file_id, intake.source_version, intake.source_sha256) for intake in intakes
    ] == [(source.id, source.version, source.sha256) for source in originals]
    assert all(
        intake.execution_profile is ExecutionProfile.LEGACY_COMPAT
        and intake.sandbox_verified is False
        for intake in intakes
    )
    assert intakes[0].duplicate_of_document_id == duplicate_of
    assert intakes[0].upload_idempotency_key == key
    assert intakes[0].required_components == ["ocr", "parser"]
    assert intakes[1].duplicate_of_document_id is None


@pytest.mark.asyncio
async def test_raw_source_intent_defaults_and_replacements_retain_or_resolve_it(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="paypay.csv",
            content_path="originals/paypay.csv",
            sha256="a" * 64,
            mime="text/csv",
            intake_intent=IntakeIntent.GENERIC_FILE,
        )
        assert (
            await documents.resolve_append_intake_intent(document_id, None)
        ) is IntakeIntent.GENERIC_FILE
        assert (
            await documents.resolve_append_intake_intent(document_id, "bill_scan")
        ) is IntakeIntent.BILL_SCAN
        inherited = await documents.append_raw_source(
            document_id,
            filename="paypay-corrected.csv",
            content_path="originals/paypay-corrected.csv",
            sha256="b" * 64,
            mime="text/csv",
            actor="reviewer",
        )
        explicit = await documents.append_raw_source(
            document_id,
            filename="receipt.png",
            content_path="originals/receipt.png",
            sha256="c" * 64,
            mime="image/png",
            actor="reviewer",
            intake_intent=IntakeIntent.BILL_SCAN,
        )
        assert (
            await documents.resolve_append_intake_intent(document_id, None)
        ) is IntakeIntent.BILL_SCAN
        await session.commit()

    async with session_factory() as session:
        intents = (
            await session.scalars(
                select(SourceIntake.intake_intent)
                .where(SourceIntake.document_id == document_id)
                .order_by(SourceIntake.source_version)
            )
        ).all()

    assert inherited.intake_intent is IntakeIntent.GENERIC_FILE
    assert explicit.intake_intent is IntakeIntent.BILL_SCAN
    assert intents == [
        IntakeIntent.GENERIC_FILE,
        IntakeIntent.GENERIC_FILE,
        IntakeIntent.BILL_SCAN,
    ]


@pytest.mark.asyncio
async def test_keyed_direct_legacy_companion_reserves_before_intake_flush(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'keyed-companion.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await upgrade_sqlite_demo_schema(connection)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        pre_reserved_key = uuid4()
        direct_key = uuid4()

        async with session_factory() as session:
            pre_reserved_document_id = await DocumentRepo(session).create_with_raw(
                filename="pre-reserved.png",
                content_path="originals/pre-reserved.png",
                sha256="e" * 64,
                mime="image/png",
                upload_idempotency_key=pre_reserved_key,
                intent_digest="f" * 64,
            )
            direct_document = Document(source_filename="direct-keyed.png")
            direct_source = DocumentFile(
                document=direct_document,
                version=1,
                kind=FileKind.ORIGINAL,
                content_path="originals/direct-keyed.png",
                sha256="a" * 64,
                mime="image/png",
                source_filename="direct-keyed.png",
            )
            session.add_all((direct_document, direct_source))
            await session.flush()
            with pytest.raises(SourceIntakeValidationError, match="registry_digest"):
                await SourceIntakeRepo(session).create_legacy_companion(
                    document_id=direct_document.id,
                    source_file=direct_source,
                    upload_idempotency_key=direct_key,
                    intent_digest="b" * 64,
                    registry_digest="not-a-digest",
                )
            assert await session.get(UploadIdempotencyReservation, direct_key) is None
            direct_intake_id = await SourceIntakeRepo(session).create_legacy_companion(
                document_id=direct_document.id,
                source_file=direct_source,
                upload_idempotency_key=direct_key,
                intent_digest="b" * 64,
            )
            await session.commit()

        async with session_factory() as session:
            pre_reserved_intake = await session.scalar(
                select(SourceIntake).where(SourceIntake.document_id == pre_reserved_document_id)
            )
            direct_intake = await session.get(SourceIntake, direct_intake_id)
            pre_reserved = await session.get(UploadIdempotencyReservation, pre_reserved_key)
            direct_reservation = await session.get(UploadIdempotencyReservation, direct_key)

        assert pre_reserved_intake is not None
        assert pre_reserved is not None
        assert pre_reserved.source_intake_id == pre_reserved_intake.id
        assert direct_intake is not None
        assert direct_intake.upload_idempotency_key == direct_key
        assert direct_reservation is not None
        assert direct_reservation.source_intake_id == direct_intake_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_compatibility_guards_keep_source_and_job_intent_immutable(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'intent-guards.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await upgrade_sqlite_demo_schema(connection)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            document_id = await DocumentRepo(session).create_with_raw(
                filename="transactions.csv",
                content_path="originals/transactions.csv",
                sha256="d" * 64,
                mime="text/csv",
                intake_intent=IntakeIntent.GENERIC_FILE,
            )
            intake = await session.scalar(
                select(SourceIntake).where(SourceIntake.document_id == document_id)
            )
            assert intake is not None
            job_id = await enqueue(
                session,
                job_type="process_document",
                payload={
                    "document_id": str(document_id),
                    "source_file_id": str(intake.source_file_id),
                    "source_intake_id": str(intake.id),
                    "source_version": intake.source_version,
                },
                idempotency_key="immutable-intent",
            )
            assert job_id is not None
            await session.commit()

        async with session_factory() as session:
            with pytest.raises(IntegrityError, match="source intake identity is immutable"):
                await session.execute(
                    update(SourceIntake)
                    .where(SourceIntake.id == intake.id)
                    .values(intake_intent=IntakeIntent.BILL_SCAN)
                )
                await session.commit()
            await session.rollback()
            with pytest.raises(IntegrityError, match="job execution evidence is immutable"):
                await session.execute(
                    update(Job).where(Job.id == job_id).values(intake_intent=IntakeIntent.BILL_SCAN)
                )
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_raw_source_and_companion_intake_share_the_outer_sqlite_transaction(
    session_factory,
) -> None:
    async with session_factory() as session:
        await DocumentRepo(session).create_with_raw(
            filename="rolled-back.png",
            content_path="originals/rolled-back.png",
            sha256="a" * 64,
            mime="image/png",
        )
        await session.rollback()
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 0
        assert await session.scalar(select(func.count()).select_from(DocumentFile)) == 0
        assert await session.scalar(select(func.count()).select_from(SourceIntake)) == 0

        document_id = await DocumentRepo(session).create_with_raw(
            filename="retained.png",
            content_path="originals/retained.png",
            sha256="b" * 64,
            mime="image/png",
        )
        await session.commit()
        await DocumentRepo(session).append_raw_source(
            document_id,
            filename="rolled-back-replacement.png",
            content_path="originals/rolled-back-replacement.png",
            sha256="c" * 64,
            mime="image/png",
            actor="reviewer",
        )
        await session.rollback()

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DocumentFile)
                .where(
                    DocumentFile.document_id == document_id,
                    DocumentFile.kind == FileKind.ORIGINAL,
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SourceIntake)
                .where(SourceIntake.document_id == document_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_upload_idempotency_replays_exact_outcome_and_conflict_writes_nothing(
    session_factory,
) -> None:
    key = uuid4()
    intent_digest = "7" * 64
    async with session_factory() as session:
        duplicate_of = await DocumentRepo(session).create_with_raw(
            filename="known.png",
            content_path="originals/known.png",
            sha256="8" * 64,
            mime="image/png",
        )
        document_id = await DocumentRepo(session).create_with_raw(
            filename="accepted.png",
            content_path="originals/accepted.png",
            sha256="9" * 64,
            mime="image/png",
            duplicate_of_document_id=duplicate_of,
            upload_idempotency_key=key,
            intent_digest=intent_digest,
        )
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={"document_id": str(document_id), "source_version": 1},
            idempotency_key="process:1",
        )
        assert job_id is not None
        await session.commit()

    async with session_factory() as session:
        resolution = await SourceIntakeRepo(session).reserve_upload_idempotency(
            key, "9" * 64, intent_digest
        )
        assert resolution.outcome is UploadIdempotencyOutcome.REPLAY
        assert resolution.replay is not None
        assert resolution.replay.document_id == document_id
        assert resolution.replay.duplicate_of_document_id == duplicate_of
        assert resolution.replay.job_id == job_id
        assert resolution.replay.job_type == "process_document"
        assert resolution.replay.job_status is JobStatus.QUEUED
        assert resolution.replay.job_idempotency_key == "process:1"

        replay_document_id = await DocumentRepo(session).create_with_raw(
            filename="lost-response-retry.png",
            content_path="quarantine/retry.png",
            sha256="9" * 64,
            mime="image/png",
            upload_idempotency_key=key,
            intent_digest=intent_digest,
        )
        assert replay_document_id == document_id

        before = {
            "documents": await session.scalar(select(func.count()).select_from(Document)),
            "files": await session.scalar(select(func.count()).select_from(DocumentFile)),
            "intakes": await session.scalar(select(func.count()).select_from(SourceIntake)),
            "jobs": await session.scalar(select(func.count()).select_from(Job)),
            "reservations": await session.scalar(
                select(func.count()).select_from(UploadIdempotencyReservation)
            ),
        }
        with pytest.raises(UploadIdempotencyConflictError):
            await DocumentRepo(session).create_with_raw(
                filename="conflict.png",
                content_path="quarantine/conflict.png",
                sha256="a" * 64,
                mime="image/png",
                upload_idempotency_key=key,
                intent_digest=intent_digest,
            )
        after = {
            "documents": await session.scalar(select(func.count()).select_from(Document)),
            "files": await session.scalar(select(func.count()).select_from(DocumentFile)),
            "intakes": await session.scalar(select(func.count()).select_from(SourceIntake)),
            "jobs": await session.scalar(select(func.count()).select_from(Job)),
            "reservations": await session.scalar(
                select(func.count()).select_from(UploadIdempotencyReservation)
            ),
        }

    assert before == after


@pytest.mark.asyncio
async def test_concurrent_different_body_upload_key_never_returns_new_to_loser(
    session_factory,
) -> None:
    key = uuid4()
    intent_digest = "b" * 64
    first_ready = asyncio.Event()
    allow_first_commit = asyncio.Event()
    second_entered = asyncio.Event()

    async def accept_first() -> UUID:
        async with session_factory() as session:
            document_id = await DocumentRepo(session).create_with_raw(
                filename="winner.png",
                content_path="published/winner.png",
                sha256="c" * 64,
                mime="image/png",
                upload_idempotency_key=key,
                intent_digest=intent_digest,
            )
            first_ready.set()
            await allow_first_commit.wait()
            await session.commit()
            return document_id

    async def resolve_loser():
        async with session_factory() as session:
            second_entered.set()
            result = await SourceIntakeRepo(session).reserve_upload_idempotency(
                key, "d" * 64, intent_digest
            )
            await session.rollback()
            return result

    first = asyncio.create_task(accept_first())
    await first_ready.wait()
    second = asyncio.create_task(resolve_loser())
    await second_entered.wait()
    await asyncio.sleep(0.05)
    assert not second.done()
    allow_first_commit.set()
    document_id, loser = await asyncio.gather(first, second)

    assert loser.outcome is UploadIdempotencyOutcome.CONFLICT
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 1
        assert await session.scalar(select(func.count()).select_from(DocumentFile)) == 1
        assert await session.scalar(select(func.count()).select_from(SourceIntake)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(UploadIdempotencyReservation))
            == 1
        )
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == document_id)
        )
        assert intake is not None
        assert intake.upload_idempotency_key == key


@pytest.mark.asyncio
async def test_source_intake_transitions_are_stable_and_optimistic(session_factory) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="transition.png",
            content_path="originals/transition.png",
            sha256="e" * 64,
            mime="image/png",
        )
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == document_id)
        )
        assert intake is not None
        repo = SourceIntakeRepo(session)
        next_version = await repo.transition(
            intake.id,
            expected_version=1,
            state=SourceIntakeState.PROCESSING,
            actor="worker",
        )
        assert next_version == 2
        with pytest.raises(StaleSourceIntakeError):
            await repo.transition(
                intake.id,
                expected_version=1,
                state=SourceIntakeState.FAILED,
                actor="worker",
                reason_code="processing_failed",
            )
        next_version = await repo.transition(
            intake.id,
            expected_version=2,
            state=SourceIntakeState.PROCESSED,
            actor="worker",
        )
        assert next_version == 3
        with pytest.raises(SourceIntakeValidationError):
            await repo.transition(
                intake.id,
                expected_version=3,
                state=SourceIntakeState.FAILED,
                actor="worker",
                reason_code="processing_failed",
            )
        await repo.transition(
            intake.id,
            expected_version=3,
            state=SourceIntakeState.QUEUED,
            actor="reviewer",
            reason_code="processing_queued",
        )
        await session.commit()

    async with session_factory() as session:
        stored = await session.get(SourceIntake, intake.id)
        assert stored is not None
        assert stored.state is SourceIntakeState.QUEUED
        assert stored.version == 4


@pytest.mark.asyncio
async def test_derivatives_require_exact_source_and_use_source_page_slots(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="source.pdf",
            content_path="originals/source.pdf",
            sha256="f" * 64,
            mime="application/pdf",
        )
        original = await _current_original(session, document_id)
        for page in (1, 2):
            await documents.add_artifact(
                document_id,
                kind=FileKind.PAGE_RENDER,
                content_path=f"renders/page-{page}.png",
                sha256="0" * 64,
                mime="image/png",
                source_file_id=original.id,
                source_version=original.version,
                page_number=page,
            )
        await documents.add_artifact(
            document_id,
            kind=FileKind.NORMALIZED,
            content_path="renders/manifest.json",
            sha256="1" * 64,
            mime="application/vnd.clerksan.preview-manifest+json",
            source_file_id=original.id,
            source_version=original.version,
        )

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await documents.add_artifact(
                    document_id,
                    kind=FileKind.PAGE_RENDER,
                    content_path="renders/duplicate-page.png",
                    sha256="2" * 64,
                    mime="image/png",
                    source_file_id=original.id,
                    source_version=original.version,
                    page_number=1,
                )
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await documents.add_artifact(
                    document_id,
                    kind=FileKind.NORMALIZED,
                    content_path="renders/duplicate-manifest.json",
                    sha256="3" * 64,
                    mime="application/vnd.clerksan.preview-manifest+json",
                    source_file_id=original.id,
                    source_version=original.version,
                )

        replacement = await documents.append_raw_source(
            document_id,
            filename="replacement.pdf",
            content_path="originals/replacement.pdf",
            sha256="0" * 64,
            mime="application/pdf",
            actor="reviewer",
        )
        assert replacement.version == 5
        with pytest.raises(SourceVersionSupersededError):
            await documents.add_artifact(
                document_id,
                kind=FileKind.NORMALIZED,
                content_path="renders/stale.txt",
                sha256="4" * 64,
                mime="text/plain",
                source_file_id=original.id,
                source_version=original.version,
            )
        await session.commit()

    async with session_factory() as session:
        pages = (
            await session.scalars(
                select(DocumentFile)
                .where(
                    DocumentFile.document_id == document_id,
                    DocumentFile.kind == FileKind.PAGE_RENDER,
                )
                .order_by(DocumentFile.page_number)
            )
        ).all()
        originals = (
            await session.scalars(
                select(DocumentFile).where(
                    DocumentFile.document_id == document_id,
                    DocumentFile.kind == FileKind.ORIGINAL,
                )
            )
        ).all()
    assert [page.page_number for page in pages] == [1, 2]
    assert pages[0].sha256 == pages[1].sha256 == "0" * 64
    assert {source.sha256 for source in originals} == {"f" * 64, "0" * 64}


@pytest.mark.asyncio
async def test_worker_capability_lease_refreshes_exact_digest_evidence(
    session_factory,
) -> None:
    heartbeat = dt.datetime(2026, 8, 22, 1, tzinfo=dt.UTC)
    async with session_factory() as session:
        repo = WorkerCapabilityLeaseRepo(session)
        await repo.refresh(
            worker_id="worker-a",
            registry_digest="5" * 64,
            capabilities_digest="6" * 64,
            sandbox_verified=False,
            heartbeat_at=heartbeat,
            expires_at=heartbeat + dt.timedelta(seconds=30),
        )
        await repo.refresh(
            worker_id="worker-a",
            registry_digest="7" * 64,
            capabilities_digest="8" * 64,
            sandbox_verified=False,
            heartbeat_at=heartbeat + dt.timedelta(seconds=10),
            expires_at=heartbeat + dt.timedelta(seconds=40),
        )
        with pytest.raises(SourceIntakeValidationError):
            await repo.refresh(
                worker_id="worker-b",
                registry_digest="9" * 64,
                capabilities_digest="a" * 64,
                sandbox_verified=False,
                heartbeat_at=heartbeat,
                expires_at=heartbeat,
            )
        await session.commit()

    async with session_factory() as session:
        leases = (await session.scalars(select(WorkerCapabilityLease))).all()
    assert len(leases) == 1
    assert leases[0].registry_digest == "7" * 64
    assert leases[0].capabilities_digest == "8" * 64


@pytest.mark.asyncio
async def test_batch_decision_revisions_do_not_publish_candidate_state(session_factory) -> None:
    async with session_factory() as session:
        document = Document(
            source_filename="review.csv",
            status=DocumentStatus.IN_REVIEW,
        )
        source = DocumentFile(
            document=document,
            version=1,
            kind=FileKind.ORIGINAL,
            content_path="originals/review.csv",
            sha256="a" * 64,
            mime="text/csv",
            source_filename="review.csv",
        )
        session.add_all((document, source))
        await session.flush()
        intake = SourceIntake(
            document_id=document.id,
            source_file_id=source.id,
            source_version=1,
            source_sha256=source.sha256,
            intake_intent=IntakeIntent.GENERIC_FILE,
            state=SourceIntakeState.PROCESSED,
        )
        session.add(intake)
        await session.flush()
        batch = ExtractionBatch(
            source_intake_id=intake.id,
            document_id=document.id,
            source_file_id=source.id,
            source_version=1,
            source_sha256=source.sha256,
            normalized_sha256="b" * 64,
            structure_fingerprint="c" * 64,
            producer="test",
            producer_version="1",
            origin="test_fixture",
            intake_intent=IntakeIntent.GENERIC_FILE,
            lifecycle=BatchLifecycle.OPEN,
            idempotency_key="repository-review-batch",
            candidate_count=2,
            reconciliation_counts={
                "mapped_candidate": 1,
                "residual_generic_candidate": 1,
                "explicit_ignore": 0,
                "blank": 0,
                "parse_error": 0,
            },
            reconciliation_digest="d" * 64,
        )
        session.add(batch)
        await session.flush()
        financial = ExtractedRecord(
            document_id=document.id,
            source_file_id=source.id,
            source_version=1,
            batch_id=batch.id,
            candidate_ordinal=1,
            candidate_key="1" * 64,
            record_kind=RecordKind.FINANCIAL,
            financial_subtype=FinancialSubtype.TRANSACTION,
            source_locator="row/1",
            row_fingerprint="2" * 64,
            validation_issues=[],
            evidence_group_keys=[],
            payload=_payload(),
            field_confidences={},
            source_spans={},
            model_name="test",
            prompt_version="1",
            status=ExtractionStatus.PENDING_REVIEW,
        )
        generic = ExtractedRecord(
            document_id=document.id,
            source_file_id=source.id,
            source_version=1,
            batch_id=batch.id,
            candidate_ordinal=2,
            candidate_key="3" * 64,
            record_kind=RecordKind.GENERIC_DOCUMENT,
            source_locator="row/2",
            row_fingerprint="4" * 64,
            validation_issues=[],
            evidence_group_keys=[],
            payload={"content_markdown": "notes"},
            field_confidences={},
            source_spans={},
            model_name="test",
            prompt_version="1",
            status=ExtractionStatus.PENDING_REVIEW,
        )
        session.add_all((financial, generic))
        await session.flush()

        result = await ReviewBatchRepo(session).apply_decisions(
            batch.id,
            1,
            (
                {
                    "extraction_id": financial.id,
                    "expected_extraction_version": 1,
                    "expected_decision_revision": 0,
                    "action": "include",
                    "corrections": {"total_amount": 1_250},
                },
                {
                    "extraction_id": generic.id,
                    "expected_extraction_version": 1,
                    "expected_decision_revision": 0,
                    "action": "exclude",
                    "exclusion_reason": "not accounting data",
                },
            ),
            "reviewer",
        )

        assert result.batch_version == 2
        assert result.lifecycle is BatchLifecycle.READY_TO_ACTIVATE
        assert financial.status is ExtractionStatus.PENDING_REVIEW
        assert generic.status is ExtractionStatus.PENDING_REVIEW
        assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
        revisions = list(
            await session.scalars(
                select(CandidateReviewDecision).order_by(CandidateReviewDecision.extraction_id)
            )
        )
        assert len(revisions) == 2
        assert revisions[0].decision_revision == revisions[1].decision_revision == 1
