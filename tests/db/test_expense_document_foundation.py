from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db.models import Base, DocumentClass, ExpenseKind, VerifiedRecord
from clerksan.db.repositories import DocumentRepo, ExtractionRepo, VerifiedRepo


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'expense.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bill_approval_persists_expense_kind_due_date_and_version(session_factory) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="tax.pdf",
            content_path="/tmp/tax.pdf",
            sha256="e" * 64,
            mime="application/pdf",
            doc_class=DocumentClass.BILL,
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload={
                "transaction_date": {"value": "2026-07-15", "confidence": 0.99},
                "total_amount": {"value": 250000, "confidence": 0.99},
                "counterparty": {"value": "City Tax Office", "confidence": 0.99},
                "currency": {"value": "VND", "confidence": 0.99},
                "expense_kind": {"value": "tax", "confidence": 0.99},
                "due_date": {"value": "2026-08-31", "confidence": 0.99},
            },
            field_confidences={},
            model_name="test",
            prompt_version="test",
            actor="worker",
        )
        verified_id = await VerifiedRepo(session).promote(
            extraction_id,
            1,
            corrections={},
            reviewer="reviewer",
        )
        verified = await session.get(VerifiedRecord, verified_id)

    assert verified is not None
    assert verified.expense_kind is ExpenseKind.TAX
    assert verified.due_date.isoformat() == "2026-08-31"
    assert verified.version == 1


def test_migration_is_additive_and_conservative() -> None:
    migration = Path("migrations/0014_expense_document_foundation.sql").read_text(encoding="utf-8")

    assert "'bill'" in migration
    assert "expense_kind" in migration
    assert "due_date" in migration
    assert "version" in migration
    assert "JOIN recurring_bills" in migration
    assert "UPDATE extracted_records" not in migration
