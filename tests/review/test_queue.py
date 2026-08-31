from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db.models import (
    Base,
    BatchLifecycle,
    DocumentFile,
    DocumentStatus,
    DuplicateFlag,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    FileKind,
    IntakeIntent,
    RecordKind,
    SourceIntake,
    SourceIntakeState,
)
from clerksan.db.repositories import (
    BatchReviewRequiredError,
    DocumentRepo,
    ExtractionRepo,
    StaleExtractionError,
)
from clerksan.review.queue import approve, pending, reject


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'review.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _payload(*, confidence: float = 0.98, amount: float = 1200.0) -> dict:
    return {
        "transaction_date": {"value": "2026-07-13", "confidence": confidence},
        "total_amount": {"value": amount, "confidence": confidence},
        "counterparty": {"value": "サンプル商店", "confidence": confidence},
        "currency": {"value": "JPY", "confidence": confidence},
    }


async def _add(session, *, digest: str, confidence: float = 0.98, amount: float = 1200.0):
    document_id = await DocumentRepo(session).create_with_raw(
        filename="receipt.png",
        content_path=f"/tmp/{digest}.png",
        sha256=digest,
        mime="image/png",
    )
    extraction_id = await ExtractionRepo(session).add(
        document_id,
        payload=_payload(confidence=confidence, amount=amount),
        field_confidences={"total_amount": confidence, "counterparty": confidence},
        model_name="fake-local-model",
        prompt_version="test",
        actor="worker",
    )
    return document_id, extraction_id


@pytest.mark.asyncio
async def test_every_extraction_is_listed_and_high_confidence_can_be_approved(
    session_factory,
) -> None:
    async with session_factory() as session:
        document_id, extraction_id = await _add(session, digest="a" * 64)
        items = await pending(session, confidence_threshold=0.85)
        assert len(items) == 1
        assert items[0]["extraction_id"] == extraction_id
        assert items[0]["flagged_fields"] == []

        verified_id = await approve(
            session,
            extraction_id,
            expected_version=items[0]["version"],
            corrections={},
            reviewer="local-reviewer",
        )
        await session.commit()
        document = await DocumentRepo(session).get(document_id)

    assert verified_id
    assert document["status"] == DocumentStatus.VERIFIED.value


@pytest.mark.asyncio
async def test_pending_default_threshold_is_local(session_factory) -> None:
    async with session_factory() as session:
        _, extraction_id = await _add(session, digest="9" * 64)
        items = await pending(session)

    assert [item["extraction_id"] for item in items] == [extraction_id]


@pytest.mark.asyncio
async def test_stale_approval_conflicts_after_reprocess(session_factory) -> None:
    async with session_factory() as session:
        document_id, first_id = await _add(session, digest="b" * 64)
        second_id = await ExtractionRepo(session).add(
            document_id,
            payload=_payload(amount=1300.0),
            field_confidences={"total_amount": 0.98},
            model_name="new-model",
            prompt_version="two",
            actor="worker",
        )
        with pytest.raises(StaleExtractionError):
            await approve(
                session,
                first_id,
                expected_version=1,
                corrections={},
                reviewer="local-reviewer",
            )
        await approve(
            session,
            second_id,
            expected_version=1,
            corrections={},
            reviewer="local-reviewer",
        )
        await session.commit()


@pytest.mark.asyncio
async def test_pending_page_order_is_stable_and_low_confidence_is_reported(
    session_factory,
) -> None:
    async with session_factory() as session:
        _, high_id = await _add(session, digest="c" * 64, confidence=0.99)
        _, low_id = await _add(session, digest="d" * 64, confidence=0.25)
        first_page = await pending(session, limit=1, confidence_threshold=0.85)
        items = await pending(session, limit=2, confidence_threshold=0.85)

    assert first_page[0]["extraction_id"] == items[0]["extraction_id"]
    assert {item["extraction_id"] for item in items} == {high_id, low_id}
    low_item = next(item for item in items if item["extraction_id"] == low_id)
    assert low_item["flagged_fields"] == ["counterparty", "total_amount"]


@pytest.mark.asyncio
async def test_rejection_moves_document_to_needs_reprocess(session_factory) -> None:
    async with session_factory() as session:
        document_id, extraction_id = await _add(session, digest="e" * 64)
        await reject(session, extraction_id, reason="blurred total", reviewer="local-reviewer")
        await session.commit()
        document = await DocumentRepo(session).get(document_id)
        extraction = document["extracted"]

    assert document["status"] == DocumentStatus.NEEDS_REPROCESS.value
    assert extraction["status"] == ExtractionStatus.REJECTED.value
    assert extraction["rejection_reason"] == "blurred total"


@pytest.mark.asyncio
async def test_review_queue_exposes_durable_duplicate_evidence(session_factory) -> None:
    async with session_factory() as session:
        source_id, _ = await _add(session, digest="f" * 64)
        duplicate_id, duplicate_extraction = await _add(session, digest="0" * 64)
        session.add(
            DuplicateFlag(
                document_id=duplicate_id,
                suspected_document_id=source_id,
                reason="exact_sha256",
                score=1,
                evidence={"sha256": ["a" * 64]},
            )
        )
        await session.flush()
        item = next(
            candidate
            for candidate in await pending(session, confidence_threshold=0.85)
            if candidate["extraction_id"] == duplicate_extraction
        )

    assert item["suspected_duplicate_of"] == [source_id]
    assert item["duplicate_candidates"][0]["reason"] == "exact_sha256"


@pytest.mark.asyncio
async def test_legacy_approval_refuses_a_generic_batch_candidate(session_factory) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="notes.txt",
            content_path="originals/notes.txt",
            sha256="1" * 64,
            mime="text/plain",
            intake_intent=IntakeIntent.GENERIC_FILE,
        )
        source = await session.scalar(
            select(DocumentFile).where(
                DocumentFile.document_id == document_id,
                DocumentFile.kind == FileKind.ORIGINAL,
            )
        )
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == document_id)
        )
        assert source is not None and intake is not None
        intake.state = SourceIntakeState.PROCESSED
        batch = ExtractionBatch(
            source_intake_id=intake.id,
            document_id=document_id,
            source_file_id=source.id,
            source_version=1,
            source_sha256=source.sha256,
            normalized_sha256="2" * 64,
            structure_fingerprint="3" * 64,
            producer="test",
            producer_version="1",
            origin="test_fixture",
            intake_intent=IntakeIntent.GENERIC_FILE,
            lifecycle=BatchLifecycle.OPEN,
            idempotency_key="legacy-generic-refusal",
            candidate_count=1,
            reconciliation_counts={
                "mapped_candidate": 0,
                "residual_generic_candidate": 1,
                "explicit_ignore": 0,
                "blank": 0,
                "parse_error": 0,
            },
            reconciliation_digest="4" * 64,
        )
        session.add(batch)
        await session.flush()
        candidate = ExtractedRecord(
            document_id=document_id,
            source_file_id=source.id,
            source_version=1,
            batch_id=batch.id,
            candidate_ordinal=1,
            candidate_key="5" * 64,
            record_kind=RecordKind.GENERIC_DOCUMENT,
            source_locator="document",
            row_fingerprint="6" * 64,
            validation_issues=[],
            evidence_group_keys=[],
            payload={"content_markdown": "notes"},
            field_confidences={},
            source_spans={},
            model_name="test",
            prompt_version="1",
            status=ExtractionStatus.PENDING_REVIEW,
        )
        session.add(candidate)
        await session.flush()
        flattened = next(
            item for item in await pending(session) if item["extraction_id"] == candidate.id
        )
        assert flattened["batch_id"] == batch.id
        assert flattened["batch_version"] == 1
        assert flattened["batch_candidate_count"] == 1
        assert flattened["record_kind"] is RecordKind.GENERIC_DOCUMENT

        with pytest.raises(BatchReviewRequiredError) as raised:
            await approve(
                session,
                candidate.id,
                expected_version=1,
                corrections={},
                reviewer="local-reviewer",
            )

        assert raised.value.code == "batch_review_required"
        assert raised.value.detail["batch_id"] == str(batch.id)
        assert candidate.status is ExtractionStatus.PENDING_REVIEW
