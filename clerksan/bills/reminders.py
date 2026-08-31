"""Due-date reminders and auditable payment transitions for recurring bills."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.bills.analysis import BillView
from clerksan.db.active_records import (
    active_extraction_authority,
    join_active_extraction_batch,
)
from clerksan.db.audit import audit_actor
from clerksan.db.models import (
    ExtractedRecord,
    Issuer,
    PaymentStatus,
    RecurringBill,
    VerifiedRecord,
)


class ReminderBucket(StrEnum):
    """The only reminder states presented to the user."""

    UPCOMING = "upcoming"
    OVERDUE = "overdue"


class ReminderItem(BaseModel):
    """A due bill with a calculated, non-persisted reminder state."""

    id: UUID
    issuer_id: UUID
    issuer: str
    issuer_kind: str
    billing_period: dt.date
    amount: Decimal
    due_date: dt.date
    days_left: int
    payment_status: str


class BillNotFoundError(LookupError):
    """The requested recurring bill does not exist."""


def bucket_reminders(
    bills: list[BillView], *, as_of: dt.date, days_ahead: int
) -> dict[str, list[ReminderItem]]:
    """Bucket unpaid bills by actual calendar due date without mutating database state."""

    if days_ahead < 0:
        raise ValueError("days_ahead must not be negative")

    end_date = as_of + dt.timedelta(days=days_ahead)
    buckets: dict[str, list[ReminderItem]] = {
        ReminderBucket.UPCOMING.value: [],
        ReminderBucket.OVERDUE.value: [],
    }
    for bill in bills:
        if bill.payment_status == PaymentStatus.PAID.value or bill.due_date is None:
            continue

        days_left = (bill.due_date - as_of).days
        if bill.due_date < as_of:
            bucket = ReminderBucket.OVERDUE
            payment_status = PaymentStatus.OVERDUE.value
        elif bill.due_date <= end_date:
            bucket = ReminderBucket.UPCOMING
            payment_status = PaymentStatus.UNPAID.value
        else:
            continue

        buckets[bucket.value].append(
            ReminderItem(
                id=bill.id,
                issuer_id=bill.issuer_id,
                issuer=bill.issuer,
                issuer_kind=bill.issuer_kind,
                billing_period=bill.billing_period,
                amount=bill.amount,
                due_date=bill.due_date,
                days_left=days_left,
                payment_status=payment_status,
            )
        )

    for items in buckets.values():
        items.sort(key=lambda item: (item.due_date, item.issuer, str(item.id)))
    return buckets


async def list_reminders(
    session: AsyncSession,
    *,
    days_ahead: int,
    as_of: dt.date | None = None,
) -> dict[str, list[ReminderItem]]:
    """Return unpaid bills due soon or overdue; overdue is calculated, never auto-written."""

    current_date = as_of or dt.date.today()
    if days_ahead < 0:
        raise ValueError("days_ahead must not be negative")
    statement = (
        select(RecurringBill, Issuer)
        .join(Issuer)
        .join(VerifiedRecord, RecurringBill.verified_record_id == VerifiedRecord.id)
        .join(
            ExtractedRecord,
            and_(
                VerifiedRecord.extracted_id == ExtractedRecord.id,
                VerifiedRecord.document_id == ExtractedRecord.document_id,
            ),
        )
    )
    statement = join_active_extraction_batch(statement).where(
        RecurringBill.superseded_at.is_(None),
        active_extraction_authority(),
        RecurringBill.payment_status != PaymentStatus.PAID,
        RecurringBill.due_date.is_not(None),
        RecurringBill.due_date <= current_date + dt.timedelta(days=days_ahead),
    )
    rows = (
        await session.execute(
            statement.order_by(
                RecurringBill.due_date.asc(), Issuer.name.asc(), RecurringBill.id.asc()
            )
        )
    ).all()
    return bucket_reminders(
        [_bill_view(bill, issuer) for bill, issuer in rows],
        as_of=current_date,
        days_ahead=days_ahead,
    )


async def mark_paid(
    session: AsyncSession,
    bill_id: UUID,
    *,
    actor: str,
    paid_at: dt.datetime | None = None,
) -> RecurringBill:
    """Mark one bill paid under trigger-owned audit actor context.

    Repeating the request leaves a paid record unchanged, which makes clients safe to
    retry after a network failure. PostgreSQL migration 0005 records the changed fields.
    """

    completed_at = paid_at or dt.datetime.now(dt.UTC)
    if completed_at.tzinfo is None:
        raise ValueError("paid_at must include a timezone")

    cleaned_actor = actor.strip()
    if not cleaned_actor:
        raise ValueError("actor must not be empty")

    async with audit_actor(session, cleaned_actor):
        statement = (
            select(RecurringBill)
            .join(VerifiedRecord, RecurringBill.verified_record_id == VerifiedRecord.id)
            .join(
                ExtractedRecord,
                and_(
                    VerifiedRecord.extracted_id == ExtractedRecord.id,
                    VerifiedRecord.document_id == ExtractedRecord.document_id,
                ),
            )
        )
        statement = join_active_extraction_batch(statement).where(
            RecurringBill.id == bill_id,
            RecurringBill.superseded_at.is_(None),
            active_extraction_authority(),
        )
        bill = await session.scalar(statement.with_for_update(of=RecurringBill))
        if bill is None:
            raise BillNotFoundError(str(bill_id))
        if bill.payment_status == PaymentStatus.PAID:
            # Repair an old manually-written paid state once; normal retries do not write.
            if bill.paid_at is None:
                bill.paid_at = completed_at
                bill.reviewer = cleaned_actor
                await session.flush()
            return bill

        bill.payment_status = PaymentStatus.PAID
        bill.paid_at = completed_at
        bill.reviewer = cleaned_actor
        await session.flush()
        return bill


def _bill_view(bill: RecurringBill, issuer: Issuer) -> BillView:
    return BillView(
        id=bill.id,
        issuer_id=bill.issuer_id,
        issuer=issuer.name,
        issuer_kind=issuer.kind.value,
        billing_period=bill.billing_period,
        amount=bill.amount,
        due_date=bill.due_date,
        payment_status=bill.payment_status.value,
        consumption_value=bill.consumption_value,
        consumption_unit=bill.consumption_unit,
    )
