"""Bounded, declarative schema mapping for normalized structural rows."""

from __future__ import annotations

import datetime as dt
import enum
import html
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from clerksan.db.models import FinancialSubtype, RecordKind
from clerksan.ingest.normalized import canonical_digest
from clerksan.ingest.records import (
    CandidateDraft,
    CompositionLedger,
    StructuralDisposition,
    StructuralUnitDecision,
    build_candidate_key,
)
from clerksan.ingest.staging import StagedRow, StagedStructure, StagedTable

MAX_MAPPING_FIELDS = 64
MAX_RULE_SOURCES = 8
MAX_NULL_MARKERS = 32
MAX_VALUE_MAP_ENTRIES = 256
MAX_MAPPING_STRING = 2_048
MAX_MAPPING_SET_ENTRIES = 256
MAX_PREVIEW_ROWS = 50


class MappingValidationError(ValueError):
    """A mapping is unsafe, incomplete, or bound to stale structure."""


class FieldParser(enum.StrEnum):
    RAW = "raw"
    DATE = "date"
    DECIMAL = "decimal"
    CURRENCY = "currency"


class DateStyle(enum.StrEnum):
    ISO = "iso"
    YMD_SLASH = "ymd_slash"
    DMY_SLASH = "dmy_slash"
    MDY_SLASH = "mdy_slash"
    JAPANESE = "japanese"


class DecimalStyle(enum.StrEnum):
    DOT = "dot"
    COMMA = "comma"


class SignRule(enum.StrEnum):
    PRESERVE = "preserve"
    NEGATE = "negate"
    ABSOLUTE = "absolute"


@dataclass(frozen=True, slots=True)
class FieldRule:
    """One allowlisted field projection; no executable expressions are accepted."""

    target_field: str
    source_columns: tuple[str, ...] = ()
    literal: str | None = None
    separator: str = " "
    trim: bool = True
    null_markers: tuple[str, ...] = ()
    value_map: tuple[tuple[str, str], ...] = ()
    parser: FieldParser = FieldParser.RAW
    date_style: DateStyle | None = None
    decimal_style: DecimalStyle | None = None
    sign_rule: SignRule = SignRule.PRESERVE
    currency_aliases: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.target_field, "target_field")
        if bool(self.source_columns) == (self.literal is not None):
            raise MappingValidationError("field rule requires either source columns or one literal")
        if len(self.source_columns) > MAX_RULE_SOURCES:
            raise MappingValidationError("field rule has too many source columns")
        for column in self.source_columns:
            _bounded_text(column, "source column")
        if self.literal is not None:
            _bounded_text(self.literal, "literal", allow_blank=True)
        _bounded_text(self.separator, "separator", allow_blank=True)
        if len(self.null_markers) > MAX_NULL_MARKERS:
            raise MappingValidationError("field rule has too many null markers")
        if len(self.value_map) > MAX_VALUE_MAP_ENTRIES:
            raise MappingValidationError("field rule value map is too large")
        if len(self.currency_aliases) > MAX_VALUE_MAP_ENTRIES:
            raise MappingValidationError("currency alias map is too large")
        _validate_pairs(self.value_map, "value map")
        _validate_pairs(self.currency_aliases, "currency alias")
        if self.parser is FieldParser.DATE and self.date_style is None:
            raise MappingValidationError("date parser requires an explicit date style")
        if self.parser is not FieldParser.DATE and self.date_style is not None:
            raise MappingValidationError("date style is valid only for the date parser")
        if self.parser is FieldParser.DECIMAL and self.decimal_style is None:
            raise MappingValidationError("decimal parser requires an explicit decimal style")
        if self.parser is not FieldParser.DECIMAL and self.decimal_style is not None:
            raise MappingValidationError("decimal style is valid only for the decimal parser")
        if self.parser is not FieldParser.DECIMAL and self.sign_rule is not SignRule.PRESERVE:
            raise MappingValidationError("sign rules are valid only for decimal fields")
        if self.parser is not FieldParser.CURRENCY and self.currency_aliases:
            raise MappingValidationError("currency aliases require the currency parser")


@dataclass(frozen=True, slots=True)
class MappingContract:
    table_locator: str
    record_kind: RecordKind
    financial_subtype: FinancialSubtype | None
    schema_fingerprint: str
    field_rules: tuple[FieldRule, ...]
    required_fields: tuple[str, ...] = ()
    mapping_version: int = 1
    mapping_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _bounded_text(self.table_locator, "table_locator")
        _require_digest(self.schema_fingerprint, "schema_fingerprint")
        if self.mapping_version < 1:
            raise MappingValidationError("mapping_version must be positive")
        if not self.field_rules or len(self.field_rules) > MAX_MAPPING_FIELDS:
            raise MappingValidationError("mapping requires between 1 and 64 field rules")
        targets = [rule.target_field for rule in self.field_rules]
        if len(targets) != len(set(targets)):
            raise MappingValidationError("mapping target fields must be unique")
        if len(self.required_fields) > MAX_MAPPING_FIELDS:
            raise MappingValidationError("mapping has too many required fields")
        if not set(self.required_fields).issubset(targets):
            raise MappingValidationError("required fields must be mapped targets")
        if self.record_kind is RecordKind.FINANCIAL and self.financial_subtype is None:
            raise MappingValidationError("financial mapping requires a subtype")
        if self.record_kind is RecordKind.GENERIC_DOCUMENT and self.financial_subtype is not None:
            raise MappingValidationError("generic mapping forbids a financial subtype")

    @property
    def contract_digest(self) -> str:
        return canonical_digest(_mapping_payload(self))


@dataclass(frozen=True, slots=True)
class MappingSetEntryContract:
    table_locator: str
    schema_fingerprint: str
    mapping: MappingContract | None = None
    ignore_reason: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.table_locator, "table_locator")
        _require_digest(self.schema_fingerprint, "schema_fingerprint")
        if (self.mapping is None) == (self.ignore_reason is None):
            raise MappingValidationError(
                "mapping-set entry requires a mapping or explicit ignore reason"
            )
        if self.ignore_reason is not None:
            _bounded_text(self.ignore_reason, "ignore_reason")
        if self.mapping is not None and (
            self.mapping.table_locator != self.table_locator
            or self.mapping.schema_fingerprint != self.schema_fingerprint
        ):
            raise MappingValidationError("mapping-set entry does not match mapping identity")


@dataclass(frozen=True, slots=True)
class MappingSetContract:
    source_file_id: UUID
    source_version: int
    structure_fingerprint: str
    entries: tuple[MappingSetEntryContract, ...]
    created_by: str
    version: int = 1
    mapping_set_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.source_version < 1 or self.version < 1:
            raise MappingValidationError("mapping-set versions must be positive")
        _require_digest(self.structure_fingerprint, "structure_fingerprint")
        _bounded_text(self.created_by, "created_by")
        if len(self.entries) > MAX_MAPPING_SET_ENTRIES:
            raise MappingValidationError("mapping set has too many entries")
        locators = [entry.table_locator for entry in self.entries]
        if len(locators) != len(set(locators)):
            raise MappingValidationError("mapping set repeats a table locator")

    @property
    def set_digest(self) -> str:
        return canonical_digest(
            {
                "source_file_id": str(self.source_file_id),
                "source_version": self.source_version,
                "structure_fingerprint": self.structure_fingerprint,
                "version": self.version,
                "entries": [
                    {
                        "table_locator": entry.table_locator,
                        "schema_fingerprint": entry.schema_fingerprint,
                        "mapping_digest": (
                            entry.mapping.contract_digest if entry.mapping else None
                        ),
                        "ignore_reason": entry.ignore_reason,
                    }
                    for entry in self.entries
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class MappingPreviewRow:
    row_ordinal: int
    source_locator: str
    values: dict[str, Any]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MappingPreview:
    table_locator: str
    rows: tuple[MappingPreviewRow, ...]
    total_rows: int
    valid_rows: int
    error_rows: int
    blank_rows: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class MappingApplication:
    candidates: tuple[CandidateDraft, ...]
    ledger: CompositionLedger


def preview_mapping(
    mapping: MappingContract,
    staged_table: StagedTable,
    *,
    limit: int = MAX_PREVIEW_ROWS,
) -> MappingPreview:
    if limit < 1 or limit > MAX_PREVIEW_ROWS:
        raise MappingValidationError("preview limit must be between 1 and 50")
    _validate_mapping_against_table(mapping, staged_table)
    previews: list[MappingPreviewRow] = []
    valid_rows = 0
    error_rows = 0
    blank_rows = 0
    for row in staged_table.rows:
        if row.blank:
            blank_rows += 1
            values: dict[str, Any] = {}
            errors: tuple[str, ...] = ()
        else:
            values, errors = _apply_row(mapping, staged_table, row)
            if errors:
                error_rows += 1
            else:
                valid_rows += 1
        if len(previews) < limit:
            previews.append(
                MappingPreviewRow(
                    row_ordinal=row.row_ordinal,
                    source_locator=row.row_locator,
                    values=_escaped_values(values),
                    errors=errors,
                )
            )
    return MappingPreview(
        table_locator=mapping.table_locator,
        rows=tuple(previews),
        total_rows=len(staged_table.rows),
        valid_rows=valid_rows,
        error_rows=error_rows,
        blank_rows=blank_rows,
        truncated=len(staged_table.rows) > limit,
    )


def apply_mapping_set(
    mapping_set: MappingSetContract,
    staged: StagedStructure,
    *,
    source_sha256: str,
) -> MappingApplication:
    """Map one complete structure into immutable candidates and a full ledger."""

    _require_digest(source_sha256, "source_sha256")
    if staged.structure_fingerprint != mapping_set.structure_fingerprint:
        raise MappingValidationError("mapping set is bound to stale structure")
    descriptors = {table.descriptor.table_locator: table for table in staged.tables}
    entries = {entry.table_locator: entry for entry in mapping_set.entries}
    if set(descriptors) != set(entries):
        raise MappingValidationError("mapping set must cover every discovered table exactly once")
    for table in staged.tables:
        if table.descriptor.source_file_id != mapping_set.source_file_id or (
            table.descriptor.source_version != mapping_set.source_version
        ):
            raise MappingValidationError("mapping set is bound to a different source")

    candidates: list[CandidateDraft] = []
    decisions: list[StructuralUnitDecision] = []
    candidate_ordinal = 0
    for table in staged.tables:
        entry = entries[table.descriptor.table_locator]
        if entry.schema_fingerprint != table.descriptor.schema_fingerprint:
            raise MappingValidationError("mapping set contains a stale schema fingerprint")
        if entry.mapping is None:
            assert entry.ignore_reason is not None
            decisions.extend(
                StructuralUnitDecision(
                    unit_id=row.row_locator,
                    locator=row.row_locator,
                    content_digest=row.row_fingerprint,
                    disposition=StructuralDisposition.EXPLICIT_IGNORE,
                    reason=entry.ignore_reason,
                )
                for row in table.rows
            )
            continue

        mapping = entry.mapping
        _validate_mapping_against_table(mapping, table)
        for row in table.rows:
            if row.blank:
                decisions.append(
                    StructuralUnitDecision(
                        unit_id=row.row_locator,
                        locator=row.row_locator,
                        content_digest=row.row_fingerprint,
                        disposition=StructuralDisposition.BLANK,
                    )
                )
                continue
            candidate_ordinal += 1
            payload, errors = _apply_row(mapping, table, row)
            candidate_key = build_candidate_key(
                source_sha256=source_sha256,
                source_locator=row.row_locator,
                candidate_ordinal=candidate_ordinal,
                normalized_item_hash=row.row_fingerprint,
                record_kind=mapping.record_kind,
                financial_subtype=mapping.financial_subtype,
                mapping_version=mapping.mapping_version,
            )
            candidates.append(
                CandidateDraft(
                    candidate_ordinal=candidate_ordinal,
                    candidate_key=candidate_key,
                    record_kind=mapping.record_kind,
                    financial_subtype=mapping.financial_subtype,
                    payload=payload,
                    confidences={field_name: 1.0 for field_name in payload},
                    source_locator=row.row_locator,
                    row_fingerprint=row.row_fingerprint,
                    validation_issues=errors,
                )
            )
            decisions.append(
                StructuralUnitDecision(
                    unit_id=row.row_locator,
                    locator=row.row_locator,
                    content_digest=row.row_fingerprint,
                    disposition=StructuralDisposition.MAPPED_CANDIDATE,
                    candidate_key=candidate_key,
                )
            )
    if staged.residual_markdown is not None:
        if staged.residual_locator is None or staged.residual_fingerprint is None:
            raise MappingValidationError("residual structure identity is incomplete")
        candidate_ordinal += 1
        payload = {"content_markdown": staged.residual_markdown}
        candidate_key = build_candidate_key(
            source_sha256=source_sha256,
            source_locator=staged.residual_locator,
            candidate_ordinal=candidate_ordinal,
            normalized_item_hash=staged.residual_fingerprint,
            record_kind=RecordKind.GENERIC_DOCUMENT,
            financial_subtype=None,
            mapping_version=1,
        )
        candidates.append(
            CandidateDraft(
                candidate_ordinal=candidate_ordinal,
                candidate_key=candidate_key,
                record_kind=RecordKind.GENERIC_DOCUMENT,
                financial_subtype=None,
                payload=payload,
                confidences={},
                source_locator=staged.residual_locator,
                row_fingerprint=staged.residual_fingerprint,
            )
        )
        decisions.append(
            StructuralUnitDecision(
                unit_id=staged.residual_locator,
                locator=staged.residual_locator,
                content_digest=staged.residual_fingerprint,
                disposition=StructuralDisposition.RESIDUAL_GENERIC_CANDIDATE,
                candidate_key=candidate_key,
            )
        )
    ledger = CompositionLedger(tuple(decisions))
    if len(ledger.decisions) != staged.unit_count:
        raise MappingValidationError("not every structural unit was reconciled")
    return MappingApplication(candidates=tuple(candidates), ledger=ledger)


def _validate_mapping_against_table(mapping: MappingContract, staged_table: StagedTable) -> None:
    descriptor = staged_table.descriptor
    if mapping.table_locator != descriptor.table_locator:
        raise MappingValidationError("mapping is bound to a different table")
    if mapping.schema_fingerprint != descriptor.schema_fingerprint:
        raise MappingValidationError("mapping is bound to a stale schema")
    headers = set(descriptor.ordered_headers)
    missing = {
        column
        for rule in mapping.field_rules
        for column in rule.source_columns
        if column not in headers
    }
    if missing:
        raise MappingValidationError("mapping refers to an unknown source column")


def _apply_row(
    mapping: MappingContract, staged_table: StagedTable, row: StagedRow
) -> tuple[dict[str, Any], tuple[str, ...]]:
    record = row.as_record(staged_table.descriptor.ordered_headers)
    values: dict[str, Any] = {}
    errors: list[str] = []
    for rule in mapping.field_rules:
        try:
            values[rule.target_field] = _apply_field_rule(rule, record)
        except MappingValidationError as error:
            values[rule.target_field] = None
            errors.append(f"{rule.target_field}:{error}")
    for required in mapping.required_fields:
        if values.get(required) in (None, ""):
            errors.append(f"{required}:required")
    return values, tuple(dict.fromkeys(errors))


def _apply_field_rule(rule: FieldRule, record: dict[str, str]) -> Any:
    if rule.literal is not None:
        value = rule.literal
    else:
        value = rule.separator.join(record[column] for column in rule.source_columns)
    if rule.trim:
        value = value.strip()
    if value in rule.null_markers or value == "":
        return None
    value = dict(rule.value_map).get(value, value)

    if rule.parser is FieldParser.RAW:
        return value
    if rule.parser is FieldParser.DATE:
        assert rule.date_style is not None
        return _parse_date(value, rule.date_style).isoformat()
    if rule.parser is FieldParser.DECIMAL:
        assert rule.decimal_style is not None
        amount = _parse_decimal(value, rule.decimal_style)
        if rule.sign_rule is SignRule.NEGATE:
            amount = -amount
        elif rule.sign_rule is SignRule.ABSOLUTE:
            amount = abs(amount)
        return format(amount, "f")
    aliases = {key.casefold(): replacement.upper() for key, replacement in rule.currency_aliases}
    currency = aliases.get(value.casefold(), value.upper())
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise MappingValidationError("invalid currency code")
    return currency


def _parse_date(value: str, style: DateStyle) -> dt.date:
    try:
        if style is DateStyle.ISO:
            return dt.date.fromisoformat(value)
        if style is DateStyle.JAPANESE:
            match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
            if match is None:
                raise ValueError
            return dt.date(*(int(part) for part in match.groups()))
        formats = {
            DateStyle.YMD_SLASH: "%Y/%m/%d",
            DateStyle.DMY_SLASH: "%d/%m/%Y",
            DateStyle.MDY_SLASH: "%m/%d/%Y",
        }
        return dt.datetime.strptime(value, formats[style]).date()
    except (ValueError, OverflowError) as error:
        raise MappingValidationError("invalid date") from error


def _parse_decimal(value: str, style: DecimalStyle) -> Decimal:
    compact = value.replace(" ", "")
    if style is DecimalStyle.DOT:
        normalized = compact.replace(",", "")
    else:
        normalized = compact.replace(".", "").replace(",", ".")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", normalized):
        raise MappingValidationError("invalid decimal")
    try:
        return Decimal(normalized)
    except InvalidOperation as error:
        raise MappingValidationError("invalid decimal") from error


def _mapping_payload(mapping: MappingContract) -> dict[str, Any]:
    return {
        "mapping_id": str(mapping.mapping_id),
        "mapping_version": mapping.mapping_version,
        "table_locator": mapping.table_locator,
        "schema_fingerprint": mapping.schema_fingerprint,
        "record_kind": mapping.record_kind.value,
        "financial_subtype": (
            mapping.financial_subtype.value if mapping.financial_subtype else None
        ),
        "required_fields": list(mapping.required_fields),
        "field_rules": [
            {
                "target_field": rule.target_field,
                "source_columns": list(rule.source_columns),
                "literal": rule.literal,
                "separator": rule.separator,
                "trim": rule.trim,
                "null_markers": list(rule.null_markers),
                "value_map": [list(pair) for pair in rule.value_map],
                "parser": rule.parser.value,
                "date_style": rule.date_style.value if rule.date_style else None,
                "decimal_style": (rule.decimal_style.value if rule.decimal_style else None),
                "sign_rule": rule.sign_rule.value,
                "currency_aliases": [list(pair) for pair in rule.currency_aliases],
            }
            for rule in mapping.field_rules
        ],
    }


def _escaped_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: html.escape(value, quote=True) if isinstance(value, str) else value
        for key, value in values.items()
    }


def _validate_pairs(pairs: tuple[tuple[str, str], ...], label: str) -> None:
    keys: set[str] = set()
    for key, value in pairs:
        _bounded_text(key, f"{label} key", allow_blank=True)
        _bounded_text(value, f"{label} value", allow_blank=True)
        if key in keys:
            raise MappingValidationError(f"{label} keys must be unique")
        keys.add(key)


def _bounded_text(value: str, label: str, *, allow_blank: bool = False) -> None:
    if (not allow_blank and not value.strip()) or len(value) > MAX_MAPPING_STRING:
        raise MappingValidationError(f"{label} is blank or too long")


def _require_digest(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise MappingValidationError(f"{label} must be a lowercase SHA-256 digest")
