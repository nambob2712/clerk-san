from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from eval import benchmark_extraction_models
from eval.benchmark_extraction_models import _select, fixture_documents
from eval.metrics import EvalReport


def _fixture(manifest: str = "a" * 64, documents: int = 3) -> dict[str, object]:
    return {
        "generator": "eval.synthetic.generate",
        "seed": 73,
        "documents": documents,
        "manifest_sha256": manifest,
    }


def _baseline(correct: int, *, fixture: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "fields": [{"field": "total_amount", "correct": correct, "total": 100}],
        "meta": {
            "mode": "model-benchmark",
            "prediction_evaluation": True,
            "fixture": fixture or _fixture(),
        },
    }


def _benchmark_report(
    correct: int, *, fixture: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "decision_version": 1,
        "candidates": [],
        "selected": {"extract_model": "local-test"},
        "baseline": _baseline(correct, fixture=fixture),
    }


def _write_baseline(path: Path, correct: int, *, fixture: dict[str, object] | None = None) -> str:
    report = EvalReport.model_validate(_baseline(correct, fixture=fixture))
    encoded = report.model_dump_json(indent=2) + "\n"
    path.write_text(encoded, encoding="utf-8")
    return encoded


def _run_main(monkeypatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["python -m eval.benchmark_extraction_models", *arguments])
    benchmark_extraction_models.main()


def test_d3_fixtures_have_the_expected_structured_fields() -> None:
    documents, labels = fixture_documents(2)
    assert len(documents) == len(labels) == 2
    assert all("TOTAL:" in document.markdown_body for document in documents)
    assert all(label["currency"] == "JPY" for label in labels)


def test_d3_selection_requires_pre_repair_validity() -> None:
    report = _select(
        [
            {
                "tag": "invalid",
                "status": "available",
                "pre_repair_json_validity": 0.5,
            },
            {
                "tag": "local",
                "status": "available",
                "pre_repair_json_validity": 1.0,
                "mean_field_accuracy": 0.8,
                "ollama_server_peak_resident_bytes": 100,
                "memory_budget_passed": True,
                "resolved_digest": "a" * 64,
            },
        ]
    )
    assert report is not None
    assert report["extract_model"] == "local"


def test_d3_selection_rejects_schema_valid_but_empty_extractions() -> None:
    report = _select(
        [
            {
                "tag": "empty",
                "status": "available",
                "pre_repair_json_validity": 1.0,
                "mean_field_accuracy": 0.0,
                "ollama_server_peak_resident_bytes": 100,
                "memory_budget_passed": True,
                "resolved_digest": "a" * 64,
            }
        ]
    )

    assert report is None


def test_d3_selection_prefers_lower_measured_ollama_residency() -> None:
    report = _select(
        [
            {
                "tag": "large-peak",
                "status": "available",
                "pre_repair_json_validity": 1.0,
                "mean_field_accuracy": 1.0,
                "ollama_server_peak_resident_bytes": 900,
                "memory_budget_passed": True,
                "process_rss_delta_bytes": 1,
                "resolved_digest": "a" * 64,
            },
            {
                "tag": "small-peak",
                "status": "available",
                "pre_repair_json_validity": 1.0,
                "mean_field_accuracy": 1.0,
                "ollama_server_peak_resident_bytes": 100,
                "memory_budget_passed": True,
                "process_rss_delta_bytes": 500,
                "resolved_digest": "b" * 64,
            },
        ]
    )

    assert report is not None
    assert report["extract_model"] == "small-peak"


@pytest.mark.asyncio
async def test_d3_benchmark_records_os_peak_rss(monkeypatch) -> None:
    documents, labels = fixture_documents(1)
    peaks = iter((100, 400))

    class FakeParsed:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return labels[0]

    class FakeClient:
        async def generate(self, *_: object, **__: object) -> str:
            return "{}"

        async def loaded_models(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "qwen2.5:7b",
                    "size": 3 * 1024 * 1024 * 1024,
                    "details": {"quantization_level": "Q4_K_M"},
                }
            ]

    monkeypatch.setattr(benchmark_extraction_models, "_parse_extraction", lambda *_: FakeParsed())
    monkeypatch.setattr(benchmark_extraction_models, "process_peak_rss_bytes", lambda: next(peaks))

    report = await benchmark_extraction_models._benchmark_model(
        FakeClient(),
        "qwen2.5:7b",
        [{"name": "qwen2.5:7b", "digest": "a" * 64}],
        documents,
        labels,
    )

    assert report["peak_rss_bytes"] == 400
    assert report["peak_rss_increase_bytes"] == 300
    assert report["ollama_server_peak_resident_bytes"] == 3 * 1024 * 1024 * 1024
    assert report["memory_budget_passed"] is True
    baseline = report["evaluation"]
    assert baseline["meta"]["prediction_evaluation"] is True
    assert {field["field"] for field in baseline["fields"]} == {
        "counterparty",
        "currency",
        "registration_number",
        "total_amount",
        "transaction_date",
    }


def test_d3_selection_rejects_an_unmeasured_or_over_budget_candidate() -> None:
    assert (
        _select(
            [
                {
                    "tag": "unmeasured",
                    "status": "available",
                    "pre_repair_json_validity": 1.0,
                    "mean_field_accuracy": 1.0,
                    "resolved_digest": "a" * 64,
                },
                {
                    "tag": "over-budget",
                    "status": "over_memory_budget",
                    "pre_repair_json_validity": 1.0,
                    "mean_field_accuracy": 1.0,
                    "memory_budget_passed": False,
                    "resolved_digest": "b" * 64,
                },
            ]
        )
        is None
    )


@pytest.mark.asyncio
async def test_d3_unloads_each_benchmark_loaded_model_before_the_next_candidate(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeClient:
        async def list_models(self) -> list[dict[str, object]]:
            return []

        async def loaded_models(self) -> list[dict[str, object]]:
            return []

        async def unload(self, model: str) -> None:
            events.append(f"unload:{model}")

        async def aclose(self) -> None:
            events.append("close")

    async def fake_benchmark_model(
        _client: object,
        model: str,
        _installed: object,
        _documents: object,
        _labels: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        events.append(f"benchmark:{model}")
        return {
            "tag": model,
            "status": "available",
            "pre_repair_json_validity": 1.0,
            "mean_field_accuracy": 1.0,
            "memory_budget_passed": True,
            "ollama_server_peak_resident_bytes": 1,
            "resolved_digest": model,
            "evaluation": _baseline(100),
        }

    monkeypatch.setattr(
        benchmark_extraction_models, "OllamaClient", lambda *_args, **_kwargs: FakeClient()
    )
    monkeypatch.setattr(benchmark_extraction_models, "_benchmark_model", fake_benchmark_model)

    await benchmark_extraction_models.benchmark(models=("model-a", "model-b"), fixture_count=1)

    assert events == [
        "benchmark:model-a",
        "unload:model-a",
        "benchmark:model-b",
        "unload:model-b",
        "close",
    ]


def test_selected_baseline_uses_the_selected_candidate_evaluation() -> None:
    baseline = {"fields": [{"field": "total_amount", "correct": 3, "total": 3}]}

    assert (
        benchmark_extraction_models._selected_baseline(
            [{"tag": "qwen2.5:7b", "evaluation": baseline}],
            {"extract_model": "qwen2.5:7b"},
        )
        == baseline
    )


def test_benchmark_rejects_a_worse_candidate_before_replacing_its_baseline(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    baseline_path = tmp_path / "baseline.json"
    original_baseline = _write_baseline(baseline_path, 100)
    out_path = tmp_path / "decision.json"
    out_path.write_text("existing decision\n", encoding="utf-8")

    async def fake_benchmark(**_: object) -> dict[str, object]:
        return _benchmark_report(97)

    monkeypatch.setattr(benchmark_extraction_models, "benchmark", fake_benchmark)

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            [
                "--baseline",
                str(baseline_path),
                "--baseline-out",
                str(baseline_path),
                "--out",
                str(out_path),
            ],
        )

    assert error.value.code == 1
    assert baseline_path.read_text(encoding="utf-8") == original_baseline
    assert out_path.read_text(encoding="utf-8") == "existing decision\n"
    assert "Regression beyond two points: total_amount" in capsys.readouterr().err


def test_benchmark_rejects_a_different_fixture_before_replacing_its_baseline(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    baseline_path = tmp_path / "baseline.json"
    original_baseline = _write_baseline(baseline_path, 100)
    out_path = tmp_path / "decision.json"

    async def fake_benchmark(**_: object) -> dict[str, object]:
        return _benchmark_report(100, fixture=_fixture(manifest="b" * 64, documents=1))

    monkeypatch.setattr(benchmark_extraction_models, "benchmark", fake_benchmark)

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            [
                "--baseline",
                str(baseline_path),
                "--baseline-out",
                str(baseline_path),
                "--out",
                str(out_path),
            ],
        )

    assert error.value.code == 1
    assert baseline_path.read_text(encoding="utf-8") == original_baseline
    assert not out_path.exists()
    assert "different fixture manifests" in capsys.readouterr().err


def test_benchmark_refuses_to_replace_an_existing_baseline_without_input(
    tmp_path: Path, monkeypatch
) -> None:
    baseline_path = tmp_path / "baseline.json"
    original_baseline = _write_baseline(baseline_path, 100)
    out_path = tmp_path / "decision.json"

    async def unexpected_benchmark(**_: object) -> dict[str, object]:
        raise AssertionError("the baseline guard must run before benchmarking")

    monkeypatch.setattr(benchmark_extraction_models, "benchmark", unexpected_benchmark)

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            ["--baseline-out", str(baseline_path), "--out", str(out_path)],
        )

    assert error.value.code == 2
    assert baseline_path.read_text(encoding="utf-8") == original_baseline
    assert not out_path.exists()


def test_benchmark_requires_the_existing_output_baseline_as_its_input(
    tmp_path: Path, monkeypatch
) -> None:
    input_path = tmp_path / "input-baseline.json"
    _write_baseline(input_path, 100)
    output_path = tmp_path / "output-baseline.json"
    original_output = _write_baseline(output_path, 80)
    decision_path = tmp_path / "decision.json"

    async def unexpected_benchmark(**_: object) -> dict[str, object]:
        raise AssertionError("the baseline path guard must run before benchmarking")

    monkeypatch.setattr(benchmark_extraction_models, "benchmark", unexpected_benchmark)

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            [
                "--baseline",
                str(input_path),
                "--baseline-out",
                str(output_path),
                "--out",
                str(decision_path),
            ],
        )

    assert error.value.code == 2
    assert output_path.read_text(encoding="utf-8") == original_output
    assert not decision_path.exists()


def test_benchmark_writes_an_accepted_candidate_and_replaces_its_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, 100)
    out_path = tmp_path / "decision.json"
    report = _benchmark_report(99)

    async def fake_benchmark(**_: object) -> dict[str, object]:
        return report

    monkeypatch.setattr(benchmark_extraction_models, "benchmark", fake_benchmark)
    _run_main(
        monkeypatch,
        [
            "--baseline",
            str(baseline_path),
            "--baseline-out",
            str(baseline_path),
            "--out",
            str(out_path),
        ],
    )

    assert json.loads(out_path.read_text(encoding="utf-8")) == report
    written_baseline = EvalReport.model_validate_json(baseline_path.read_text(encoding="utf-8"))
    assert written_baseline.fields[0].correct == 99


def test_benchmark_first_run_writes_a_baseline_without_an_input_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    baseline_path = tmp_path / "baseline.json"
    out_path = tmp_path / "decision.json"

    async def fake_benchmark(**_: object) -> dict[str, object]:
        return _benchmark_report(100)

    monkeypatch.setattr(benchmark_extraction_models, "benchmark", fake_benchmark)
    _run_main(
        monkeypatch,
        ["--baseline-out", str(baseline_path), "--out", str(out_path)],
    )

    assert json.loads(out_path.read_text(encoding="utf-8"))["selected"] == {
        "extract_model": "local-test"
    }
    written_baseline = EvalReport.model_validate_json(baseline_path.read_text(encoding="utf-8"))
    assert written_baseline.fields[0].correct == 100
