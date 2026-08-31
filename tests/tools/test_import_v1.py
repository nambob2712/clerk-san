from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from clerksan.config import Settings
from clerksan.db.engine import dispose_engines, get_session
from clerksan.db.models import Document, DocumentFile, ExtractedRecord
from clerksan.tools.import_v1 import import_v1_data


@pytest.mark.asyncio
async def test_import_v1_synthetic_history_is_idempotent_and_keeps_missing_image_evidence(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "synthetic_receipt_history.csv"
    csv_path.write_text(
        "Timestamp,Date,Total_Amount,Currency,Category,Merchant,Image_Path,ID,Line_Items\n"
        "2026-01-01T00:00:00,2025-12-01,100,JPY,Food,Synthetic Missing Shop A,"
        "saved_receipts\\missing-a.png,legacy-synthetic-a,[]\n"
        "2026-01-02T00:00:00,2025-12-02,200,JPY,Transport,Synthetic Missing Shop B,"
        "saved_receipts\\missing-b.png,legacy-synthetic-b,[]\n",
        encoding="utf-8",
    )
    images = tmp_path / "saved_receipts"
    images.mkdir()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'import.db'}",
        storage_dir=tmp_path / "store",
        demo_mode=True,
    )

    try:
        assert await import_v1_data(csv_path, images, settings=settings) == {
            "imported": 2,
            "skipped": 0,
            "missing_images": 2,
        }
        assert await import_v1_data(csv_path, images, settings=settings) == {
            "imported": 0,
            "skipped": 2,
            "missing_images": 0,
        }
        async with get_session(settings) as session:
            documents = (await session.scalars(select(Document))).all()
            files = (await session.scalars(select(DocumentFile))).all()
            records = (await session.scalars(select(ExtractedRecord))).all()
        assert len(documents) == len(files) == len(records) == 2
        assert all(document.status.value == "in_review" for document in documents)
        assert all(record.status.value == "pending_review" for record in records)
        missing = [record for record in records if record.payload.get("_import_flags")]
        assert len(missing) == 2
        assert all(record.payload["_import_flags"] == ["missing_image"] for record in missing)

        present = b"present image bytes"
        (images / "present.png").write_bytes(present)
        present_csv = tmp_path / "present_receipt_history.csv"
        present_csv.write_text(
            "Timestamp,Date,Total_Amount,Currency,Category,Merchant,Image_Path,ID,Line_Items\n"
            "2026-01-03T00:00:00,2025-12-03,300,JPY,Food,Synthetic Present Shop,"
            "saved_receipts\\present.png,legacy-present,[]\n",
            encoding="utf-8",
        )
        assert await import_v1_data(present_csv, images, settings=settings) == {
            "imported": 1,
            "skipped": 0,
            "missing_images": 0,
        }
        async with get_session(settings) as session:
            present_file = await session.scalar(
                select(DocumentFile).where(DocumentFile.source_filename == "present.png")
            )
        assert present_file is not None
        assert present_file.sha256 == hashlib.sha256(present).hexdigest()
    finally:
        await dispose_engines()
