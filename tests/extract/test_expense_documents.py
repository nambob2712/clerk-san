from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from clerksan.db.models import DocumentClass, ExpenseKind
from clerksan.extract.classifier import classify_by_keywords
from clerksan.extract.extractor import extract, response_schema
from clerksan.extract.schemas import (
    BillExtraction,
    ReceiptExtraction,
    RecurringBillExtraction,
    schema_for_document_class,
)
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument


def _document(text: str) -> NormalizedDocument:
    return NormalizedDocument(
        markdown_body=text,
        metadata=DocMetadata(filename="expense.pdf", detected_type=FileType.PDF, sha256="a" * 64),
    )


def test_one_off_vietnamese_expense_documents_route_to_bill() -> None:
    examples = (
        "Thông báo nộp thuế thu nhập",
        "Phí bảo hiểm xe năm 2026",
        "Hóa đơn internet cần thanh toán",
    )

    assert [classify_by_keywords(_document(text)).label for text in examples] == [
        DocumentClass.BILL,
        DocumentClass.BILL,
        DocumentClass.BILL,
    ]


def test_strong_periodic_usage_evidence_remains_recurring_bill() -> None:
    result = classify_by_keywords(_document("電気料金 ご使用量 2026年7月分"))

    assert result.label is DocumentClass.RECURRING_BILL


def test_bill_schema_does_not_require_a_billing_period() -> None:
    schema = schema_for_document_class(DocumentClass.BILL)
    extraction = schema.model_validate(
        {
            "transaction_date": {"value": "2026-07-15", "confidence": 0.9},
            "total_amount": {"value": 250000, "confidence": 0.9},
            "counterparty": {"value": "City Tax Office", "confidence": 0.9},
            "expense_kind": {"value": "tax", "confidence": 0.9},
            "issuer_name": {"value": "City Tax Office", "confidence": 0.9},
            "due_date": {"value": "2026-08-31", "confidence": 0.9},
        }
    )

    assert schema is BillExtraction
    assert extraction.expense_kind.value == "tax"
    assert extraction.due_date.value.isoformat() == "2026-08-31"
    assert issubclass(RecurringBillExtraction, BillExtraction)


def test_expense_document_response_contract_requests_review_critical_fields() -> None:
    receipt = response_schema(ReceiptExtraction)
    bill = response_schema(BillExtraction)
    recurring = response_schema(RecurringBillExtraction)

    assert "expense_kind" in receipt["required"]
    assert {"expense_kind", "issuer_name", "due_date"}.issubset(bill["required"])
    assert {
        "expense_kind",
        "issuer_name",
        "issuer_kind",
        "billing_period",
        "due_date",
        "consumption_value",
        "consumption_unit",
    }.issubset(recurring["required"])


def test_expense_kind_only_accepts_values_that_verified_history_can_persist() -> None:
    extraction = BillExtraction.model_validate(
        {"expense_kind": {"value": "tax", "confidence": 0.9, "source_span": "tax"}}
    )

    assert extraction.expense_kind.value is ExpenseKind.TAX
    with pytest.raises(ValidationError):
        BillExtraction.model_validate(
            {"expense_kind": {"value": "utility", "confidence": 0.9, "source_span": "utility"}}
        )


@pytest.mark.asyncio
async def test_visible_tax_keyword_fills_a_missing_expense_kind_for_review() -> None:
    payload = {
        "transaction_date": {"value": "2026-07-10", "confidence": 0.9, "source_span": "2026-07-10"},
        "total_amount": {
            "value": 1250000,
            "confidence": 0.9,
            "source_span": "1,250,000 VND",
        },
        "counterparty": {
            "value": "Synthetic Tax Office",
            "confidence": 0.9,
            "source_span": "Tax Office",
        },
        "currency": {"value": "VND", "confidence": 0.9, "source_span": "VND"},
        "expense_kind": {"value": None, "confidence": 0.0, "source_span": None},
        "issuer_name": {
            "value": "Synthetic Tax Office",
            "confidence": 0.9,
            "source_span": "Tax Office",
        },
        "due_date": {"value": "2026-08-31", "confidence": 0.9, "source_span": "2026-08-31"},
    }

    class _Client:
        async def generate(self, *_: object, **__: object) -> str:
            return json.dumps(payload)

    class _Models:
        async def ensure_loaded(self, _: object) -> str:
            return "extract:7b"

    extraction = await extract(
        _document("TAX NOTICE\nSynthetic Tax Office\nDue date: 2026-08-31"),
        DocumentClass.BILL,
        _Client(),  # type: ignore[arg-type]
        _Models(),  # type: ignore[arg-type]
    )

    assert extraction.expense_kind.value is ExpenseKind.TAX
    assert extraction.expense_kind.confidence < 0.85
    assert extraction.model_dump(mode="json")["expense_kind"]["value"] == "tax"
