from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from clerksan.api.deps import database_session
from clerksan.api.routes import query
from clerksan.config import Settings
from clerksan.query.router import Route
from clerksan.query.service import Answer
from clerksan.search.indexer import Hit


def test_query_route_maps_local_answer_to_http_contract(monkeypatch, tmp_path: Path) -> None:
    app = FastAPI()
    app.state.settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'query-api.sqlite'}",
        demo_mode=True,
    )

    async def fake_session():
        yield object()

    async def fake_answer(question, session, client, models):
        del question, session, client, models
        return Answer(
            text="Found it.",
            mode=Route.SEMANTIC,
            citations=[
                Hit(
                    document_id=uuid4(),
                    chunk_seq=0,
                    heading_path="Gift",
                    text="needle",
                    distance=0.1,
                )
            ],
        )

    app.dependency_overrides[database_session] = fake_session
    monkeypatch.setattr(query, "answer", fake_answer)
    app.include_router(query.router)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/query", json={"question": "Find the gift receipt"})
        empty = client.post("/query", json={"question": "   "})

    assert response.status_code == 200
    assert response.json()["mode"] == "semantic"
    assert response.json()["citations"][0]["snippet"] == "needle"
    assert empty.status_code == 422
