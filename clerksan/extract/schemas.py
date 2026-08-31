"""Extraction payload schemas with confidence and provenance for every value."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any, Generic, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from clerksan.db.models import DocumentClass, ExpenseKind

T = TypeVar("T")


class _ExtractionModel(BaseModel):
    """Reject invented fields while allowing normal JSON coercion from model output."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FieldValue(_ExtractionModel, Generic[T]):
    """A single extracted field, including the model's confidence and source text."""

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_span: str | None = None


class LineItemX(_ExtractionModel):
    """One receipt or quote line item, with independent confidence for every field."""

    description: FieldValue[str] = Field(default_factory=FieldValue)
    quantity: FieldValue[float] = Field(default_factory=FieldValue)
    unit_price: FieldValue[float] = Field(default_factory=FieldValue)
    total_price: FieldValue[float] = Field(default_factory=FieldValue)
    tax_rate: FieldValue[int] = Field(default_factory=FieldValue)


class BaseExtraction(_ExtractionModel):
    """Shared electronic-record fields used by every document class."""

    _model_name: str | None = PrivateAttr(default=None)
    _prompt_version: str | None = PrivateAttr(default=None)

    transaction_date: FieldValue[dt.date] = Field(default_factory=FieldValue)
    total_amount: FieldValue[float] = Field(default_factory=FieldValue)
    counterparty: FieldValue[str] = Field(default_factory=FieldValue)
    currency: FieldValue[str] = Field(default_factory=FieldValue)
    document_language: FieldValue[str] = Field(default_factory=FieldValue)
    expense_kind: FieldValue[ExpenseKind] = Field(default_factory=FieldValue)

    def iter_field_values(self) -> Iterator[tuple[str, FieldValue[Any]]]:
        """Yield every confidence-bearing value, including nested line-item fields."""
        yield from _iter_field_values(self, "")

    def below_threshold(self, threshold: float) -> list[str]:
        """Return field paths that require highlighting at the supplied threshold."""
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        return [
            path
            for path, field_value in self.iter_field_values()
            if field_value.confidence < threshold
        ]

    def field_confidences(self) -> dict[str, float]:
        """Produce the JSON-ready path-to-confidence map stored with an extraction."""
        return {path: field_value.confidence for path, field_value in self.iter_field_values()}

    def source_spans(self) -> dict[str, str]:
        """Produce the JSON-ready path-to-provenance map for fields with a source span."""
        return {
            path: field_value.source_span
            for path, field_value in self.iter_field_values()
            if field_value.source_span
        }

    def stamp(self, *, model_name: str, prompt_version: str) -> Self:
        """Attach non-payload provenance for the persistence layer."""
        self._model_name = model_name
        self._prompt_version = prompt_version
        return self

    @property
    def model_name(self) -> str | None:
        """The local model that produced this payload, excluded from payload JSON."""
        return self._model_name

    @property
    def prompt_version(self) -> str | None:
        """The extraction prompt revision, excluded from payload JSON."""
        return self._prompt_version


class ReceiptExtraction(BaseExtraction):
    """Receipt and invoice fields, including Japanese qualified-invoice fields."""

    merchant_address: FieldValue[str] = Field(default_factory=FieldValue)
    payment_method: FieldValue[str] = Field(default_factory=FieldValue)
    subtotal: FieldValue[float] = Field(default_factory=FieldValue)
    registration_number: FieldValue[str] = Field(default_factory=FieldValue)
    tax_8_amount: FieldValue[float] = Field(default_factory=FieldValue)
    tax_10_amount: FieldValue[float] = Field(default_factory=FieldValue)
    expense_category: FieldValue[str] = Field(default_factory=FieldValue)
    line_items: list[LineItemX] = Field(default_factory=list)


class BillExtraction(BaseExtraction):
    """Issuer and due-date fields shared by one-off and recurring bills."""

    issuer_name: FieldValue[str] = Field(default_factory=FieldValue)
    due_date: FieldValue[dt.date] = Field(default_factory=FieldValue)


class RecurringBillExtraction(BillExtraction):
    """Periodic utility or statutory bill fields used by recurring analysis."""

    issuer_kind: FieldValue[str] = Field(default_factory=FieldValue)
    billing_period: FieldValue[str] = Field(default_factory=FieldValue)
    consumption_value: FieldValue[float] = Field(default_factory=FieldValue)
    consumption_unit: FieldValue[str] = Field(default_factory=FieldValue)


class QuoteExtraction(BaseExtraction):
    """Quote fields, including validity and line items."""

    valid_until: FieldValue[dt.date] = Field(default_factory=FieldValue)
    line_items: list[LineItemX] = Field(default_factory=list)


SCHEMA_BY_CLASS: dict[str, type[BaseExtraction]] = {
    DocumentClass.RECEIPT.value: ReceiptExtraction,
    DocumentClass.INVOICE.value: ReceiptExtraction,
    DocumentClass.BILL.value: BillExtraction,
    DocumentClass.RECURRING_BILL.value: RecurringBillExtraction,
    DocumentClass.QUOTE.value: QuoteExtraction,
    DocumentClass.OTHER.value: BaseExtraction,
}


def schema_for_document_class(document_class: DocumentClass | str) -> type[BaseExtraction]:
    """Return a safe generic schema for unknown or future classes."""
    key = document_class.value if isinstance(document_class, DocumentClass) else str(document_class)
    return SCHEMA_BY_CLASS.get(key, BaseExtraction)


def validate_extraction_payload(
    document_class: DocumentClass | str, payload: dict[str, Any]
) -> BaseExtraction:
    """Validate a persisted or generated JSON payload against its routed schema."""
    return schema_for_document_class(document_class).model_validate(payload)


def _iter_field_values(value: Any, path: str) -> Iterator[tuple[str, FieldValue[Any]]]:
    if isinstance(value, FieldValue):
        yield path, value
        return
    if isinstance(value, BaseModel):
        for field_name in value.__class__.model_fields:
            child_path = field_name if not path else f"{path}.{field_name}"
            yield from _iter_field_values(getattr(value, field_name), child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_field_values(child, f"{path}[{index}]")
