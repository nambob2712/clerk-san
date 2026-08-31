from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db.models import Base
from clerksan.db.repositories import DocumentRepo, ExtractionRepo, VerifiedRepo
from clerksan.query.sql_answerer import answer_sql, parse_sql_intent


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'query.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _verified(
    session,
    *,
    digest: str,
    date: str,
    amount: float,
    category: str,
    currency: str = "JPY",
    counterparty: str = "Sample Shop",
):
    document_id = await DocumentRepo(session).create_with_raw(
        filename=f"{digest}.png",
        content_path=f"/tmp/{digest}.png",
        sha256=digest,
        mime="image/png",
    )
    extraction_id = await ExtractionRepo(session).add(
        document_id,
        payload={
            "transaction_date": {"value": date, "confidence": 0.99},
            "total_amount": {"value": amount, "confidence": 0.99},
            "counterparty": {"value": counterparty, "confidence": 0.99},
            "currency": {"value": currency, "confidence": 0.99},
            "expense_category": {"value": category, "confidence": 0.99},
        },
        field_confidences={},
        model_name="test",
        prompt_version="test",
        actor="worker",
    )
    await VerifiedRepo(session).promote(extraction_id, 1, corrections={}, reviewer="reviewer")
    return document_id


@pytest.mark.asyncio
async def test_sum_uses_verified_records_and_typed_month_category_filters(session_factory) -> None:
    async with session_factory() as session:
        await _verified(session, digest="a" * 64, date="2026-06-10", amount=1200, category="食費")
        await _verified(session, digest="b" * 64, date="2026-07-10", amount=900, category="食費")
        unverified_document = await DocumentRepo(session).create_with_raw(
            filename="unverified.png",
            content_path="/tmp/unverified.png",
            sha256="c" * 64,
            mime="image/png",
        )
        await ExtractionRepo(session).add(
            unverified_document,
            payload={
                "transaction_date": {"value": "2026-06-11", "confidence": 0.99},
                "total_amount": {"value": 9999, "confidence": 0.99},
                "counterparty": {"value": "Sample Shop", "confidence": 0.99},
            },
            field_confidences={},
            model_name="test",
            prompt_version="test",
            actor="worker",
        )
        result = await answer_sql(
            "What is the total food expense in 2026-06?",
            VerifiedRepo(session),
            cast(Any, object()),
            cast(Any, object()),
            today=dt.date(2026, 7, 13),
        )

    assert result.template_id == "verified_total"
    assert result.rows == [{"currency": "JPY", "amount": 1200.0, "record_count": 1}]


@pytest.mark.asyncio
async def test_sum_and_top_counterparties_do_not_mix_currencies(session_factory) -> None:
    async with session_factory() as session:
        await _verified(
            session,
            digest="1" * 64,
            date="2026-07-10",
            amount=1200,
            category="food",
            counterparty="Tokyo Shop",
        )
        await _verified(
            session,
            digest="2" * 64,
            date="2026-07-11",
            amount=250000,
            category="food",
            currency="VND",
            counterparty="Hanoi Shop",
        )
        repo = VerifiedRepo(session)
        dependencies = (cast(Any, object()), cast(Any, object()))
        total = await answer_sql("What is the total in July 2026?", repo, *dependencies)
        top = await answer_sql(
            "Which counterparties have the highest spending in July 2026?",
            repo,
            *dependencies,
        )

    assert total.rows == [
        {"currency": "JPY", "amount": 1200.0, "record_count": 1},
        {"currency": "VND", "amount": 250000.0, "record_count": 1},
    ]
    assert "No currency conversion was performed." in total.text
    assert top.rows == [
        {
            "counterparty": "Tokyo Shop",
            "currency": "JPY",
            "amount": 1200.0,
            "record_count": 1,
        },
        {
            "counterparty": "Hanoi Shop",
            "currency": "VND",
            "amount": 250000.0,
            "record_count": 1,
        },
    ]
    assert "No currency conversion was performed." in top.text


@pytest.mark.asyncio
async def test_sql_aggregates_exclude_superseded_verified_versions(session_factory) -> None:
    async with session_factory() as session:
        document_id = await _verified(
            session,
            digest="d" * 64,
            date="2026-06-10",
            amount=1200,
            category="食費",
        )
        replacement_id = await ExtractionRepo(session).add(
            document_id,
            payload={
                "transaction_date": {"value": "2026-06-10", "confidence": 0.99},
                "total_amount": {"value": 1800, "confidence": 0.99},
                "counterparty": {"value": "Sample Shop", "confidence": 0.99},
                "currency": {"value": "JPY", "confidence": 0.99},
                "expense_category": {"value": "食費", "confidence": 0.99},
            },
            field_confidences={},
            model_name="replacement",
            prompt_version="replacement",
            actor="worker",
        )
        await VerifiedRepo(session).promote(
            replacement_id, 1, corrections={}, reviewer="replacement-reviewer"
        )
        repo = VerifiedRepo(session)
        dependencies = (cast(Any, object()), cast(Any, object()))
        total = await answer_sql("What is the total food expense in 2026-06?", repo, *dependencies)
        count = await answer_sql("How many records are there?", repo, *dependencies)
        top = await answer_sql(
            "Which counterparties have the highest spending?", repo, *dependencies
        )

    assert total.rows == [{"currency": "JPY", "amount": 1800.0, "record_count": 1}]
    assert count.rows == [{"record_count": 1}]
    assert top.rows == [
        {
            "counterparty": "Sample Shop",
            "currency": "JPY",
            "amount": 1800.0,
            "record_count": 1,
        }
    ]


def test_japanese_month_slots_are_deterministic() -> None:
    intent = parse_sql_intent("6月の食費の合計は？", today=dt.date(2026, 7, 13))
    assert intent.category == "食費"
    assert intent.date_from == dt.date(2026, 6, 1)
    assert intent.date_to == dt.date(2026, 6, 30)


def test_vietnamese_month_slots_are_deterministic() -> None:
    intent = parse_sql_intent("Tháng 7 năm 2026 tôi đã chi bao nhiêu?", today=dt.date(2026, 8, 5))

    assert intent.metric == "sum"
    assert intent.date_from == dt.date(2026, 7, 1)
    assert intent.date_to == dt.date(2026, 7, 31)
