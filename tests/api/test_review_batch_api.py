from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from clerksan.api.main import create_app
from clerksan.api.routes import review as review_routes
from clerksan.bills.service import BillValidationError
from clerksan.config import Settings
from clerksan.db import repositories
from clerksan.db.engine import get_session
from clerksan.db.models import (
    BatchLifecycle,
    CandidateReviewDecision,
    Chunk,
    Document,
    DocumentFile,
    DocumentStatus,
    DuplicateFlag,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    FileKind,
    FinancialSubtype,
    IntakeIntent,
    Job,
    RecordKind,
    RecurringBill,
    SourceIntake,
    SourceIntakeState,
    VerifiedRecord,
)
from clerksan.ingest.capabilities import build_capability_registry
from clerksan.review.queue import approve, reject_batch_and_reprocess


def _financial_payload(*, amount: int = 1_200) -> dict:
    return {
        "transaction_date": {"value": "2026-08-23", "confidence": 0.99},
        "total_amount": {"value": amount, "confidence": 0.99},
        "counterparty": {"value": "サンプル商店", "confidence": 0.99},
        "currency": {"value": "JPY", "confidence": 0.99},
    }


async def _seed_batch(
    settings: Settings,
    *,
    kinds: tuple[RecordKind, ...],
    stage_chunks: bool = True,
    recurring: bool = False,
    validation_issues_by_ordinal: dict[int, list[str]] | None = None,
    evidence_group_keys_by_ordinal: dict[int, list[str]] | None = None,
) -> tuple[UUID, list[UUID]]:
    async with get_session(settings) as session:
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
        counts = {
            "mapped_candidate": sum(kind is RecordKind.FINANCIAL for kind in kinds),
            "residual_generic_candidate": sum(
                kind is RecordKind.GENERIC_DOCUMENT for kind in kinds
            ),
            "explicit_ignore": 0,
            "blank": 0,
            "parse_error": 0,
        }
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
            idempotency_key="review-batch",
            candidate_count=len(kinds),
            reconciliation_counts=counts,
            reconciliation_digest="d" * 64,
        )
        session.add(batch)
        await session.flush()
        candidates = [
            ExtractedRecord(
                document_id=document.id,
                source_file_id=source.id,
                source_version=1,
                batch_id=batch.id,
                candidate_ordinal=index,
                candidate_key=f"{index:064x}",
                record_kind=kind,
                financial_subtype=(
                    (FinancialSubtype.RECURRING_BILL if recurring else FinancialSubtype.TRANSACTION)
                    if kind is RecordKind.FINANCIAL
                    else None
                ),
                source_locator=f"row/{index}",
                row_fingerprint=f"{index + 100:064x}",
                validation_issues=list((validation_issues_by_ordinal or {}).get(index, ())),
                evidence_group_keys=list((evidence_group_keys_by_ordinal or {}).get(index, ())),
                payload=(
                    {
                        **_financial_payload(amount=1_000 + index),
                        "issuer_name": {"value": "東京電力", "confidence": 0.99},
                        "issuer_kind": {"value": "electric", "confidence": 0.99},
                        "billing_period": {"value": "2026-08", "confidence": 0.99},
                        "due_date": {"value": "2026-08-31", "confidence": 0.99},
                    }
                    if kind is RecordKind.FINANCIAL and recurring
                    else _financial_payload(amount=1_000 + index)
                    if kind is RecordKind.FINANCIAL
                    else {"text": f"generic {index}"}
                ),
                field_confidences={},
                source_spans={},
                model_name="test",
                prompt_version="1",
                status=ExtractionStatus.PENDING_REVIEW,
            )
            for index, kind in enumerate(kinds, start=1)
        ]
        session.add_all(candidates)
        await session.flush()
        if stage_chunks:
            session.add_all(
                Chunk(
                    document_id=document.id,
                    batch_id=batch.id,
                    extraction_id=candidate.id,
                    record_kind=candidate.record_kind,
                    source_file_id=source.id,
                    source_version=1,
                    candidate_key=candidate.candidate_key,
                    seq=0,
                    heading_path=candidate.source_locator or "candidate",
                    text=f"searchable candidate {candidate.candidate_ordinal}",
                    embedding=[0.1, 0.2],
                    embed_model="test-embed",
                    embed_model_digest="e" * 64,
                    token_count=2,
                )
                for candidate in candidates
            )
            await session.flush()
        return batch.id, [candidate.id for candidate in candidates]


def _settings(tmp_path: Path, name: str) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / name}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )


def test_batch_decisions_preview_and_activation_are_record_scoped(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "batch-review.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(
            _seed_batch(
                settings,
                kinds=(RecordKind.FINANCIAL, RecordKind.GENERIC_DOCUMENT),
            )
        )

        batches = client.get("/review/batches", params={"limit": 1})
        assert batches.status_code == 200
        assert batches.json()["total"] == 1
        assert batches.json()["items"][0]["pending_count"] == 2

        candidates = client.get(
            f"/review/batches/{batch_id}/candidates",
            params={"limit": 1, "offset": 1},
        )
        assert candidates.status_code == 200
        assert candidates.json()["total"] == 2
        assert len(candidates.json()["items"]) == 1

        staged = client.post(
            f"/review/batches/{batch_id}/decisions",
            json={
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [
                    {
                        "extraction_id": str(candidate_ids[0]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "include",
                        "corrections": {"total_amount": 1_250},
                    },
                    {
                        "extraction_id": str(candidate_ids[1]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "exclude",
                        "exclusion_reason": "not an accounting record",
                    },
                ],
            },
        )
        assert staged.status_code == 201, staged.text
        assert staged.json()["batch_version"] == 2
        assert staged.json()["lifecycle"] == "ready_to_activate"

        async def staged_assertions() -> None:
            async with get_session(settings) as session:
                statuses = list(
                    await session.scalars(
                        select(ExtractedRecord.status)
                        .where(ExtractedRecord.batch_id == batch_id)
                        .order_by(ExtractedRecord.candidate_ordinal)
                    )
                )
                assert statuses == [
                    ExtractionStatus.PENDING_REVIEW,
                    ExtractionStatus.PENDING_REVIEW,
                ]
                assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0

        asyncio.run(staged_assertions())

        first_preview = client.get(f"/review/batches/{batch_id}/activation-preview")
        second_preview = client.get(f"/review/batches/{batch_id}/activation-preview")
        assert first_preview.status_code == 200, first_preview.text
        assert (
            first_preview.json()["activation_vector_sha256"]
            == second_preview.json()["activation_vector_sha256"]
        )
        assert first_preview.json()["included_count"] == 1
        assert first_preview.json()["excluded_count"] == 1
        assert first_preview.json()["pending_count"] == 0
        assert first_preview.json()["error_count"] == 0
        assert first_preview.json()["requires_accept_exclusions"] is True

        missing_consent = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": 2,
                "expected_vector_sha256": first_preview.json()["activation_vector_sha256"],
                "actor": "reviewer",
            },
        )
        assert missing_consent.status_code == 422
        assert missing_consent.json()["code"] == "activation_consent_required"

        activated = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": 2,
                "expected_vector_sha256": first_preview.json()["activation_vector_sha256"],
                "actor": "reviewer",
                "accept_exclusions": True,
            },
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["included_count"] == 1
        assert activated.json()["excluded_count"] == 1
        assert list(activated.json()["verified_by_extraction"]) == [str(candidate_ids[0])]

        async def activated_assertions() -> None:
            async with get_session(settings) as session:
                rows = list(
                    await session.scalars(
                        select(ExtractedRecord)
                        .where(ExtractedRecord.batch_id == batch_id)
                        .order_by(ExtractedRecord.candidate_ordinal)
                    )
                )
                assert [row.status for row in rows] == [
                    ExtractionStatus.APPROVED,
                    ExtractionStatus.SUPERSEDED,
                ]
                assert await session.scalar(select(func.count(VerifiedRecord.id))) == 1
                verified = await session.scalar(select(VerifiedRecord))
                assert verified is not None
                assert str(verified.total_amount) == "1250.00"

        asyncio.run(activated_assertions())


def test_one_stale_candidate_makes_the_decision_request_write_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "stale-batch-review.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(
            _seed_batch(
                settings,
                kinds=(RecordKind.FINANCIAL, RecordKind.FINANCIAL),
            )
        )
        response = client.post(
            f"/review/batches/{batch_id}/decisions",
            json={
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [
                    {
                        "extraction_id": str(candidate_ids[0]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "include",
                    },
                    {
                        "extraction_id": str(candidate_ids[1]),
                        "expected_extraction_version": 2,
                        "expected_decision_revision": 0,
                        "action": "include",
                    },
                ],
            },
        )
        assert response.status_code == 409
        assert response.json()["code"] == "stale_review_batch"
        assert response.json()["detail"]["affected_extraction_ids"] == [str(candidate_ids[1])]

        async def assertions() -> None:
            async with get_session(settings) as session:
                assert await session.scalar(select(func.count(CandidateReviewDecision.id))) == 0
                batch = await session.get(ExtractionBatch, batch_id)
                assert batch is not None
                assert batch.version == 1
                assert batch.lifecycle is BatchLifecycle.OPEN

        asyncio.run(assertions())


def test_activation_preview_rejects_missing_candidate_search_chunks(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "missing-candidate-chunks.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(
            _seed_batch(
                settings,
                kinds=(RecordKind.GENERIC_DOCUMENT,),
                stage_chunks=False,
            )
        )
        staged = client.post(
            f"/review/batches/{batch_id}/decisions",
            json={
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [
                    {
                        "extraction_id": str(candidate_ids[0]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "include",
                    }
                ],
            },
        )
        assert staged.status_code == 201, staged.text

        preview = client.get(f"/review/batches/{batch_id}/activation-preview")
        assert preview.status_code == 200, preview.text
        assert preview.json()["ready_for_activation"] is False
        assert preview.json()["error_count"] == 1
        assert preview.json()["errors"][0]["code"] == "candidate_search_chunks_missing"
        blocked = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": 2,
                "expected_vector_sha256": preview.json()["activation_vector_sha256"],
                "actor": "reviewer",
            },
        )
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["code"] == "invalid_review_reconciliation"


def test_empty_batch_activation_requires_explicit_consent(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "empty-batch-review.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(_seed_batch(settings, kinds=()))
        assert candidate_ids == []

        preview = client.get(f"/review/batches/{batch_id}/activation-preview")
        assert preview.status_code == 200, preview.text
        assert preview.json()["ready_for_activation"] is True
        assert preview.json()["requires_accept_empty"] is True

        missing_consent = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": 1,
                "expected_vector_sha256": preview.json()["activation_vector_sha256"],
                "actor": "reviewer",
            },
        )
        assert missing_consent.status_code == 422
        assert missing_consent.json()["code"] == "activation_consent_required"

        activated = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": 1,
                "expected_vector_sha256": preview.json()["activation_vector_sha256"],
                "actor": "reviewer",
                "accept_empty": True,
            },
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["lifecycle"] == "active"
        assert activated.json()["accepted_empty"] is True
        assert activated.json()["verified_by_extraction"] == {}


def test_recurring_projection_is_created_inside_batch_activation(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "recurring-batch-review.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(
            _seed_batch(
                settings,
                kinds=(RecordKind.FINANCIAL,),
                recurring=True,
            )
        )
        staged = client.post(
            f"/review/batches/{batch_id}/decisions",
            json={
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [
                    {
                        "extraction_id": str(candidate_ids[0]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "include",
                    }
                ],
            },
        )
        assert staged.status_code == 201, staged.text
        preview = client.get(f"/review/batches/{batch_id}/activation-preview")
        assert preview.status_code == 200, preview.text
        activated = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": 2,
                "expected_vector_sha256": preview.json()["activation_vector_sha256"],
                "actor": "reviewer",
            },
        )
        assert activated.status_code == 200, activated.text

        async def assertions() -> None:
            async with get_session(settings) as session:
                bill = await session.scalar(select(RecurringBill))
                assert bill is not None
                assert bill.billing_period.isoformat() == "2026-08-01"
                assert str(bill.verified_record_id) in set(
                    activated.json()["verified_by_extraction"].values()
                )

        asyncio.run(assertions())


def test_recurring_decision_rejects_a_nonpositive_verified_amount(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "recurring-nonpositive.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(
            _seed_batch(
                settings,
                kinds=(RecordKind.FINANCIAL,),
                recurring=True,
            )
        )
        rejected = client.post(
            f"/review/batches/{batch_id}/decisions",
            json={
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [
                    {
                        "extraction_id": str(candidate_ids[0]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "include",
                        "corrections": {"total_amount": 0},
                    }
                ],
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["code"] == "invalid_review_decision"

        async def assertions() -> None:
            async with get_session(settings) as session:
                batch = await session.get(ExtractionBatch, batch_id)
                assert batch is not None
                assert batch.version == 1
                assert batch.lifecycle is BatchLifecycle.OPEN
                assert await session.scalar(select(func.count(CandidateReviewDecision.id))) == 0

        asyncio.run(assertions())


def test_recurring_projection_failure_rolls_back_the_entire_activation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path, "recurring-rollback.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(
            _seed_batch(
                settings,
                kinds=(RecordKind.FINANCIAL,),
                recurring=True,
            )
        )
        staged = client.post(
            f"/review/batches/{batch_id}/decisions",
            json={
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [
                    {
                        "extraction_id": str(candidate_ids[0]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "include",
                    }
                ],
            },
        )
        assert staged.status_code == 201, staged.text
        preview = client.get(f"/review/batches/{batch_id}/activation-preview")
        assert preview.status_code == 200, preview.text

        async def fail_projection(*args, **kwargs):
            raise BillValidationError("forced projection failure")

        monkeypatch.setattr(repositories, "record_verified_bill", fail_projection)
        failed = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": 2,
                "expected_vector_sha256": preview.json()["activation_vector_sha256"],
                "actor": "reviewer",
            },
        )
        assert failed.status_code == 422, failed.text
        assert failed.json()["code"] == "recurring_projection_conflict"

        async def assertions() -> None:
            async with get_session(settings) as session:
                batch = await session.get(ExtractionBatch, batch_id)
                candidate = await session.get(ExtractedRecord, candidate_ids[0])
                assert batch is not None and candidate is not None
                assert batch.lifecycle is BatchLifecycle.READY_TO_ACTIVATE
                assert batch.version == 2
                assert candidate.status is ExtractionStatus.PENDING_REVIEW
                assert candidate.version == 1
                assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
                assert await session.scalar(select(func.count(RecurringBill.id))) == 0

        asyncio.run(assertions())


def test_activation_supersedes_legacy_approved_authority(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "legacy-authority-cutover.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(_seed_batch(settings, kinds=(RecordKind.FINANCIAL,)))

        async def add_legacy_authority() -> UUID:
            async with get_session(settings) as session:
                batch = await session.get(ExtractionBatch, batch_id)
                assert batch is not None
                legacy = ExtractedRecord(
                    document_id=batch.document_id,
                    source_file_id=batch.source_file_id,
                    source_version=batch.source_version,
                    payload=_financial_payload(amount=900),
                    field_confidences={},
                    source_spans={},
                    model_name="legacy",
                    prompt_version="1",
                    status=ExtractionStatus.APPROVED,
                    version=2,
                    reviewer="legacy-reviewer",
                )
                session.add(legacy)
                await session.flush()
                session.add(
                    VerifiedRecord(
                        document_id=batch.document_id,
                        extracted_id=legacy.id,
                        transaction_date=dt.date(2026, 8, 23),
                        total_amount=900,
                        counterparty="旧レコード",
                        currency="JPY",
                        reviewer="legacy-reviewer",
                    )
                )
                await session.flush()
                return legacy.id

        legacy_id = asyncio.run(add_legacy_authority())
        staged = client.post(
            f"/review/batches/{batch_id}/decisions",
            json={
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [
                    {
                        "extraction_id": str(candidate_ids[0]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "include",
                    }
                ],
            },
        )
        assert staged.status_code == 201, staged.text
        preview = client.get(f"/review/batches/{batch_id}/activation-preview")
        activated = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": 2,
                "expected_vector_sha256": preview.json()["activation_vector_sha256"],
                "actor": "reviewer",
            },
        )
        assert activated.status_code == 200, activated.text

        async def assertions() -> None:
            async with get_session(settings) as session:
                legacy = await session.get(ExtractedRecord, legacy_id)
                replacement = await session.get(ExtractedRecord, candidate_ids[0])
                assert legacy is not None and replacement is not None
                assert legacy.status is ExtractionStatus.SUPERSEDED
                assert replacement.status is ExtractionStatus.APPROVED

        asyncio.run(assertions())


def test_singleton_queue_approval_respects_caller_rollback(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "singleton-approval-rollback.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000"):
        batch_id, candidate_ids = asyncio.run(_seed_batch(settings, kinds=(RecordKind.FINANCIAL,)))

        async def approve_then_rollback() -> None:
            async with get_session(settings) as session:
                verified_id = await approve(
                    session,
                    candidate_ids[0],
                    expected_version=1,
                    corrections={},
                    reviewer="reviewer",
                )
                assert verified_id is not None
                await session.rollback()

        asyncio.run(approve_then_rollback())

        async def assertions() -> None:
            async with get_session(settings) as session:
                batch = await session.get(ExtractionBatch, batch_id)
                candidate = await session.get(ExtractedRecord, candidate_ids[0])
                assert batch is not None and candidate is not None
                assert batch.lifecycle is BatchLifecycle.OPEN
                assert batch.version == 1
                assert candidate.status is ExtractionStatus.PENDING_REVIEW
                assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
                assert await session.scalar(select(func.count(CandidateReviewDecision.id))) == 0

        asyncio.run(assertions())


def test_reject_and_reprocess_respects_caller_rollback(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "reject-reprocess-rollback.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000"):
        batch_id, candidate_ids = asyncio.run(_seed_batch(settings, kinds=(RecordKind.FINANCIAL,)))

        async def reject_then_rollback() -> None:
            async with get_session(settings) as session:
                result, job_id = await reject_batch_and_reprocess(
                    session,
                    batch_id,
                    expected_batch_version=1,
                    actor="reviewer",
                    reason="run extraction again",
                    settings=settings,
                    capability_registry=build_capability_registry(settings),
                    adapter_key=None,
                    detected_format=None,
                    required_components=(),
                )
                assert result.lifecycle is BatchLifecycle.REJECTED
                assert job_id is not None
                await session.rollback()

        asyncio.run(reject_then_rollback())

        async def assertions() -> None:
            async with get_session(settings) as session:
                batch = await session.get(ExtractionBatch, batch_id)
                candidate = await session.get(ExtractedRecord, candidate_ids[0])
                intake = (
                    await session.scalar(
                        select(SourceIntake).where(SourceIntake.id == batch.source_intake_id)
                    )
                    if batch is not None
                    else None
                )
                assert batch is not None and candidate is not None and intake is not None
                assert batch.lifecycle is BatchLifecycle.OPEN
                assert batch.version == 1
                assert candidate.status is ExtractionStatus.PENDING_REVIEW
                assert intake.state is SourceIntakeState.PROCESSED
                assert await session.scalar(select(func.count(Job.id))) == 0

        asyncio.run(assertions())


def test_batch_detail_and_flattened_queue_keep_document_duplicate_evidence(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, "batch-duplicate-evidence.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(
            _seed_batch(settings, kinds=(RecordKind.GENERIC_DOCUMENT,))
        )

        async def add_duplicate_flag() -> UUID:
            async with get_session(settings) as session:
                batch = await session.get(ExtractionBatch, batch_id)
                assert batch is not None
                suspected = Document(
                    source_filename="suspected.txt",
                    status=DocumentStatus.VERIFIED,
                )
                session.add(suspected)
                await session.flush()
                session.add(
                    DuplicateFlag(
                        document_id=batch.document_id,
                        suspected_document_id=suspected.id,
                        reason="exact_sha256",
                        score=1,
                        evidence={"sha256": [batch.source_sha256]},
                    )
                )
                await session.flush()
                return suspected.id

        suspected_id = asyncio.run(add_duplicate_flag())
        detail = client.get(f"/review/batches/{batch_id}/candidates")
        assert detail.status_code == 200, detail.text
        source_evidence = detail.json()["source_duplicate_evidence"]
        assert len(source_evidence) == 1
        assert source_evidence[0]["scope"] == "document"
        assert source_evidence[0]["suspected_document_id"] == str(suspected_id)
        assert detail.json()["items"][0]["duplicate_evidence"] == []

        flattened = client.get("/review", params={"limit": 100})
        assert flattened.status_code == 200, flattened.text
        item = next(
            row for row in flattened.json() if row["extraction_id"] == str(candidate_ids[0])
        )
        assert item["suspected_duplicate_of"] == [str(suspected_id)]


def test_exception_only_filters_before_pagination_and_includes_candidate_evidence(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, "batch-exception-filter.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(
            _seed_batch(
                settings,
                kinds=(RecordKind.GENERIC_DOCUMENT,) * 6,
                validation_issues_by_ordinal={4: ["missing_required_value"]},
                evidence_group_keys_by_ordinal={5: ["repeated-provider-id:42"]},
            )
        )

        async def add_exception_evidence() -> tuple[UUID, UUID]:
            async with get_session(settings) as session:
                batch = await session.get(ExtractionBatch, batch_id)
                duplicate_candidate = await session.get(ExtractedRecord, candidate_ids[5])
                assert batch is not None
                assert duplicate_candidate is not None

                source_suspect = Document(
                    source_filename="source-suspect.txt",
                    status=DocumentStatus.VERIFIED,
                )
                candidate_suspect = Document(
                    source_filename="candidate-suspect.txt",
                    status=DocumentStatus.VERIFIED,
                )
                session.add_all((source_suspect, candidate_suspect))
                await session.flush()
                session.add_all(
                    (
                        DuplicateFlag(
                            document_id=batch.document_id,
                            suspected_document_id=source_suspect.id,
                            reason="exact_sha256",
                            score=1,
                            evidence={"sha256": [batch.source_sha256]},
                        ),
                        DuplicateFlag(
                            document_id=batch.document_id,
                            suspected_document_id=candidate_suspect.id,
                            source_file_id=batch.source_file_id,
                            source_version=batch.source_version,
                            batch_id=batch.id,
                            extraction_id=duplicate_candidate.id,
                            candidate_key=duplicate_candidate.candidate_key,
                            record_kind=duplicate_candidate.record_kind,
                            reason="repeated_source_row",
                            score=0.95,
                            evidence={"candidate_key": [duplicate_candidate.candidate_key]},
                        ),
                    )
                )
                await session.flush()
                return source_suspect.id, candidate_suspect.id

        source_suspect_id, candidate_suspect_id = asyncio.run(add_exception_evidence())

        async def assert_filtered_snapshot_lineage() -> None:
            async with get_session(settings) as session:
                page = await repositories.ReviewBatchRepo(session).list_candidates(
                    batch_id,
                    limit=1,
                    exceptions_only=True,
                )
                assert page.items[0].row_fingerprint == f"{104:064x}"

        asyncio.run(assert_filtered_snapshot_lineage())

        normal_page = client.get(
            f"/review/batches/{batch_id}/candidates",
            params={"limit": 2, "offset": 0},
        )
        assert normal_page.status_code == 200, normal_page.text
        assert normal_page.json()["total"] == 6
        assert [item["extraction_id"] for item in normal_page.json()["items"]] == [
            str(candidate_id) for candidate_id in candidate_ids[3:5]
        ]
        next_normal_page = client.get(
            f"/review/batches/{batch_id}/candidates",
            params={"limit": 2, "offset": 2},
        )
        assert next_normal_page.status_code == 200, next_normal_page.text
        assert [item["extraction_id"] for item in next_normal_page.json()["items"]] == [
            str(candidate_ids[5]),
            str(candidate_ids[0]),
        ]

        validation_page = client.get(
            f"/review/batches/{batch_id}/candidates",
            params={"limit": 1, "offset": 0, "exceptions_only": True},
        )
        assert validation_page.status_code == 200, validation_page.text
        assert validation_page.json()["total"] == 3
        assert validation_page.json()["items"][0]["extraction_id"] == str(candidate_ids[3])
        assert validation_page.json()["items"][0]["validation_issues"] == ["missing_required_value"]
        assert validation_page.json()["source_duplicate_evidence"][0][
            "suspected_document_id"
        ] == str(source_suspect_id)

        group_page = client.get(
            f"/review/batches/{batch_id}/candidates",
            params={"limit": 1, "offset": 1, "exceptions_only": True},
        )
        assert group_page.status_code == 200, group_page.text
        assert group_page.json()["total"] == 3
        assert group_page.json()["items"][0]["extraction_id"] == str(candidate_ids[4])
        assert group_page.json()["items"][0]["evidence_group_keys"] == ["repeated-provider-id:42"]

        duplicate_page = client.get(
            f"/review/batches/{batch_id}/candidates",
            params={"limit": 1, "offset": 2, "exceptions_only": True},
        )
        assert duplicate_page.status_code == 200, duplicate_page.text
        assert duplicate_page.json()["total"] == 3
        assert duplicate_page.json()["items"][0]["extraction_id"] == str(candidate_ids[5])
        assert duplicate_page.json()["items"][0]["duplicate_evidence"][0]["scope"] == "candidate"
        assert duplicate_page.json()["items"][0]["duplicate_evidence"][0][
            "suspected_document_id"
        ] == str(candidate_suspect_id)


def test_optimistic_version_fields_reject_json_booleans(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "strict-review-versions.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(_seed_batch(settings, kinds=(RecordKind.FINANCIAL,)))
        base_decision = {
            "extraction_id": str(candidate_ids[0]),
            "expected_extraction_version": 1,
            "expected_decision_revision": 0,
            "action": "include",
        }
        bodies = [
            {
                "expected_batch_version": True,
                "actor": "reviewer",
                "decisions": [base_decision],
            },
            {
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [{**base_decision, "expected_extraction_version": True}],
            },
            {
                "expected_batch_version": 1,
                "actor": "reviewer",
                "decisions": [{**base_decision, "expected_decision_revision": False}],
            },
        ]
        for body in bodies:
            response = client.post(
                f"/review/batches/{batch_id}/decisions",
                json=body,
            )
            assert response.status_code == 422, response.text

        activation = client.post(
            f"/review/batches/{batch_id}/activate",
            json={
                "expected_batch_version": True,
                "expected_vector_sha256": "0" * 64,
                "actor": "reviewer",
            },
        )
        assert activation.status_code == 422, activation.text
        reprocess = client.post(
            f"/review/batches/{batch_id}/reject-and-reprocess",
            json={
                "expected_batch_version": True,
                "actor": "reviewer",
                "reason": "retry",
            },
        )
        assert reprocess.status_code == 422, reprocess.text
        legacy = client.post(
            "/review/approve",
            json={
                "extraction_id": str(candidate_ids[0]),
                "expected_version": True,
                "reviewer": "reviewer",
                "corrections": {},
            },
        )
        assert legacy.status_code == 422, legacy.text

        async def assertions() -> None:
            async with get_session(settings) as session:
                batch = await session.get(ExtractionBatch, batch_id)
                assert batch is not None and batch.version == 1
                assert await session.scalar(select(func.count(CandidateReviewDecision.id))) == 0

        asyncio.run(assertions())


def test_locked_rejection_refreshes_cached_batch_version(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "locked-review-refresh.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000"):
        batch_id, candidate_ids = asyncio.run(_seed_batch(settings, kinds=(RecordKind.FINANCIAL,)))

        async def stale_reader_race() -> None:
            async with get_session(settings) as reader:
                preview = await repositories.ReviewBatchRepo(reader).activation_preview(batch_id)
                assert preview.batch_version == 1
                async with get_session(settings) as writer:
                    await repositories.ReviewBatchRepo(writer).apply_decisions(
                        batch_id,
                        1,
                        [
                            repositories.ReviewDecisionDraft(
                                extraction_id=candidate_ids[0],
                                expected_extraction_version=1,
                                expected_decision_revision=0,
                                action="include",
                            )
                        ],
                        "other-reviewer",
                    )
                with pytest.raises(repositories.ReviewBatchConflictError) as conflict:
                    await repositories.ReviewBatchRepo(reader).reject_batch(
                        batch_id,
                        expected_batch_version=1,
                        actor="reviewer",
                        reason="stale screen",
                    )
                assert conflict.value.detail["current_version"] == 2
                assert (
                    conflict.value.detail["current_lifecycle"]
                    == BatchLifecycle.READY_TO_ACTIVATE.value
                )

        asyncio.run(stale_reader_race())


def test_stale_reprocess_is_rejected_before_capability_planning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path, "stale-reprocess-preflight.sqlite")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        batch_id, candidate_ids = asyncio.run(_seed_batch(settings, kinds=(RecordKind.FINANCIAL,)))
        staged = client.post(
            f"/review/batches/{batch_id}/decisions",
            json={
                "expected_batch_version": 1,
                "actor": "other-reviewer",
                "decisions": [
                    {
                        "extraction_id": str(candidate_ids[0]),
                        "expected_extraction_version": 1,
                        "expected_decision_revision": 0,
                        "action": "include",
                    }
                ],
            },
        )
        assert staged.status_code == 201, staged.text

        capability_called = False

        async def unexpected_capability_plan(*args, **kwargs):
            nonlocal capability_called
            capability_called = True
            raise AssertionError("capability planning must follow locked version validation")

        monkeypatch.setattr(
            review_routes,
            "plan_exact_source_reprocess",
            unexpected_capability_plan,
        )
        response = client.post(
            f"/review/batches/{batch_id}/reject-and-reprocess",
            json={
                "expected_batch_version": 1,
                "actor": "reviewer",
                "reason": "stale screen",
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "stale_review_batch"
        assert response.json()["detail"]["current_version"] == 2
        assert capability_called is False
