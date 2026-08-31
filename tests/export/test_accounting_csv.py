from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db.models import Base
from clerksan.db.repositories import DocumentRepo, ExtractionRepo, VerifiedRepo
from clerksan.export.accounting_csv import (
    AccountingExportError,
    ExportRecord,
    export_freee,
    render_freee,
    render_yayoi,
)


def record() -> ExportRecord:
    return ExportRecord(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        document_id=UUID("00000000-0000-0000-0000-000000000002"),
        transaction_date=dt.date(2026, 7, 13),
        total_amount=Decimal("1200"),
        counterparty="サンプル商店",
        category="会議費",
        currency="JPY",
        tax_8_amount=None,
        tax_10_amount=Decimal("109"),
        source_filename="receipt.png",
    )


def test_freee_utf8_csv_matches_the_pinned_golden_bytes() -> None:
    expected_header = (
        "[表題行]",
        "日付",
        "伝票番号",
        "決算整理仕訳",
        "借方勘定科目",
        "借方科目コード",
        "借方補助科目",
        "借方取引先",
        "借方取引先コード",
        "借方部門",
        "借方品目",
        "借方メモタグ",
        "借方セグメント1",
        "借方セグメント2",
        "借方セグメント3",
        "借方金額",
        "借方税区分",
        "借方税額",
        "貸方勘定科目",
        "貸方科目コード",
        "貸方補助科目",
        "貸方取引先",
        "貸方取引先コード",
        "貸方部門",
        "貸方品目",
        "貸方メモタグ",
        "貸方セグメント1",
        "貸方セグメント2",
        "貸方セグメント3",
        "貸方金額",
        "貸方税区分",
        "貸方税額",
        "摘要",
    )
    expected_row = (
        "[明細行]",
        "2026/07/13",
        "1",
        "",
        "会議費",
        "",
        "",
        "サンプル商店",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "1200",
        "課対仕入10%",
        "109",
        "未払金",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "1200",
        "対象外",
        "",
        "サンプル商店 [Clerk-san:00000000-0000-0000-0000-000000000001]",
    )
    expected = (",".join(expected_header) + "\r\n" + ",".join(expected_row) + "\r\n").encode()

    assert render_freee([record()]) == expected


def test_yayoi_shift_jis_csv_matches_the_pinned_golden_bytes_without_header() -> None:
    expected_row = (
        "2000",
        "",
        "",
        "2026/07/13",
        "会議費",
        "",
        "",
        "課対仕入10%",
        "1200",
        "109",
        "未払金",
        "",
        "",
        "対象外",
        "1200",
        "",
        "サンプル商店 [Clerk-san:00000000-000",
        "",
        "",
        "0",
        "",
        "",
        "0",
        "0",
        "no",
        "サンプル商店",
        "",
    )
    expected = (",".join(expected_row) + "\r\n").encode("shift_jis")

    content = render_yayoi([record()])
    assert content == expected
    decoded = content.decode("shift_jis")
    assert "[表題行]" not in decoded
    assert len(next(csv.reader(io.StringIO(decoded)))) == 27


def test_export_rejects_an_ambiguous_multi_rate_tax_record() -> None:
    with pytest.raises(AccountingExportError, match="both 8% and 10%"):
        render_freee([replace(record(), tax_8_amount=Decimal("8"))])


def test_export_escapes_spreadsheet_formula_cells() -> None:
    content = render_freee([replace(record(), counterparty="=SUM(1,1)")]).decode("utf-8")
    row = list(csv.reader(io.StringIO(content)))[1]

    assert row[7] == "'=SUM(1,1)"
    assert row[-1].startswith("'=SUM(1,1)")


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def payload(counterparty: str) -> dict:
    return {
        "transaction_date": {"value": "2026-07-13", "confidence": 0.99},
        "total_amount": {"value": 1200, "confidence": 0.99},
        "counterparty": {"value": counterparty, "confidence": 0.99},
        "currency": {"value": "JPY", "confidence": 0.99},
        "expense_category": {"value": "会議費", "confidence": 0.99},
        "tax_10_amount": {"value": 109, "confidence": 0.99},
    }


@pytest.mark.asyncio
async def test_export_reads_verified_records_and_excludes_pending_extractions(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        verified_document_id = await documents.create_with_raw(
            filename="verified.png",
            content_path="/tmp/verified.png",
            sha256="a" * 64,
            mime="image/png",
        )
        verified_extraction_id = await ExtractionRepo(session).add(
            verified_document_id,
            payload=payload("Verified Merchant"),
            field_confidences={},
            model_name="test",
            prompt_version="test",
            actor="worker",
        )
        await VerifiedRepo(session).promote(
            verified_extraction_id,
            1,
            corrections={},
            reviewer="reviewer",
        )

        pending_document_id = await documents.create_with_raw(
            filename="pending.png",
            content_path="/tmp/pending.png",
            sha256="b" * 64,
            mime="image/png",
        )
        await ExtractionRepo(session).add(
            pending_document_id,
            payload=payload("Unverified Merchant"),
            field_confidences={},
            model_name="test",
            prompt_version="test",
            actor="worker",
        )

        content = await export_freee(
            session,
            date_from=dt.date(2026, 7, 1),
            date_to=dt.date(2026, 7, 31),
        )

    decoded = content.decode("utf-8")
    assert "Verified Merchant" in decoded
    assert "Unverified Merchant" not in decoded
