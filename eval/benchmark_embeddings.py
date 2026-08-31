"""Benchmark and pin the local embedding model before creating the vector schema.

The corpus is deliberately synthetic and non-personal.  It measures the exact local
Ollama artifact that will be recorded in the database, rather than relying on a moving
remote registry label.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from clerksan.config import Settings
from clerksan.llm.client import OllamaClient
from eval.metrics import (
    LOCAL_MEMORY_BUDGET_BYTES,
    hit_rate_at_k,
    memory_budget_passes,
    ollama_residency_snapshot,
    process_peak_rss_bytes,
)

DEFAULT_MODEL = "nomic-embed-text:v1.5"
PASSAGE_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "
TOP_K = 3
REQUIRED_TOP_K_HIT_RATE = 1.0
BENCHMARK_KEEP_ALIVE = "5m"


def fixture_corpus(
    size: int = 50, queries: int = 30
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return deterministic document/query pairs with one unambiguous relevant item."""

    if size < queries or queries < 1:
        raise ValueError("size must be at least queries, and queries must be positive")
    corpus = [
        (
            f"receipt-{index:03d}",
            "Clerk-san receipt "
            f"needle-{index:03d}. Merchant Synthetic Shop {index:03d}. "
            f"Category {('utilities', 'food', 'travel')[index % 3]}. "
            f"Invoice reference unique-code-{index:03d}.",
        )
        for index in range(size)
    ]
    questions = [
        (
            f"Which receipt mentions unique-code-{index:03d} and Synthetic Shop {index:03d}?",
            f"receipt-{index:03d}",
        )
        for index in range(queries)
    ]
    return corpus, questions


async def benchmark(
    *, model: str = DEFAULT_MODEL, ollama_url: str = "http://127.0.0.1:11434", batch_size: int = 16
) -> dict[str, Any]:
    """Measure one installed local model and return a decision-ready report payload."""

    corpus, questions = fixture_corpus()
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", ollama_url=ollama_url)
    client = OllamaClient(settings, request_keep_alive=BENCHMARK_KEEP_ALIVE)
    preloaded_models: list[dict[str, Any]] = []
    try:
        installed = await client.list_models()
        preloaded_models = await client.loaded_models()
        digest = _digest_for(model, installed)
        artifact = _model_artifact(model, installed, await client.show_model(model))
        start_rss = psutil.Process().memory_info().rss
        start_peak_rss = process_peak_rss_bytes()
        start = time.perf_counter()
        corpus_vectors = await _embed_batched(
            client, model, [PASSAGE_PREFIX + body for _, body in corpus], batch_size
        )
        query_vectors = await _embed_batched(
            client, model, [QUERY_PREFIX + question for question, _ in questions], batch_size
        )
        elapsed = time.perf_counter() - start
        residency = ollama_residency_snapshot(await client.loaded_models(), model)
        if residency["model_resident_bytes"] is None or not memory_budget_passes(
            residency["server_resident_bytes"]
        ):
            raise RuntimeError(
                "Embedding benchmark requires measured Ollama server residency within "
                "the configured 16 GiB budget"
            )
        peak_rss = process_peak_rss_bytes()
    finally:
        if not _model_is_loaded(model, preloaded_models):
            try:
                await client.unload(model)
            except Exception:
                pass
        await client.aclose()
    _check_vectors(corpus_vectors + query_vectors)
    ranked_ids = [_rank_ids(query, corpus, corpus_vectors) for query in query_vectors]
    gold_ids = [gold_id for _, gold_id in questions]
    dimension = len(corpus_vectors[0])
    top_3_hit_rate = hit_rate_at_k(ranked_ids, gold_ids, k=TOP_K)
    _enforce_top_3_gate(top_3_hit_rate)
    query_evidence = [
        {"gold_document": gold_id, "retrieved_documents": ranked[:TOP_K]}
        for ranked, gold_id in zip(ranked_ids, gold_ids, strict=True)
    ]
    return {
        "decision_version": 1,
        "selected": {
            "tag": model,
            "digest": digest,
            "modelfile_sha256": artifact["modelfile_sha256"],
            "dimension": dimension,
            "passage_prefix": PASSAGE_PREFIX,
            "query_prefix": QUERY_PREFIX,
        },
        "candidates": [
            {
                "tag": model,
                "resolved_digest": digest,
                "modelfile_sha256": artifact["modelfile_sha256"],
                "dimension": dimension,
                "hit_rate_at_3": top_3_hit_rate,
                "hit_rate_at_5": hit_rate_at_k(ranked_ids, gold_ids, k=5),
                "top_3_gate_passed": True,
                "documents_per_second": round(len(corpus) / elapsed, 3),
                "vectors_per_second": round((len(corpus) + len(questions)) / elapsed, 3),
                "process_rss_delta_bytes": psutil.Process().memory_info().rss - start_rss,
                "benchmark_process_peak_rss_bytes": peak_rss,
                "benchmark_process_peak_rss_increase_bytes": max(0, peak_rss - start_peak_rss),
                "ollama_model_resident_bytes": residency["model_resident_bytes"],
                "ollama_server_resident_bytes": residency["server_resident_bytes"],
                "memory_budget_bytes": LOCAL_MEMORY_BUDGET_BYTES,
                "memory_budget_passed": memory_budget_passes(residency["server_resident_bytes"]),
                "quantization": artifact["quantization"],
                "batch_size": batch_size,
            }
        ],
        "fixture": {
            "generator": "eval.benchmark_embeddings.fixture_corpus",
            "documents": len(corpus),
            "queries": len(questions),
            "manifest_sha256": _fixture_manifest_sha256(corpus, questions),
            "gate": f"hit_rate_at_{TOP_K} == {REQUIRED_TOP_K_HIT_RATE}",
        },
        "query_evidence": query_evidence,
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor() or platform.machine(),
            "memory_total_bytes": psutil.virtual_memory().total,
        },
        "git_revision": _git_revision(),
        "recorded_at": datetime.now(UTC).isoformat(),
        "selection_reason": (
            "Installed exact local Ollama tag passed the deterministic top-3 retrieval fixture; "
            "its digest, Modelfile revision, and 768-dimensional output are now schema inputs."
        ),
    }


def _enforce_top_3_gate(hit_rate: float) -> None:
    """Reject an embedding candidate unless every deterministic needle is top-3."""

    if hit_rate < REQUIRED_TOP_K_HIT_RATE:
        raise RuntimeError(
            f"Embedding candidate failed top-{TOP_K} retrieval gate: "
            f"{hit_rate:.3f} < {REQUIRED_TOP_K_HIT_RATE:.3f}"
        )


async def _embed_batched(
    client: OllamaClient, model: str, texts: list[str], batch_size: int
) -> list[list[float]]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), batch_size):
        vectors.extend(await client.embeddings(model, texts[offset : offset + batch_size]))
    return vectors


def _digest_for(model: str, installed: list[dict[str, Any]]) -> str:
    canonical = model.removesuffix(":latest")
    for entry in installed:
        name = str(entry.get("name") or entry.get("model") or "").removesuffix(":latest")
        digest = entry.get("digest")
        if name == canonical and isinstance(digest, str) and digest:
            return digest
    raise RuntimeError(f"Model {model!r} is not installed. Run: ollama pull {model}")


def _model_artifact(
    model: str, installed: list[dict[str, Any]], shown: dict[str, Any]
) -> dict[str, str | None]:
    modelfile = shown.get("modelfile")
    modelfile_sha256 = (
        hashlib.sha256(modelfile.encode("utf-8")).hexdigest()
        if isinstance(modelfile, str) and modelfile
        else None
    )
    canonical = model.removesuffix(":latest")
    for entry in installed:
        name = str(entry.get("name") or entry.get("model") or "").removesuffix(":latest")
        if name == canonical:
            details = entry.get("details")
            quantization = (
                details.get("quantization_level")
                if isinstance(details, dict) and isinstance(details.get("quantization_level"), str)
                else None
            )
            return {
                "quantization": quantization,
                "modelfile_sha256": modelfile_sha256,
            }
    return {"quantization": None, "modelfile_sha256": modelfile_sha256}


def _fixture_manifest_sha256(
    corpus: list[tuple[str, str]], questions: list[tuple[str, str]]
) -> str:
    payload = json.dumps(
        {"corpus": corpus, "questions": questions}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git_revision() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _model_is_loaded(model: str, loaded_models: list[dict[str, Any]]) -> bool:
    canonical = model.removesuffix(":latest")
    return any(
        str(entry.get("name") or entry.get("model") or "").removesuffix(":latest") == canonical
        for entry in loaded_models
    )


def _check_vectors(vectors: list[list[float]]) -> None:
    if not vectors or not vectors[0]:
        raise RuntimeError("Ollama returned no embedding vectors")
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise RuntimeError("Ollama returned inconsistent embedding dimensions")


def _rank_ids(
    query: list[float], corpus: list[tuple[str, str]], vectors: list[list[float]]
) -> list[str]:
    return [
        item[0]
        for _, item in sorted(
            (
                (_cosine(query, vector), document)
                for document, vector in zip(corpus, vectors, strict=True)
            ),
            reverse=True,
        )
    ]


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--out", type=Path, default=Path("eval/results/embedding-decision.json"))
    arguments = parser.parse_args()
    report = asyncio.run(
        benchmark(
            model=arguments.model, ollama_url=arguments.ollama_url, batch_size=arguments.batch_size
        )
    )
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
