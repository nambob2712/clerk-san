from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from clerksan.api.main import create_app
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.models import (
    AuditEntry,
    BatchLifecycle,
    DocumentFile,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    FileKind,
    FinancialSubtype,
    IntakeIntent,
    Issuer,
    IssuerKind,
    RecordKind,
    RecurringBill,
    SourceIntake,
    SourceIntakeState,
)
from clerksan.db.repositories import DocumentRepo, ExtractionRepo, SourceIntakeRepo


async def _seed_review_item(settings: Settings) -> tuple[UUID, UUID]:
    async with get_session(settings) as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="review.png",
            content_path="/tmp/review.png",
            sha256="c" * 64,
            mime="image/png",
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload={
                "transaction_date": {"value": "2026-07-13", "confidence": 0.99},
                "total_amount": {"value": 1200, "confidence": 0.99},
                "counterparty": {"value": "サンプル商店", "confidence": 0.99},
                "currency": {"value": "JPY", "confidence": 0.99},
            },
            field_confidences={
                "transaction_date": 0.99,
                "total_amount": 0.99,
                "counterparty": 0.99,
            },
            model_name="test-model",
            prompt_version="test",
            actor="worker",
        )
    return document_id, extraction_id


async def _seed_batch_review_item(
    settings: Settings, *, record_kind: RecordKind, candidate_count: int
) -> tuple[UUID, UUID]:
    async with get_session(settings) as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="generated-review-source.csv",
            content_path="originals/generated-review-source.csv",
            sha256="9" * 64,
            mime="text/csv",
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
        await SourceIntakeRepo(session).transition(
            intake.id,
            expected_version=intake.version,
            state=SourceIntakeState.PROCESSED,
            actor="test",
        )
        batch = ExtractionBatch(
            source_intake_id=intake.id,
            document_id=document_id,
            source_file_id=source.id,
            source_version=1,
            source_sha256=source.sha256,
            normalized_sha256="8" * 64,
            structure_fingerprint="7" * 64,
            producer="test",
            producer_version="1",
            origin="generated_fixture",
            intake_intent=IntakeIntent.GENERIC_FILE,
            lifecycle=BatchLifecycle.OPEN,
            idempotency_key=f"api-batch-review-{record_kind.value}-{candidate_count}",
            candidate_count=candidate_count,
            reconciliation_counts={
                "mapped_candidate": candidate_count,
                "residual_generic_candidate": 0,
                "explicit_ignore": 0,
                "blank": 0,
                "parse_error": 0,
            },
            reconciliation_digest="6" * 64,
        )
        session.add(batch)
        await session.flush()
        candidates: list[ExtractedRecord] = []
        for ordinal in range(1, candidate_count + 1):
            candidate = ExtractedRecord(
                document_id=document_id,
                source_file_id=source.id,
                source_version=1,
                batch_id=batch.id,
                candidate_ordinal=ordinal,
                candidate_key=f"{ordinal:064x}",
                record_kind=record_kind,
                financial_subtype=(
                    FinancialSubtype.TRANSACTION if record_kind is RecordKind.FINANCIAL else None
                ),
                source_locator=f"row:{ordinal}",
                row_fingerprint=f"{ordinal + 10:064x}",
                validation_issues=[],
                evidence_group_keys=[],
                payload=(
                    {
                        "transaction_date": {"value": "2026-08-23", "confidence": 1.0},
                        "total_amount": {"value": ordinal, "confidence": 1.0},
                        "currency": {"value": "JPY", "confidence": 1.0},
                    }
                    if record_kind is RecordKind.FINANCIAL
                    else {"content_markdown": f"Synthetic row {ordinal}"}
                ),
                field_confidences={},
                source_spans={},
                model_name="test",
                prompt_version="1",
                status=ExtractionStatus.PENDING_REVIEW,
            )
            session.add(candidate)
            candidates.append(candidate)
        await session.flush()
        return candidates[0].id, batch.id


async def _reprocess_review_item(settings: Settings, document_id: UUID) -> UUID:
    async with get_session(settings) as session:
        return await ExtractionRepo(session).add(
            document_id,
            payload={
                "transaction_date": {"value": "2026-07-14", "confidence": 0.99},
                "total_amount": {"value": 1300, "confidence": 0.99},
                "counterparty": {"value": "サンプル商店", "confidence": 0.99},
                "currency": {"value": "JPY", "confidence": 0.99},
            },
            field_confidences={
                "transaction_date": 0.99,
                "total_amount": 0.99,
                "counterparty": 0.99,
            },
            model_name="replacement-model",
            prompt_version="replacement",
            actor="worker",
        )


async def _add_prior_version_audit_entries(
    settings: Settings, extraction_id: UUID, verified_id: str
) -> None:
    async with get_session(settings) as session:
        session.add_all(
            (
                AuditEntry(
                    actor="reviewer",
                    table_name="verified_records",
                    row_pk=verified_id,
                    action="UPDATE",
                    field="total_amount",
                    old_value="1200.00",
                    new_value="1250.00",
                    at=dt.datetime(2026, 7, 14, 9, tzinfo=dt.UTC),
                ),
                AuditEntry(
                    actor="replacement-reviewer",
                    table_name="extracted_records",
                    row_pk=str(extraction_id),
                    action="UPDATE",
                    field="status",
                    old_value="approved",
                    new_value="superseded",
                    at=dt.datetime(2026, 7, 14, 10, tzinfo=dt.UTC),
                ),
            )
        )


async def _add_recurring_bill_audit_entry(
    settings: Settings, document_id: UUID, verified_id: str
) -> UUID:
    async with get_session(settings) as session:
        issuer = Issuer(name="東京ガス", kind=IssuerKind.GAS)
        session.add(issuer)
        await session.flush()
        bill = RecurringBill(
            issuer_id=issuer.id,
            document_id=document_id,
            verified_record_id=UUID(verified_id),
            billing_period=dt.date(2026, 7, 1),
            amount=Decimal("1200.00"),
        )
        session.add(bill)
        await session.flush()
        session.add(
            AuditEntry(
                actor="reviewer",
                table_name="recurring_bills",
                row_pk=str(bill.id),
                action="UPDATE",
                field="payment_status",
                old_value='"unpaid"',
                new_value='"paid"',
            )
        )
        return bill.id


def _review_item(client: TestClient, extraction_id: UUID) -> dict:
    return next(
        entry
        for entry in client.get("/review").json()
        if entry["extraction_id"] == str(extraction_id)
    )


def test_review_approval_uses_extraction_version_and_conflicts_when_stale(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'review.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        document_id, extraction_id = asyncio.run(_seed_review_item(settings))
        review = client.get("/review")
        assert review.status_code == 200
        item = next(
            entry for entry in review.json() if entry["extraction_id"] == str(extraction_id)
        )
        assert item["flagged_fields"] == []
        assert item["source_version"] == 1
        assert item["source_file_id"]
        assert item["source_spans"] == {}

        approval = client.post(
            "/review/approve",
            json={
                "extraction_id": str(extraction_id),
                "expected_version": item["version"],
                "corrections": {"total_amount": 1250},
                "reviewer": "reviewer",
            },
        )
        assert approval.status_code == 200
        assert approval.json()["verified_id"]

        stale = client.post(
            "/review/approve",
            json={
                "extraction_id": str(extraction_id),
                "expected_version": item["version"],
                "corrections": {},
                "reviewer": "reviewer",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "stale_extraction"

        detail = client.get(f"/documents/{document_id}")
        assert detail.status_code == 200
        assert detail.json()["verified"]["total_amount"] == "1250.00"
        assert detail.json()["verified"]["source_file_id"] == item["source_file_id"]
        assert detail.json()["verified"]["source_version"] == item["source_version"]


def test_document_detail_keeps_verified_source_identity_when_a_new_source_is_pending(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'verified-source.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        document_id, extraction_id = asyncio.run(_seed_review_item(settings))
        first_item = _review_item(client, extraction_id)
        approval = client.post(
            "/review/approve",
            json={
                "extraction_id": str(extraction_id),
                "expected_version": first_item["version"],
                "corrections": {},
                "reviewer": "reviewer",
            },
        )
        assert approval.status_code == 200

        async def append_pending_source() -> UUID:
            async with get_session(settings) as session:
                await DocumentRepo(session).append_raw_source(
                    document_id,
                    filename="replacement.png",
                    content_path="/tmp/replacement.png",
                    sha256="d" * 64,
                    mime="image/png",
                    actor="reviewer",
                )
                return await ExtractionRepo(session).add(
                    document_id,
                    payload={
                        "transaction_date": {"value": "2026-07-14", "confidence": 0.99},
                        "total_amount": {"value": 1300, "confidence": 0.99},
                        "counterparty": {"value": "サンプル商店", "confidence": 0.99},
                        "currency": {"value": "JPY", "confidence": 0.99},
                    },
                    field_confidences={},
                    model_name="replacement-model",
                    prompt_version="replacement",
                    actor="worker",
                )

        pending_id = asyncio.run(append_pending_source())
        detail = client.get(f"/documents/{document_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["extracted"]["id"] == str(pending_id)
        assert body["extracted"]["source_version"] == 2
        assert body["verified"]["source_file_id"] == first_item["source_file_id"]
        assert body["verified"]["source_version"] == first_item["source_version"]


def test_review_validation_returns_typed_422_responses(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'review-validation.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        _, extraction_id = asyncio.run(_seed_review_item(settings))
        invalid_correction = client.post(
            "/review/approve",
            json={
                "extraction_id": str(extraction_id),
                "expected_version": 1,
                "corrections": {"unexpected": "value"},
                "reviewer": "reviewer",
            },
        )
        blank_reviewer = client.post(
            "/review/approve",
            json={
                "extraction_id": str(extraction_id),
                "expected_version": 1,
                "corrections": {},
                "reviewer": "   ",
            },
        )
        blank_reason = client.post(
            "/review/reject",
            json={
                "extraction_id": str(extraction_id),
                "reason": "   ",
                "reviewer": "reviewer",
            },
        )

    assert invalid_correction.status_code == 422
    assert invalid_correction.json()["code"] == "invalid_review"
    assert blank_reviewer.status_code == 422
    assert blank_reason.status_code == 422


@pytest.mark.parametrize(
    ("record_kind", "candidate_count"),
    ((RecordKind.GENERIC_DOCUMENT, 1), (RecordKind.FINANCIAL, 2)),
)
def test_legacy_review_mutations_require_batch_review_for_generic_or_multi_record_sources(
    tmp_path: Path,
    record_kind: RecordKind,
    candidate_count: int,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'batch-review.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        extraction_id, batch_id = asyncio.run(
            _seed_batch_review_item(
                settings,
                record_kind=record_kind,
                candidate_count=candidate_count,
            )
        )
        approval = client.post(
            "/review/approve",
            json={
                "extraction_id": str(extraction_id),
                "expected_version": 1,
                "corrections": {},
                "reviewer": "local-reviewer",
            },
        )
        rejection = client.post(
            "/review/reject",
            json={
                "extraction_id": str(extraction_id),
                "reason": "Use batch review",
                "reviewer": "local-reviewer",
            },
        )

    for response in (approval, rejection):
        assert response.status_code == 409
        assert response.json()["code"] == "batch_review_required"
        assert response.json()["detail"]["batch_id"] == str(batch_id)


def test_document_detail_keeps_audit_history_for_superseded_versions(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'review-history.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        document_id, first_extraction_id = asyncio.run(_seed_review_item(settings))
        first_item = _review_item(client, first_extraction_id)
        first_approval = client.post(
            "/review/approve",
            json={
                "extraction_id": str(first_extraction_id),
                "expected_version": first_item["version"],
                "corrections": {"total_amount": 1250},
                "reviewer": "reviewer",
            },
        )
        assert first_approval.status_code == 200

        replacement_extraction_id = asyncio.run(_reprocess_review_item(settings, document_id))
        replacement_item = _review_item(client, replacement_extraction_id)
        replacement_approval = client.post(
            "/review/approve",
            json={
                "extraction_id": str(replacement_extraction_id),
                "expected_version": replacement_item["version"],
                "corrections": {},
                "reviewer": "replacement-reviewer",
            },
        )
        assert replacement_approval.status_code == 200
        asyncio.run(
            _add_prior_version_audit_entries(
                settings,
                first_extraction_id,
                first_approval.json()["verified_id"],
            )
        )

        detail = client.get(f"/documents/{document_id}")

    assert detail.status_code == 200
    history = detail.json()["audit_history"]
    assert {(entry["table_name"], entry["field"], entry["new_value"]) for entry in history} == {
        ("verified_records", "total_amount", "1250.00"),
        ("extracted_records", "status", "superseded"),
    }
    assert len(history) == len({entry["id"] for entry in history})


def test_document_detail_includes_recurring_bill_audit_history(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'bill-history.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        document_id, extraction_id = asyncio.run(_seed_review_item(settings))
        item = _review_item(client, extraction_id)
        approval = client.post(
            "/review/approve",
            json={
                "extraction_id": str(extraction_id),
                "expected_version": item["version"],
                "corrections": {},
                "reviewer": "reviewer",
            },
        )
        assert approval.status_code == 200
        bill_id = asyncio.run(
            _add_recurring_bill_audit_entry(settings, document_id, approval.json()["verified_id"])
        )
        detail = client.get(f"/documents/{document_id}")

    assert detail.status_code == 200
    assert ("recurring_bills", str(bill_id), "payment_status") in {
        (entry["table_name"], entry["row_pk"], entry["field"])
        for entry in detail.json()["audit_history"]
    }
