from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest

from clerksan.bills.analysis import BillPoint, compare_points, flag_anomalies_for_points

ISSUER_ID = UUID("00000000-0000-0000-0000-000000000001")


def point(
    year: int,
    month: int,
    amount: str,
    consumption: str | None = None,
    unit: str | None = None,
) -> BillPoint:
    return BillPoint(
        issuer_id=ISSUER_ID,
        billing_period=dt.date(year, month, 1),
        amount=Decimal(amount),
        consumption_value=Decimal(consumption) if consumption is not None else None,
        consumption_unit=unit,
    )


def test_period_comparisons_keep_missing_months_explicit() -> None:
    comparisons = {
        item.billing_period: item
        for item in compare_points(
            [
                point(2024, 12, "100", "10", "kWh"),
                point(2025, 1, "120", "12", "kWh"),
                point(2026, 1, "150", "15", "kWh"),
            ]
        )
    }

    january_2025 = comparisons[dt.date(2025, 1, 1)]
    assert january_2025.month_over_month.missing_reference is False
    assert january_2025.month_over_month.amount_delta == Decimal("20")
    assert january_2025.month_over_month.consumption_delta == Decimal("2")
    assert january_2025.month_over_month.unit_price_delta == Decimal("0")
    assert january_2025.year_over_year.missing_reference is True
    assert january_2025.year_over_year.amount_delta is None

    january_2026 = comparisons[dt.date(2026, 1, 1)]
    assert january_2026.month_over_month.missing_reference is True
    assert january_2026.month_over_month.reference_period == dt.date(2025, 12, 1)
    assert january_2026.year_over_year.missing_reference is False
    assert january_2026.year_over_year.amount_delta == Decimal("30")
    assert january_2026.year_over_year.consumption_delta == Decimal("3")


def test_comparisons_reject_duplicate_issuer_periods() -> None:
    with pytest.raises(ValueError, match="duplicate billing periods"):
        compare_points([point(2026, 1, "100"), point(2026, 1, "110")])


def test_anomaly_requires_six_prior_points_and_flags_consumption_once() -> None:
    history = [point(2026, month, "100", "10", "kWh") for month in range(1, 7)]
    assert flag_anomalies_for_points(history) == []

    flags = flag_anomalies_for_points(history + [point(2026, 7, "200", "20", "kWh")])
    assert len(flags) == 1
    assert flags[0].billing_period == dt.date(2026, 7, 1)
    assert flags[0].metric == "consumption"
    assert flags[0].mad == Decimal("0")
    assert "zero variation" in flags[0].explanation


def test_anomaly_distinguishes_unit_price_from_consumption() -> None:
    history = [point(2026, month, "100", "10", "kWh") for month in range(1, 7)]
    flags = flag_anomalies_for_points(history + [point(2026, 7, "200", "10", "kWh")])

    assert len(flags) == 1
    assert flags[0].metric == "unit_price"
    assert flags[0].value == Decimal("20")
