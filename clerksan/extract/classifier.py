"""Document routing with deterministic Japanese and English keyword priors."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from clerksan.db.models import DocumentClass, ExpenseKind
from clerksan.ingest.normalized import NormalizedDocument
from clerksan.llm.client import ModelManager, ModelRole, OllamaClient


class ClassificationResult(BaseModel):
    """A recoverable routing decision; review can always reclassify it later."""

    model_config = ConfigDict(extra="forbid")

    label: DocumentClass
    confidence: float = Field(ge=0.0, le=1.0)
    method: str


_KEYWORDS: dict[DocumentClass, tuple[str, ...]] = {
    DocumentClass.RECEIPT: (
        "領収書",
        "レシート",
        "お買上げ",
        "お買い上げ",
        "receipt",
    ),
    DocumentClass.INVOICE: (
        "請求書",
        "御請求",
        "ご請求",
        "invoice",
    ),
    DocumentClass.BILL: (
        "nộp thuế",
        "thuế thu nhập",
        "bảo hiểm",
        "tiền điện",
        "tiền nước",
        "tiền gas",
        "hóa đơn internet",
        "cước viễn thông",
        "tax notice",
        "insurance premium",
        "payment notice",
    ),
    DocumentClass.RECURRING_BILL: (
        "検針票",
        "ご使用量",
        "納入通知書",
        "督促",
        "電気料金",
        "ガス料金",
        "水道料金",
        "chỉ số tiêu thụ",
        "kỳ thanh toán",
    ),
    DocumentClass.QUOTE: ("見積書", "御見積", "quotation", "quote"),
}

_EXPENSE_KIND_KEYWORDS: dict[ExpenseKind, tuple[str, ...]] = {
    ExpenseKind.RETAIL: ("hóa đơn bán lẻ", "領収書", "レシート", "receipt"),
    ExpenseKind.ELECTRICITY: ("electricity bill", "electricity", "電気料金", "tiền điện"),
    ExpenseKind.WATER: ("water bill", "water charge", "水道料金", "tiền nước"),
    ExpenseKind.GAS: ("gas bill", "ガス料金", "tiền gas"),
    ExpenseKind.TELECOM: (
        "telecom",
        "internet",
        "phone bill",
        "cước viễn thông",
        "hóa đơn internet",
    ),
    ExpenseKind.TAX: (
        "tax notice",
        "tax office",
        "tax payment",
        "nộp thuế",
        "thuế thu nhập",
        "納付書",
        "税金",
    ),
    ExpenseKind.INSURANCE: ("insurance premium", "insurance", "bảo hiểm", "保険"),
    ExpenseKind.RENT: ("rent payment", "rent notice", "tiền thuê", "家賃"),
    ExpenseKind.SUBSCRIPTION: ("subscription", "membership", "定期購読", "gói đăng ký"),
}

_CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label", "confidence"],
    "properties": {
        "label": {"type": "string", "enum": [member.value for member in DocumentClass]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def classify_by_keywords(doc: NormalizedDocument) -> ClassificationResult:
    """Classify obvious documents without needing a model server.

    Tied keyword evidence intentionally becomes ``other`` rather than guessing.  A
    single direct class marker is enough to route a document to review; it never
    promotes a record on its own.
    """
    text = f"{doc.metadata.filename}\n{doc.markdown_body[:2_000]}".casefold()
    scores = {
        label: sum(1 for keyword in keywords if keyword.casefold() in text)
        for label, keywords in _KEYWORDS.items()
    }
    highest = max(scores.values(), default=0)
    if highest == 0:
        return ClassificationResult(label=DocumentClass.OTHER, confidence=0.0, method="prior")

    winners = [label for label, score in scores.items() if score == highest]
    if len(winners) != 1:
        return ClassificationResult(label=DocumentClass.OTHER, confidence=0.2, method="prior")

    confidence = min(0.95, 0.75 + 0.10 * (highest - 1))
    return ClassificationResult(label=winners[0], confidence=confidence, method="prior")


def infer_expense_kind_by_keywords(
    doc: NormalizedDocument,
) -> tuple[ExpenseKind, str] | None:
    """Return one conservative category prior backed by visible document text."""

    source = f"{doc.metadata.filename}\n{doc.markdown_body[:2_000]}"
    text = source.casefold()
    scores = {
        kind: sum(1 for keyword in keywords if keyword.casefold() in text)
        for kind, keywords in _EXPENSE_KIND_KEYWORDS.items()
    }
    highest = max(scores.values(), default=0)
    if highest == 0:
        return None
    winners = [kind for kind, score in scores.items() if score == highest]
    if len(winners) != 1:
        return None
    kind = winners[0]
    keyword = next(
        keyword for keyword in _EXPENSE_KIND_KEYWORDS[kind] if keyword.casefold() in text
    )
    return kind, _visible_span(source, keyword)


async def classify(
    doc: NormalizedDocument, client: OllamaClient, models: ModelManager
) -> ClassificationResult:
    """Classify a document using a local router only when keyword evidence is absent."""
    prior = classify_by_keywords(doc)
    if prior.label is not DocumentClass.OTHER:
        return prior

    try:
        model = await models.ensure_loaded(ModelRole.ROUTER)
        raw = await client.generate(
            model,
            _classifier_prompt(doc),
            json_schema=_CLASSIFIER_SCHEMA,
        )
        return _parse_llm_classification(raw)
    except Exception:
        # Classification is deliberately recoverable: unknown input must enter review
        # as ``other`` instead of allowing a local model outage to fail ingestion.
        return ClassificationResult(label=DocumentClass.OTHER, confidence=0.0, method="prior")


def _classifier_prompt(doc: NormalizedDocument) -> str:
    body = doc.markdown_body[:2_000]
    return f"""Classify this local document into exactly one label:
receipt, invoice, bill, recurring_bill, quote, or other.
Use bill for one-off utility, telecom, tax, insurance, rent, or similar payment notices.

Use only visible evidence. If uncertain, choose other with low confidence.
Filename: {doc.metadata.filename}
Document text:
{body}
"""


def _parse_llm_classification(raw: str) -> ClassificationResult:
    payload = json.loads(_json_fragment(raw))
    if not isinstance(payload, dict):
        raise ValueError("classifier response is not an object")
    label = DocumentClass(str(payload["label"]))
    confidence = float(payload["confidence"])
    return ClassificationResult(label=label, confidence=confidence, method="llm")


def _json_fragment(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    start = stripped.find("{")
    end = stripped.rfind("}")
    return stripped[start : end + 1] if start >= 0 and end >= start else stripped


def _visible_span(source: str, keyword: str) -> str:
    """Return the original-case keyword span used for the conservative prior."""

    start = source.casefold().find(keyword.casefold())
    return source[start : start + len(keyword)] if start >= 0 else keyword
