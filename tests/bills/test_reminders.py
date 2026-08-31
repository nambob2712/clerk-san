from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest

from clerksan.bills.analysis import BillView
from clerksan.bills.reminders import bucket_reminders


def bill(
    number: int,
    *,
    due_date: dt.date | None,
    payment_status: str = "unpaid",
) -> BillView:
    return BillView(
        id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        issuer_id=UUID("00000000-0000-0000-0000-000000000010"),
        issuer=f"Issuer {number}",
        issuer_kind="electric",
        billing_period=dt.date(2026, 7, 1),
        amount=Decimal("1000"),
        due_date=due_date,
        payment_status=payment_status,
    )


def test_reminder_boundaries_are_calculated_without_persisting_overdue_state() -> None:
    today = dt.date(2026, 7, 13)
    reminders = bucket_reminders(
        [
            bill(1, due_date=dt.date(2026, 7, 12)),
            bill(2, due_date=today),
            bill(3, due_date=dt.date(2026, 7, 20)),
            bill(4, due_date=dt.date(2026, 7, 21)),
            bill(5, due_date=today, payment_status="paid"),
            bill(6, due_date=None),
        ],
        as_of=today,
        days_ahead=7,
    )

    assert [item.id for item in reminders["overdue"]] == [bill(1, due_date=today).id]
    assert reminders["overdue"][0].days_left == -1
    assert reminders["overdue"][0].payment_status == "overdue"
    assert [item.id for item in reminders["upcoming"]] == [
        bill(2, due_date=today).id,
        bill(3, due_date=today).id,
    ]
    assert [item.days_left for item in reminders["upcoming"]] == [0, 7]
    assert all(item.payment_status == "unpaid" for item in reminders["upcoming"])


def test_reminder_rejects_a_negative_window() -> None:
    with pytest.raises(ValueError, match="days_ahead"):
        bucket_reminders([], as_of=dt.date(2026, 7, 13), days_ahead=-1)
