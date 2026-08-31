from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from clerksan.db.models import DocumentClass
from clerksan.extract.schemas import (
    FieldValue,
    LineItemX,
    ReceiptExtraction,
    schema_for_document_class,
    validate_extraction_payload,
)


def test_extraction_round_trips_with_nested_line_item_confidences() -> None:
    extraction = ReceiptExtraction(
        transaction_date=FieldValue(value=dt.date(2026, 7, 13), confidence=0.98),
        total_amount=FieldValue(value=1234.0, confidence=0.99, source_span="合計 1,234円"),
        counterparty=FieldValue(value="テスト商店", confidence=0.9),
        line_items=[
            LineItemX(
                description=FieldValue(value="ノート", confidence=0.95),
                quantity=FieldValue(value=2.0, confidence=0.4),
                unit_price=FieldValue(value=617.0, confidence=0.9),
                total_price=FieldValue(value=1234.0, confidence=0.9),
            )
        ],
    )

    restored = ReceiptExtraction.model_validate_json(extraction.model_dump_json())

    assert restored == extraction
    assert restored.below_threshold(0.85) == [
        "currency",
        "document_language",
        "expense_kind",
        "merchant_address",
        "payment_method",
        "subtotal",
        "registration_number",
        "tax_8_amount",
        "tax_10_amount",
        "expense_category",
        "line_items[0].quantity",
        "line_items[0].tax_rate",
    ]
    assert restored.field_confidences()["line_items[0].quantity"] == 0.4
    assert restored.source_spans() == {"total_amount": "合計 1,234円"}


def test_schema_validation_rejects_bare_scalars_and_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        ReceiptExtraction.model_validate({"total_amount": 1000})
    with pytest.raises(ValidationError):
        FieldValue[str](value="x", confidence=1.1)


def test_document_class_selects_receipt_schema_and_unknown_falls_back_safely() -> None:
    assert schema_for_document_class(DocumentClass.INVOICE) is ReceiptExtraction
    assert schema_for_document_class("future_class").__name__ == "BaseExtraction"

    result = validate_extraction_payload(
        "receipt",
        {"total_amount": {"value": 10, "confidence": 0.5, "source_span": "合計"}},
    )
    assert isinstance(result, ReceiptExtraction)


def test_extraction_stamp_is_available_to_persistence_without_leaking_into_payload() -> None:
    extraction = ReceiptExtraction().stamp(model_name="extract:7b", prompt_version="v2.0.0")

    assert extraction.model_name == "extract:7b"
    assert extraction.prompt_version == "v2.0.0"
    assert "model_name" not in extraction.model_dump()
    assert "prompt_version" not in extraction.model_dump()
