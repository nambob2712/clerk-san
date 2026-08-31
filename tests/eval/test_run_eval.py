from __future__ import annotations

import json
import sys
from pathlib import Path

from eval import run_eval
from eval.synthetic import generate


def _run(monkeypatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["python -m eval.run_eval", *arguments])
    run_eval.main()


def _only_report(out_dir: Path) -> dict[str, object]:
    reports = list(out_dir.glob("*.json"))
    assert len(reports) == 1
    return json.loads(reports[0].read_text(encoding="utf-8"))


def test_run_eval_generates_temporary_integrity_fixtures_without_echoing_labels(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    out_dir = tmp_path / "reports"

    _run(monkeypatch, ["--out", str(out_dir)])

    report = _only_report(out_dir)
    assert report["fields"] == []
    assert report["meta"]["mode"] == "fixture-integrity"
    assert report["meta"]["prediction_evaluation"] is False
    assert report["meta"]["fixture_integrity"]["origin"] == "generated-temporary"
    assert not (tmp_path / "eval" / "fixtures" / "synthetic").exists()


def test_run_eval_records_retrieval_and_routing_evidence_at_requested_k(
    tmp_path: Path, monkeypatch
) -> None:
    fixture_dir = tmp_path / "fixtures"
    labels = generate(2, 42, fixture_dir)
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps(labels), encoding="utf-8")
    query_results = tmp_path / "queries.json"
    query_results.write_text(
        json.dumps(
            [
                {
                    "gold_document": "synthetic-0000",
                    "retrieved_documents": ["other", "synthetic-0000"],
                    "expected_route": "hybrid",
                    "actual_route": "hybrid",
                }
            ]
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "reports"

    _run(
        monkeypatch,
        [
            "--fixtures",
            str(fixture_dir),
            "--predictions",
            str(predictions),
            "--query-results",
            str(query_results),
            "--k",
            "3",
            "--out",
            str(out_dir),
        ],
    )

    report = _only_report(out_dir)
    assert report["k"] == 3
    assert report["retrieval_hit_rate_at_k"] == 1.0
    assert report["routing_accuracy"] == 1.0
    assert report["meta"]["prediction_evaluation"] is True


def test_run_eval_accepts_embedding_benchmark_query_evidence(tmp_path: Path, monkeypatch) -> None:
    query_results = tmp_path / "embedding-decision.json"
    query_results.write_text(
        json.dumps(
            {
                "query_evidence": [
                    {
                        "gold_document": "receipt-001",
                        "retrieved_documents": ["receipt-001", "receipt-002"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "reports"

    _run(
        monkeypatch,
        ["--query-results", str(query_results), "--k", "3", "--out", str(out_dir)],
    )

    report = _only_report(out_dir)
    assert report["retrieval_hit_rate_at_k"] == 1.0
    assert report["k"] == 3
    assert report["meta"]["mode"] == "query-evidence"
