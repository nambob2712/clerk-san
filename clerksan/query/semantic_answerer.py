"""Cited local semantic lookup with no arithmetic authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.llm.client import ModelManager, ModelRole, OllamaClient
from clerksan.query.router import is_aggregation_question
from clerksan.search.indexer import Hit, SearchDomain, search

SearchFunction = Callable[[AsyncSession, str], Awaitable[list[Hit]]]


class SemanticSafetyError(ValueError):
    """Semantic lookup was asked to make an arithmetic claim without SQL."""


class SemanticAnswer(BaseModel):
    text: str
    citations: list[Hit]


async def answer_semantic(
    question: str,
    session: AsyncSession,
    client: OllamaClient,
    models: ModelManager,
    *,
    searcher: SearchFunction | None = None,
    allow_aggregation_context: bool = False,
    search_domain: SearchDomain | str = SearchDomain.ALL,
) -> SemanticAnswer:
    """Find documents locally and return an answer with resolvable source citations."""

    cleaned = question.strip()
    if not cleaned:
        raise ValueError("question must not be empty")
    if is_aggregation_question(cleaned) and not allow_aggregation_context:
        raise SemanticSafetyError(
            "aggregation questions require verified SQL, never semantic-only answers"
        )
    if searcher is None:
        hits = await _default_search(session, cleaned, domain=search_domain)
    else:
        hits = await searcher(session, cleaned)
    if not hits:
        return SemanticAnswer(text="No matching local documents were found.", citations=[])

    generated = await _synthesize_locally(cleaned, hits, client, models)
    return SemanticAnswer(text=_with_sources(generated, hits), citations=hits)


async def _default_search(
    session: AsyncSession,
    question: str,
    *,
    domain: SearchDomain | str,
) -> list[Hit]:
    return await search(session, question, domain=domain)


async def _synthesize_locally(
    question: str, hits: list[Hit], client: OllamaClient, models: ModelManager
) -> str:
    """Use only retrieved context; a deterministic source summary is the safe fallback."""

    context = "\n\n".join(
        f"[{index}] document={hit.document_id} heading={hit.heading_path or '(preamble)'}\n"
        f"{hit.text[:1200]}"
        for index, hit in enumerate(hits, start=1)
    )
    prompt = (
        "Answer only from the local source excerpts below. Do not total, average, compare, "
        "or otherwise calculate amounts. If the excerpts do not establish an answer, say so. "
        "Use source markers like [1].\n\n"
        f"Question: {question}\n\nSources:\n{context}\n\nAnswer:"
    )
    try:
        model = await models.ensure_loaded(ModelRole.ROUTER)
        answer = (await client.generate(model, prompt)).strip()
        if answer:
            return answer
    except Exception:
        pass
    return f"Found {len(hits)} matching local source excerpt(s)."


def _with_sources(answer: str, hits: list[Hit]) -> str:
    sources = "; ".join(
        f"[{index}] {hit.document_id} — {hit.heading_path or 'preamble'}"
        for index, hit in enumerate(hits, start=1)
    )
    return f"{answer.rstrip()}\n\nSources: {sources}"
