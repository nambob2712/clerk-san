from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from clerksan.api.main import create_app
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.models import SourceIntake
from clerksan.db.repositories import DocumentRepo
from clerksan.ingest.jobs import enqueue


@pytest.fixture
def intake_client(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'intakes.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        yield client, settings


async def _seed_intake(settings: Settings) -> tuple[UUID, UUID, UUID]:
    async with get_session(settings) as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="transactions.csv",
            content_path="originals/transactions.csv",
            sha256="a" * 64,
            mime="text/csv",
        )
        intake = await session.scalar(
            select(SourceIntake).where(SourceIntake.document_id == document_id)
        )
        assert intake is not None
        job_id = await enqueue(
            session,
            job_type="process_document",
            payload={
                "document_id": str(document_id),
                "source_file_id": str(intake.source_file_id),
                "source_intake_id": str(intake.id),
                "source_version": intake.source_version,
            },
            idempotency_key=f"test-intake-{uuid4()}",
            settings=settings,
        )
        assert job_id is not None
        return intake.id, document_id, job_id


def test_exact_source_status_includes_immutable_identity_and_job(intake_client) -> None:
    client, settings = intake_client
    intake_id, document_id, job_id = asyncio.run(_seed_intake(settings))

    response = client.get(f"/intakes/{intake_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["intake_id"] == str(intake_id)
    assert body["document_id"] == str(document_id)
    assert body["source_version"] == 1
    assert body["source_sha256"] == "a" * 64
    assert body["intake_intent"] == "legacy_unspecified"
    assert body["detected_format"] is None
    assert body["state"] == "queued"
    assert body["reason_code"] == "processing_queued"
    assert body["version"] == 1
    assert body["job_reference"] == {
        "job_id": str(job_id),
        "job_type": "process_document",
        "status": "queued",
    }


def test_unknown_intake_is_404_and_polling_is_not_conflict(intake_client) -> None:
    client, settings = intake_client
    intake_id, _, _ = asyncio.run(_seed_intake(settings))

    first = client.get(f"/intakes/{intake_id}")
    second = client.get(f"/intakes/{intake_id}")
    missing = client.get(f"/intakes/{uuid4()}")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert missing.status_code == 404
    assert missing.json()["code"] == "source_intake_not_found"


def test_recent_intakes_are_bounded_and_exact_source_rehydratable(intake_client) -> None:
    client, settings = intake_client
    first, _, _ = asyncio.run(_seed_intake(settings))
    second, _, _ = asyncio.run(_seed_intake(settings))

    response = client.get("/intakes", params={"limit": 1})

    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]["intake_id"] in {str(first), str(second)}
    assert items[0]["source_file_id"]
    assert items[0]["source_sha256"] == "a" * 64
