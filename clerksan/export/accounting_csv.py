"""Deterministic, verified-only journal CSV exporters for freee and Yayoi."""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.db.repositories import VerifiedRepo


class AccountingExportError(ValueError):
    """A verified record cannot safely be represented in an accounting journal."""


FREEE_HEADER = (
    "[表題行]",
    "日付",
    "伝票番号",
    "決算整理仕訳",
    "借方勘定科目",
    "借方科目コード",
    "借方補助科目",
    "借方取引先",
    "借方取引先コード",
    "借方部門",
    "借方品目",
    "借方メモタグ",
    "借方セグメント1",
    "借方セグメント2",
    "借方セグメント3",
    "借方金額",
    "借方税区分",
    "借方税額",
    "貸方勘定科目",
    "貸方科目コード",
    "貸方補助科目",
    "貸方取引先",
    "貸方取引先コード",
    "貸方部門",
    "貸方品目",
    "貸方メモタグ",
    "貸方セグメント1",
    "貸方セグメント2",
    "貸方セグメント3",
    "貸方金額",
    "貸方税区分",
    "貸方税額",
    "摘要",
)

# The Yayoi journal import does not have a header. Its fields run A through AA.
YAYOI_COLUMN_COUNT = 27
PAYABLE_ACCOUNT = "未払金"

_ACCOUNT_BY_CATEGORY = {
    "会議費": "会議費",
    "接待交際費": "接待交際費",
    "消耗品費": "消耗品費",
    "旅費交通費": "旅費交通費",
    "水道光熱費": "水道光熱費",
    "通信費": "通信費",
    "福利厚生費": "福利厚生費",
    "新聞図書費": "新聞図書費",
    "地代家賃": "地代家賃",
    "租税公課": "租税公課",
    "雑費": "雑費",
    "Food": "会議費",
    "Transportation": "旅費交通費",
    "Entertainment": "接待交際費",
    "Utilities": "水道光熱費",
    "Healthcare": "福利厚生費",
    "Clothing": "消耗品費",
    "Electronics": "消耗品費",
    "Household": "消耗品費",
    "Office": "消耗品費",
}


@dataclass(frozen=True)
class ExportRecord:
    """The small verified-record projection needed for a journal row."""

    id: UUID
    document_id: UUID
    transaction_date: dt.date
    total_amount: Decimal
    counterparty: str
    category: str | None
    currency: str | None
    tax_8_amount: Decimal | None
    tax_10_amount: Decimal | None
    source_filename: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ExportRecord:
        """Validate a ``VerifiedRepo.range_query`` row before serializing it."""

        try:
            record_id = UUID(str(row["id"]))
            document_id = UUID(str(row["document_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise AccountingExportError(
                "verified export rows require UUID id and document_id"
            ) from error
        transaction_date = _as_date(row.get("transaction_date"), "transaction_date")
        total_amount = _as_decimal(row.get("total_amount"), "total_amount", required=True)
        if total_amount <= 0:
            raise AccountingExportError("total_amount must be greater than zero")
        counterparty = _clean_text(row.get("counterparty"), "counterparty")
        currency = row.get("currency")
        if currency is not None:
            currency = _clean_text(currency, "currency").upper()
            if currency != "JPY":
                raise AccountingExportError(
                    "accounting CSV export currently supports JPY records only"
                )

        tax_8_amount = _as_decimal(row.get("tax_8_amount"), "tax_8_amount", required=False)
        tax_10_amount = _as_decimal(row.get("tax_10_amount"), "tax_10_amount", required=False)
        for name, value in (("tax_8_amount", tax_8_amount), ("tax_10_amount", tax_10_amount)):
            if value is not None and value < 0:
                raise AccountingExportError(f"{name} must not be negative")
            if value is not None and value > total_amount:
                raise AccountingExportError(f"{name} cannot exceed total_amount")

        source_filename = row.get("source_filename")
        return cls(
            id=record_id,
            document_id=document_id,
            transaction_date=transaction_date,
            total_amount=total_amount,
            counterparty=counterparty,
            category=_optional_text(row.get("category")),
            currency=currency,
            tax_8_amount=tax_8_amount,
            tax_10_amount=tax_10_amount,
            source_filename=_optional_text(source_filename) or "",
        )


async def export_freee(session: AsyncSession, *, date_from: dt.date, date_to: dt.date) -> bytes:
    """Render a UTF-8 freee journal-import CSV from verified records only."""

    records = await _verified_records(session, date_from=date_from, date_to=date_to)
    return render_freee(records)


async def export_yayoi(session: AsyncSession, *, date_from: dt.date, date_to: dt.date) -> bytes:
    """Render a Shift_JIS Yayoi journal-import CSV from verified records only."""

    records = await _verified_records(session, date_from=date_from, date_to=date_to)
    return render_yayoi(records)


def render_freee(records: Iterable[ExportRecord]) -> bytes:
    """Serialize the documented freee journal-import header and detail rows."""

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(FREEE_HEADER)
    for voucher_number, record in enumerate(_ordered_records(records), start=1):
        debit_account = account_for_category(record.category)
        tax_category, tax_amount = _tax_treatment(record)
        amount = _format_yen(record.total_amount, "total_amount")
        writer.writerow(
            (
                "[明細行]",
                record.transaction_date.strftime("%Y/%m/%d"),
                str(voucher_number),
                "",
                debit_account,
                "",
                "",
                _safe_cell(record.counterparty),
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                amount,
                tax_category,
                tax_amount,
                PAYABLE_ACCOUNT,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                amount,
                "対象外",
                "",
                _summary(record),
            )
        )
    return output.getvalue().encode("utf-8")


def render_yayoi(records: Iterable[ExportRecord]) -> bytes:
    """Serialize headerless, Shift_JIS Yayoi 27-column journal rows."""

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    for record in _ordered_records(records):
        debit_account = account_for_category(record.category)
        tax_category, tax_amount = _tax_treatment(record)
        amount = _format_yen(record.total_amount, "total_amount")
        row = (
            "2000",  # A: one-line journal entry
            "",  # B: voucher number is intentionally not imported by Yayoi
            "",  # C: normal, non-adjusting entry
            record.transaction_date.strftime("%Y/%m/%d"),
            debit_account,
            "",
            "",
            tax_category,
            amount,
            tax_amount,
            PAYABLE_ACCOUNT,
            "",
            "",
            "対象外",
            amount,
            "",
            _yayoi_summary(_summary(record)),
            "",
            "",
            "0",
            "",
            "",
            "0",
            "0",
            "no",
            _yayoi_counterparty(record.counterparty),
            "",
        )
        if len(row) != YAYOI_COLUMN_COUNT:  # defensive contract against vendor-shape drift
            raise AssertionError("Yayoi journal row does not have 27 columns")
        writer.writerow(row)
    try:
        return output.getvalue().encode("shift_jis")
    except UnicodeEncodeError as error:
        raise AccountingExportError(
            "Yayoi Shift_JIS export cannot encode one or more verified fields"
        ) from error


def account_for_category(category: str | None) -> str:
    """Map the reviewed category to a standard account; unknowns remain explicit 雑費."""

    if category is None:
        return "雑費"
    return _ACCOUNT_BY_CATEGORY.get(category.strip(), "雑費")


async def _verified_records(
    session: AsyncSession, *, date_from: dt.date, date_to: dt.date
) -> list[ExportRecord]:
    if date_from > date_to:
        raise AccountingExportError("date_from must not be after date_to")
    rows = await VerifiedRepo(session).range_query(date_from=date_from, date_to=date_to)
    return [ExportRecord.from_row(row) for row in rows]


def _ordered_records(records: Iterable[ExportRecord]) -> list[ExportRecord]:
    return sorted(records, key=lambda record: (record.transaction_date, str(record.id)))


def _tax_treatment(record: ExportRecord) -> tuple[str, str]:
    present_rates = [
        (8, record.tax_8_amount),
        (10, record.tax_10_amount),
    ]
    non_zero_rates = [(rate, amount) for rate, amount in present_rates if amount and amount > 0]
    if len(non_zero_rates) > 1:
        raise AccountingExportError(
            f"verified record {record.id} has both 8% and 10% tax amounts; split it before export"
        )
    if not non_zero_rates:
        # The source contains no reviewed tax treatment, so do not invent one.
        return "対象外", ""
    rate, tax_amount = non_zero_rates[0]
    return f"課対仕入{rate}%", _format_yen(tax_amount, f"tax_{rate}_amount")


def _summary(record: ExportRecord) -> str:
    reference = str(record.id)
    return _safe_cell(f"{record.counterparty} [Clerk-san:{reference}]")


def _yayoi_summary(value: str) -> str:
    return value[:30]


def _yayoi_counterparty(value: str) -> str:
    return _safe_cell(value)[:15]


def _format_yen(value: Decimal, field_name: str) -> str:
    if value != value.to_integral_value():
        raise AccountingExportError(f"{field_name} must be a whole JPY amount")
    if value < 1:
        raise AccountingExportError(f"{field_name} must be at least one JPY")
    return str(int(value))


def _as_date(value: Any, field_name: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as error:
            raise AccountingExportError(f"{field_name} must be an ISO date") from error
    raise AccountingExportError(f"{field_name} is required")


def _as_decimal(value: Any, field_name: str, *, required: bool) -> Decimal | None:
    if value is None:
        if required:
            raise AccountingExportError(f"{field_name} is required")
        return None
    if isinstance(value, bool):
        raise AccountingExportError(f"{field_name} must be a decimal number")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise AccountingExportError(f"{field_name} must be a decimal number") from error


def _clean_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise AccountingExportError(f"{field_name} is required")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise AccountingExportError(f"{field_name} must not be empty")
    return cleaned


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _clean_text(value, "text")


def _safe_cell(value: str) -> str:
    """Prevent spreadsheet formula execution when a user opens the exported CSV."""

    cleaned = " ".join(value.split())
    if cleaned[:1] in {"=", "+", "-", "@"}:
        return f"'{cleaned}"
    return cleaned
