from __future__ import annotations

from uuid import uuid4

import pytest

from clerksan.db.models import FinancialSubtype, RecordKind
from clerksan.ingest.filetype import FileType
from clerksan.ingest.mapping import (
    DateStyle,
    DecimalStyle,
    FieldParser,
    FieldRule,
    MappingContract,
    MappingSetContract,
    MappingSetEntryContract,
    MappingValidationError,
    SignRule,
    apply_mapping_set,
    preview_mapping,
)
from clerksan.ingest.normalized import DocMetadata, ExtractedTable, NormalizedDocument
from clerksan.ingest.staging import build_staged_structure


def _staged():
    source_file_id = uuid4()
    document = NormalizedDocument(
        markdown_body="two tables",
        metadata=DocMetadata(
            filename="records.csv",
            detected_type=FileType.CSV,
            sha256="a" * 64,
        ),
        tables=[
            ExtractedTable(
                header=["date", "amount", "currency", "memo"],
                rows=[
                    ["2026年8月1日", "1.234,50", "円", "<b>literal</b>"],
                    ["2026年8月2日", "1.234,50", "円", "=1+1"],
                    ["", "", "", ""],
                    ["not-a-date", "bad", "円", "kept as exception"],
                ],
                source_location="sheet:transactions",
            ),
            ExtractedTable(
                header=["note"],
                rows=[["not part of the mapped cohort"]],
                source_location="sheet:notes",
            ),
        ],
        embeddable=False,
    )
    return build_staged_structure(document, source_file_id=source_file_id, source_version=1)


def _financial_mapping(staged) -> MappingContract:
    descriptor = staged.tables[0].descriptor
    return MappingContract(
        table_locator=descriptor.table_locator,
        record_kind=RecordKind.FINANCIAL,
        financial_subtype=FinancialSubtype.TRANSACTION,
        schema_fingerprint=descriptor.schema_fingerprint,
        field_rules=(
            FieldRule(
                target_field="transaction_date",
                source_columns=("date",),
                parser=FieldParser.DATE,
                date_style=DateStyle.JAPANESE,
            ),
            FieldRule(
                target_field="amount",
                source_columns=("amount",),
                parser=FieldParser.DECIMAL,
                decimal_style=DecimalStyle.COMMA,
                sign_rule=SignRule.NEGATE,
            ),
            FieldRule(
                target_field="currency",
                source_columns=("currency",),
                parser=FieldParser.CURRENCY,
                currency_aliases=(("円", "JPY"),),
            ),
            FieldRule(target_field="memo", source_columns=("memo",)),
        ),
        required_fields=("transaction_date", "amount", "currency"),
    )


def test_preview_is_bounded_escaped_and_reports_parse_errors() -> None:
    staged = _staged()
    preview = preview_mapping(_financial_mapping(staged), staged.tables[0], limit=3)

    assert len(preview.rows) == 3
    assert preview.truncated is True
    assert preview.rows[0].values == {
        "transaction_date": "2026-08-01",
        "amount": "-1234.50",
        "currency": "JPY",
        "memo": "&lt;b&gt;literal&lt;/b&gt;",
    }
    assert preview.rows[1].values["memo"] == "=1+1"
    assert preview.error_rows == 1
    assert preview.blank_rows == 1


def test_complete_mapping_preserves_each_row_and_reconciles_every_unit() -> None:
    staged = _staged()
    mapping = _financial_mapping(staged)
    ignored = staged.tables[1].descriptor
    mapping_set = MappingSetContract(
        source_file_id=staged.tables[0].descriptor.source_file_id,
        source_version=1,
        structure_fingerprint=staged.structure_fingerprint,
        entries=(
            MappingSetEntryContract(
                table_locator=mapping.table_locator,
                schema_fingerprint=mapping.schema_fingerprint,
                mapping=mapping,
            ),
            MappingSetEntryContract(
                table_locator=ignored.table_locator,
                schema_fingerprint=ignored.schema_fingerprint,
                ignore_reason="not a transaction table",
            ),
        ),
        created_by="local-reviewer",
    )

    result = apply_mapping_set(mapping_set, staged, source_sha256="a" * 64)

    assert len(result.candidates) == 3
    assert result.candidates[0].source_locator != result.candidates[1].source_locator
    assert result.candidates[0].candidate_key != result.candidates[1].candidate_key
    assert result.candidates[2].validation_issues == (
        "transaction_date:invalid date",
        "amount:invalid decimal",
        "transaction_date:required",
        "amount:required",
    )
    assert result.ledger.reconciliation_counts == {
        "mapped_candidate": 3,
        "residual_generic_candidate": 0,
        "explicit_ignore": 1,
        "blank": 1,
        "parse_error": 0,
    }


def test_mixed_source_maps_rows_and_keeps_independent_residual_generic_content() -> None:
    source_file_id = uuid4()
    document = NormalizedDocument(
        markdown_body="Narrative outside the table",
        metadata=DocMetadata(
            filename="mixed.html",
            detected_type=FileType.HTML,
            sha256="b" * 64,
            extra={"residual_markdown": "Narrative outside the table"},
        ),
        tables=[
            ExtractedTable(
                header=["date", "amount"],
                rows=[["2026-08-01", "1200"]],
                source_location="table:1",
            )
        ],
        embeddable=False,
    )
    staged = build_staged_structure(document, source_file_id=source_file_id, source_version=1)
    descriptor = staged.tables[0].descriptor
    mapping = MappingContract(
        table_locator=descriptor.table_locator,
        record_kind=RecordKind.FINANCIAL,
        financial_subtype=FinancialSubtype.TRANSACTION,
        schema_fingerprint=descriptor.schema_fingerprint,
        field_rules=(
            FieldRule(target_field="date", source_columns=("date",)),
            FieldRule(target_field="amount", source_columns=("amount",)),
        ),
    )
    mapping_set = MappingSetContract(
        source_file_id=source_file_id,
        source_version=1,
        structure_fingerprint=staged.structure_fingerprint,
        entries=(
            MappingSetEntryContract(
                table_locator=descriptor.table_locator,
                schema_fingerprint=descriptor.schema_fingerprint,
                mapping=mapping,
            ),
        ),
        created_by="reviewer",
    )

    result = apply_mapping_set(mapping_set, staged, source_sha256="b" * 64)

    assert staged.unit_count == 2
    assert [candidate.record_kind for candidate in result.candidates] == [
        RecordKind.FINANCIAL,
        RecordKind.GENERIC_DOCUMENT,
    ]
    assert result.candidates[1].payload == {"content_markdown": "Narrative outside the table"}
    assert result.candidates[0].source_locator != result.candidates[1].source_locator
    assert result.ledger.reconciliation_counts == {
        "mapped_candidate": 1,
        "residual_generic_candidate": 1,
        "explicit_ignore": 0,
        "blank": 0,
        "parse_error": 0,
    }


def test_mapping_set_requires_complete_locator_coverage() -> None:
    staged = _staged()
    mapping = _financial_mapping(staged)
    incomplete = MappingSetContract(
        source_file_id=staged.tables[0].descriptor.source_file_id,
        source_version=1,
        structure_fingerprint=staged.structure_fingerprint,
        entries=(
            MappingSetEntryContract(
                table_locator=mapping.table_locator,
                schema_fingerprint=mapping.schema_fingerprint,
                mapping=mapping,
            ),
        ),
        created_by="local-reviewer",
    )
    with pytest.raises(MappingValidationError, match="cover every discovered table"):
        apply_mapping_set(incomplete, staged, source_sha256="a" * 64)


def test_record_kind_and_transform_allowlist_are_strict() -> None:
    staged = _staged()
    descriptor = staged.tables[0].descriptor
    with pytest.raises(MappingValidationError, match="requires a subtype"):
        MappingContract(
            table_locator=descriptor.table_locator,
            record_kind=RecordKind.FINANCIAL,
            financial_subtype=None,
            schema_fingerprint=descriptor.schema_fingerprint,
            field_rules=(FieldRule(target_field="memo", source_columns=("memo",)),),
        )
    with pytest.raises(MappingValidationError, match="forbids"):
        MappingContract(
            table_locator=descriptor.table_locator,
            record_kind=RecordKind.GENERIC_DOCUMENT,
            financial_subtype=FinancialSubtype.OTHER_FINANCIAL,
            schema_fingerprint=descriptor.schema_fingerprint,
            field_rules=(FieldRule(target_field="memo", source_columns=("memo",)),),
        )
    with pytest.raises(MappingValidationError, match="explicit date style"):
        FieldRule(
            target_field="date",
            source_columns=("date",),
            parser=FieldParser.DATE,
        )


def test_stale_schema_is_rejected_before_mapping() -> None:
    staged = _staged()
    mapping = _financial_mapping(staged)
    stale = MappingContract(
        table_locator=mapping.table_locator,
        record_kind=mapping.record_kind,
        financial_subtype=mapping.financial_subtype,
        schema_fingerprint="f" * 64,
        field_rules=mapping.field_rules,
        required_fields=mapping.required_fields,
    )
    with pytest.raises(MappingValidationError, match="stale schema"):
        preview_mapping(stale, staged.tables[0])
