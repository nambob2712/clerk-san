from __future__ import annotations

from pathlib import Path

import pytest

from clerksan.extract.tax_id import is_valid_registration_number
from eval.metrics import (
    LOCAL_MEMORY_BUDGET_BYTES,
    EvalReport,
    field_accuracy,
    hit_rate_at_k,
    memory_budget_passes,
    ollama_residency_snapshot,
    regressions,
    routing_accuracy,
)
from eval.synthetic import generate


def test_synthetic_generation_is_deterministic_and_tax_ids_are_valid(tmp_path: Path) -> None:
    first = generate(3, 42, tmp_path / "first")
    second = generate(3, 42, tmp_path / "second")
    assert first == second
    for index in range(3):
        assert (tmp_path / "first" / f"{index:04d}.png").read_bytes() == (
            tmp_path / "second" / f"{index:04d}.png"
        ).read_bytes()
    assert all(is_valid_registration_number(record["registration_number"]) for record in first)


def test_field_accuracy_unwraps_values_and_normalizes_types() -> None:
    labels = [
        {
            "transaction_date": "2026-07-13",
            "total_amount": 1200,
            "counterparty": "サンプル 商店",
            "registration_number": "T8700110005901",
        }
    ]
    predictions = [
        {
            "transaction_date": {"value": "2026-07-13"},
            "total_amount": {"value": 1200.4},
            "counterparty": {"value": "サンプル商店"},
            "registration_number": {"value": "t8700110005901"},
        }
    ]
    assert all(field.accuracy == 1 for field in field_accuracy(predictions, labels))


def test_retrieval_and_regression_metrics() -> None:
    assert hit_rate_at_k([["a", "b"], ["x"]], ["b", "missing"], k=2) == 0.5
    assert routing_accuracy(["sql", "hybrid"], ["sql", "semantic"]) == 0.5
    with pytest.raises(ValueError):
        hit_rate_at_k([], ["a"])

    baseline = EvalReport.model_validate(
        {"fields": [{"field": "total_amount", "correct": 100, "total": 100}]}
    )
    report = EvalReport.model_validate(
        {"fields": [{"field": "total_amount", "correct": 97, "total": 100}]}
    )
    assert regressions(report, baseline) == ["total_amount"]


def test_regressions_gate_matching_retrieval_and_routing_metrics() -> None:
    baseline = EvalReport.model_validate(
        {"retrieval_hit_rate_at_k": 1.0, "k": 3, "routing_accuracy": 1.0}
    )
    report = EvalReport.model_validate(
        {"retrieval_hit_rate_at_k": 0.5, "k": 3, "routing_accuracy": 0.5}
    )

    assert regressions(report, baseline) == ["retrieval_hit_rate_at_3", "routing_accuracy"]


def test_ollama_residency_snapshot_distinguishes_engine_memory_from_client_memory() -> None:
    snapshot = ollama_residency_snapshot(
        [
            {
                "name": "qwen2.5:7b",
                "size": 7 * 1024 * 1024 * 1024,
                "details": {"quantization_level": "Q4_K_M"},
            },
            {"name": "nomic-embed-text:v1.5", "size": 512 * 1024 * 1024},
        ],
        "qwen2.5:7b",
    )

    assert snapshot == {
        "model_resident_bytes": 7 * 1024 * 1024 * 1024,
        "server_resident_bytes": 7 * 1024 * 1024 * 1024 + 512 * 1024 * 1024,
        "quantization": "Q4_K_M",
        "model_loaded": True,
    }
    assert memory_budget_passes(snapshot["server_resident_bytes"])
    assert not memory_budget_passes(LOCAL_MEMORY_BUDGET_BYTES + 1)
    assert not memory_budget_passes(None)
