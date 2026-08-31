"""HTTP contracts for the mounted accounting and audit export routes."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

from fastapi.testclient import TestClient

from clerksan.api.main import create_app
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.models import AuditEntry
from clerksan.db.repositories import DocumentRepo, ExtractionRepo, VerifiedRepo


async def seed_export_data(settings: Settings) -> None:
    async with get_session(settings) as session:
        documents = DocumentRepo(session)
        verified_document_id = await documents.create_with_raw(
            filename="verified.png",
            content_path="/tmp/verified.png",
            sha256="e" * 64,
            mime="image/png",
        )
        verified_extraction_id = await ExtractionRepo(session).add(
            verified_document_id,
            payload={
                "transaction_date": {"value": "2026-07-13", "confidence": 0.99},
                "total_amount": {"value": 1200, "confidence": 0.99},
                "counterparty": {"value": "Verified Merchant", "confidence": 0.99},
                "currency": {"value": "JPY", "confidence": 0.99},
                "expense_category": {"value": "会議費", "confidence": 0.99},
                "tax_10_amount": {"value": 109, "confidence": 0.99},
            },
            field_confidences={},
            model_name="test",
            prompt_version="test",
            actor="worker",
        )
        await VerifiedRepo(session).promote(
            verified_extraction_id,
            1,
            corrections={},
            reviewer="reviewer",
        )
        session.add(
            AuditEntry(
                actor="reviewer",
                table_name="verified_records",
                row_pk="record-1",
                action="UPDATE",
                field="total_amount",
                old_value="100",
                new_value="120",
                at=dt.datetime(2026, 7, 13, 12, tzinfo=dt.UTC),
            )
        )


def test_export_and_audit_routes_are_mounted_and_download_safe_csv(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'export-route.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        asyncio.run(seed_export_data(settings))

        export = client.get("/export?format=freee&date_from=2026-07-01&date_to=2026-07-31")
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv; charset=utf-8")
        assert (
            'attachment; filename="clerksan_freee_20260701-20260731.csv"'
            in export.headers["content-disposition"]
        )
        assert "Verified Merchant" in export.content.decode("utf-8")

        audit = client.get("/export/audit?date_from=2026-07-13&date_to=2026-07-13")
        assert audit.status_code == 200
        assert audit.headers["content-type"].startswith("text/csv; charset=utf-8")
        assert "audit_id,at,actor" in audit.content.decode("utf-8")
