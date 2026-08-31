from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from clerksan.api.main import create_app
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.repositories import DocumentRepo


async def _seed_original(
    settings: Settings,
    *,
    filename: str,
    mime: str,
    content: bytes,
) -> tuple[UUID, str]:
    digest = hashlib.sha256(content).hexdigest()
    relative_path = f"originals/{digest}"
    target = settings.storage_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    async with get_session(settings) as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename=filename,
            content_path=relative_path,
            sha256=digest,
            mime=mime,
        )
    return document_id, digest


@pytest.mark.parametrize(
    ("filename", "mime", "content", "disposition"),
    (
        ("safe.png", "image/png", b"synthetic-png", "inline"),
        ("source.pdf", "application/pdf", b"%PDF-1.4\nsynthetic\n%%EOF", "attachment"),
        ("records.csv", "text/csv", b"a,b\n1,2\n", "attachment"),
    ),
)
def test_original_header_and_disposition_matrix_is_source_bound(
    tmp_path: Path,
    filename: str,
    mime: str,
    content: bytes,
    disposition: str,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'headers.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        document_id, digest = asyncio.run(
            _seed_original(settings, filename=filename, mime=mime, content=content)
        )
        detail = client.get(f"/documents/{document_id}").json()
        source = next(item for item in detail["files"] if item["kind"] == "original")
        response = client.get(
            f"/documents/{document_id}/original"
            f"?version=1&source_file_id={source['id']}&sha256={digest}"
        )

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-disposition"].startswith(disposition)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'self'; sandbox"
    )
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_export_responses_are_attachment_only_and_not_cacheable(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'export-headers.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/export?format=freee&date_from=2026-01-01&date_to=2026-01-31")

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
