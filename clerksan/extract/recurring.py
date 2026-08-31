"""Recurring-bill normalization between reviewed extraction payloads and bill history."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from clerksan.db.models import DocumentClass, IssuerKind
from clerksan.extract.extractor import extract
from clerksan.extract.schemas import RecurringBillExtraction
from clerksan.ingest.normalized import NormalizedDocument
from clerksan.llm.client import ModelManager, OllamaClient

_BILL_CORRECTION_FIELDS = frozenset(
    {
        "issuer_name",
        "issuer_kind",
        "billing_period",
        "due_date",
        "consumption_value",
        "consumption_unit",
    }
)
_BILL_PROJECTION_CORRECTION_FIELDS = _BILL_CORRECTION_FIELDS - {"due_date"}
_GREGORIAN_PERIOD = re.compile(r"^(\d{4})\s*(?:年|[-./])\s*(\d{1,2})(?:月)?(?:分)?$")
_REIWA_PERIOD = re.compile(
    r"^(?:令和|R)\s*(\d{1,2})\s*(?:年|[-./])\s*(\d{1,2})(?:月)?(?:分)?$", re.I
)
_GREGORIAN_DATE = re.compile(
    r"^(\d{4})\s*(?:年|[-./])\s*(\d{1,2})\s*(?:月|[-./])\s*(\d{1,2})(?:日)?$"
)
_REIWA_DATE = re.compile(
    r"^(?:令和|R)\s*(\d{1,2})\s*(?:年|[-./])\s*(\d{1,2})(?:月|[-./])\s*(\d{1,2})(?:日)?$",
    re.I,
)


class RecurringBillNormalizationError(ValueError):
    """A reviewed recurring-bill payload cannot form a safe time-series row."""


@dataclass(frozen=True, slots=True)
class NormalizedRecurringBill:
    issuer_name: str
    issuer_kind: IssuerKind
    billing_period: dt.date
    due_date: dt.date | None
    consumption_value: Decimal | None
    consumption_unit: str | None


def bill_correction_fields() -> frozenset[str]:
    """Return correction keys stored only in the normalized recurring-bill projection."""

    return _BILL_CORRECTION_FIELDS


def bill_projection_correction_fields() -> frozenset[str]:
    """Return recurring-only fields that do not belong in a verified record."""

    return _BILL_PROJECTION_CORRECTION_FIELDS


def normalize_issuer(raw_name: str) -> tuple[str, IssuerKind]:
    """Return a stable issuer name and conservative utility/tax category."""

    if not isinstance(raw_name, str):
        raise RecurringBillNormalizationError("issuer_name must be text")
    clean = " ".join(unicodedata.normalize("NFKC", raw_name).split())
    if not clean:
        raise RecurringBillNormalizationError("issuer_name is required")
    folded = clean.casefold()
    aliases: tuple[tuple[tuple[str, ...], str, IssuerKind], ...] = (
        (("東京電力", "tepco"), "東京電力", IssuerKind.ELECTRIC),
        (("東京ガス", "tokyo gas"), "東京ガス", IssuerKind.GAS),
    )
    for terms, canonical, kind in aliases:
        if any(term in folded for term in terms):
            return canonical, kind
    if any(term in folded for term in ("電力", "electricity", "電気")):
        return clean, IssuerKind.ELECTRIC
    if any(term in folded for term in ("ガス", "gas")):
        return clean, IssuerKind.GAS
    if any(term in folded for term in ("水道", "water")):
        return clean, IssuerKind.WATER
    if any(term in folded for term in ("国民健康保険", "国保", "nhi")):
        return clean, IssuerKind.NHI
    if any(term in folded for term in ("税", "tax", "住民税", "固定資産")):
        return clean, IssuerKind.TAX
    return clean, IssuerKind.OTHER


def normalize_billing_period(value: str | dt.date) -> dt.date:
    """Normalize Gregorian or Reiwa month labels to their first Gregorian day."""

    if isinstance(value, dt.datetime):
        return dt.date(value.year, value.month, 1)
    if isinstance(value, dt.date):
        return dt.date(value.year, value.month, 1)
    if not isinstance(value, str):
        raise RecurringBillNormalizationError("billing_period is required")
    raw = unicodedata.normalize("NFKC", value).strip()
    try:
        parsed = dt.date.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is not None:
        return dt.date(parsed.year, parsed.month, 1)
    match = _GREGORIAN_PERIOD.fullmatch(raw)
    if match is not None:
        return _first_of_month(int(match.group(1)), int(match.group(2)))
    match = _REIWA_PERIOD.fullmatch(raw)
    if match is not None:
        return _first_of_month(_reiwa_year(int(match.group(1))), int(match.group(2)))
    raise RecurringBillNormalizationError(f"unsupported billing_period: {value!r}")


def normalize_due_date(value: str | dt.date | None) -> dt.date | None:
    """Normalize an optional Gregorian or Reiwa due-date label."""

    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str):
        raise RecurringBillNormalizationError("due_date must be a date or null")
    raw = unicodedata.normalize("NFKC", value).strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        pass
    match = _GREGORIAN_DATE.fullmatch(raw)
    if match is not None:
        return _date(int(match.group(1)), int(match.group(2)), int(match.group(3)), "due_date")
    match = _REIWA_DATE.fullmatch(raw)
    if match is not None:
        return _date(
            _reiwa_year(int(match.group(1))), int(match.group(2)), int(match.group(3)), "due_date"
        )
    raise RecurringBillNormalizationError(f"unsupported due_date: {value!r}")


def normalize_recurring_bill_payload(
    payload: dict[str, Any], corrections: dict[str, Any] | None = None
) -> NormalizedRecurringBill:
    """Validate reviewed bill fields while keeping the immutable extraction untouched."""

    projected = deepcopy(payload)
    for field, value in (corrections or {}).items():
        if field not in _BILL_CORRECTION_FIELDS:
            continue
        existing = projected.get(field)
        confidence = existing.get("confidence", 1.0) if isinstance(existing, dict) else 1.0
        projected[field] = {
            "value": value,
            "confidence": confidence,
            "source_span": "review correction",
        }
    due_date = projected.get("due_date")
    if isinstance(due_date, dict) and due_date.get("value") is not None:
        due_date = dict(due_date)
        normalized_due_date = normalize_due_date(due_date["value"])
        due_date["value"] = normalized_due_date.isoformat() if normalized_due_date else None
        projected["due_date"] = due_date
    try:
        extraction = RecurringBillExtraction.model_validate(projected)
    except ValueError as error:
        raise RecurringBillNormalizationError("recurring-bill payload is invalid") from error

    canonical_name, inferred_kind = normalize_issuer(
        _required_text(extraction.issuer_name.value, "issuer_name")
    )
    raw_kind = extraction.issuer_kind.value
    try:
        kind = IssuerKind(raw_kind) if raw_kind else inferred_kind
    except ValueError as error:
        raise RecurringBillNormalizationError(f"unsupported issuer_kind: {raw_kind!r}") from error
    if kind is IssuerKind.OTHER:
        kind = inferred_kind
    consumption_value, consumption_unit = _normalize_consumption(
        extraction.consumption_value.value,
        extraction.consumption_unit.value,
    )
    return NormalizedRecurringBill(
        issuer_name=canonical_name,
        issuer_kind=kind,
        billing_period=normalize_billing_period(
            _required_text(extraction.billing_period.value, "billing_period")
        ),
        due_date=normalize_due_date(extraction.due_date.value),
        consumption_value=consumption_value,
        consumption_unit=consumption_unit,
    )


async def extract_recurring_bill(
    doc: NormalizedDocument, client: OllamaClient, models: ModelManager
) -> RecurringBillExtraction:
    """Use the standard local structured extractor with the recurring-bill schema."""

    result = await extract(doc, DocumentClass.RECURRING_BILL, client, models)
    if not isinstance(result, RecurringBillExtraction):
        raise RecurringBillNormalizationError("recurring-bill extractor returned the wrong schema")
    return result


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecurringBillNormalizationError(f"{field} is required")
    return value.strip()


def _normalize_consumption(value: object, unit: object) -> tuple[Decimal | None, str | None]:
    if value is None:
        if unit not in (None, ""):
            raise RecurringBillNormalizationError("consumption_unit requires consumption_value")
        return None, None
    if isinstance(value, bool):
        raise RecurringBillNormalizationError("consumption_value must be numeric")
    try:
        normalized_value = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as error:
        raise RecurringBillNormalizationError("consumption_value must be numeric") from error
    if normalized_value <= 0:
        raise RecurringBillNormalizationError("consumption_value must be greater than zero")
    if not isinstance(unit, str) or not unit.strip():
        raise RecurringBillNormalizationError("consumption_value requires consumption_unit")
    normalized_unit = unicodedata.normalize("NFKC", unit).strip()
    aliases = {"kwh": "kWh", "kw/h": "kWh", "m3": "m³", "m^3": "m³"}
    return normalized_value, aliases.get(normalized_unit.casefold(), normalized_unit)


def _reiwa_year(year: int) -> int:
    if year < 1:
        raise RecurringBillNormalizationError("Reiwa year must be positive")
    return 2018 + year


def _first_of_month(year: int, month: int) -> dt.date:
    return _date(year, month, 1, "billing_period")


def _date(year: int, month: int, day: int, field: str) -> dt.date:
    try:
        return dt.date(year, month, day)
    except ValueError as error:
        raise RecurringBillNormalizationError(f"invalid {field}") from error
