from __future__ import annotations

import hashlib
import io
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db.models import Base, Document, DocumentFile, ExtractedRecord, FileKind
from clerksan.dedupe.detector import find_duplicates


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dedupe.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _document(
    session,
    *,
    filename: str,
    content_path: str,
    content: bytes,
    mime: str = "application/octet-stream",
) -> UUID:
    document = Document(source_filename=filename)
    session.add(document)
    await session.flush()
    session.add(
        DocumentFile(
            document_id=document.id,
            version=1,
            kind=FileKind.ORIGINAL,
            content_path=content_path,
            sha256=hashlib.sha256(content).hexdigest(),
            mime=mime,
            source_filename=filename,
        )
    )
    await session.flush()
    return document.id


async def _extraction(
    session,
    document_id: UUID,
    *,
    transaction_date: str,
    total_amount: int,
    counterparty: str,
) -> None:
    source = await session.scalar(
        select(DocumentFile)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
        )
        .order_by(DocumentFile.version.desc())
        .limit(1)
    )
    assert source is not None
    session.add(
        ExtractedRecord(
            document_id=document_id,
            source_file_id=source.id,
            source_version=source.version,
            payload={
                "transaction_date": {"value": transaction_date},
                "total_amount": {"value": total_amount},
                "counterparty": {"value": counterparty},
            },
            field_confidences={},
            source_spans={},
            model_name="test-model",
            prompt_version="test",
        )
    )
    await session.flush()


def _receipt_image(*, image_format: str, quality: int | None = None) -> bytes:
    image = Image.new("RGB", (128, 128), color="white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((20, 20, 108, 108), outline="black", width=5)
    drawing.line((20, 108, 108, 20), fill="navy", width=4)
    output = io.BytesIO()
    options = {"quality": quality} if quality is not None else {}
    image.save(output, format=image_format, **options)
    image.close()
    return output.getvalue()


async def test_exact_checksum_is_flagged_without_deleting_either_document(session_factory) -> None:
    async with session_factory() as session:
        target = await _document(
            session,
            filename="first.png",
            content_path="first.png",
            content=b"identical-upload",
        )
        duplicate = await _document(
            session,
            filename="second.png",
            content_path="second.png",
            content=b"identical-upload",
        )
        await session.commit()

        candidates = await find_duplicates(session, target)
        document_count = await session.scalar(select(func.count()).select_from(Document))

    assert candidates == [
        {
            "document_id": str(duplicate),
            "reason": "exact_sha256",
            "reasons": ["exact_sha256"],
            "score": 1.0,
            "evidence": {
                "exact_sha256": {"sha256": [hashlib.sha256(b"identical-upload").hexdigest()]}
            },
        }
    ]
    assert document_count == 2


async def test_reencoded_image_is_flagged_by_perceptual_hash(session_factory) -> None:
    png = _receipt_image(image_format="PNG")
    jpeg = _receipt_image(image_format="JPEG", quality=75)
    artifacts = {"first.png": png, "second.jpg": jpeg}

    async def read_bytes(path: str) -> bytes:
        return artifacts[path]

    async with session_factory() as session:
        target = await _document(
            session,
            filename="first.png",
            content_path="first.png",
            content=png,
            mime="image/png",
        )
        duplicate = await _document(
            session,
            filename="second.jpg",
            content_path="second.jpg",
            content=jpeg,
            mime="image/jpeg",
        )
        await session.commit()

        candidates = await find_duplicates(session, target, read_bytes=read_bytes)

    assert len(candidates) == 1
    assert candidates[0]["document_id"] == str(duplicate)
    assert candidates[0]["reason"] == "phash"
    assert candidates[0]["evidence"]["phash"]["distance"] <= 8


async def test_business_triple_flags_same_amount_and_vendor_but_not_different_amount(
    session_factory,
) -> None:
    async with session_factory() as session:
        target = await _document(
            session,
            filename="target.json",
            content_path="target.json",
            content=b"target",
        )
        matching = await _document(
            session,
            filename="matching.json",
            content_path="matching.json",
            content=b"matching",
        )
        different_amount = await _document(
            session,
            filename="different.json",
            content_path="different.json",
            content=b"different",
        )
        await _extraction(
            session,
            target,
            transaction_date=date(2026, 7, 13).isoformat(),
            total_amount=1200,
            counterparty="サンプル商店",
        )
        await _extraction(
            session,
            matching,
            transaction_date=date(2026, 7, 14).isoformat(),
            total_amount=1200,
            counterparty="サンプル商店",
        )
        await _extraction(
            session,
            different_amount,
            transaction_date=date(2026, 7, 13).isoformat(),
            total_amount=1300,
            counterparty="サンプル商店",
        )
        await session.commit()

        candidates = await find_duplicates(session, target)

    assert len(candidates) == 1
    assert candidates[0]["document_id"] == str(matching)
    assert candidates[0]["reason"] == "fuzzy"
    assert candidates[0]["evidence"]["fuzzy"] == {
        "date_delta_days": 1,
        "total_amount": "1200",
        "counterparty_similarity": 100.0,
    }
