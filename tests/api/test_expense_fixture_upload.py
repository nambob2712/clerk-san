from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clerksan.api.main import create_app
from clerksan.config import Settings
from eval.expense_documents import generate_expense_documents


def test_every_expense_fixture_uploads_through_the_live_api(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures"
    labels = generate_expense_documents(fixture_dir)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'expense-upload.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        uploaded_ids: list[str] = []
        for label in labels:
            path = fixture_dir / label["filename"]
            response = client.post(
                "/documents",
                files={"file": (path.name, path.read_bytes(), label["mime_type"])},
            )

            assert response.status_code == 202, label["id"]
            accepted = response.json()
            assert accepted["status"] == "uploaded"
            assert accepted["duplicate_of"] is None
            uploaded_ids.append(accepted["document_id"])

            detail = client.get(f"/documents/{accepted['document_id']}")
            assert detail.status_code == 200
            assert detail.json()["source_filename"] == path.name
            assert detail.json()["files"][0]["sha256"] == label["sha256"]

            original = client.get(f"/documents/{accepted['document_id']}/original")
            assert original.status_code == 200
            assert original.content == path.read_bytes()

        listing = client.get("/documents?limit=20")

    assert listing.status_code == 200
    assert len(uploaded_ids) == len(labels)
    assert {item["id"] for item in listing.json()["items"]} == set(uploaded_ids)
