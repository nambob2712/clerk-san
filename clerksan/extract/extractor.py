"""Structured local extraction from normalized document text."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from clerksan.db.models import DocumentClass, ExpenseKind
from clerksan.extract.classifier import infer_expense_kind_by_keywords
from clerksan.extract.schemas import (
    BaseExtraction,
    BillExtraction,
    FieldValue,
    QuoteExtraction,
    ReceiptExtraction,
    RecurringBillExtraction,
    schema_for_document_class,
)
from clerksan.extract.tax_id import is_valid_registration_number
from clerksan.ingest.normalized import NormalizedDocument
from clerksan.llm.client import ModelManager, ModelRole, OllamaClient

PROMPT_VERSION = "v2.0.2"
_MAX_PROMPT_BODY_CHARS = 80_000
_MAX_PROMPT_TABLE_CHARS = 20_000
_EXPENSE_KIND_VALUES = ", ".join(kind.value for kind in ExpenseKind)


class ExtractionFailed(RuntimeError):
    """Raised when one generation and one repair cannot form a valid payload."""


async def extract(
    doc: NormalizedDocument,
    doc_class: DocumentClass,
    client: OllamaClient,
    models: ModelManager,
) -> BaseExtraction:
    """Extract a schema-constrained, review-only payload with one repair attempt."""
    schema = schema_for_document_class(doc_class)
    model = await models.ensure_loaded(ModelRole.EXTRACT)
    json_schema = response_schema(schema)

    first_response = await client.generate(
        model,
        _extraction_prompt(doc, doc_class),
        json_schema=json_schema,
    )
    try:
        extracted = _parse_extraction(first_response, schema)
    except _PayloadValidationError:
        repair_response = await client.generate(
            model,
            _repair_prompt(first_response, json_schema),
            json_schema=json_schema,
        )
        try:
            extracted = _parse_extraction(repair_response, schema)
        except _PayloadValidationError as error:
            raise ExtractionFailed(
                "Local extraction did not produce a schema-valid payload after one repair attempt"
            ) from error

    _validate_registration_number(extracted)
    _fill_expense_kind_from_visible_text(extracted, doc)
    return extracted.stamp(model_name=model, prompt_version=PROMPT_VERSION)


class _PayloadValidationError(ValueError):
    """Internal sentinel used to keep transport errors distinct from malformed output."""


def _parse_extraction(raw: str, schema: type[BaseExtraction]) -> BaseExtraction:
    try:
        payload = json.loads(_json_fragment(raw))
    except json.JSONDecodeError as error:
        raise _PayloadValidationError("response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise _PayloadValidationError("response JSON is not an object")
    missing = set(_core_fields(schema)).difference(payload)
    if missing:
        raise _PayloadValidationError(
            "response omits required core fields: " + ", ".join(sorted(missing))
        )
    try:
        return schema.model_validate(payload)
    except ValidationError as error:
        raise _PayloadValidationError("response does not match the extraction schema") from error


def _validate_registration_number(extracted: BaseExtraction) -> None:
    """Keep an invalid registration number visible while forcing it into review."""
    if not isinstance(extracted, ReceiptExtraction):
        return
    registration_number = extracted.registration_number
    if registration_number.value is not None and not is_valid_registration_number(
        registration_number.value
    ):
        registration_number.confidence = 0.0


def _fill_expense_kind_from_visible_text(
    extracted: BaseExtraction, doc: NormalizedDocument
) -> None:
    """Prefill a missing category from an unambiguous visible keyword for review."""

    if extracted.expense_kind.value is not None:
        return
    inferred = infer_expense_kind_by_keywords(doc)
    if inferred is None:
        return
    kind, source_span = inferred
    extracted.expense_kind = FieldValue(value=kind, confidence=0.80, source_span=source_span)


def response_schema(schema: type[BaseExtraction]) -> dict[str, Any]:
    """Return the compact LLM contract for visible, review-critical fields.

    The persistence schema deliberately gives every field a safe default so a human
    can approve a partial extraction.  Sending that permissive schema directly to a
    local model lets it return a syntactically valid object containing only optional
    fields.  The response contract instead requires the visible core fields while
    retaining nullable values for genuinely absent facts.
    """

    full_schema = schema.model_json_schema()
    properties = full_schema.get("properties")
    if not isinstance(properties, dict):
        raise TypeError("extraction schema must define object properties")
    required = [field for field in _core_fields(schema) if field in properties]
    response: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {field: properties[field] for field in required},
        "required": required,
    }
    definitions = full_schema.get("$defs")
    if isinstance(definitions, dict):
        response["$defs"] = deepcopy(definitions)
        for definition in response["$defs"].values():
            if isinstance(definition, dict) and {"value", "confidence", "source_span"}.issubset(
                definition.get("properties", {})
            ):
                definition["required"] = ["value", "confidence", "source_span"]
    return response


def _core_fields(schema: type[BaseExtraction]) -> tuple[str, ...]:
    common = ("transaction_date", "total_amount", "counterparty", "currency")
    if issubclass(schema, ReceiptExtraction):
        return (*common, "expense_kind", "registration_number", "expense_category")
    if issubclass(schema, RecurringBillExtraction):
        return (
            *common,
            "expense_kind",
            "issuer_name",
            "issuer_kind",
            "billing_period",
            "due_date",
            "consumption_value",
            "consumption_unit",
        )
    if issubclass(schema, BillExtraction):
        return (*common, "expense_kind", "issuer_name", "due_date")
    if issubclass(schema, QuoteExtraction):
        return (*common, "valid_until")
    return common


def _extraction_prompt(doc: NormalizedDocument, doc_class: DocumentClass) -> str:
    tables = json.dumps(
        [table.model_dump(mode="json") for table in doc.tables],
        ensure_ascii=False,
        separators=(",", ":"),
    )[:_MAX_PROMPT_TABLE_CHARS]
    body = doc.markdown_body[:_MAX_PROMPT_BODY_CHARS]
    return f"""Extract fields from this local {doc_class.value} document.

Return only the requested JSON object. Each extracted scalar must be represented
as an object with `value`, `confidence`, and `source_span`. Use null and 0
confidence when a value is absent or unclear. Do not invent values or use facts
outside the provided text and tables. Preserve any visible Japanese invoice
registration number exactly as shown. Every required core field must be present;
when a core value is visible, do not leave its value null.
When the response schema includes `expense_kind`, use exactly one of:
{_EXPENSE_KIND_VALUES}.

Filename: {doc.metadata.filename}
Document text:
{body}

Tables JSON:
{tables}
"""


def _repair_prompt(raw_response: str, json_schema: dict[str, Any]) -> str:
    schema = json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
    response = raw_response[:20_000]
    return f"""Repair this local extraction response into valid JSON only.

Do not add facts. Preserve values when possible. The result must conform exactly
to this JSON schema:
{schema}

Invalid response:
{response}
"""


def _json_fragment(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    start = stripped.find("{")
    end = stripped.rfind("}")
    return stripped[start : end + 1] if start >= 0 and end >= start else stripped
