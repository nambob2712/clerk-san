"""Parameterized answers over reviewed records; user text is never SQL."""

from __future__ import annotations

import calendar
import datetime as dt
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select

from clerksan.db.active_records import restrict_to_active_verified
from clerksan.db.models import Document, DocumentClass, VerifiedRecord
from clerksan.db.repositories import VerifiedRepo
from clerksan.llm.client import ModelManager, OllamaClient
from clerksan.query.router import is_aggregation_question


class UnsafeSqlQuestionError(ValueError):
    """No safe, typed aggregate template matches a routed SQL question."""


class SqlIntent(BaseModel):
    """Typed slots for the fixed verified-record template catalog."""

    metric: Literal["sum", "average", "count", "top_counterparties", "unsupported"]
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    counterparty: str | None = None
    category: str | None = None
    doc_class: DocumentClass | None = None
    document_ids: list[UUID] = Field(default_factory=list)


class SqlAnswer(BaseModel):
    text: str
    rows: list[dict[str, Any]]
    template_id: str
    params: dict[str, Any]


_COUNT_CUES = re.compile(
    r"(?:\b(?:count|how\s+many|number\s+of)\b|何件|件数|"
    r"bao\s+nhiêu\s+(?:bản\s+ghi|hoá\s+đơn|hóa\s+đơn|mục|lần))",
    re.IGNORECASE,
)
_AVERAGE_CUES = re.compile(r"(?:\b(?:average|avg)\b|平均|trung\s+bình)", re.IGNORECASE)
_TOP_CUES = re.compile(
    r"(?:\b(?:top|most|highest|largest)\b|最も|一番|nhiều\s+nhất|cao\s+nhất)",
    re.IGNORECASE,
)
_COMPARISON_CUES = re.compile(
    r"(?:\b(?:compare|comparison|versus|vs\.? )\b|比較|比べ|so\s+sánh)", re.IGNORECASE
)
_MONTH_ISO = re.compile(r"\b(?P<year>20\d{2})[-/](?P<month>0?[1-9]|1[0-2])\b")
_MONTH_JA = re.compile(r"(?:(?P<year>20\d{2})年)?\s*(?P<month>1[0-2]|[1-9])月")
_MONTH_VI = re.compile(
    r"\btháng\s+(?P<month>1[0-2]|0?[1-9])(?:\s+năm\s+(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)
_MONTH_EN = re.compile(
    r"\b(?P<month>january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s*(?P<year>20\d{2})?\b",
    re.IGNORECASE,
)
_COUNTERPARTY = re.compile(
    r"(?:\b(?:counterparty|merchant|at)\b|取引先)\s*[:：]?\s*(?P<value>[^,，?？\n]+?)(?=\s+(?:in|for|from)\b|$)",
    re.IGNORECASE,
)
_CATEGORY = re.compile(
    r"(?:\bcategory\b|カテゴリ(?:ー)?|費目)\s*[:：]?\s*(?P<value>[^,，?？\n]+)",
    re.IGNORECASE,
)
_MONTH_NAMES = {month.lower(): index for index, month in enumerate(calendar.month_name) if month}
_CATEGORY_CUES = {
    "food": "食費",
    "meals": "食費",
    "食費": "食費",
    "meeting": "会議費",
    "会議費": "会議費",
    "travel": "旅費交通費",
    "交通費": "旅費交通費",
}


def parse_sql_intent(question: str, *, today: dt.date | None = None) -> SqlIntent:
    """Parse only values that can bind a fixed ORM query; reject free-form SQL ideas."""

    cleaned = question.strip()
    if not cleaned:
        raise UnsafeSqlQuestionError("question must not be empty")
    if not is_aggregation_question(cleaned):
        raise UnsafeSqlQuestionError("question does not require a verified-data aggregate")
    now = today or dt.date.today()
    if _COMPARISON_CUES.search(cleaned):
        metric: Literal["sum", "average", "count", "top_counterparties", "unsupported"] = (
            "unsupported"
        )
    elif _TOP_CUES.search(cleaned):
        metric = "top_counterparties"
    elif _COUNT_CUES.search(cleaned):
        metric = "count"
    elif _AVERAGE_CUES.search(cleaned):
        metric = "average"
    else:
        metric = "sum"
    date_from, date_to = _date_range(cleaned, now)
    return SqlIntent(
        metric=metric,
        date_from=date_from,
        date_to=date_to,
        counterparty=_counterparty(cleaned),
        category=_category(cleaned),
        doc_class=_document_class(cleaned),
    )


async def answer_sql(
    question: str,
    repo: VerifiedRepo,
    client: OllamaClient,
    models: ModelManager,
    *,
    document_ids: Sequence[UUID] | None = None,
    today: dt.date | None = None,
) -> SqlAnswer:
    """Compute a fixed aggregate exclusively over ``verified_records``.

    The local model parameters remain in this public seam for compatibility with
    future typed-slot assistance, but no model output is allowed to generate SQL.
    """

    del client, models
    intent = parse_sql_intent(question, today=today)
    intent.document_ids = list(dict.fromkeys(document_ids or []))
    if intent.metric == "unsupported":
        return SqlAnswer(
            text=(
                "I cannot answer that comparison safely with the available verified-data templates."
            ),
            rows=[],
            template_id="unsupported",
            params=_intent_params(intent),
        )
    if intent.metric == "top_counterparties":
        return await _top_counterparties(repo, intent)
    if intent.metric == "count":
        return await _count_records(repo, intent)
    if intent.metric == "average":
        return await _aggregate_amount(repo, intent, average=True)
    return await _aggregate_amount(repo, intent, average=False)


async def _aggregate_amount(repo: VerifiedRepo, intent: SqlIntent, *, average: bool) -> SqlAnswer:
    aggregate = (
        func.avg(VerifiedRecord.total_amount) if average else func.sum(VerifiedRecord.total_amount)
    )
    currency = func.coalesce(VerifiedRecord.currency, "unspecified").label("currency")
    statement = select(
        currency,
        func.coalesce(aggregate, 0).label("amount"),
        func.count(VerifiedRecord.id).label("record_count"),
    ).select_from(VerifiedRecord)
    statement = _apply_filters(statement, intent).group_by(currency).order_by(currency)
    rows = (await repo.session.execute(statement)).all()
    result_rows = [
        {
            "currency": str(row.currency),
            "amount": float(Decimal(str(row.amount or 0))),
            "record_count": int(row.record_count),
        }
        for row in rows
    ]
    label = "average" if average else "total"
    return SqlAnswer(
        text=_currency_aggregate_text(label, result_rows),
        rows=result_rows,
        template_id=f"verified_{label}",
        params=_intent_params(intent),
    )


async def _count_records(repo: VerifiedRepo, intent: SqlIntent) -> SqlAnswer:
    statement = select(func.count(VerifiedRecord.id).label("record_count")).select_from(
        VerifiedRecord
    )
    statement = _apply_filters(statement, intent)
    record_count = int((await repo.session.execute(statement)).scalar_one())
    return SqlAnswer(
        text=f"Verified record count: {record_count}.",
        rows=[{"record_count": record_count}],
        template_id="verified_count",
        params=_intent_params(intent),
    )


async def _top_counterparties(repo: VerifiedRepo, intent: SqlIntent) -> SqlAnswer:
    currency = func.coalesce(VerifiedRecord.currency, "unspecified").label("currency")
    statement = (
        select(
            VerifiedRecord.counterparty.label("counterparty"),
            currency,
            func.coalesce(func.sum(VerifiedRecord.total_amount), 0).label("amount"),
            func.count(VerifiedRecord.id).label("record_count"),
        )
        .select_from(VerifiedRecord)
        .group_by(VerifiedRecord.counterparty, currency)
        .order_by(
            currency,
            func.sum(VerifiedRecord.total_amount).desc(),
            VerifiedRecord.counterparty.asc(),
        )
    )
    statement = _apply_filters(statement, intent)
    rows = (await repo.session.execute(statement)).all()
    by_currency: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.currency)
        by_currency.setdefault(key, []).append(
            {
                "counterparty": str(row.counterparty),
                "currency": key,
                "amount": float(Decimal(str(row.amount or 0))),
                "record_count": int(row.record_count),
            }
        )
    result_rows = [row for entries in by_currency.values() for row in entries[:5]]
    if not result_rows:
        message = "No verified counterparties match the requested filters."
    elif len(by_currency) == 1:
        message = "Top verified counterparties: " + ", ".join(
            f"{row['counterparty']} ({_format_amount(row['currency'], row['amount'])})"
            for row in result_rows
        )
    else:
        message = "Top verified counterparties by currency: " + "; ".join(
            f"{currency}: "
            + ", ".join(
                f"{row['counterparty']} ({_format_amount(currency, row['amount'])})"
                for row in entries[:5]
            )
            for currency, entries in by_currency.items()
        )
        message += ". No currency conversion was performed."
    return SqlAnswer(
        text=message,
        rows=result_rows,
        template_id="verified_top_counterparties",
        params=_intent_params(intent),
    )


def _apply_filters(statement: Select[Any], intent: SqlIntent) -> Select[Any]:
    statement = restrict_to_active_verified(statement)
    conditions = []
    if intent.date_from is not None:
        conditions.append(VerifiedRecord.transaction_date >= intent.date_from)
    if intent.date_to is not None:
        conditions.append(VerifiedRecord.transaction_date <= intent.date_to)
    if intent.counterparty:
        conditions.append(VerifiedRecord.counterparty == intent.counterparty)
    if intent.category:
        conditions.append(VerifiedRecord.category == intent.category)
    if intent.doc_class is not None:
        statement = statement.join(Document, Document.id == VerifiedRecord.document_id)
        conditions.append(Document.document_class == intent.doc_class)
    if intent.document_ids:
        conditions.append(VerifiedRecord.document_id.in_(intent.document_ids))
    return statement.where(*conditions) if conditions else statement


def _currency_aggregate_text(label: str, rows: list[dict[str, Any]]) -> str:
    """Render grouped monetary aggregates without inventing an exchange rate."""

    if not rows:
        return f"No verified amounts match the requested {label} filters."
    if len(rows) == 1:
        row = rows[0]
        return (
            f"Verified {label}: {_format_amount(row['currency'], row['amount'])} "
            f"across {row['record_count']} record(s)."
        )
    plural_label = f"{label}s"
    groups = "; ".join(
        f"{_format_amount(row['currency'], row['amount'])} across {row['record_count']} record(s)"
        for row in rows
    )
    return f"Verified {plural_label} by currency: {groups}. No currency conversion was performed."


def _format_amount(currency: str, amount: float) -> str:
    return f"{currency} {amount:,.2f}"


def _date_range(question: str, today: dt.date) -> tuple[dt.date | None, dt.date | None]:
    iso = _MONTH_ISO.search(question)
    if iso is not None:
        return _month_range(int(iso.group("year")), int(iso.group("month")))
    japanese = _MONTH_JA.search(question)
    if japanese is not None:
        return _month_range(int(japanese.group("year") or today.year), int(japanese.group("month")))
    vietnamese = _MONTH_VI.search(question)
    if vietnamese is not None:
        return _month_range(
            int(vietnamese.group("year") or today.year), int(vietnamese.group("month"))
        )
    english = _MONTH_EN.search(question)
    if english is not None:
        return _month_range(
            int(english.group("year") or today.year), _MONTH_NAMES[english.group("month").lower()]
        )
    lowered = question.lower()
    if "last month" in lowered or "先月" in question or "tháng trước" in lowered:
        first_this_month = today.replace(day=1)
        previous_day = first_this_month - dt.timedelta(days=1)
        return _month_range(previous_day.year, previous_day.month)
    if "this month" in lowered or "今月" in question or "tháng này" in lowered:
        return _month_range(today.year, today.month)
    if "last year" in lowered or "昨年" in question or "năm ngoái" in lowered:
        return dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31)
    if "this year" in lowered or "今年" in question or "năm nay" in lowered:
        return dt.date(today.year, 1, 1), dt.date(today.year, 12, 31)
    return None, None


def _month_range(year: int, month: int) -> tuple[dt.date, dt.date]:
    return dt.date(year, month, 1), dt.date(year, month, calendar.monthrange(year, month)[1])


def _counterparty(question: str) -> str | None:
    match = _COUNTERPARTY.search(question)
    if match is None:
        return None
    value = match.group("value").strip(" .。")
    return value or None


def _category(question: str) -> str | None:
    explicit = _CATEGORY.search(question)
    if explicit is not None:
        value = explicit.group("value").strip(" .。")
        return value or None
    lowered = question.lower()
    for cue, category in _CATEGORY_CUES.items():
        if cue in lowered or cue in question:
            return category
    return None


def _document_class(question: str) -> DocumentClass | None:
    lowered = question.lower()
    if "invoice" in lowered or "請求書" in question:
        return DocumentClass.INVOICE
    if "receipt" in lowered or "領収書" in question or "レシート" in question:
        return DocumentClass.RECEIPT
    if "quote" in lowered or "見積書" in question:
        return DocumentClass.QUOTE
    if "bill" in lowered or "請求" in question:
        return DocumentClass.RECURRING_BILL
    return None


def _intent_params(intent: SqlIntent) -> dict[str, Any]:
    return {
        "date_from": intent.date_from.isoformat() if intent.date_from else None,
        "date_to": intent.date_to.isoformat() if intent.date_to else None,
        "counterparty": intent.counterparty,
        "category": intent.category,
        "doc_class": intent.doc_class.value if intent.doc_class else None,
        "document_ids": [str(document_id) for document_id in intent.document_ids],
    }
