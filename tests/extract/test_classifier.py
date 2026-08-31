from __future__ import annotations

from typing import Any

import pytest

from clerksan.db.models import DocumentClass
from clerksan.extract.classifier import classify, classify_by_keywords
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument


def _document(text: str, filename: str = "document.pdf") -> NormalizedDocument:
    return NormalizedDocument(
        markdown_body=text,
        metadata=DocMetadata(filename=filename, detected_type=FileType.PDF, sha256="a" * 64),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("領収書\n合計 1,000円", DocumentClass.RECEIPT),
        ("請求書\nお支払金額", DocumentClass.INVOICE),
        ("電気料金 ご使用量 検針票", DocumentClass.RECURRING_BILL),
        ("御見積書\n見積金額", DocumentClass.QUOTE),
    ],
)
def test_keyword_priors_route_common_document_classes(text: str, expected: DocumentClass) -> None:
    result = classify_by_keywords(_document(text))

    assert result.label is expected
    assert result.confidence >= 0.75
    assert result.method == "prior"


def test_conflicting_priors_fall_back_to_other() -> None:
    result = classify_by_keywords(_document("領収書 と 請求書"))

    assert result.label is DocumentClass.OTHER
    assert result.confidence <= 0.2


class _UnavailableModels:
    async def ensure_loaded(self, _: object) -> str:
        raise RuntimeError("local model unavailable")


class _NeverUsedClient:
    async def generate(self, *_: Any, **__: Any) -> str:
        raise AssertionError("the client should not be used")


@pytest.mark.asyncio
async def test_unknown_document_gracefully_degrades_to_other_when_local_router_is_down() -> None:
    result = await classify(_document("unlabeled prose"), _NeverUsedClient(), _UnavailableModels())  # type: ignore[arg-type]

    assert result.label is DocumentClass.OTHER
    assert result.confidence == 0.0


class _RouterModels:
    async def ensure_loaded(self, _: object) -> str:
        return "router:3b"


class _RouterClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(self, model: str, prompt: str, **kwargs: Any) -> str:
        self.calls.append({"model": model, "prompt": prompt, **kwargs})
        return '{"label":"quote","confidence":0.62}'


@pytest.mark.asyncio
async def test_unknown_document_can_use_the_local_router() -> None:
    client = _RouterClient()
    result = await classify(_document("Proposal for consulting work"), client, _RouterModels())  # type: ignore[arg-type]

    assert result.label is DocumentClass.QUOTE
    assert result.method == "llm"
    assert client.calls[0]["model"] == "router:3b"
    assert client.calls[0]["json_schema"]["type"] == "object"
