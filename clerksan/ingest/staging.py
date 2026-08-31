"""Deterministic SQL staging for spreadsheet rows without semantic row embedding."""

from __future__ import annotations

import datetime as dt
import enum
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Numeric, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.db.models import DocumentFile, FileKind, SpreadsheetRow
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import (
    ExtractedTable,
    NormalizedDocument,
    canonical_digest,
    canonical_locator,
)

_INTEGER = re.compile(r"[+-]?\d+")
_DECIMAL = re.compile(r"[+-]?(?:\d+\.\d+|\d+\.\d*|\.\d+)")
_AGGREGATE_QUANTUM = Decimal("0.000001")


class SpreadsheetAggregateError(ValueError):
    """A requested staged-sheet aggregate cannot be computed safely."""


class StagedSourceVersionSupersededError(RuntimeError):
    """A spreadsheet staging job belongs to an original that is no longer current."""


class InferredCellType(enum.StrEnum):
    BLANK = "blank"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    DATE = "date"
    STRING = "string"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class SchemaDescriptor:
    source_file_id: UUID
    source_version: int
    table_locator: str
    ordered_headers: tuple[str, ...]
    inferred_types: tuple[InferredCellType, ...]
    row_count: int
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class StagedRow:
    table_locator: str
    row_ordinal: int
    row_locator: str
    cells: tuple[str, ...]
    row_fingerprint: str
    blank: bool

    def as_record(self, headers: tuple[str, ...]) -> dict[str, str]:
        return {
            header: self.cells[index] if index < len(self.cells) else ""
            for index, header in enumerate(headers)
        }


@dataclass(frozen=True, slots=True)
class StagedTable:
    descriptor: SchemaDescriptor
    rows: tuple[StagedRow, ...]


@dataclass(frozen=True, slots=True)
class StagedStructure:
    tables: tuple[StagedTable, ...]
    normalized_sha256: str
    structure_fingerprint: str
    residual_markdown: str | None = None
    residual_locator: str | None = None
    residual_fingerprint: str | None = None

    @property
    def row_count(self) -> int:
        return sum(len(table.rows) for table in self.tables)

    @property
    def unit_count(self) -> int:
        return self.row_count + (1 if self.residual_markdown is not None else 0)


def build_staged_structure(
    document: NormalizedDocument,
    *,
    source_file_id: UUID,
    source_version: int,
) -> StagedStructure:
    """Build immutable descriptors and exact row identities without business inference."""

    if source_version < 1:
        raise ValueError("source_version must be greater than zero")
    tables: list[StagedTable] = []
    for table_ordinal, table in enumerate(document.tables, start=1):
        table_locator = canonical_locator("table", table.source_location or table_ordinal)
        headers = _canonical_headers(table.header)
        rows = tuple(
            StagedRow(
                table_locator=table_locator,
                row_ordinal=row_ordinal,
                row_locator=canonical_locator("row", table_locator, row_ordinal),
                cells=tuple(cells),
                row_fingerprint=canonical_digest(list(cells)),
                blank=not any(cell.strip() for cell in cells),
            )
            for row_ordinal, cells in enumerate(table.rows, start=1)
        )
        inferred_types = _infer_column_types(headers, rows)
        descriptor_payload = {
            "table_locator": table_locator,
            "ordered_headers": list(headers),
            "inferred_types": [value.value for value in inferred_types],
        }
        descriptor = SchemaDescriptor(
            source_file_id=source_file_id,
            source_version=source_version,
            table_locator=table_locator,
            ordered_headers=headers,
            inferred_types=inferred_types,
            row_count=len(rows),
            schema_fingerprint=canonical_digest(descriptor_payload),
        )
        tables.append(StagedTable(descriptor=descriptor, rows=rows))

    residual_value = document.metadata.extra.get("residual_markdown")
    residual_markdown = (
        residual_value.strip()
        if isinstance(residual_value, str) and residual_value.strip() and tables
        else None
    )
    residual_locator = (
        canonical_locator("document", "residual") if residual_markdown is not None else None
    )
    residual_fingerprint = (
        canonical_digest(residual_markdown) if residual_markdown is not None else None
    )
    normalized_payload = document.model_dump(mode="json")
    normalized_sha256 = canonical_digest(normalized_payload)
    structure_fingerprint = canonical_digest(
        {
            "tables": [
                {
                    "table_locator": table.descriptor.table_locator,
                    "schema_fingerprint": table.descriptor.schema_fingerprint,
                    "row_count": table.descriptor.row_count,
                }
                for table in tables
            ],
            "residual": (
                {
                    "locator": residual_locator,
                    "fingerprint": residual_fingerprint,
                }
                if residual_markdown is not None
                else None
            ),
        }
    )
    return StagedStructure(
        tables=tuple(tables),
        normalized_sha256=normalized_sha256,
        structure_fingerprint=structure_fingerprint,
        residual_markdown=residual_markdown,
        residual_locator=residual_locator,
        residual_fingerprint=residual_fingerprint,
    )


@dataclass(frozen=True, slots=True)
class SpreadsheetAggregate:
    """A parameterized SQL aggregate over one typed staged spreadsheet column."""

    document_id: UUID
    column: str
    source_location: str | None
    total: Decimal
    average: Decimal
    row_count: int


async def stage_spreadsheet_rows(
    session: AsyncSession,
    document_id: UUID,
    document: NormalizedDocument,
    *,
    source_version: int,
) -> int:
    """Replace one current-source XLSX projection with typed JSON records."""

    if document.metadata.detected_type is not FileType.XLSX:
        return 0
    await _require_current_source(session, document_id, source_version)
    await session.execute(
        delete(SpreadsheetRow).where(
            SpreadsheetRow.document_id == document_id,
            SpreadsheetRow.source_version == source_version,
        )
    )
    staged = 0
    for table_number, table in enumerate(document.tables, start=1):
        source_location = table.source_location or f"table:{table_number}"
        for row_index, cells in enumerate(table.rows, start=1):
            values: dict[str, Any] = {}
            value_types: dict[str, str] = {}
            for column_index, header in enumerate(table.header, start=1):
                column = header.strip() or f"column_{column_index}"
                raw = cells[column_index - 1] if column_index <= len(cells) else ""
                value, value_type = _typed_cell(raw)
                values[column] = value
                value_types[column] = value_type
            session.add(
                SpreadsheetRow(
                    document_id=document_id,
                    source_version=source_version,
                    source_location=source_location,
                    row_index=row_index,
                    values=values,
                    value_types=value_types,
                )
            )
            staged += 1
    await session.flush()
    return staged


async def stage_tabular_rows(
    document_id: UUID,
    source_file_id: UUID | None,
    source_version: int,
    tables: list[ExtractedTable],
    session: AsyncSession,
) -> int:
    """Stage any normalized table using deterministic locators and ordinals.

    ``source_file_id`` is accepted as part of the universal intake contract.  The
    current SQL projection is document/source-version scoped, so the source file
    identity is deliberately not copied into the legacy table until its schema is
    versioned.  Rows remain literal strings here; numeric typing is used only by
    the existing XLSX compatibility path.
    """

    del source_file_id
    await _require_current_source(session, document_id, source_version)
    await session.execute(
        delete(SpreadsheetRow).where(
            SpreadsheetRow.document_id == document_id,
            SpreadsheetRow.source_version == source_version,
        )
    )
    staged = 0
    for table_number, table in enumerate(tables, start=1):
        source_location = table.source_location or f"table:{table_number}"
        for row_index, cells in enumerate(table.rows, start=1):
            values: dict[str, Any] = {}
            value_types: dict[str, str] = {}
            for column_index, header in enumerate(table.header, start=1):
                column = header.strip() or f"column_{column_index}"
                raw = cells[column_index - 1] if column_index <= len(cells) else ""
                values[column] = raw
                value_types[column] = "string"
            session.add(
                SpreadsheetRow(
                    document_id=document_id,
                    source_version=source_version,
                    source_location=source_location,
                    row_index=row_index,
                    values=values,
                    value_types=value_types,
                )
            )
            staged += 1
    await session.flush()
    return staged


async def aggregate_staged_numeric_column(
    session: AsyncSession,
    document_id: UUID,
    *,
    column: str,
    source_location: str | None = None,
) -> SpreadsheetAggregate:
    """Compute a bound SQL aggregate without turning sheet rows into semantic text.

    Column names are JSON object keys, not interpolated SQL identifiers.  The typed
    staging metadata restricts the calculation to values parsed as numbers, so blank
    and textual cells do not silently influence a financial total.
    """

    cleaned_column = column.strip()
    if not cleaned_column:
        raise SpreadsheetAggregateError("column must not be blank")
    cleaned_source = source_location.strip() if source_location else None
    if source_location is not None and not cleaned_source:
        raise SpreadsheetAggregateError("source_location must not be blank")

    source_version = await _current_source_version(session, document_id)
    if source_version is None:
        raise SpreadsheetAggregateError("document has no preserved original source")

    value = cast(SpreadsheetRow.values[cleaned_column].as_string(), Numeric(24, 6))
    value_type = SpreadsheetRow.value_types[cleaned_column].as_string()
    conditions = [
        SpreadsheetRow.document_id == document_id,
        SpreadsheetRow.source_version == source_version,
        value_type.in_(("integer", "number")),
    ]
    if cleaned_source is not None:
        conditions.append(SpreadsheetRow.source_location == cleaned_source)
    statement = select(
        func.coalesce(func.sum(value), 0).label("total"),
        func.coalesce(func.avg(value), 0).label("average"),
        func.count(SpreadsheetRow.id).label("row_count"),
    ).where(*conditions)
    row = (await session.execute(statement)).one()
    row_count = int(row.row_count)
    if row_count == 0:
        scope = f" in {cleaned_source}" if cleaned_source else ""
        raise SpreadsheetAggregateError(
            f"no numeric staged values found for column {cleaned_column!r}{scope}"
        )
    return SpreadsheetAggregate(
        document_id=document_id,
        column=cleaned_column,
        source_location=cleaned_source,
        total=Decimal(str(row.total)).quantize(_AGGREGATE_QUANTUM),
        average=Decimal(str(row.average)).quantize(_AGGREGATE_QUANTUM),
        row_count=row_count,
    )


async def _require_current_source(
    session: AsyncSession, document_id: UUID, source_version: int
) -> None:
    if source_version < 1:
        raise ValueError("source_version must be greater than zero")
    if await _current_source_version(session, document_id) != source_version:
        raise StagedSourceVersionSupersededError(
            "the source version was replaced before spreadsheet rows could be staged"
        )


async def _current_source_version(session: AsyncSession, document_id: UUID) -> int | None:
    return await session.scalar(
        select(DocumentFile.version)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
        )
        .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
        .limit(1)
    )


def _typed_cell(raw: str) -> tuple[Any, str]:
    value = raw.strip()
    if not value:
        return None, "null"
    if value.casefold() in {"true", "false"}:
        return value.casefold() == "true", "boolean"
    if _INTEGER.fullmatch(value):
        return int(value), "integer"
    if _DECIMAL.fullmatch(value):
        return float(value), "number"
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return value, "string"
    return value, "date"


def _canonical_headers(headers: list[str]) -> tuple[str, ...]:
    used: dict[str, int] = {}
    canonical: list[str] = []
    for column_ordinal, raw_header in enumerate(headers, start=1):
        base = raw_header.strip() or f"column_{column_ordinal}"
        used[base] = used.get(base, 0) + 1
        canonical.append(base if used[base] == 1 else f"{base}__{used[base]}")
    return tuple(canonical)


def _infer_column_types(
    headers: tuple[str, ...], rows: tuple[StagedRow, ...]
) -> tuple[InferredCellType, ...]:
    result: list[InferredCellType] = []
    for column_index in range(len(headers)):
        observed: set[InferredCellType] = set()
        for row in rows:
            raw = row.cells[column_index] if column_index < len(row.cells) else ""
            _, value_type = _typed_cell(raw)
            observed.add(
                InferredCellType.BLANK if value_type == "null" else InferredCellType(value_type)
            )
        nonblank = observed - {InferredCellType.BLANK}
        if not nonblank:
            result.append(InferredCellType.BLANK)
        elif len(nonblank) == 1:
            result.append(next(iter(nonblank)))
        else:
            result.append(InferredCellType.MIXED)
    return tuple(result)
