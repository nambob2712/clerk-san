from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from clerksan.config import Settings
from clerksan.db.models import IssuerKind
from clerksan.extract.recurring import (
    RecurringBillNormalizationError,
    extract_recurring_bill,
    normalize_billing_period,
    normalize_due_date,
    normalize_issuer,
    normalize_recurring_bill_payload,
)
from clerksan.ingest.adapters.pdf import PdfAdapter
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata
from clerksan.llm.ocr import OcrResult


def _payload() -> dict:
    return {
        "transaction_date": {"value": "2026-07-01", "confidence": 0.99},
        "total_amount": {"value": 1200, "confidence": 0.99},
        "counterparty": {"value": "東京電力", "confidence": 0.99},
        "currency": {"value": "JPY", "confidence": 0.99},
        "issuer_name": {"value": "東京電力エナジーパートナー", "confidence": 0.98},
        "issuer_kind": {"value": "electric", "confidence": 0.98},
        "billing_period": {"value": "令和8年6月分", "confidence": 0.98},
        "due_date": {"value": "2026年7月31日", "confidence": 0.98},
        "consumption_value": {"value": 42.5, "confidence": 0.95},
        "consumption_unit": {"value": "kwh", "confidence": 0.95},
    }


def test_recurring_bill_normalizes_aliases_reiwa_dates_and_consumption_units() -> None:
    bill = normalize_recurring_bill_payload(_payload())

    assert normalize_issuer("TEPCO")[0] == "東京電力"
    assert bill.issuer_name == "東京電力"
    assert bill.issuer_kind is IssuerKind.ELECTRIC
    assert bill.billing_period == dt.date(2026, 6, 1)
    assert bill.due_date == dt.date(2026, 7, 31)
    assert str(bill.consumption_value) == "42.5"
    assert bill.consumption_unit == "kWh"


def test_recurring_bill_review_corrections_project_without_mutating_the_extraction() -> None:
    payload = _payload()
    bill = normalize_recurring_bill_payload(
        payload,
        {"billing_period": "2026-07", "consumption_value": 51, "consumption_unit": "m3"},
    )

    assert payload["billing_period"]["value"] == "令和8年6月分"
    assert bill.billing_period == dt.date(2026, 7, 1)
    assert bill.consumption_value == 51
    assert bill.consumption_unit == "m³"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-07", dt.date(2026, 7, 1)), ("R8.6", dt.date(2026, 6, 1))],
)
def test_period_and_due_date_parsers_support_document_labels(value: str, expected: dt.date) -> None:
    assert normalize_billing_period(value) == expected
    assert normalize_due_date("令和8年7月31日") == dt.date(2026, 7, 31)


def test_recurring_bill_rejects_a_consumption_value_without_a_unit() -> None:
    payload = _payload()
    payload["consumption_unit"]["value"] = None

    with pytest.raises(RecurringBillNormalizationError, match="requires consumption_unit"):
        normalize_recurring_bill_payload(payload)


class _NoOcr:
    name = "no-ocr"

    async def ocr(self, _: bytes) -> OcrResult:
        raise AssertionError("the healthy text-layer bill fixture must not invoke OCR")


class _Models:
    async def ensure_loaded(self, _: object) -> str:
        return "qwen2.5:7b"


class _Client:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload, ensure_ascii=False)

    async def generate(self, *_: object, **__: object) -> str:
        return self.payload


def _recurring_bill_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    lines = (
        "Issuer: Tokyo Gas",
        "Billing period: R8.6",
        "Due date: 2026-07-31",
        "Consumption: 42.5 kWh",
        "Total: 1200 JPY",
        "Transaction date: 2026-07-01",
    )
    commands = [
        f"BT /F1 12 Tf 72 {720 - index * 24} Td ({line}) Tj ET" for index, line in enumerate(lines)
    ]
    content = DecodedStreamObject()
    content.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page.replace_contents(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _field(value: object, confidence: float, source_span: str) -> dict[str, object]:
    return {"value": value, "confidence": confidence, "source_span": source_span}


@pytest.mark.asyncio
async def test_recurring_bill_pdf_fixture_keeps_normalized_fields_and_source_spans() -> None:
    raw = _recurring_bill_pdf()
    normalized = await PdfAdapter(
        _NoOcr(),
        Settings(database_url="sqlite+aiosqlite:///:memory:", pdf_min_chars_per_page=5),
    ).adapt(
        raw,
        DocMetadata(
            filename="utility-bill.pdf",
            detected_type=FileType.PDF,
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
    )
    payload = {
        "transaction_date": _field("2026-07-01", 0.99, "Transaction date: 2026-07-01"),
        "total_amount": _field(1200, 0.99, "Total: 1200 JPY"),
        "counterparty": _field("Tokyo Gas", 0.99, "Issuer: Tokyo Gas"),
        "currency": _field("JPY", 0.99, "Total: 1200 JPY"),
        "expense_kind": _field("gas", 0.98, "Issuer: Tokyo Gas"),
        "issuer_name": _field("Tokyo Gas", 0.98, "Issuer: Tokyo Gas"),
        "issuer_kind": _field("gas", 0.98, "Issuer: Tokyo Gas"),
        "billing_period": _field("R8.6", 0.98, "Billing period: R8.6"),
        "due_date": _field("2026-07-31", 0.98, "Due date: 2026-07-31"),
        "consumption_value": _field(42.5, 0.95, "Consumption: 42.5 kWh"),
        "consumption_unit": _field("kWh", 0.95, "Consumption: 42.5 kWh"),
    }

    extraction = await extract_recurring_bill(
        normalized,
        _Client(payload),  # type: ignore[arg-type]
        _Models(),  # type: ignore[arg-type]
    )
    bill = normalize_recurring_bill_payload(extraction.model_dump(mode="json"))

    assert normalized.metadata.page_provenance == ["text_layer"]
    assert extraction.source_spans() == {
        name: value["source_span"] for name, value in payload.items()
    }
    assert all(span in normalized.markdown_body for span in extraction.source_spans().values())
    assert bill.issuer_name == "東京ガス"
    assert bill.issuer_kind is IssuerKind.GAS
    assert bill.billing_period == dt.date(2026, 6, 1)
    assert bill.due_date == dt.date(2026, 7, 31)
    assert bill.consumption_value == 42.5
    assert bill.consumption_unit == "kWh"
