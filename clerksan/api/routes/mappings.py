"""Exact-source schema mapping, bounded preview, and immutable batch APIs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.api.deps import database_session, settings_from_request
from clerksan.api.schemas import (
    ExtractionBatchOut,
    MappingCreateIn,
    MappingFieldRuleIn,
    MappingOut,
    MappingPreviewOut,
    MappingPreviewRowOut,
    MappingSetApplyIn,
    MappingSetDraftIn,
    MappingSetEntryOut,
    MappingSetOut,
    MappingSetPreviewOut,
    MappingSourceRef,
    MappingsOut,
    SchemaDescriptorOut,
    SchemaDescriptorsOut,
)
from clerksan.config import Settings
from clerksan.db.models import DocumentFile, FileKind, SchemaMapping
from clerksan.db.repositories import (
    ExactMappingSource,
    ExtractionBatchRepo,
    ExtractionBatchSummary,
    MappingConflictError,
    MappingSetRepo,
    MappingSetSnapshot,
    MappingSourceRepo,
    SchemaMappingRepo,
)
from clerksan.ingest.jobs import enqueue
from clerksan.ingest.limits import ResourceLimitExceeded
from clerksan.ingest.mapping import (
    FieldRule,
    MappingContract,
    MappingPreview,
    MappingSetContract,
    MappingSetEntryContract,
    apply_mapping_set,
    preview_mapping,
)
from clerksan.ingest.normalized import NormalizedDocument
from clerksan.ingest.staging import StagedStructure, build_staged_structure
from clerksan.storage import StoragePathError, resolve_storage_path, verify_artifact_bytes

router = APIRouter(tags=["mappings"])

_MAPPING_NAMESPACE = UUID("c3951f4f-6c33-40ad-9bde-7db3d20d3ed1")
_MAPPING_SET_NAMESPACE = UUID("48fb7cfb-4c7c-4b7a-b5dd-bdd68ff59ec5")


@dataclass(frozen=True, slots=True)
class _StagedSource:
    source: ExactMappingSource
    staged: StagedStructure

    @property
    def reference(self) -> MappingSourceRef:
        return MappingSourceRef(
            source_intake_id=self.source.intake_id,
            source_file_id=self.source.source_file_id,
            source_version=self.source.source_version,
            source_sha256=self.source.source_sha256,
            normalized_sha256=self.staged.normalized_sha256,
            structure_fingerprint=self.staged.structure_fingerprint,
        )


@router.get(
    "/documents/{document_id}/schema-descriptors",
    response_model=SchemaDescriptorsOut,
)
async def schema_descriptors(
    document_id: UUID,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> SchemaDescriptorsOut:
    loaded = await _load_staged_source(document_id, session, settings)
    return SchemaDescriptorsOut(
        document_id=document_id,
        source=loaded.reference,
        descriptors=[
            SchemaDescriptorOut(
                table_locator=table.descriptor.table_locator,
                ordered_headers=list(table.descriptor.ordered_headers),
                inferred_types=[value.value for value in table.descriptor.inferred_types],
                row_count=table.descriptor.row_count,
                schema_fingerprint=table.descriptor.schema_fingerprint,
            )
            for table in loaded.staged.tables
        ],
    )


@router.get("/documents/{document_id}/mappings", response_model=MappingsOut)
async def list_mappings(
    document_id: UUID,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> MappingsOut:
    loaded = await _load_staged_source(document_id, session, settings)
    current_contracts = {
        (table.descriptor.table_locator, table.descriptor.schema_fingerprint)
        for table in loaded.staged.tables
    }
    records = await SchemaMappingRepo(session).list_for_schemas(
        {table.descriptor.schema_fingerprint for table in loaded.staged.tables}
    )
    records = [
        record
        for record in records
        if (record.table_locator, record.schema_fingerprint) in current_contracts
    ]
    return MappingsOut(
        document_id=document_id,
        source=loaded.reference,
        items=[await _mapping_out(session, record) for record in records],
    )


@router.post(
    "/documents/{document_id}/mappings",
    response_model=MappingOut,
    status_code=201,
)
async def create_mapping(
    document_id: UUID,
    body: MappingCreateIn,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> MappingOut:
    loaded = await _load_staged_source(
        document_id,
        session,
        settings,
        expected=body.source,
    )
    descriptor = next(
        (
            table.descriptor
            for table in loaded.staged.tables
            if table.descriptor.table_locator == body.table_locator
        ),
        None,
    )
    if descriptor is None or descriptor.schema_fingerprint != body.schema_fingerprint:
        raise MappingConflictError(
            "stale_schema_fingerprint",
            "The selected table schema changed after it was displayed.",
            detail={"table_locator": body.table_locator},
        )
    mapping_id = uuid5(
        _MAPPING_NAMESPACE,
        f"{loaded.source.intake_id}:{body.idempotency_key}",
    )
    contract = MappingContract(
        table_locator=body.table_locator,
        record_kind=body.record_kind,
        financial_subtype=body.financial_subtype,
        schema_fingerprint=body.schema_fingerprint,
        field_rules=tuple(_field_rule(rule) for rule in body.field_rules),
        required_fields=tuple(body.required_fields),
        mapping_version=body.mapping_version,
        mapping_id=mapping_id,
    )
    record = await SchemaMappingRepo(session).create(
        document_id,
        contract,
        created_by=body.created_by,
        source_intake_id=loaded.source.intake_id,
        source_file_id=loaded.source.source_file_id,
        source_version=loaded.source.source_version,
        source_sha256=loaded.source.source_sha256,
    )
    return await _mapping_out(session, record)


@router.post(
    "/documents/{document_id}/mapping-sets/preview",
    response_model=MappingSetPreviewOut,
)
async def preview_mapping_set(
    document_id: UUID,
    body: MappingSetDraftIn,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> MappingSetPreviewOut:
    loaded = await _load_staged_source(
        document_id,
        session,
        settings,
        expected=body.source,
    )
    contract = await _mapping_set_contract(session, loaded, body)
    application = apply_mapping_set(
        contract,
        loaded.staged,
        source_sha256=loaded.source.source_sha256,
    )
    tables = {table.descriptor.table_locator: table for table in loaded.staged.tables}
    previews = [
        preview_mapping(entry.mapping, tables[entry.table_locator], limit=body.preview_limit)
        for entry in contract.entries
        if entry.mapping is not None
    ]
    return MappingSetPreviewOut(
        document_id=document_id,
        source=loaded.reference,
        previews=[_preview_out(preview) for preview in previews],
        reconciliation_counts=application.ledger.reconciliation_counts,
        candidate_count=len(application.candidates),
    )


@router.post(
    "/documents/{document_id}/mapping-sets",
    response_model=MappingSetOut,
    status_code=201,
)
async def create_mapping_set(
    document_id: UUID,
    body: MappingSetDraftIn,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> MappingSetOut:
    loaded = await _load_staged_source(
        document_id,
        session,
        settings,
        expected=body.source,
    )
    contract = await _mapping_set_contract(session, loaded, body)
    # Full application is a deterministic preflight here: every row must reconcile
    # before the immutable mapping-set definition is persisted.
    apply_mapping_set(
        contract,
        loaded.staged,
        source_sha256=loaded.source.source_sha256,
    )
    snapshot = await MappingSetRepo(session).create(
        document_id,
        contract,
        source_intake_id=loaded.source.intake_id,
        source_sha256=loaded.source.source_sha256,
    )
    return _mapping_set_out(snapshot, loaded.reference)


@router.post(
    "/documents/{document_id}/mapping-sets/{mapping_set_id}/apply",
    response_model=ExtractionBatchOut,
    status_code=201,
)
async def apply_confirmed_mapping_set(
    document_id: UUID,
    mapping_set_id: UUID,
    body: MappingSetApplyIn,
    request: Request,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> ExtractionBatchOut:
    loaded = await _load_staged_source(
        document_id,
        session,
        settings,
        expected=body.source,
    )
    snapshot = await MappingSetRepo(session).get(
        document_id,
        mapping_set_id,
        expected_version=body.mapping_set_version,
        expected_digest=body.mapping_set_digest,
    )
    _verify_mapping_set_source(snapshot, loaded)
    actual_versions = {
        entry.mapping.mapping_id: entry.mapping.mapping_version
        for entry in snapshot.contract.entries
        if entry.mapping is not None
    }
    if any(version < 1 for version in body.expected_mapping_versions.values()) or (
        body.expected_mapping_versions != actual_versions
    ):
        raise MappingConflictError(
            "stale_mapping_version",
            "The mapping-version vector changed after it was displayed.",
            detail={
                "expected": {
                    str(key): value for key, value in body.expected_mapping_versions.items()
                },
                "current": {str(key): value for key, value in actual_versions.items()},
            },
        )
    application = apply_mapping_set(
        snapshot.contract,
        loaded.staged,
        source_sha256=loaded.source.source_sha256,
    )
    summary = await ExtractionBatchRepo(session).add_mapping_batch(
        document_id,
        source_intake_id=loaded.source.intake_id,
        source_file_id=loaded.source.source_file_id,
        source_version=loaded.source.source_version,
        source_sha256=loaded.source.source_sha256,
        normalized_sha256=loaded.staged.normalized_sha256,
        structure_fingerprint=loaded.staged.structure_fingerprint,
        mapping_set=snapshot,
        application=application,
        producer="mapping-engine",
        producer_version="1",
        origin="confirmed_mapping",
        idempotency_key=body.idempotency_key,
        producer_job_id=None,
    )
    if summary.candidate_count:
        await enqueue(
            session,
            job_type="index_candidate_batch",
            payload={
                "document_id": str(summary.document_id),
                "batch_id": str(summary.id),
                "source_intake_id": str(summary.source_intake_id),
                "source_file_id": str(summary.source_file_id),
                "source_version": summary.source_version,
                "normalized_sha256": summary.normalized_sha256,
            },
            idempotency_key=(f"candidate-index:{summary.id}:{summary.normalized_sha256}"),
            settings=settings,
            required_components=_index_requirements(settings),
            capability_registry=getattr(
                request.app.state,
                "capability_registry",
                None,
            ),
        )
    return _batch_out(summary)


async def _load_staged_source(
    document_id: UUID,
    session: AsyncSession,
    settings: Settings,
    *,
    expected: MappingSourceRef | None = None,
) -> _StagedSource:
    sources = MappingSourceRepo(session)
    source = (
        await sources.current(document_id)
        if expected is None
        else await sources.require_current(
            document_id,
            source_intake_id=expected.source_intake_id,
            source_file_id=expected.source_file_id,
            source_version=expected.source_version,
            source_sha256=expected.source_sha256,
        )
    )
    artifact = await session.scalar(
        select(DocumentFile)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.NORMALIZED,
            DocumentFile.source_file_id == source.source_file_id,
            DocumentFile.source_version == source.source_version,
        )
        .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
        .limit(1)
    )
    if artifact is None:
        raise MappingConflictError(
            "mapping_not_ready",
            "The current source has no normalized structure to map yet.",
        )
    try:
        path = resolve_storage_path(settings.storage_dir, artifact.content_path)
        encoded = await asyncio.to_thread(
            _read_bounded,
            path,
            settings.max_normalized_output_bytes,
        )
        normalized = NormalizedDocument.model_validate_json(
            verify_artifact_bytes(encoded, artifact.sha256)
        )
    except (OSError, StoragePathError, ValidationError) as error:
        raise MappingConflictError(
            "mapping_not_ready",
            "The normalized structure cannot be loaded safely.",
        ) from error
    if normalized.metadata.sha256 != source.source_sha256:
        raise MappingConflictError(
            "stale_normalized_digest",
            "The normalized structure is bound to a different source checksum.",
        )
    row_count = sum(len(table.rows) for table in normalized.tables)
    cell_count = sum(len(row) for table in normalized.tables for row in table.rows)
    if row_count > settings.max_tabular_rows:
        raise ResourceLimitExceeded("max_tabular_rows", settings.max_tabular_rows, row_count)
    if cell_count > settings.max_tabular_cells:
        raise ResourceLimitExceeded("max_tabular_cells", settings.max_tabular_cells, cell_count)
    staged = build_staged_structure(
        normalized,
        source_file_id=source.source_file_id,
        source_version=source.source_version,
    )
    loaded = _StagedSource(source=source, staged=staged)
    if expected is not None and (
        expected.normalized_sha256 != staged.normalized_sha256
        or expected.structure_fingerprint != staged.structure_fingerprint
    ):
        raise MappingConflictError(
            "stale_structure_fingerprint",
            "The normalized structure changed after it was displayed.",
            detail={
                "current_normalized_sha256": staged.normalized_sha256,
                "current_structure_fingerprint": staged.structure_fingerprint,
            },
        )
    return loaded


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        encoded = source.read(limit + 1)
    if len(encoded) > limit:
        raise ResourceLimitExceeded("max_normalized_output_bytes", limit, len(encoded))
    return encoded


def _index_requirements(settings: Settings) -> tuple[str, ...]:
    if not settings.embed_model:
        return ()
    return (f"model:{settings.embed_model}",)


async def _mapping_set_contract(
    session: AsyncSession,
    loaded: _StagedSource,
    body: MappingSetDraftIn,
) -> MappingSetContract:
    mapping_repo = SchemaMappingRepo(session)
    entries: list[MappingSetEntryContract] = []
    for entry in body.entries:
        contract: MappingContract | None = None
        if entry.mapping_id is not None:
            assert entry.mapping_version is not None
            _, contract = await mapping_repo.get_contract(
                entry.mapping_id,
                expected_version=entry.mapping_version,
            )
        entries.append(
            MappingSetEntryContract(
                table_locator=entry.table_locator,
                schema_fingerprint=entry.schema_fingerprint,
                mapping=contract,
                ignore_reason=entry.ignore_reason,
            )
        )
    mapping_set_id = uuid5(
        _MAPPING_SET_NAMESPACE,
        f"{loaded.source.intake_id}:{body.idempotency_key}",
    )
    return MappingSetContract(
        source_file_id=loaded.source.source_file_id,
        source_version=loaded.source.source_version,
        structure_fingerprint=loaded.staged.structure_fingerprint,
        entries=tuple(entries),
        created_by=body.created_by,
        mapping_set_id=mapping_set_id,
    )


def _field_rule(body: MappingFieldRuleIn) -> FieldRule:
    return FieldRule(
        target_field=body.target_field,
        source_columns=tuple(body.source_columns),
        literal=body.literal,
        separator=body.separator,
        trim=body.trim,
        null_markers=tuple(body.null_markers),
        value_map=tuple(body.value_map),
        parser=body.parser,
        date_style=body.date_style,
        decimal_style=body.decimal_style,
        sign_rule=body.sign_rule,
        currency_aliases=tuple(body.currency_aliases),
    )


async def _mapping_out(session: AsyncSession, record: SchemaMapping) -> MappingOut:
    _, contract = await SchemaMappingRepo(session).get_contract(
        record.id,
        expected_version=record.mapping_version,
    )
    return MappingOut(
        id=record.id,
        table_locator=record.table_locator,
        schema_fingerprint=record.schema_fingerprint,
        record_kind=record.record_kind,
        financial_subtype=record.financial_subtype,
        field_rules=[MappingFieldRuleIn(**_field_rule_dict(rule)) for rule in contract.field_rules],
        required_fields=list(contract.required_fields),
        mapping_version=record.mapping_version,
        mapping_digest=record.mapping_digest,
        created_by=record.created_by,
        created_at=record.created_at,
    )


def _field_rule_dict(rule: FieldRule) -> dict[str, object]:
    return {
        "target_field": rule.target_field,
        "source_columns": list(rule.source_columns),
        "literal": rule.literal,
        "separator": rule.separator,
        "trim": rule.trim,
        "null_markers": list(rule.null_markers),
        "value_map": list(rule.value_map),
        "parser": rule.parser,
        "date_style": rule.date_style,
        "decimal_style": rule.decimal_style,
        "sign_rule": rule.sign_rule,
        "currency_aliases": list(rule.currency_aliases),
    }


def _preview_out(preview: MappingPreview) -> MappingPreviewOut:
    return MappingPreviewOut(
        table_locator=preview.table_locator,
        rows=[
            MappingPreviewRowOut(
                row_ordinal=row.row_ordinal,
                source_locator=row.source_locator,
                values=dict(row.values),
                errors=list(row.errors),
            )
            for row in preview.rows
        ],
        total_rows=preview.total_rows,
        valid_rows=preview.valid_rows,
        error_rows=preview.error_rows,
        blank_rows=preview.blank_rows,
        truncated=preview.truncated,
    )


def _mapping_set_out(
    snapshot: MappingSetSnapshot,
    source: MappingSourceRef,
) -> MappingSetOut:
    return MappingSetOut(
        id=snapshot.id,
        document_id=snapshot.document_id,
        source=source,
        set_digest=snapshot.set_digest,
        version=snapshot.version,
        created_by=snapshot.created_by,
        created_at=snapshot.created_at,
        entries=[
            MappingSetEntryOut(
                ordinal=ordinal,
                table_locator=entry.table_locator,
                schema_fingerprint=entry.schema_fingerprint,
                mapping_id=entry.mapping.mapping_id if entry.mapping else None,
                mapping_version=entry.mapping.mapping_version if entry.mapping else None,
                ignore_reason=entry.ignore_reason,
            )
            for ordinal, entry in enumerate(snapshot.contract.entries)
        ],
    )


def _verify_mapping_set_source(
    snapshot: MappingSetSnapshot,
    loaded: _StagedSource,
) -> None:
    if (
        snapshot.source_intake_id != loaded.source.intake_id
        or snapshot.source_file_id != loaded.source.source_file_id
        or snapshot.source_version != loaded.source.source_version
        or snapshot.source_sha256 != loaded.source.source_sha256
        or snapshot.structure_fingerprint != loaded.staged.structure_fingerprint
    ):
        raise MappingConflictError(
            "stale_mapping_set_source",
            "The mapping set is not bound to the current exact source.",
            detail={"mapping_set_id": str(snapshot.id)},
        )


def _batch_out(summary: ExtractionBatchSummary) -> ExtractionBatchOut:
    return ExtractionBatchOut(
        id=summary.id,
        document_id=summary.document_id,
        source_intake_id=summary.source_intake_id,
        source_file_id=summary.source_file_id,
        source_version=summary.source_version,
        source_sha256=summary.source_sha256,
        normalized_sha256=summary.normalized_sha256,
        structure_fingerprint=summary.structure_fingerprint,
        mapping_set_id=summary.mapping_set_id,
        mapping_set_version=summary.mapping_set_version,
        mapping_set_digest=summary.mapping_set_digest,
        lifecycle=summary.lifecycle.value,
        candidate_count=summary.candidate_count,
        reconciliation_counts=summary.reconciliation_counts,
        reconciliation_digest=summary.reconciliation_digest,
        version=summary.version,
        replayed=summary.replayed,
    )
