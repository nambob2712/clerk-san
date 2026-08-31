"""OCR engines behind a single asynchronous, local-only interface."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from io import BytesIO
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clerksan.config import Settings
from clerksan.llm.client import OllamaClient


class OcrEngineError(RuntimeError):
    """Base error for an OCR engine result that cannot be used safely."""


class OptionalOcrRuntimeUnavailable(OcrEngineError):
    """Raised when an optional candidate was selected without its local runtime."""


class OptionalOcrRuntimeError(OcrEngineError):
    """Raised when an installed optional OCR runtime returns an unsupported result."""


class OcrBlock(BaseModel):
    """One recognized text block in reading order."""

    model_config = ConfigDict(extra="forbid")

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: tuple[int, int, int, int] | None = None


class OcrResult(BaseModel):
    """The normalized text returned to image and PDF adapters."""

    model_config = ConfigDict(extra="forbid")

    text: str
    blocks: list[OcrBlock] = Field(default_factory=list)
    language: str | None = None
    engine: str = ""
    confidence_is_self_reported: bool = False


class OcrEngine(Protocol):
    """The one OCR interface adapters depend on."""

    name: str

    async def ocr(self, image_bytes: bytes) -> OcrResult: ...


_VISION_OCR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {
        "text": {"type": "string"},
        "language": {"type": ["string", "null"]},
    },
}

_VISION_PROMPT = """Transcribe this document image locally.

Return all legible text in visual reading order. Preserve line breaks and do not
infer financial fields, totals, dates, or facts that are not visible. Return a compact
OCR JSON object with only `text` and, if known, `language`. Do not create block,
bounding-box, or confidence data: the vision model's generated layout details make
CPU-only local OCR materially slower without improving downstream extraction.
"""


class VisionLlmOcr:
    """OCR through an Ollama vision model; confidences are model self-reports."""

    name = "vision_llm"

    def __init__(self, client: OllamaClient, model: str) -> None:
        self.client = client
        self.model = model

    async def ocr(self, image_bytes: bytes) -> OcrResult:
        if not image_bytes:
            raise ValueError("image_bytes must not be empty")
        response = await self.client.generate(
            self.model,
            _VISION_PROMPT,
            images=[image_bytes],
            json_schema=_VISION_OCR_SCHEMA,
        )
        return _vision_result(response)


class YomiTokuOcr:
    """Optional Japanese OCR candidate executed synchronously inside a worker thread."""

    name = "yomitoku"

    def __init__(self, runner: Callable[[bytes], OcrResult] | None = None) -> None:
        self._runner = runner

    async def ocr(self, image_bytes: bytes) -> OcrResult:
        if not image_bytes:
            raise ValueError("image_bytes must not be empty")
        runner = self._runner or _run_yomitoku
        return await asyncio.to_thread(runner, image_bytes)


class PaddleOcr:
    """Optional PaddleOCR candidate executed synchronously inside a worker thread."""

    name = "paddleocr"

    def __init__(self, runner: Callable[[bytes], OcrResult] | None = None) -> None:
        self._runner = runner

    async def ocr(self, image_bytes: bytes) -> OcrResult:
        if not image_bytes:
            raise ValueError("image_bytes must not be empty")
        runner = self._runner or _run_paddleocr
        return await asyncio.to_thread(runner, image_bytes)


def get_ocr_engine(settings: Settings, client: OllamaClient) -> OcrEngine:
    """Return the explicitly selected local OCR implementation."""
    selected = settings.ocr_engine.strip().lower()
    if selected == VisionLlmOcr.name:
        return VisionLlmOcr(client, settings.ocr_model)
    if selected == YomiTokuOcr.name:
        return YomiTokuOcr()
    if selected == PaddleOcr.name:
        return PaddleOcr()
    raise ValueError(
        "CLERKSAN_OCR_ENGINE must be one of: vision_llm, yomitoku, paddleocr "
        f"(received {settings.ocr_engine!r})"
    )


def _vision_result(response: str) -> OcrResult:
    """Accept schema-constrained vision output while retaining a plain-text fallback."""
    text = response.strip()
    if not text:
        raise OcrEngineError("Vision OCR returned an empty response")
    try:
        payload = json.loads(_json_fragment(text))
    except json.JSONDecodeError:
        return OcrResult(
            text=text,
            blocks=[OcrBlock(text=text, confidence=0.0)],
            engine=VisionLlmOcr.name,
            confidence_is_self_reported=True,
        )
    if not isinstance(payload, dict):
        raise OcrEngineError("Vision OCR returned JSON that is not an object")

    payload["engine"] = VisionLlmOcr.name
    payload["confidence_is_self_reported"] = True
    if not payload.get("text") and isinstance(payload.get("blocks"), list):
        payload["text"] = "\n".join(
            block.get("text", "") for block in payload["blocks"] if isinstance(block, dict)
        ).strip()
    try:
        result = OcrResult.model_validate(payload)
    except ValidationError as error:
        raise OcrEngineError(f"Vision OCR returned an invalid OCR payload: {error}") from error
    if not result.text.strip():
        raise OcrEngineError("Vision OCR returned no recognized text")
    return result


def _run_yomitoku(image_bytes: bytes) -> OcrResult:
    """Run YomiToku's documented ``OCR`` API without importing it at app startup."""
    try:
        import numpy as np
        from PIL import Image
        from yomitoku import OCR
    except ImportError as error:
        raise OptionalOcrRuntimeUnavailable(
            "YomiToku is not installed. Install its local CPU/GPU runtime before setting "
            "CLERKSAN_OCR_ENGINE=yomitoku."
        ) from error

    image = np.asarray(Image.open(BytesIO(image_bytes)).convert("RGB"))
    try:
        result = OCR(visualize=False, device="cpu")(image)
    except Exception as error:  # runtime-specific exceptions should retain their cause
        raise OptionalOcrRuntimeError(f"YomiToku OCR failed: {error}") from error

    document = result[0] if isinstance(result, tuple) else result
    blocks = _blocks_from_runtime_output(document)
    return _result_from_blocks(blocks, engine=YomiTokuOcr.name)


def _run_paddleocr(image_bytes: bytes) -> OcrResult:
    """Run PaddleOCR's local Python API without making it a base dependency."""
    try:
        import numpy as np
        from paddleocr import PaddleOCR
        from PIL import Image
    except ImportError as error:
        raise OptionalOcrRuntimeUnavailable(
            "PaddleOCR is not installed. Install its local runtime before setting "
            "CLERKSAN_OCR_ENGINE=paddleocr."
        ) from error

    image = np.asarray(Image.open(BytesIO(image_bytes)).convert("RGB"))
    try:
        engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        result = list(engine.predict(image))
    except Exception as error:  # runtime-specific exceptions should retain their cause
        raise OptionalOcrRuntimeError(f"PaddleOCR failed: {error}") from error

    blocks = _paddle_blocks(result)
    return _result_from_blocks(blocks, engine=PaddleOcr.name)


def _paddle_blocks(result: list[Any]) -> list[OcrBlock]:
    """Normalize PaddleOCR v3 result objects and mapping-shaped test doubles."""
    blocks: list[OcrBlock] = []
    for page in result:
        payload = _mapping_from_runtime_object(page)
        if not isinstance(payload, Mapping):
            continue
        data = payload.get("res", payload)
        if not isinstance(data, Mapping):
            continue
        texts = data.get("rec_texts")
        scores = data.get("rec_scores")
        boxes = data.get("rec_boxes") or data.get("rec_polys")
        texts = _as_sequence(texts)
        if texts is None:
            continue
        for index, raw_text in enumerate(texts):
            if not isinstance(raw_text, str) or not raw_text.strip():
                continue
            score = _confidence_at(scores, index)
            bbox = _bbox_at(boxes, index)
            blocks.append(OcrBlock(text=raw_text, confidence=score, bbox=bbox))
    return blocks


def _blocks_from_runtime_output(output: Any) -> list[OcrBlock]:
    """Extract text records from YomiToku's serializable result shape defensively."""
    return _blocks_from_mapping(_mapping_from_runtime_object(output))


def _blocks_from_mapping(value: Any) -> list[OcrBlock]:
    blocks: list[OcrBlock] = []

    def visit(current: Any) -> None:
        if isinstance(current, Mapping):
            text = next(
                (
                    current[key]
                    for key in ("text", "recognized_text", "word", "content")
                    if isinstance(current.get(key), str)
                ),
                None,
            )
            if isinstance(text, str) and text.strip():
                blocks.append(
                    OcrBlock(
                        text=text,
                        confidence=_confidence_from_mapping(current),
                        bbox=_bbox_from_mapping(current),
                    )
                )
                return
            for child in current.values():
                visit(child)
        elif isinstance(current, (list, tuple)):
            for child in current:
                visit(child)

    visit(value)
    return blocks


def _mapping_from_runtime_object(value: Any) -> Mapping[str, Any] | list[Any]:
    if isinstance(value, (dict, list, tuple)):
        return value
    for method_name in ("to_dict", "model_dump"):
        method = getattr(value, method_name, None)
        if callable(method):
            converted = method()
            if isinstance(converted, (dict, list, tuple)):
                return converted
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return attributes
    raise OptionalOcrRuntimeError(
        "OCR runtime returned a result that cannot be converted to text blocks"
    )


def _result_from_blocks(blocks: list[OcrBlock], *, engine: str) -> OcrResult:
    if not blocks:
        raise OptionalOcrRuntimeError(f"{engine} returned no recognized text blocks")
    return OcrResult(
        text="\n".join(block.text for block in blocks),
        blocks=blocks,
        engine=engine,
        confidence_is_self_reported=False,
    )


def _confidence_at(values: Any, index: int) -> float:
    sequence = _as_sequence(values)
    if sequence is not None and index < len(sequence):
        return _clamp_confidence(sequence[index])
    return 0.0


def _confidence_from_mapping(value: Mapping[str, Any]) -> float:
    for key in ("confidence", "score", "probability"):
        if key in value:
            return _clamp_confidence(value[key])
    return 0.0


def _clamp_confidence(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _bbox_at(values: Any, index: int) -> tuple[int, int, int, int] | None:
    sequence = _as_sequence(values)
    if sequence is not None and index < len(sequence):
        return _bbox_from_value(sequence[index])
    return None


def _bbox_from_mapping(value: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    for key in ("bbox", "box", "polygon", "points", "rec_box", "rec_poly"):
        if key in value:
            return _bbox_from_value(value[key])
    return None


def _bbox_from_value(value: Any) -> tuple[int, int, int, int] | None:
    sequence = _as_sequence(value)
    if sequence is None:
        return None
    flattened: list[float] = []
    for element in sequence:
        nested = _as_sequence(element)
        if nested is not None:
            flattened.extend(float(part) for part in nested[:2] if isinstance(part, (int, float)))
        elif isinstance(element, (int, float)):
            flattened.append(float(element))
    if len(flattened) < 4:
        return None
    xs = flattened[::2]
    ys = flattened[1::2]
    if not xs or not ys:
        return None
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


def _as_sequence(value: Any) -> list[Any] | tuple[Any, ...] | None:
    if isinstance(value, (list, tuple)):
        return value
    to_list = getattr(value, "tolist", None)
    if callable(to_list):
        converted = to_list()
        return converted if isinstance(converted, (list, tuple)) else None
    return None


def _json_fragment(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    start = stripped.find("{")
    end = stripped.rfind("}")
    return stripped[start : end + 1] if start >= 0 and end >= start else stripped
