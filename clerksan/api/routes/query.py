"""HTTP boundary for guarded local question answering."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.api.deps import database_session, settings_from_request
from clerksan.api.schemas import AnswerOut, CitationOut, QueryIn
from clerksan.config import Settings
from clerksan.llm.client import ModelManager, OllamaClient, OllamaError
from clerksan.query.semantic_answerer import SemanticSafetyError
from clerksan.query.service import answer
from clerksan.query.sql_answerer import UnsafeSqlQuestionError
from clerksan.search.indexer import IndexingError, SearchSchemaUnavailable

router = APIRouter(tags=["query"])


@router.post("/query", response_model=AnswerOut)
async def ask(
    body: QueryIn,
    settings: Settings = Depends(settings_from_request),
    session: AsyncSession = Depends(database_session),
) -> AnswerOut:
    """Answer a question through local models and the verified-record guardrail."""

    question = body.question.strip()
    if not question:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_question", "message": "Question is required."},
        )
    client = OllamaClient(settings)
    try:
        result = await answer(question, session, client, ModelManager(client, settings))
    except (UnsafeSqlQuestionError, SemanticSafetyError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "unsafe_query", "message": str(error)},
        ) from error
    except (OllamaError, SearchSchemaUnavailable, IndexingError) as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "local_query_unavailable", "message": str(error)},
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail={"code": "query_failed", "message": "Local query processing failed."},
        ) from error
    finally:
        await client.aclose()
    return AnswerOut(
        text=result.text,
        mode=result.mode.value,
        citations=[
            CitationOut(
                document_id=hit.document_id,
                heading_path=hit.heading_path,
                snippet=hit.text[:500],
            )
            for hit in result.citations
        ],
        sql_result=result.sql_result,
    )
