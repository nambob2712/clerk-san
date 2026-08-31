from __future__ import annotations

import json
from typing import Any

import pytest

from clerksan.db.models import DocumentClass
from clerksan.extract.extractor import PROMPT_VERSION, ExtractionFailed, extract, response_schema
from clerksan.extract.schemas import ReceiptExtraction
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument


def _document() -> NormalizedDocument:
    return NormalizedDocument(
        markdown_body="領収書\n2026-07-13\nテスト商店\n合計 1,234円\n登録番号 T8700110005902",
        metadata=DocMetadata(filename="receipt.png", detected_type=FileType.PNG, sha256="a" * 64),
    )


def _receipt_payload() -> str:
    return json.dumps(
        {
            "transaction_date": {
                "value": "2026-07-13",
                "confidence": 0.98,
                "source_span": "2026-07-13",
            },
            "total_amount": {
                "value": 1234,
                "confidence": 0.99,
                "source_span": "合計 1,234円",
            },
            "counterparty": {
                "value": "テスト商店",
                "confidence": 0.95,
                "source_span": "テスト商店",
            },
            "currency": {
                "value": "JPY",
                "confidence": 0.99,
                "source_span": "合計 1,234円",
            },
            "expense_kind": {
                "value": "retail",
                "confidence": 0.95,
                "source_span": "領収書",
            },
            "registration_number": {
                "value": "T8700110005902",
                "confidence": 0.97,
                "source_span": "登録番号 T8700110005902",
            },
            "expense_category": {
                "value": None,
                "confidence": 0.0,
                "source_span": None,
            },
        },
        ensure_ascii=False,
    )


class _Models:
    async def ensure_loaded(self, _: object) -> str:
        return "extract:7b"


class _Client:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate(self, model: str, prompt: str, **kwargs: Any) -> str:
        self.calls.append({"model": model, "prompt": prompt, **kwargs})
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_extract_populates_receipt_and_preserves_invalid_registration_value() -> None:
    client = _Client([_receipt_payload()])

    result = await extract(_document(), DocumentClass.RECEIPT, client, _Models())  # type: ignore[arg-type]

    assert isinstance(result, ReceiptExtraction)
    assert result.total_amount.value == 1234.0
    assert result.registration_number.value == "T8700110005902"
    assert result.registration_number.confidence == 0.0
    assert result.model_name == "extract:7b"
    assert result.prompt_version == PROMPT_VERSION
    assert client.calls[0]["json_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_extract_performs_exactly_one_repair_after_malformed_json() -> None:
    client = _Client(["not-json", _receipt_payload()])

    result = await extract(_document(), DocumentClass.INVOICE, client, _Models())  # type: ignore[arg-type]

    assert isinstance(result, ReceiptExtraction)
    assert len(client.calls) == 2
    assert "Repair this local extraction response" in client.calls[1]["prompt"]


@pytest.mark.asyncio
async def test_extract_repairs_schema_valid_json_that_omits_a_required_core_field() -> None:
    partial = json.loads(_receipt_payload())
    partial.pop("expense_category", None)
    client = _Client([json.dumps(partial), _receipt_payload()])

    result = await extract(_document(), DocumentClass.RECEIPT, client, _Models())  # type: ignore[arg-type]

    assert isinstance(result, ReceiptExtraction)
    assert len(client.calls) == 2
    assert "expense_category" in client.calls[1]["json_schema"]["required"]


@pytest.mark.asyncio
async def test_extract_fails_after_one_invalid_repair() -> None:
    client = _Client(["not-json", "still-not-json"])

    with pytest.raises(ExtractionFailed, match="one repair attempt"):
        await extract(_document(), DocumentClass.RECEIPT, client, _Models())  # type: ignore[arg-type]
    assert len(client.calls) == 2


def test_response_schema_requires_visible_receipt_fields_not_detail_fields() -> None:
    schema = response_schema(ReceiptExtraction)

    assert set(schema["required"]) == {
        "transaction_date",
        "total_amount",
        "counterparty",
        "currency",
        "expense_kind",
        "registration_number",
        "expense_category",
    }
    assert "merchant_address" not in schema["properties"]
    for property_schema in schema["properties"].values():
        definition_name = property_schema["$ref"].rsplit("/", 1)[-1]
        assert schema["$defs"][definition_name]["required"] == [
            "value",
            "confidence",
            "source_span",
        ]
