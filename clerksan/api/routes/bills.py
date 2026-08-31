"""Recurring-bill history, analysis, reminders, and payment routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.api.deps import database_session, settings_from_request
from clerksan.api.schemas import BillOut
from clerksan.bills.analysis import compare_periods, flag_anomalies, list_bills
from clerksan.bills.reminders import BillNotFoundError, ReminderItem, list_reminders, mark_paid
from clerksan.config import Settings

router = APIRouter(prefix="/bills", tags=["bills"])


@router.get("", response_model=list[BillOut])
async def list_bill_history(
    issuer_id: UUID | None = None,
    session: AsyncSession = Depends(database_session),
) -> list[BillOut]:
    """Return reviewed recurring-bill rows, optionally narrowed to one issuer."""

    return [BillOut(**bill.model_dump()) for bill in await list_bills(session, issuer_id=issuer_id)]


@router.get("/reminders", response_model=dict[str, list[ReminderItem]])
async def reminders(
    days_ahead: int | None = Query(default=None, ge=0, le=365),
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> dict[str, list[ReminderItem]]:
    """List due-soon and overdue bills without changing their stored payment state."""

    return await list_reminders(
        session,
        days_ahead=days_ahead if days_ahead is not None else settings.reminder_days_ahead,
    )


@router.get("/{issuer_id}/analysis")
async def issuer_analysis(
    issuer_id: UUID,
    months: int = Query(default=13, ge=1, le=120),
    anomaly_window: int = Query(default=12, ge=6, le=120),
    session: AsyncSession = Depends(database_session),
) -> dict:
    """Return explainable MoM/YoY deltas and advisory anomaly flags for one issuer."""

    comparisons = await compare_periods(session, issuer_id, months=months)
    anomalies = await flag_anomalies(session, issuer_id, window=anomaly_window)
    return {
        "issuer_id": str(issuer_id),
        "comparisons": [comparison.model_dump(mode="json") for comparison in comparisons],
        "anomalies": [anomaly.model_dump(mode="json") for anomaly in anomalies],
    }


@router.post("/{bill_id}/mark-paid")
async def mark_bill_paid(
    bill_id: UUID,
    actor: str = Query(default="local-user", min_length=1, max_length=255),
    session: AsyncSession = Depends(database_session),
) -> dict[str, str]:
    """Mark a bill paid; PostgreSQL triggers write the field-level audit history."""

    try:
        bill = await mark_paid(session, bill_id, actor=actor)
    except BillNotFoundError as error:
        raise HTTPException(status_code=404, detail="Recurring bill not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"status": "paid", "bill_id": str(bill.id), "paid_at": bill.paid_at.isoformat()}
