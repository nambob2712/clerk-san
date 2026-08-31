"""Conservative local query routing with deterministic money guardrails."""

from __future__ import annotations

import enum
import re

from clerksan.llm.client import ModelManager, ModelRole, OllamaClient


class Route(enum.StrEnum):
    SQL = "sql"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


_AGGREGATION = re.compile(
    r"(?:"
    r"\b(?:total|sum|average|avg|how\s+much|how\s+many|count|spent|spend|"
    r"expense|highest|lowest|compare|comparison|monthly|per\s+month)\b|"
    r"合計|総額|平均|いくら|何件|件数|合算|支出|支払(?:い)?|最も|比較|比べ|月別|年別|"
    r"tổng(?:\s+cộng)?|bao\s+nhiêu|chi\s+tiêu|đã\s+chi|trung\s+bình|"
    r"nhiều\s+nhất|cao\s+nhất|so\s+sánh|tháng\s+(?:này|trước)|năm\s+(?:nay|ngoái)"
    r")",
    re.IGNORECASE,
)
_CONTEXTUAL_NARROWING = re.compile(
    r"(?:\b(?:where\s+i|that\s+i|the\s+(?:shop|place|receipt|document)|"
    r"which\s+(?:shop|receipt|document)|same\s+(?:shop|place))\b|"
    r"(?:その|あの|先ほど|前に|どこで|店|店舗).*(?:買|購入|使|支払))",
    re.IGNORECASE,
)


def is_aggregation_question(question: str) -> bool:
    """Return whether a question can make an arithmetic or comparison claim."""

    return bool(_AGGREGATION.search(question))


def requires_semantic_narrowing(question: str) -> bool:
    """Recognize references that require retrieval before a verified SQL calculation."""

    return bool(_CONTEXTUAL_NARROWING.search(question))


async def route_question(question: str, client: OllamaClient, models: ModelManager) -> Route:
    """Route a question while forcing every aggregation through verified SQL.

    A model can improve non-financial intent routing, but it never has authority to
    downgrade an aggregation to semantic retrieval.
    """

    cleaned = question.strip()
    if not cleaned:
        raise ValueError("question must not be empty")
    if is_aggregation_question(cleaned):
        return Route.HYBRID if requires_semantic_narrowing(cleaned) else Route.SQL

    try:
        model = await models.ensure_loaded(ModelRole.ROUTER)
        response = await client.generate(model, _router_prompt(cleaned))
        return _parse_model_route(response)
    except Exception:
        # A local model outage cannot turn a lookup request into a fabricated answer.
        # Semantic retrieval has citations and is the conservative no-model fallback.
        return Route.SEMANTIC


def _router_prompt(question: str) -> str:
    return (
        "Classify the document question as exactly one token: sql, semantic, or hybrid. "
        "Use sql for verified-record filters/aggregates, semantic for finding documents, "
        "and hybrid only when retrieval must identify the SQL scope.\n"
        f"Question: {question}\nRoute:"
    )


def _parse_model_route(response: str) -> Route:
    normalized = response.strip().lower()
    if normalized in {route.value for route in Route}:
        return Route(normalized)
    match = re.search(r"\b(sql|semantic|hybrid)\b", normalized)
    if match is None:
        return Route.SEMANTIC
    return Route(match.group(1))
