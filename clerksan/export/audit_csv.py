"""UTF-8 audit-history export for review, retention, and professional inspection."""

from __future__ import annotations

import csv
import datetime as dt
import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.db.models import AuditEntry

AUDIT_HEADER = (
    "audit_id",
    "at",
    "actor",
    "table_name",
    "row_pk",
    "action",
    "field",
    "old_value",
    "new_value",
)


class AuditExportError(ValueError):
    """An audit export request has an invalid time range or table filter."""


async def export_audit_csv(
    session: AsyncSession,
    *,
    date_from: dt.date,
    date_to: dt.date,
    table: str | None = None,
) -> bytes:
    """Export trigger-owned audit rows in chronological order over an inclusive date range."""

    if date_from > date_to:
        raise AuditExportError("date_from must not be after date_to")
    start = dt.datetime.combine(date_from, dt.time.min, tzinfo=dt.UTC)
    end = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    statement = (
        select(AuditEntry)
        .where(AuditEntry.at >= start, AuditEntry.at < end)
        .order_by(AuditEntry.at.asc(), AuditEntry.id.asc())
    )
    if table is not None:
        cleaned_table = table.strip()
        if not cleaned_table:
            raise AuditExportError("table must not be empty")
        statement = statement.where(AuditEntry.table_name == cleaned_table)
    entries = (await session.scalars(statement)).all()
    return render_audit_csv(entries)


def render_audit_csv(entries: list[AuditEntry]) -> bytes:
    """Serialize stable columns without making a direct write to ``audit_log``."""

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(AUDIT_HEADER)
    for entry in entries:
        writer.writerow(
            (
                str(entry.id),
                entry.at.isoformat(),
                _safe_cell(entry.actor),
                _safe_cell(entry.table_name),
                _safe_cell(entry.row_pk),
                _safe_cell(entry.action),
                _safe_cell(entry.field),
                _safe_cell(entry.old_value or ""),
                _safe_cell(entry.new_value or ""),
            )
        )
    return output.getvalue().encode("utf-8")


def _safe_cell(value: str) -> str:
    """Prevent a spreadsheet from evaluating untrusted audit data as a formula."""

    cleaned = " ".join(value.split())
    if cleaned[:1] in {"=", "+", "-", "@"}:
        return f"'{cleaned}"
    return cleaned
