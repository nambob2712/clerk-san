"""Pinned, local-only embedding helpers for retrieval and indexing."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable

from clerksan.config import EMBED_MODEL_TAG, Settings
from clerksan.llm.client import OllamaClient, OllamaUnavailable


class EmbeddingConfigurationError(RuntimeError):
    """Embedding work was requested without a complete model pin."""


class EmbeddingResponseError(RuntimeError):
    """The local model returned vectors that do not match the pinned contract."""


def prepare_texts(texts: Iterable[str], *, model: str, purpose: str) -> list[str]:
    """Apply the pin-specific retrieval instruction without leaking it to callers."""

    if purpose not in {"query", "passage"}:
        raise ValueError("purpose must be 'query' or 'passage'")
    if model == EMBED_MODEL_TAG:
        prefix = "search_query: " if purpose == "query" else "search_document: "
    elif "e5" in model.lower():
        prefix = f"{purpose}: "
    else:
        prefix = ""
    prepared: list[str] = []
    for text in texts:
        if not isinstance(text, str):
            raise TypeError("texts must contain only strings")
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("texts must not contain empty strings")
        prepared.append(cleaned if not prefix or cleaned.startswith(prefix) else prefix + cleaned)
    return prepared


async def embed_texts(
    texts: list[str],
    client: OllamaClient,
    settings: Settings,
    *,
    purpose: str = "passage",
    batch_size: int | None = None,
) -> list[list[float]]:
    """Embed bounded batches through Ollama and validate the immutable model pin."""

    model, _, dimension = _embedding_pin(settings)
    effective_batch_size = batch_size if batch_size is not None else settings.embedding_batch_size
    if effective_batch_size < 1:
        raise ValueError("batch_size must be greater than zero")
    prepared = prepare_texts(texts, model=model, purpose=purpose)
    vectors: list[list[float]] = []
    for start in range(0, len(prepared), effective_batch_size):
        batch = prepared[start : start + effective_batch_size]
        batch_vectors = await _embed_once_with_retry(client, model, batch)
        if len(batch_vectors) != len(batch):
            raise EmbeddingResponseError("local embedding model returned the wrong vector count")
        vectors.extend(_validate_and_normalize(vector, dimension) for vector in batch_vectors)
    return vectors


async def embed_query(text: str, client: OllamaClient, settings: Settings) -> list[float]:
    """Embed exactly one retrieval query with the query-specific model convention."""

    vectors = await embed_texts([text], client, settings, purpose="query")
    return vectors[0]


def _embedding_pin(settings: Settings) -> tuple[str, str, int]:
    model = settings.embed_model
    digest = settings.embed_model_digest
    dimension = settings.embed_dim
    if not model or not digest or not dimension:
        raise EmbeddingConfigurationError(
            "local retrieval requires embed_model, embed_model_digest, and embed_dim"
        )
    return model, digest, dimension


async def _embed_once_with_retry(
    client: OllamaClient, model: str, texts: list[str]
) -> list[list[float]]:
    try:
        return await client.embeddings(model, texts)
    except OllamaUnavailable:
        await asyncio.sleep(0.1)
        return await client.embeddings(model, texts)


def _validate_and_normalize(vector: list[float], dimension: int) -> list[float]:
    if len(vector) != dimension:
        raise EmbeddingResponseError(
            f"local embedding dimension {len(vector)} does not match pinned dimension {dimension}"
        )
    if any(not math.isfinite(value) for value in vector):
        raise EmbeddingResponseError("local embedding vector contains a non-finite value")
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        raise EmbeddingResponseError("local embedding vector has zero magnitude")
    return [value / magnitude for value in vector]
