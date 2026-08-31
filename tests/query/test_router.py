from __future__ import annotations

import pytest

from clerksan.query.router import Route, is_aggregation_question, route_question


class FakeModels:
    async def ensure_loaded(self, role) -> str:
        del role
        return "router-model"


class FakeClient:
    def __init__(self, response: str) -> None:
        self.response = response

    async def generate(self, model: str, prompt: str) -> str:
        assert model == "router-model"
        assert "Question:" in prompt
        return self.response


@pytest.mark.asyncio
async def test_aggregation_guardrail_overrides_model_semantic_vote() -> None:
    route = await route_question("6月の食費の合計は？", FakeClient("semantic"), FakeModels())
    assert route is Route.SQL


@pytest.mark.asyncio
async def test_vietnamese_aggregation_uses_the_verified_sql_guardrail() -> None:
    question = "Tháng 7 năm 2026 tôi đã chi bao nhiêu?"

    route = await route_question(question, FakeClient("semantic"), FakeModels())

    assert is_aggregation_question(question)
    assert route is Route.SQL


@pytest.mark.asyncio
async def test_contextual_aggregation_is_hybrid() -> None:
    route = await route_question(
        "How much did I spend at the shop where I bought the client gift?",
        FakeClient("semantic"),
        FakeModels(),
    )
    assert route is Route.HYBRID


@pytest.mark.asyncio
async def test_non_aggregate_uses_local_router_result() -> None:
    route = await route_question(
        "Find the receipt for the client gift", FakeClient("hybrid"), FakeModels()
    )
    assert route is Route.HYBRID
