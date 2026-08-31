from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from clerksan.api.main import create_app
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.models import (
    Document,
    DocumentFile,
    DocumentStatus,
    ExtractedRecord,
    ExtractionBatch,
    FileKind,
    IntakeIntent,
    SourceIntake,
    SourceIntakeState,
    VerifiedRecord,
)
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, ExtractedTable, NormalizedDocument


@pytest.fixture
def mapping_client(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'mapping-api.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        yield client, settings


async def _seed_mapping_source(settings: Settings) -> UUID:
    normalized = NormalizedDocument(
        markdown_body="",
        metadata=DocMetadata(
            filename="records.csv",
            detected_type=FileType.CSV,
            sha256="a" * 64,
        ),
        tables=[
            ExtractedTable(
                header=["date", "amount", "memo"],
                rows=[
                    ["2026-08-01", "100", "<b>literal</b>"],
                    ["2026-08-02", "200", "second"],
                ],
                source_location="transactions",
            ),
            ExtractedTable(
                header=["note"],
                rows=[["not financial"]],
                source_location="notes",
            ),
        ],
        embeddable=False,
    )
    encoded = normalized.model_dump_json().encode("utf-8")
    normalized_path = settings.storage_dir / "normalized" / "source.json"
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_bytes(encoded)

    async with get_session(settings) as session:
        document = Document(
            source_filename="records.csv",
            status=DocumentStatus.UPLOADED,
        )
        original = DocumentFile(
            document=document,
            version=1,
            kind=FileKind.ORIGINAL,
            content_path="originals/records.csv",
            sha256="a" * 64,
            mime="text/csv",
            source_filename="records.csv",
        )
        session.add_all((document, original))
        await session.flush()
        intake = SourceIntake(
            document_id=document.id,
            source_file_id=original.id,
            source_version=1,
            source_sha256=original.sha256,
            intake_intent=IntakeIntent.GENERIC_FILE,
            state=SourceIntakeState.NEEDS_MAPPING,
        )
        normalized_artifact = DocumentFile(
            document_id=document.id,
            version=2,
            kind=FileKind.NORMALIZED,
            source_file_id=original.id,
            source_version=1,
            content_path="normalized/source.json",
            sha256=hashlib.sha256(encoded).hexdigest(),
            mime="application/json",
            source_filename="records.csv",
            ocr_text="",
            text_provenance="normalized_document:source_version:1",
        )
        session.add_all((intake, normalized_artifact))
        await session.flush()
        return document.id


def _mapping_body(source: dict, descriptor: dict) -> dict:
    return {
        "source": source,
        "idempotency_key": "mapping-1",
        "table_locator": descriptor["table_locator"],
        "schema_fingerprint": descriptor["schema_fingerprint"],
        "record_kind": "financial",
        "financial_subtype": "transaction",
        "field_rules": [
            {"target_field": "date", "source_columns": ["date"]},
            {"target_field": "amount", "source_columns": ["amount"]},
            {"target_field": "memo", "source_columns": ["memo"]},
        ],
        "required_fields": ["date", "amount"],
        "created_by": "reviewer",
    }


def _set_body(source: dict, descriptors: list[dict], mapping: dict) -> dict:
    return {
        "source": source,
        "idempotency_key": "mapping-set-1",
        "created_by": "reviewer",
        "entries": [
            {
                "table_locator": descriptors[0]["table_locator"],
                "schema_fingerprint": descriptors[0]["schema_fingerprint"],
                "mapping_id": mapping["id"],
                "mapping_version": mapping["mapping_version"],
            },
            {
                "table_locator": descriptors[1]["table_locator"],
                "schema_fingerprint": descriptors[1]["schema_fingerprint"],
                "ignore_reason": "not a transaction table",
            },
        ],
    }


def test_mapping_preview_create_and_apply_are_exact_and_idempotent(mapping_client) -> None:
    client, settings = mapping_client
    document_id = asyncio.run(_seed_mapping_source(settings))

    descriptors_response = client.get(f"/documents/{document_id}/schema-descriptors")
    assert descriptors_response.status_code == 200
    descriptors_body = descriptors_response.json()
    source = descriptors_body["source"]
    descriptors = descriptors_body["descriptors"]
    assert [item["row_count"] for item in descriptors] == [2, 1]

    created = client.post(
        f"/documents/{document_id}/mappings",
        json=_mapping_body(source, descriptors[0]),
    )
    assert created.status_code == 201, created.text
    mapping = created.json()
    replay = client.post(
        f"/documents/{document_id}/mappings",
        json=_mapping_body(source, descriptors[0]),
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == mapping["id"]

    set_body = _set_body(source, descriptors, mapping)
    preview = client.post(
        f"/documents/{document_id}/mapping-sets/preview",
        json=set_body,
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["candidate_count"] == 2
    assert preview_body["reconciliation_counts"]["explicit_ignore"] == 1
    assert preview_body["previews"][0]["rows"][0]["values"]["memo"] == (
        "&lt;b&gt;literal&lt;/b&gt;"
    )

    created_set = client.post(
        f"/documents/{document_id}/mapping-sets",
        json=set_body,
    )
    assert created_set.status_code == 201, created_set.text
    mapping_set = created_set.json()
    apply_body = {
        "source": source,
        "mapping_set_version": mapping_set["version"],
        "mapping_set_digest": mapping_set["set_digest"],
        "expected_mapping_versions": {mapping["id"]: mapping["mapping_version"]},
        "idempotency_key": "apply-job-1",
    }
    forged_provenance = client.post(
        f"/documents/{document_id}/mapping-sets/{mapping_set['id']}/apply",
        json={**apply_body, "producer": "client-controlled"},
    )
    assert forged_provenance.status_code == 422

    async def no_forged_batch() -> None:
        async with get_session(settings) as session:
            assert await session.scalar(select(func.count(ExtractionBatch.id))) == 0

    asyncio.run(no_forged_batch())

    applied = client.post(
        f"/documents/{document_id}/mapping-sets/{mapping_set['id']}/apply",
        json=apply_body,
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["candidate_count"] == 2
    assert applied.json()["lifecycle"] == "open"
    assert applied.json()["replayed"] is False

    apply_replay = client.post(
        f"/documents/{document_id}/mapping-sets/{mapping_set['id']}/apply",
        json=apply_body,
    )
    assert apply_replay.status_code == 201
    assert apply_replay.json()["id"] == applied.json()["id"]
    assert apply_replay.json()["replayed"] is True

    async def assertions() -> None:
        async with get_session(settings) as session:
            assert await session.scalar(select(func.count(ExtractionBatch.id))) == 1
            assert await session.scalar(select(func.count(ExtractedRecord.id))) == 2
            assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
            document = await session.get(Document, document_id)
            assert document is not None
            assert document.status is DocumentStatus.UPLOADED

    asyncio.run(assertions())


def test_stale_source_structure_and_mapping_version_are_typed_conflicts(
    mapping_client,
) -> None:
    client, settings = mapping_client
    document_id = asyncio.run(_seed_mapping_source(settings))
    descriptors_body = client.get(f"/documents/{document_id}/schema-descriptors").json()
    source = descriptors_body["source"]
    descriptors = descriptors_body["descriptors"]

    stale_source = dict(source)
    stale_source["structure_fingerprint"] = "f" * 64
    stale = client.post(
        f"/documents/{document_id}/mappings",
        json={**_mapping_body(source, descriptors[0]), "source": stale_source},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_structure_fingerprint"

    created = client.post(
        f"/documents/{document_id}/mappings",
        json=_mapping_body(source, descriptors[0]),
    ).json()
    incomplete = client.post(
        f"/documents/{document_id}/mapping-sets/preview",
        json={
            "source": source,
            "idempotency_key": "incomplete",
            "created_by": "reviewer",
            "entries": [
                {
                    "table_locator": descriptors[0]["table_locator"],
                    "schema_fingerprint": descriptors[0]["schema_fingerprint"],
                    "mapping_id": created["id"],
                    "mapping_version": created["mapping_version"],
                }
            ],
        },
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["code"] == "invalid_mapping"

    mapping_set = client.post(
        f"/documents/{document_id}/mapping-sets",
        json=_set_body(source, descriptors, created),
    ).json()
    stale_version = client.post(
        f"/documents/{document_id}/mapping-sets/{mapping_set['id']}/apply",
        json={
            "source": source,
            "mapping_set_version": mapping_set["version"],
            "mapping_set_digest": mapping_set["set_digest"],
            "expected_mapping_versions": {created["id"]: 2},
            "idempotency_key": "apply-stale",
        },
    )
    assert stale_version.status_code == 409
    assert stale_version.json()["code"] == "stale_mapping_version"
