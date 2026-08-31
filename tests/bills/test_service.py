from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.bills.analysis import list_bills
from clerksan.bills.reminders import BillNotFoundError, list_reminders, mark_paid
from clerksan.bills.service import BillConflictError, record_verified_bill
from clerksan.db.models import (
    Base,
    BatchLifecycle,
    DocumentClass,
    ExtractedRecord,
    ExtractionBatch,
    FinancialSubtype,
    Issuer,
    PaymentStatus,
    RecordKind,
    RecurringBill,
    SourceIntake,
    VerifiedRecord,
)
from clerksan.db.repositories import DocumentRepo, ExtractionRepo, VerifiedRepo
from clerksan.review.queue import approve


def payload(*, counterparty: str) -> dict:
    return {
        "transaction_date": {"value": "2026-07-01", "confidence": 0.99},
        "total_amount": {"value": 1200, "confidence": 0.99},
        "counterparty": {"value": counterparty, "confidence": 0.99},
        "currency": {"value": "JPY", "confidence": 0.99},
    }


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bills.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def create_verified(session, *, suffix: str, counterparty: str):
    documents = DocumentRepo(session)
    document_id = await documents.create_with_raw(
        filename=f"bill-{suffix}.pdf",
        content_path=f"/tmp/bill-{suffix}.pdf",
        sha256=suffix * 64,
        mime="application/pdf",
        doc_class=DocumentClass.RECURRING_BILL,
    )
    extraction_id = await ExtractionRepo(session).add(
        document_id,
        payload=payload(counterparty=counterparty),
        field_confidences={},
        model_name="test-model",
        prompt_version="test",
        actor="worker",
    )
    verified_id = await VerifiedRepo(session).promote(
        extraction_id,
        1,
        corrections={},
        reviewer="reviewer",
    )
    return document_id, verified_id


async def attach_non_active_batch(session, *, verified_id, document_id) -> None:
    verified = await session.get(VerifiedRecord, verified_id)
    assert verified is not None
    extraction = await session.get(ExtractedRecord, verified.extracted_id)
    assert extraction is not None
    intake = await session.scalar(
        select(SourceIntake).where(SourceIntake.document_id == document_id)
    )
    assert intake is not None
    batch = ExtractionBatch(
        source_intake_id=intake.id,
        document_id=document_id,
        source_file_id=intake.source_file_id,
        source_version=intake.source_version,
        source_sha256=intake.source_sha256,
        normalized_sha256="1" * 64,
        structure_fingerprint="2" * 64,
        producer="test",
        producer_version="1",
        origin="test",
        intake_intent=intake.intake_intent,
        lifecycle=BatchLifecycle.OPEN,
        idempotency_key=f"non-active:{verified_id}",
        candidate_count=1,
        reconciliation_counts={
            "mapped_candidate": 1,
            "residual_generic_candidate": 0,
            "explicit_ignore": 0,
            "blank": 0,
            "parse_error": 0,
        },
        reconciliation_digest="3" * 64,
    )
    session.add(batch)
    await session.flush()
    extraction.batch_id = batch.id
    extraction.candidate_ordinal = 1
    extraction.candidate_key = "4" * 64
    extraction.record_kind = RecordKind.FINANCIAL
    extraction.financial_subtype = FinancialSubtype.RECURRING_BILL
    extraction.source_file_id = intake.source_file_id
    extraction.source_version = intake.source_version
    extraction.source_locator = "test:row:1"
    extraction.row_fingerprint = "5" * 64
    extraction.validation_issues = []
    extraction.evidence_group_keys = []
    await session.flush()


@pytest.mark.asyncio
async def test_verified_bill_is_idempotent_and_payment_transition_is_retry_safe(
    session_factory,
) -> None:
    async with session_factory() as session:
        document_id, verified_id = await create_verified(
            session, suffix="a", counterparty="東京電力"
        )
        bill = await record_verified_bill(
            session,
            verified_record_id=verified_id,
            issuer_name="東京電力",
            issuer_kind="electric",
            billing_period=dt.date(2026, 7, 1),
            due_date=dt.date(2026, 7, 31),
            consumption_value=Decimal("42.5"),
            consumption_unit="kWh",
        )
        repeat = await record_verified_bill(
            session,
            verified_record_id=verified_id,
            issuer_name="東京電力",
            issuer_kind="electric",
            billing_period=dt.date(2026, 7, 1),
            due_date=dt.date(2026, 7, 31),
            consumption_value=Decimal("42.5"),
            consumption_unit="kWh",
        )
        assert repeat.id == bill.id
        assert bill.document_id == document_id
        assert bill.amount == Decimal("1200.00")

        paid = await mark_paid(session, bill.id, actor="reviewer")
        retry = await mark_paid(session, bill.id, actor="reviewer")
        assert paid.payment_status == PaymentStatus.PAID
        assert paid.paid_at is not None
        assert retry.paid_at == paid.paid_at
        assert retry.reviewer == "reviewer"
        await session.commit()


@pytest.mark.asyncio
async def test_non_active_batch_has_no_bill_or_payment_authority(session_factory) -> None:
    async with session_factory() as session:
        document_id, verified_id = await create_verified(
            session, suffix="9", counterparty="Inactive Energy"
        )
        bill = await record_verified_bill(
            session,
            verified_record_id=verified_id,
            issuer_name="Inactive Energy",
            issuer_kind="electric",
            billing_period=dt.date(2026, 8, 1),
            due_date=dt.date(2026, 8, 31),
        )
        await attach_non_active_batch(
            session,
            verified_id=verified_id,
            document_id=document_id,
        )

        assert await list_bills(session) == []
        assert await list_reminders(
            session,
            days_ahead=31,
            as_of=dt.date(2026, 8, 1),
        ) == {"upcoming": [], "overdue": []}
        with pytest.raises(BillNotFoundError):
            await mark_paid(session, bill.id, actor="reviewer")


@pytest.mark.asyncio
async def test_a_second_source_cannot_silently_replace_an_issuer_period(session_factory) -> None:
    async with session_factory() as session:
        _, first_verified_id = await create_verified(session, suffix="b", counterparty="東京ガス")
        await record_verified_bill(
            session,
            verified_record_id=first_verified_id,
            issuer_name="東京ガス",
            issuer_kind="gas",
            billing_period=dt.date(2026, 7, 1),
            due_date=None,
        )
        _, second_verified_id = await create_verified(session, suffix="c", counterparty="東京ガス")
        with pytest.raises(BillConflictError, match="already has a bill"):
            await record_verified_bill(
                session,
                verified_record_id=second_verified_id,
                issuer_name="東京ガス",
                issuer_kind="gas",
                billing_period=dt.date(2026, 7, 1),
                due_date=None,
            )


@pytest.mark.asyncio
async def test_recurring_bill_review_approval_creates_the_normalized_bill_projection(
    session_factory,
) -> None:
    recurring_payload = payload(counterparty="東京電力") | {
        "issuer_name": {"value": "東京電力", "confidence": 0.99},
        "issuer_kind": {"value": "electric", "confidence": 0.99},
        "billing_period": {"value": "2026-07", "confidence": 0.99},
        "due_date": {"value": "2026-07-31", "confidence": 0.99},
        "consumption_value": {"value": 42, "confidence": 0.99},
        "consumption_unit": {"value": "kWh", "confidence": 0.99},
    }
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="electricity.pdf",
            content_path="/tmp/electricity.pdf",
            sha256="d" * 64,
            mime="application/pdf",
            doc_class=DocumentClass.RECURRING_BILL,
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload=recurring_payload,
            field_confidences={},
            model_name="test-model",
            prompt_version="test",
            actor="worker",
        )
        verified_id = await approve(
            session,
            extraction_id,
            expected_version=1,
            corrections={},
            reviewer="reviewer",
        )
        bill = await session.scalar(
            select(RecurringBill).where(RecurringBill.verified_record_id == verified_id)
        )

    assert bill is not None
    assert bill.billing_period == dt.date(2026, 7, 1)
    assert bill.consumption_unit == "kWh"


@pytest.mark.asyncio
async def test_recurring_bill_review_corrections_can_complete_missing_projection_fields(
    session_factory,
) -> None:
    recurring_payload = payload(counterparty="東京電力") | {
        "issuer_name": {"value": None, "confidence": 0.0},
        "issuer_kind": {"value": None, "confidence": 0.0},
        "billing_period": {"value": "2026-07", "confidence": 0.99},
        "due_date": {"value": "2026-07-31", "confidence": 0.99},
        "consumption_value": {"value": 42, "confidence": 0.99},
        "consumption_unit": {"value": "kWh", "confidence": 0.99},
    }
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="incomplete-electricity.pdf",
            content_path="/tmp/incomplete-electricity.pdf",
            sha256="f" * 64,
            mime="application/pdf",
            doc_class=DocumentClass.RECURRING_BILL,
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload=recurring_payload,
            field_confidences={},
            model_name="test-model",
            prompt_version="test",
            actor="worker",
        )
        verified_id = await approve(
            session,
            extraction_id,
            expected_version=1,
            corrections={"issuer_name": "東京電力", "issuer_kind": "electric"},
            reviewer="reviewer",
        )
        bill = await session.scalar(
            select(RecurringBill).where(RecurringBill.verified_record_id == verified_id)
        )
        issuer = await session.get(Issuer, bill.issuer_id) if bill is not None else None

    assert bill is not None
    assert issuer is not None
    assert issuer.name == "東京電力"
    assert bill.review_corrections == {"issuer_name": True, "issuer_kind": True}


@pytest.mark.asyncio
async def test_reprocessed_recurring_bill_replaces_only_the_active_period_projection(
    session_factory,
) -> None:
    recurring_payload = payload(counterparty="東京電力") | {
        "issuer_name": {"value": "東京電力", "confidence": 0.99},
        "issuer_kind": {"value": "electric", "confidence": 0.99},
        "billing_period": {"value": "2026-07", "confidence": 0.99},
        "due_date": {"value": "2026-07-31", "confidence": 0.99},
        "consumption_value": {"value": 42, "confidence": 0.99},
        "consumption_unit": {"value": "kWh", "confidence": 0.99},
    }
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="electricity.pdf",
            content_path="/tmp/electricity-reprocess.pdf",
            sha256="e" * 64,
            mime="application/pdf",
            doc_class=DocumentClass.RECURRING_BILL,
        )
        extractions = ExtractionRepo(session)
        first_id = await extractions.add(
            document_id,
            payload=recurring_payload,
            field_confidences={},
            model_name="test-model",
            prompt_version="one",
            actor="worker",
        )
        first_verified = await approve(
            session,
            first_id,
            expected_version=1,
            corrections={"due_date": "2026-08-01"},
            reviewer="reviewer",
        )
        second_id = await extractions.add(
            document_id,
            payload=recurring_payload | {"total_amount": {"value": 1300, "confidence": 0.99}},
            field_confidences={},
            model_name="test-model",
            prompt_version="two",
            actor="worker",
        )
        second_verified = await approve(
            session,
            second_id,
            expected_version=1,
            corrections={},
            reviewer="replacement-reviewer",
        )
        bills = (
            await session.scalars(
                select(RecurringBill)
                .where(RecurringBill.document_id == document_id)
                .order_by(RecurringBill.created_at.asc())
            )
        ).all()

    assert len(bills) == 2
    assert bills[0].verified_record_id == first_verified
    assert bills[0].review_corrections == {"due_date": True}
    assert bills[0].superseded_at is not None
    assert bills[1].verified_record_id == second_verified
    assert bills[1].superseded_at is None
