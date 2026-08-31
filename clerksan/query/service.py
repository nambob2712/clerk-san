"""One guarded entry point for verified SQL and cited local retrieval."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.db.repositories import VerifiedRepo
from clerksan.llm.client import ModelManager, OllamaClient
from clerksan.query.router import Route, route_question
from clerksan.query.semantic_answerer import SemanticAnswer, answer_semantic
from clerksan.query.sql_answerer import SqlAnswer, answer_sql
from clerksan.search.indexer import Hit, SearchDomain

Router = Callable[[str, OllamaClient, ModelManager], Awaitable[Route]]
SqlRunner = Callable[..., Awaitable[SqlAnswer]]
SemanticRunner = Callable[..., Awaitable[SemanticAnswer]]


class Answer(BaseModel):
    text: str
    mode: Route
    citations: list[Hit] = Field(default_factory=list)
    sql_result: dict[str, Any] | None = None


async def answer(
    question: str,
    session: AsyncSession,
    client: OllamaClient,
    models: ModelManager,
    *,
    router: Router = route_question,
    sql_runner: SqlRunner = answer_sql,
    semantic_runner: SemanticRunner = answer_semantic,
) -> Answer:
    """Route a question and keep all arithmetic under verified SQL authority."""

    cleaned = question.strip()
    if not cleaned:
        raise ValueError("question must not be empty")
    route = await router(cleaned, client, models)
    verified = VerifiedRepo(session)
    if route is Route.SQL:
        sql = await sql_runner(cleaned, verified, client, models)
        return _sql_answer(sql, Route.SQL)
    if route is Route.SEMANTIC:
        semantic = await semantic_runner(cleaned, session, client, models)
        return Answer(text=semantic.text, mode=Route.SEMANTIC, citations=semantic.citations)
    return await _hybrid_answer(
        cleaned,
        session,
        client,
        models,
        verified,
        sql_runner,
        semantic_runner,
    )


async def _hybrid_answer(
    question: str,
    session: AsyncSession,
    client: OllamaClient,
    models: ModelManager,
    verified: VerifiedRepo,
    sql_runner: SqlRunner,
    semantic_runner: SemanticRunner,
) -> Answer:
    """Retrieve the document scope first, then calculate only inside that scope."""

    semantic = await semantic_runner(
        question,
        session,
        client,
        models,
        allow_aggregation_context=True,
        search_domain=SearchDomain.FINANCIAL,
    )
    document_ids = list(dict.fromkeys(hit.document_id for hit in semantic.citations))
    if not document_ids:
        return Answer(
            text="No local sources were found to safely narrow the requested calculation.",
            mode=Route.HYBRID,
            citations=[],
            sql_result=None,
        )
    sql = await sql_runner(question, verified, client, models, document_ids=document_ids)
    return Answer(
        text=f"{sql.text}\n\nCalculation scope: {len(document_ids)} retrieved local document(s).",
        mode=Route.HYBRID,
        citations=semantic.citations,
        sql_result=sql.model_dump(mode="json"),
    )


def _sql_answer(sql: SqlAnswer, mode: Route) -> Answer:
    return Answer(text=sql.text, mode=mode, sql_result=sql.model_dump(mode="json"))
