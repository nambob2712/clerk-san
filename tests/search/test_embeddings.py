from __future__ import annotations

import math

import pytest

from clerksan.config import Settings
from clerksan.search.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingResponseError,
    embed_query,
    embed_texts,
)


class FakeEmbeddingClient:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[tuple[str, list[str]]] = []
        self.offset = 0

    async def embeddings(self, model: str, texts: list[str]) -> list[list[float]]:
        self.calls.append((model, texts))
        result = self.vectors[self.offset : self.offset + len(texts)]
        self.offset += len(texts)
        return result


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        embed_model="intfloat/multilingual-e5-small",
        embed_model_digest="sha256:test",
        embed_dim=2,
    )


@pytest.mark.asyncio
async def test_e5_prefixes_and_normalizes_passages_and_queries() -> None:
    client = FakeEmbeddingClient([[3.0, 4.0], [0.0, 2.0], [0.0, 2.0]])
    settings = _settings()

    passages = await embed_texts(["receipt", "invoice"], client, settings, batch_size=1)
    query = await embed_query("where is the receipt", client, settings)

    assert client.calls[0] == (settings.embed_model, ["passage: receipt"])
    assert client.calls[1] == (settings.embed_model, ["passage: invoice"])
    assert client.calls[2] == (settings.embed_model, ["query: where is the receipt"])
    assert passages[0] == [0.6, 0.8]
    assert math.isclose(query[1], 1.0)


@pytest.mark.asyncio
async def test_embedding_contract_requires_pin_and_exact_dimension() -> None:
    client = FakeEmbeddingClient([[1.0, 2.0, 3.0]])
    unconfigured = _settings().model_copy(
        update={"embed_model": None, "embed_model_digest": None, "embed_dim": None}
    )
    with pytest.raises(EmbeddingConfigurationError):
        await embed_texts(["x"], client, unconfigured)
    with pytest.raises(EmbeddingResponseError, match="dimension"):
        await embed_texts(["x"], client, _settings())


@pytest.mark.asyncio
async def test_d2_nomic_pin_uses_the_model_card_search_instructions() -> None:
    client = FakeEmbeddingClient([[1.0, 0.0], [0.0, 1.0]])
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", embed_dim=2)

    await embed_texts(["receipt context"], client, settings)
    await embed_query("where is the receipt", client, settings)

    assert client.calls == [
        (settings.embed_model, ["search_document: receipt context"]),
        (settings.embed_model, ["search_query: where is the receipt"]),
    ]
