"""Persist a normalized recurring bill only after its source record is verified."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.db.models import (
    ExtractedRecord,
    ExtractionStatus,
    Issuer,
    IssuerKind,
    RecurringBill,
    VerifiedRecord,
)
from clerksan.extract.recurring import bill_correction_fields


class BillValidationError(ValueError):
    """A normalized recurring-bill field is unsafe or inconsistent."""


class BillConflictError(RuntimeError):
    """A second source tried to claim an existing issuer-period natural key."""


async def record_verified_bill(
    session: AsyncSession,
    *,
    verified_record_id: UUID,
    issuer_name: str,
    issuer_kind: IssuerKind | str,
    billing_period: dt.date,
    due_date: dt.date | None,
    consumption_value: Decimal | float | None = None,
    consumption_unit: str | None = None,
    reviewer: str | None = None,
    review_corrections: dict[str, object] | None = None,
) -> RecurringBill:
    """Link reviewed bill metadata to its immutable verified source exactly once.

    ``verified_records.total_amount`` remains the canonical amount, so a later
    extraction cannot create a bill whose amount disagrees with the human-approved
    financial record. A conflicting issuer-period is surfaced for review instead of
    silently replacing its source linkage.
    """

    clean_name = issuer_name.strip()
    if not clean_name:
        raise BillValidationError("issuer_name must not be empty")
    if billing_period.day != 1:
        raise BillValidationError("billing_period must be the first day of its month")
    if consumption_value is None and consumption_unit is not None:
        raise BillValidationError("consumption_unit requires consumption_value")
    if consumption_value is not None:
        if isinstance(consumption_value, bool):
            raise BillValidationError("consumption_value must be a number")
        try:
            consumption_value = Decimal(str(consumption_value))
        except (InvalidOperation, ValueError) as error:
            raise BillValidationError("consumption_value must be a number") from error
        if consumption_value <= 0:
            raise BillValidationError("consumption_value must be greater than zero")
        if not consumption_unit or not consumption_unit.strip():
            raise BillValidationError("consumption_value requires consumption_unit")

    try:
        kind = IssuerKind(issuer_kind)
    except (TypeError, ValueError) as error:
        raise BillValidationError(f"unsupported issuer_kind: {issuer_kind}") from error

    verified = await session.get(VerifiedRecord, verified_record_id)
    if verified is None:
        raise BillValidationError("verified_record_id does not refer to a verified record")
    existing = await session.scalar(
        select(RecurringBill)
        .where(RecurringBill.verified_record_id == verified_record_id)
        .with_for_update()
    )
    if existing is not None:
        return existing

    corrections = dict(review_corrections or {})
    unexpected_corrections = set(corrections) - bill_correction_fields()
    if unexpected_corrections:
        raise BillValidationError(
            "unsupported recurring-bill correction fields: "
            + ", ".join(sorted(unexpected_corrections))
        )
    cleaned_reviewer = reviewer.strip() if reviewer is not None else None
    if cleaned_reviewer == "":
        raise BillValidationError("reviewer must not be empty")

    issuer = await session.scalar(select(Issuer).where(Issuer.name == clean_name).with_for_update())
    if issuer is None:
        issuer = Issuer(name=clean_name, kind=kind)
        session.add(issuer)
        await session.flush()
    elif issuer.kind != kind:
        raise BillValidationError(
            f"issuer {clean_name!r} is already classified as {issuer.kind.value!r}"
        )

    period_owner = await session.scalar(
        select(RecurringBill)
        .where(
            RecurringBill.issuer_id == issuer.id,
            RecurringBill.billing_period == billing_period,
            RecurringBill.superseded_at.is_(None),
        )
        .with_for_update()
    )
    if period_owner is not None:
        prior_status = await session.scalar(
            select(ExtractedRecord.status)
            .join(
                VerifiedRecord,
                and_(
                    VerifiedRecord.extracted_id == ExtractedRecord.id,
                    VerifiedRecord.document_id == ExtractedRecord.document_id,
                ),
            )
            .where(VerifiedRecord.id == period_owner.verified_record_id)
        )
        if (
            period_owner.document_id == verified.document_id
            and prior_status is ExtractionStatus.SUPERSEDED
        ):
            period_owner.superseded_at = dt.datetime.now(dt.UTC)
            await session.flush()
        else:
            raise BillConflictError(
                f"issuer {clean_name!r} already has a bill for {billing_period.isoformat()}"
            )

    bill = RecurringBill(
        issuer_id=issuer.id,
        document_id=verified.document_id,
        verified_record_id=verified.id,
        billing_period=billing_period,
        amount=verified.total_amount,
        due_date=due_date,
        consumption_value=consumption_value,
        consumption_unit=consumption_unit.strip() if consumption_unit else None,
        reviewer=cleaned_reviewer,
        review_corrections=corrections,
    )
    session.add(bill)
    await session.flush()
    return bill
