from __future__ import annotations

import builtins
from typing import Any

import pytest

from clerksan.config import Settings
from clerksan.llm.ocr import (
    OcrBlock,
    OcrResult,
    OptionalOcrRuntimeUnavailable,
    PaddleOcr,
    VisionLlmOcr,
    YomiTokuOcr,
    get_ocr_engine,
)


class _FakeClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def generate(self, model: str, prompt: str, **kwargs: Any) -> str:
        self.calls.append({"model": model, "prompt": prompt, **kwargs})
        return self.response


@pytest.mark.asyncio
async def test_vision_ocr_uses_local_vision_request_and_marks_confidence() -> None:
    client = _FakeClient(
        '{"text":"領収書\\n合計 1000円","language":"ja","blocks":['
        '{"text":"領収書","confidence":0.91,"bbox":[1,2,3,4]}]}'
    )
    engine = VisionLlmOcr(client, "vision:7b")  # type: ignore[arg-type]

    result = await engine.ocr(b"image-bytes")

    assert result.text == "領収書\n合計 1000円"
    assert result.blocks[0].bbox == (1, 2, 3, 4)
    assert result.engine == "vision_llm"
    assert result.confidence_is_self_reported is True
    assert client.calls[0]["images"] == [b"image-bytes"]
    assert client.calls[0]["json_schema"]["type"] == "object"
    assert client.calls[0]["json_schema"]["required"] == ["text"]


@pytest.mark.asyncio
async def test_vision_ocr_accepts_plain_text_when_a_model_ignores_schema() -> None:
    engine = VisionLlmOcr(_FakeClient("receipt\ntotal 1000"), "vision:7b")  # type: ignore[arg-type]

    result = await engine.ocr(b"image-bytes")

    assert result.blocks == [OcrBlock(text="receipt\ntotal 1000", confidence=0.0)]
    assert result.confidence_is_self_reported is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engine_type", "package_name"),
    [(YomiTokuOcr, "yomitoku"), (PaddleOcr, "paddleocr")],
)
async def test_optional_engines_explain_missing_runtime(
    monkeypatch: pytest.MonkeyPatch,
    engine_type: type[YomiTokuOcr] | type[PaddleOcr],
    package_name: str,
) -> None:
    original_import = builtins.__import__

    def missing_optional(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == package_name:
            raise ImportError(f"missing {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_optional)

    with pytest.raises(OptionalOcrRuntimeUnavailable, match="not installed"):
        await engine_type().ocr(b"not-decoded-before-runtime-check")


@pytest.mark.asyncio
async def test_candidate_runner_uses_the_shared_async_contract() -> None:
    expected = OcrResult(
        text="電気料金",
        blocks=[OcrBlock(text="電気料金", confidence=0.88)],
        engine="custom",
    )
    result = await YomiTokuOcr(runner=lambda _: expected).ocr(b"image")

    assert result == expected


def test_selected_engine_comes_only_from_settings() -> None:
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", ocr_engine="paddleocr")
    engine = get_ocr_engine(settings, _FakeClient(""))  # type: ignore[arg-type]

    assert isinstance(engine, PaddleOcr)
    with pytest.raises(ValueError, match="vision_llm, yomitoku, paddleocr"):
        Settings(database_url="sqlite+aiosqlite:///:memory:", ocr_engine="cloud")
