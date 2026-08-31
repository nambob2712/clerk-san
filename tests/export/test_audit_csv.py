from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db.models import AuditEntry, Base
from clerksan.export.audit_csv import export_audit_csv, render_audit_csv


def audit_entry(*, entry_id: int, at: dt.datetime, table: str = "verified_records") -> AuditEntry:
    return AuditEntry(
        id=entry_id,
        at=at,
        actor="reviewer",
        table_name=table,
        row_pk="record-1",
        action="UPDATE",
        field="total_amount",
        old_value="100",
        new_value="120",
    )


def test_audit_csv_has_a_stable_utf8_golden_shape_and_escapes_formula_values() -> None:
    entry = audit_entry(entry_id=1, at=dt.datetime(2026, 7, 13, 12, tzinfo=dt.UTC))
    entry.actor = "=untrusted"

    content = render_audit_csv([entry])
    expected = (
        b"audit_id,at,actor,table_name,row_pk,action,field,old_value,new_value\r\n"
        b"1,2026-07-13T12:00:00+00:00,'=untrusted,verified_records,record-1,UPDATE,"
        b"total_amount,100,120\r\n"
    )
    assert content == expected


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit-export.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_audit_export_filters_by_inclusive_date_range_and_table(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            (
                audit_entry(entry_id=1, at=dt.datetime(2026, 7, 12, 23, tzinfo=dt.UTC)),
                audit_entry(entry_id=2, at=dt.datetime(2026, 7, 13, 1, tzinfo=dt.UTC)),
                audit_entry(
                    entry_id=3,
                    at=dt.datetime(2026, 7, 13, 2, tzinfo=dt.UTC),
                    table="extracted_records",
                ),
            )
        )
        await session.flush()

        content = await export_audit_csv(
            session,
            date_from=dt.date(2026, 7, 13),
            date_to=dt.date(2026, 7, 13),
            table="verified_records",
        )

    rows = list(csv.reader(io.StringIO(content.decode())))
    assert rows[0][0] == "audit_id"
    assert [row[0] for row in rows[1:]] == ["2"]
