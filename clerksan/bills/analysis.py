"""Explainable comparisons and anomaly signals for recurring-bill series."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.db.active_records import (
    active_extraction_authority,
    join_active_extraction_batch,
)
from clerksan.db.models import (
    ExtractedRecord,
    Issuer,
    RecurringBill,
    VerifiedRecord,
)


class BillPoint(BaseModel):
    """The analysis-safe representation of one reviewed billing period."""

    id: UUID | None = None
    issuer_id: UUID
    billing_period: dt.date
    amount: Decimal = Field(gt=0)
    consumption_value: Decimal | None = Field(default=None, gt=0)
    consumption_unit: str | None = None


class ReferenceDelta(BaseModel):
    """A comparison against a specific expected period, including an explicit gap."""

    reference: str
    reference_period: dt.date
    missing_reference: bool
    amount_delta: Decimal | None = None
    consumption_delta: Decimal | None = None
    unit_price_delta: Decimal | None = None


class PeriodDelta(BaseModel):
    """One bill with month-over-month and year-over-year comparison evidence."""

    billing_period: dt.date
    amount: Decimal
    consumption: Decimal | None = None
    consumption_unit: str | None = None
    unit_price: Decimal | None = None
    month_over_month: ReferenceDelta
    year_over_year: ReferenceDelta


class AnomalyFlag(BaseModel):
    """One advisory signal with its robust-history inputs, not a fraud decision."""

    billing_period: dt.date
    metric: str
    value: Decimal
    median: Decimal
    mad: Decimal
    robust_z: Decimal | None = None
    explanation: str


class BillView(BaseModel):
    """A bill presentation row shared by the HTTP and reminder layers."""

    id: UUID
    issuer_id: UUID
    issuer: str
    issuer_kind: str
    billing_period: dt.date
    amount: Decimal
    due_date: dt.date | None
    payment_status: str
    consumption_value: Decimal | None = None
    consumption_unit: str | None = None


async def list_bills(session: AsyncSession, *, issuer_id: UUID | None = None) -> list[BillView]:
    """Return reviewed recurring bills, newest first, without guessing any values."""
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
    )
    statement = statement.order_by(RecurringBill.billing_period.desc(), RecurringBill.id.desc())
    if issuer_id is not None:
        statement = statement.where(RecurringBill.issuer_id == issuer_id)
    rows = (await session.execute(statement)).all()
    return [_bill_view(bill, issuer) for bill, issuer in rows]


async def compare_periods(
    session: AsyncSession, issuer_id: UUID, *, months: int = 13
) -> list[PeriodDelta]:
    """Compare each selected period with its actual prior month and prior year rows."""
    points = await _issuer_points(session, issuer_id)
    return compare_points(points, months=months)


def compare_points(points: list[BillPoint], *, months: int | None = None) -> list[PeriodDelta]:
    """Pure comparison helper used for deterministic fixtures and database results."""
    if months is not None and months < 1:
        raise ValueError("months must be greater than zero")
    ordered = _ordered_unique_points(points)
    by_period = {point.billing_period: point for point in ordered}
    selected = ordered[-months:] if months is not None else ordered
    return [
        PeriodDelta(
            billing_period=point.billing_period,
            amount=point.amount,
            consumption=point.consumption_value,
            consumption_unit=point.consumption_unit,
            unit_price=_unit_price(point),
            month_over_month=_reference_delta(
                point,
                by_period.get(_shift_month(point.billing_period, -1)),
                reference="previous_month",
                expected_period=_shift_month(point.billing_period, -1),
            ),
            year_over_year=_reference_delta(
                point,
                by_period.get(_shift_month(point.billing_period, -12)),
                reference="same_month_last_year",
                expected_period=_shift_month(point.billing_period, -12),
            ),
        )
        for point in selected
    ]


async def flag_anomalies(
    session: AsyncSession,
    issuer_id: UUID,
    *,
    window: int = 12,
    robust_z_threshold: Decimal | float = Decimal("3.5"),
) -> list[AnomalyFlag]:
    """Flag only statistically unusual new periods against that issuer's prior history."""
    return flag_anomalies_for_points(
        await _issuer_points(session, issuer_id),
        window=window,
        robust_z_threshold=robust_z_threshold,
    )


def flag_anomalies_for_points(
    points: list[BillPoint],
    *,
    window: int = 12,
    robust_z_threshold: Decimal | float = Decimal("3.5"),
) -> list[AnomalyFlag]:
    """Use trailing median/MAD baselines and emit at most one signal per period."""
    if window < 6:
        raise ValueError("window must be at least six")
    threshold = Decimal(str(robust_z_threshold))
    if threshold <= 0:
        raise ValueError("robust_z_threshold must be greater than zero")

    ordered = _ordered_unique_points(points)
    flags: list[AnomalyFlag] = []
    for position, point in enumerate(ordered):
        history = ordered[max(0, position - window) : position]
        if len(history) < 6:
            continue
        flag = _point_anomaly(point, history, threshold)
        if flag is not None:
            flags.append(flag)
    return flags


async def _issuer_points(session: AsyncSession, issuer_id: UUID) -> list[BillPoint]:
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
        RecurringBill.issuer_id == issuer_id,
        RecurringBill.superseded_at.is_(None),
        active_extraction_authority(),
    )
    result = await session.scalars(
        statement.order_by(RecurringBill.billing_period.asc(), RecurringBill.id.asc())
    )
    return [_bill_point(bill) for bill in result]


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


def _bill_point(bill: RecurringBill) -> BillPoint:
    return BillPoint(
        id=bill.id,
        issuer_id=bill.issuer_id,
        billing_period=bill.billing_period,
        amount=bill.amount,
        consumption_value=bill.consumption_value,
        consumption_unit=bill.consumption_unit,
    )


def _ordered_unique_points(points: list[BillPoint]) -> list[BillPoint]:
    ordered = sorted(points, key=lambda point: point.billing_period)
    periods = [point.billing_period for point in ordered]
    if len(periods) != len(set(periods)):
        raise ValueError("a bill series cannot contain duplicate billing periods")
    for point in ordered:
        if point.billing_period.day != 1:
            raise ValueError("billing_period must be the first day of its month")
        if point.consumption_value is None and point.consumption_unit is not None:
            raise ValueError("consumption_unit requires consumption_value")
        if point.consumption_value is not None and not point.consumption_unit:
            raise ValueError("consumption_value requires consumption_unit")
    return ordered


def _reference_delta(
    point: BillPoint,
    reference_point: BillPoint | None,
    *,
    reference: str,
    expected_period: dt.date,
) -> ReferenceDelta:
    if reference_point is None:
        return ReferenceDelta(
            reference=reference,
            reference_period=expected_period,
            missing_reference=True,
        )

    amount_delta = point.amount - reference_point.amount
    comparable_consumption = _same_consumption_unit(point, reference_point)
    current_unit_price = _unit_price(point)
    reference_unit_price = _unit_price(reference_point)
    return ReferenceDelta(
        reference=reference,
        reference_period=expected_period,
        missing_reference=False,
        amount_delta=amount_delta,
        consumption_delta=(point.consumption_value - reference_point.consumption_value)
        if comparable_consumption
        else None,
        unit_price_delta=(current_unit_price - reference_unit_price)
        if comparable_consumption
        and current_unit_price is not None
        and reference_unit_price is not None
        else None,
    )


def _point_anomaly(
    point: BillPoint, history: list[BillPoint], threshold: Decimal
) -> AnomalyFlag | None:
    same_unit_history = [
        item
        for item in history
        if _same_consumption_unit(point, item) and item.consumption_value is not None
    ]
    if len(same_unit_history) >= 6 and point.consumption_value is not None:
        consumption_flag = _metric_anomaly(
            point.billing_period,
            "consumption",
            point.consumption_value,
            [
                item.consumption_value
                for item in same_unit_history
                if item.consumption_value is not None
            ],
            threshold,
            point.consumption_unit,
        )
        if consumption_flag is not None:
            return consumption_flag

        unit_history = [
            _unit_price(item) for item in same_unit_history if _unit_price(item) is not None
        ]
        unit_price = _unit_price(point)
        if len(unit_history) >= 6 and unit_price is not None:
            unit_flag = _metric_anomaly(
                point.billing_period,
                "unit_price",
                unit_price,
                unit_history,
                threshold,
                point.consumption_unit,
            )
            if unit_flag is not None:
                return unit_flag

    return _metric_anomaly(
        point.billing_period,
        "amount",
        point.amount,
        [item.amount for item in history],
        threshold,
        None,
    )


def _metric_anomaly(
    period: dt.date,
    metric: str,
    value: Decimal,
    history: list[Decimal],
    threshold: Decimal,
    unit: str | None,
) -> AnomalyFlag | None:
    median = _median(history)
    mad = _median([abs(item - median) for item in history])
    difference = abs(value - median)
    if mad == 0:
        if difference == 0:
            return None
        return AnomalyFlag(
            billing_period=period,
            metric=metric,
            value=value,
            median=median,
            mad=mad,
            explanation=_explanation(metric, unit, zero_variation=True),
        )

    robust_z = Decimal("0.6745") * difference / mad
    if robust_z <= threshold:
        return None
    return AnomalyFlag(
        billing_period=period,
        metric=metric,
        value=value,
        median=median,
        mad=mad,
        robust_z=robust_z,
        explanation=_explanation(metric, unit, zero_variation=False),
    )


def _explanation(metric: str, unit: str | None, *, zero_variation: bool) -> str:
    metric_label = {
        "amount": "bill amount",
        "consumption": f"consumption ({unit})" if unit else "consumption",
        "unit_price": f"unit price ({unit})" if unit else "unit price",
    }[metric]
    if zero_variation:
        return f"{metric_label} differs from a six-or-more-period baseline with zero variation"
    return f"{metric_label} exceeds the issuer's trailing median/MAD threshold"


def _same_consumption_unit(left: BillPoint, right: BillPoint) -> bool:
    return (
        left.consumption_value is not None
        and right.consumption_value is not None
        and bool(left.consumption_unit)
        and left.consumption_unit == right.consumption_unit
    )


def _unit_price(point: BillPoint) -> Decimal | None:
    if point.consumption_value is None or point.consumption_value == 0:
        return None
    return point.amount / point.consumption_value


def _median(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _shift_month(value: dt.date, months: int) -> dt.date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero_index = divmod(month_index, 12)
    return dt.date(year, month_zero_index + 1, 1)
