from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db.models import Base, SpreadsheetRow
from clerksan.db.repositories import DocumentRepo
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, ExtractedTable, NormalizedDocument
from clerksan.ingest.staging import (
    InferredCellType,
    SpreadsheetAggregateError,
    StagedSourceVersionSupersededError,
    aggregate_staged_numeric_column,
    build_staged_structure,
    stage_spreadsheet_rows,
    stage_tabular_rows,
)


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'staging.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _workbook() -> NormalizedDocument:
    return NormalizedDocument(
        markdown_body="Workbook expenses.xlsx; sheet July. Columns: date, amount, vendor.",
        metadata=DocMetadata(
            filename="expenses.xlsx",
            detected_type=FileType.XLSX,
            sha256="a" * 64,
            extra={"sheet_descriptions": ["Workbook expenses.xlsx; sheet July."]},
        ),
        tables=[
            ExtractedTable(
                header=["date", "amount", "vendor"],
                rows=[["2026-07-01", "1200", "Needle Shop"], ["2026-07-02", "500.50", ""]],
                source_location="sheet:July:table:1",
            ),
            ExtractedTable(
                header=["amount", "units"],
                rows=[["250", "2"], ["not-a-number", "3"]],
                source_location="sheet:July:table:2",
            ),
        ],
        embeddable=False,
    )


@pytest.mark.asyncio
async def test_staging_preserves_typed_rows_and_replaces_only_derived_data(session_factory) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="expenses.xlsx",
            content_path="/tmp/expenses.xlsx",
            sha256="b" * 64,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        assert (
            await stage_spreadsheet_rows(session, document_id, _workbook(), source_version=1) == 4
        )
        assert (
            await stage_spreadsheet_rows(session, document_id, _workbook(), source_version=1) == 4
        )
        rows = (
            await session.scalars(
                select(SpreadsheetRow)
                .where(SpreadsheetRow.document_id == document_id)
                .order_by(SpreadsheetRow.source_location, SpreadsheetRow.row_index)
            )
        ).all()

    assert len(rows) == 4
    assert rows[0].values == {"date": "2026-07-01", "amount": 1200, "vendor": "Needle Shop"}
    assert rows[0].value_types == {"date": "date", "amount": "integer", "vendor": "string"}
    assert rows[1].values["amount"] == 500.5
    assert rows[1].values["vendor"] is None


@pytest.mark.asyncio
async def test_staged_spreadsheet_rows_support_parameterized_sql_aggregates(
    session_factory,
) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="expenses.xlsx",
            content_path="/tmp/expenses.xlsx",
            sha256="c" * 64,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        await stage_spreadsheet_rows(session, document_id, _workbook(), source_version=1)

        combined = await aggregate_staged_numeric_column(session, document_id, column="amount")
        first_table = await aggregate_staged_numeric_column(
            session,
            document_id,
            column="amount",
            source_location="sheet:July:table:1",
        )
        with pytest.raises(SpreadsheetAggregateError, match="no numeric staged values"):
            await aggregate_staged_numeric_column(session, document_id, column="vendor")

    assert combined.total == 1950.5
    assert combined.average == Decimal("650.166667")
    assert combined.row_count == 3
    assert first_table.total == 1700.5
    assert first_table.average == 850.25
    assert first_table.row_count == 2


@pytest.mark.asyncio
async def test_source_replacement_hides_and_rejects_stale_spreadsheet_derivatives(
    session_factory,
) -> None:
    async with session_factory() as session:
        documents = DocumentRepo(session)
        document_id = await documents.create_with_raw(
            filename="expenses.xlsx",
            content_path="/tmp/expenses-v1.xlsx",
            sha256="d" * 64,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        await stage_spreadsheet_rows(session, document_id, _workbook(), source_version=1)
        replacement = await documents.append_raw_source(
            document_id,
            filename="expenses-v2.xlsx",
            content_path="/tmp/expenses-v2.xlsx",
            sha256="e" * 64,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            actor="reviewer",
        )

        with pytest.raises(SpreadsheetAggregateError, match="no numeric staged values"):
            await aggregate_staged_numeric_column(session, document_id, column="amount")
        with pytest.raises(StagedSourceVersionSupersededError, match="source version was replaced"):
            await stage_spreadsheet_rows(session, document_id, _workbook(), source_version=1)

        session.add(
            SpreadsheetRow(
                document_id=document_id,
                source_version=1,
                source_location="stale-worker",
                row_index=1,
                values={"amount": 9999},
                value_types={"amount": "integer"},
            )
        )
        await session.flush()
        with pytest.raises(SpreadsheetAggregateError, match="no numeric staged values"):
            await aggregate_staged_numeric_column(session, document_id, column="amount")

        await stage_spreadsheet_rows(
            session,
            document_id,
            _workbook(),
            source_version=replacement.version,
        )
        aggregate = await aggregate_staged_numeric_column(session, document_id, column="amount")

    assert aggregate.total == 1950.5
    assert aggregate.row_count == 3


@pytest.mark.asyncio
async def test_generic_tabular_staging_keeps_formula_like_cells_literal(session_factory) -> None:
    async with session_factory() as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="transactions.csv",
            content_path="/tmp/transactions.csv",
            sha256="f" * 64,
            mime="text/csv",
        )
        table = ExtractedTable(
            header=["amount", "memo"],
            rows=[["=SUM(A1:A2)", "@user"], ["+100", "-note"]],
            source_location="delimited:table:1",
        )
        assert await stage_tabular_rows(document_id, None, 1, [table], session) == 2
        rows = (
            await session.scalars(
                select(SpreadsheetRow)
                .where(SpreadsheetRow.document_id == document_id)
                .order_by(SpreadsheetRow.row_index)
            )
        ).all()

    assert rows[0].values == {"amount": "=SUM(A1:A2)", "memo": "@user"}
    assert rows[0].value_types == {"amount": "string", "memo": "string"}


def test_structural_staging_has_stable_schema_and_distinct_row_locators() -> None:
    source_file_id = uuid4()
    first = build_staged_structure(_workbook(), source_file_id=source_file_id, source_version=1)
    second = build_staged_structure(_workbook(), source_file_id=source_file_id, source_version=1)

    assert first == second
    assert first.row_count == 4
    assert first.structure_fingerprint == second.structure_fingerprint
    assert first.tables[0].descriptor.ordered_headers == ("date", "amount", "vendor")
    assert first.tables[0].descriptor.inferred_types == (
        InferredCellType.DATE,
        InferredCellType.MIXED,
        InferredCellType.STRING,
    )
    assert first.tables[0].rows[0].row_locator != first.tables[0].rows[1].row_locator


def test_schema_identity_excludes_sample_values_but_row_identity_does_not() -> None:
    source_file_id = uuid4()
    original = _workbook()
    changed = original.model_copy(deep=True)
    changed.tables[0].rows[0][2] = "Different literal"

    first = build_staged_structure(original, source_file_id=source_file_id, source_version=1)
    second = build_staged_structure(changed, source_file_id=source_file_id, source_version=1)

    assert (
        first.tables[0].descriptor.schema_fingerprint
        == second.tables[0].descriptor.schema_fingerprint
    )
    assert first.tables[0].rows[0].row_fingerprint != second.tables[0].rows[0].row_fingerprint


def test_duplicate_and_blank_headers_are_canonicalized_without_merging_columns() -> None:
    document = NormalizedDocument(
        markdown_body="table",
        metadata=DocMetadata(
            filename="table.csv",
            detected_type=FileType.CSV,
            sha256="f" * 64,
        ),
        tables=[ExtractedTable(header=["name", "name", ""], rows=[["a", "b", "c"]])],
        embeddable=False,
    )
    staged = build_staged_structure(document, source_file_id=uuid4(), source_version=1)
    assert staged.tables[0].descriptor.ordered_headers == (
        "name",
        "name__2",
        "column_3",
    )
