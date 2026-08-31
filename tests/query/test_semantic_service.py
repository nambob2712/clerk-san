from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from clerksan.query.router import Route
from clerksan.query.semantic_answerer import SemanticAnswer, SemanticSafetyError, answer_semantic
from clerksan.query.service import answer
from clerksan.query.sql_answerer import SqlAnswer
from clerksan.search.indexer import Hit, SearchDomain


class FakeModels:
    async def ensure_loaded(self, role) -> str:
        del role
        return "local-router"


class FakeClient:
    async def generate(self, model: str, prompt: str) -> str:
        assert model == "local-router"
        assert "needle" in prompt
        return "The receipt is the client gift source [1]."


async def _hits(session, question: str) -> list[Hit]:
    del session, question
    return [
        Hit(
            document_id=uuid4(),
            chunk_seq=0,
            heading_path="Gift",
            text="needle client gift",
            distance=0.1,
        )
    ]


@pytest.mark.asyncio
async def test_semantic_answer_is_cited_and_refuses_pure_arithmetic() -> None:
    semantic = await answer_semantic(
        "Find the client gift receipt",
        cast(Any, object()),
        FakeClient(),
        FakeModels(),
        searcher=_hits,
    )
    assert semantic.citations
    assert "Sources: [1]" in semantic.text
    with pytest.raises(SemanticSafetyError):
        await answer_semantic(
            "What is the total?",
            cast(Any, object()),
            FakeClient(),
            FakeModels(),
            searcher=_hits,
        )


@pytest.mark.asyncio
async def test_hybrid_uses_retrieved_document_ids_to_scope_sql() -> None:
    captured: dict[str, Any] = {}

    async def router(question, client, models):
        del question, client, models
        return Route.HYBRID

    async def semantic(
        question,
        session,
        client,
        models,
        *,
        allow_aggregation_context=False,
        search_domain=SearchDomain.ALL,
    ):
        del question, session, client, models
        assert allow_aggregation_context
        assert search_domain is SearchDomain.FINANCIAL
        return SemanticAnswer(text="source", citations=await _hits(None, ""))

    async def sql(question, repo, client, models, *, document_ids=None):
        del question, repo, client, models
        captured["document_ids"] = document_ids
        return SqlAnswer(
            text="Verified total: ¥1200.00",
            rows=[],
            template_id="verified_total",
            params={},
        )

    result = await answer(
        "How much at the shop where I bought the client gift?",
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        router=router,
        semantic_runner=semantic,
        sql_runner=sql,
    )

    assert result.mode is Route.HYBRID
    assert captured["document_ids"] == [result.citations[0].document_id]
    assert result.sql_result["template_id"] == "verified_total"
