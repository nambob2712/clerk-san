"""Accounting CSV download routes for reviewed records only."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.api.deps import database_session
from clerksan.export.accounting_csv import AccountingExportError, export_freee, export_yayoi
from clerksan.export.audit_csv import AuditExportError, export_audit_csv

router = APIRouter(tags=["export"])


@router.get("/export/audit")
async def export_audit_history(
    date_from: dt.date,
    date_to: dt.date,
    table: str | None = None,
    session: AsyncSession = Depends(database_session),
) -> Response:
    """Download a UTF-8 audit-history CSV over a bounded inclusive date range."""

    try:
        content = await export_audit_csv(
            session,
            date_from=date_from,
            date_to=date_to,
            table=table,
        )
    except AuditExportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    filename = f"clerksan_audit_{date_from:%Y%m%d}-{date_to:%Y%m%d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/export")
async def export_csv(
    format: Literal["freee", "yayoi"],  # noqa: A002
    date_from: dt.date,
    date_to: dt.date,
    session: AsyncSession = Depends(database_session),
) -> Response:
    """Download a dated CSV attachment with the selected accounting-system encoding."""

    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    try:
        if format == "freee":
            content = await export_freee(session, date_from=date_from, date_to=date_to)
            charset = "utf-8"
        else:
            content = await export_yayoi(session, date_from=date_from, date_to=date_to)
            charset = "shift_jis"
    except AccountingExportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    filename = f"clerksan_{format}_{date_from:%Y%m%d}-{date_to:%Y%m%d}.csv"
    return Response(
        content=content,
        media_type=f"text/csv; charset={charset}",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
