"""Benchmark the exact local extraction/router model candidates."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from clerksan.config import Settings
from clerksan.db.models import DocumentClass
from clerksan.extract.extractor import (
    _extraction_prompt,
    _parse_extraction,
    _PayloadValidationError,
    response_schema,
)
from clerksan.extract.schemas import ReceiptExtraction
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.llm.client import OllamaClient
from eval.metrics import (
    LOCAL_MEMORY_BUDGET_BYTES,
    EvalReport,
    field_accuracy,
    memory_budget_passes,
    ollama_residency_snapshot,
    process_peak_rss_bytes,
    regressions,
)
from eval.synthetic import generate

DEFAULT_MODELS = ("qwen2.5:7b",)
BENCHMARK_KEEP_ALIVE = "5m"


def fixture_documents(count: int = 3) -> tuple[list[NormalizedDocument], list[dict[str, Any]]]:
    """Build compact, deterministic text documents and score only required fields."""

    documents, expected, _ = _fixture_documents_with_metadata(count)
    return documents, expected


def _fixture_documents_with_metadata(
    count: int,
) -> tuple[list[NormalizedDocument], list[dict[str, Any]], dict[str, Any]]:
    """Return the D3 evaluation rows plus the exact generated-source manifest identity."""

    with _temporary_fixture_dir() as fixture_dir:
        labels = generate(count, 73, fixture_dir)
        fixture = {
            "generator": "eval.synthetic.generate",
            "seed": 73,
            "documents": count,
            "manifest_sha256": hashlib.sha256(
                (fixture_dir / "manifest.json").read_bytes()
            ).hexdigest(),
        }
    documents: list[NormalizedDocument] = []
    expected: list[dict[str, Any]] = []
    for label in labels:
        documents.append(
            NormalizedDocument(
                markdown_body=(
                    f"Merchant: {label['counterparty']}\n"
                    f"Date: {label['transaction_date']}\n"
                    f"Registration: {label['registration_number']}\n"
                    f"TOTAL: {label['total_amount']} JPY\n"
                    "Currency: JPY"
                ),
                metadata=DocMetadata(
                    filename=f"{label['id']}.png",
                    detected_type=FileType.PNG,
                    sha256=label["sha256"],
                ),
            )
        )
        expected.append(
            {
                "transaction_date": label["transaction_date"],
                "total_amount": label["total_amount"],
                "counterparty": label["counterparty"],
                "registration_number": label["registration_number"],
                "currency": "JPY",
            }
        )
    return documents, expected, fixture


class _temporary_fixture_dir:
    """A tiny context manager avoiding a committed corpus for this text-only benchmark."""

    def __init__(self) -> None:
        import tempfile

        self._temporary = tempfile.TemporaryDirectory(prefix="clerksan-extraction-")

    def __enter__(self) -> Path:
        return Path(self._temporary.__enter__())

    def __exit__(self, *args: object) -> None:
        self._temporary.__exit__(*args)


async def benchmark(
    *,
    models: tuple[str, ...] = DEFAULT_MODELS,
    ollama_url: str = "http://127.0.0.1:11434",
    fixture_count: int = 3,
) -> dict[str, Any]:
    """Run pre-repair structured-extraction measurements against installed models."""

    documents, labels, fixture = _fixture_documents_with_metadata(fixture_count)
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", ollama_url=ollama_url)
    client = OllamaClient(settings, request_keep_alive=BENCHMARK_KEEP_ALIVE)
    preloaded_models: list[dict[str, Any]] = []
    try:
        installed = await client.list_models()
        preloaded_models = await client.loaded_models()
        preloaded_names = {
            _canonical_model_name(str(entry.get("name") or entry.get("model") or ""))
            for entry in preloaded_models
        }
        candidates = []
        for model in models:
            try:
                candidates.append(
                    await _benchmark_model(
                        client, model, installed, documents, labels, fixture_metadata=fixture
                    )
                )
            finally:
                if _canonical_model_name(model) not in preloaded_names:
                    try:
                        await client.unload(model)
                    except Exception:
                        pass
    finally:
        await client.aclose()
    selected = _select(candidates)
    baseline = _selected_baseline(candidates, selected)
    return {
        "decision_version": 1,
        "fixture_count": fixture_count,
        "fixture": fixture,
        "criterion": (
            "pre-repair JSON validity >=0.95 and mean field accuracy >=0.80, then highest "
            "field accuracy with measured Ollama server residency within the local memory budget"
        ),
        "memory_budget_bytes": LOCAL_MEMORY_BUDGET_BYTES,
        "candidates": candidates,
        "selected": selected,
        "baseline": baseline,
        "memory_measurement": (
            "peak_rss_bytes is the OS-reported benchmark-process peak (ru_maxrss); "
            "ollama_server_peak_resident_bytes is the maximum model-server residency "
            "reported by Ollama /api/ps and is the enforced 16 GiB selection budget."
        ),
        "git_revision": _git_revision(),
        "recorded_at": datetime.now(UTC).isoformat(),
    }


async def _benchmark_model(
    client: OllamaClient,
    model: str,
    installed: list[dict[str, Any]],
    documents: list[NormalizedDocument],
    labels: list[dict[str, Any]],
    *,
    fixture_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    digest = _digest_for(model, installed)
    if digest is None:
        return {
            "tag": model,
            "status": "unavailable",
            "reason": f"Run: ollama pull {model}",
        }
    process = psutil.Process()
    start_rss = process.memory_info().rss
    start_peak_rss = process_peak_rss_bytes()
    start = time.perf_counter()
    predictions: list[dict[str, Any]] = []
    valid = 0
    failures: list[str] = []
    ollama_model_peak: int | None = None
    ollama_server_peak: int | None = None
    quantization = _model_artifact(model, installed)["quantization"]
    for index, document in enumerate(documents, start=1):
        try:
            print(f"Benchmarking {model}: {index}/{len(documents)}", file=sys.stderr, flush=True)
            raw = await client.generate(
                model,
                _extraction_prompt(document, DocumentClass.RECEIPT),
                json_schema=response_schema(ReceiptExtraction),
            )
            snapshot = ollama_residency_snapshot(await client.loaded_models(), model)
            model_bytes = snapshot["model_resident_bytes"]
            server_bytes = snapshot["server_resident_bytes"]
            if model_bytes is None or server_bytes is None:
                raise RuntimeError(
                    "Ollama /api/ps did not report resident memory for the extraction model"
                )
            ollama_model_peak = max(ollama_model_peak or 0, model_bytes)
            ollama_server_peak = max(ollama_server_peak or 0, server_bytes)
            quantization = snapshot["quantization"] or quantization
            parsed = _parse_extraction(raw, ReceiptExtraction)
            valid += 1
            predictions.append(parsed.model_dump(mode="json"))
        except _PayloadValidationError as error:
            failures.append(str(error))
            predictions.append({})
        except Exception as error:
            failures.append(f"{type(error).__name__}: {error}")
            predictions.append({})
    elapsed = time.perf_counter() - start
    fields = field_accuracy(predictions, labels)
    mean_accuracy = sum(item.accuracy for item in fields) / len(fields) if fields else 0.0
    peak_rss = process_peak_rss_bytes()
    budget_passed = memory_budget_passes(ollama_server_peak)
    fixture = fixture_metadata or _evaluation_fixture_metadata(labels)
    evaluation = EvalReport(
        fields=fields,
        meta={
            "mode": "model-benchmark",
            "prediction_evaluation": True,
            "model": model,
            "resolved_digest": digest,
            "fixture": fixture,
            "memory_budget_bytes": LOCAL_MEMORY_BUDGET_BYTES,
            "ollama_server_peak_resident_bytes": ollama_server_peak,
        },
    ).model_dump(mode="json")
    return {
        "tag": model,
        "resolved_digest": digest,
        "quantization": quantization,
        "status": "available" if budget_passed else "over_memory_budget",
        "pre_repair_json_validity": valid / len(documents),
        "field_accuracy": {item.field: item.accuracy for item in fields},
        "mean_field_accuracy": mean_accuracy,
        "evaluation": evaluation,
        "documents_per_second": len(documents) / elapsed if elapsed else 0.0,
        "peak_rss_bytes": peak_rss,
        "peak_rss_increase_bytes": max(0, peak_rss - start_peak_rss),
        "process_rss_delta_bytes": process.memory_info().rss - start_rss,
        "ollama_model_peak_resident_bytes": ollama_model_peak,
        "ollama_server_peak_resident_bytes": ollama_server_peak,
        "memory_budget_bytes": LOCAL_MEMORY_BUDGET_BYTES,
        "memory_budget_passed": budget_passed,
        "failures": failures,
    }


def _select(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.get("status") == "available"
        and float(candidate.get("pre_repair_json_validity", 0.0)) >= 0.95
        and float(candidate.get("mean_field_accuracy", 0.0)) >= 0.80
        and candidate.get("memory_budget_passed") is True
    ]
    if not eligible:
        return None
    winner = max(
        eligible,
        key=lambda candidate: (
            float(candidate["mean_field_accuracy"]),
            -_memory_for_selection(candidate),
        ),
    )
    return {
        "extract_model": winner["tag"],
        "router_model": winner["tag"],
        "resolved_digest": winner["resolved_digest"],
        "reason": "best eligible pre-repair structured-extraction result",
    }


def _selected_baseline(
    candidates: list[dict[str, Any]], selected: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Return the selected model's real field-count report for regression gating."""

    if selected is None:
        return None
    selected_tag = selected["extract_model"]
    for candidate in candidates:
        if candidate.get("tag") == selected_tag:
            evaluation = candidate.get("evaluation")
            return dict(evaluation) if isinstance(evaluation, dict) else None
    return None


def _read_baseline(path: Path) -> EvalReport:
    """Load the shared EvalReport baseline format used by the regression gate."""

    try:
        return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"Cannot read extraction baseline {path}: {error}") from error


def _candidate_baseline(report: dict[str, Any]) -> EvalReport:
    """Validate the selected benchmark evaluation before it is written as a baseline."""

    baseline = report.get("baseline")
    if baseline is None:
        raise SystemExit("No eligible extraction candidate exists to write a baseline")
    try:
        return EvalReport.model_validate(baseline)
    except ValueError as error:
        raise SystemExit(f"Selected extraction baseline is invalid: {error}") from error


def _reject_regression(candidate: EvalReport, baseline: EvalReport) -> None:
    """Stop before output writes for incomparable fixtures or a material accuracy drop."""

    if _fixture_identity(candidate) != _fixture_identity(baseline):
        print(
            "Cannot compare extraction baselines from different fixture manifests",
            file=sys.stderr,
        )
        raise SystemExit(1)
    candidate_fields = {field.field for field in candidate.fields}
    baseline_fields = {field.field for field in baseline.fields}
    if candidate_fields != baseline_fields:
        print("Cannot compare extraction baselines with different field schemas", file=sys.stderr)
        raise SystemExit(1)

    dropped = regressions(candidate, baseline)
    if dropped:
        print(f"Regression beyond two points: {', '.join(dropped)}", file=sys.stderr)
        raise SystemExit(1)


def _fixture_identity(report: EvalReport) -> tuple[str, int, int, str]:
    fixture = report.meta.get("fixture")
    if not isinstance(fixture, dict):
        raise SystemExit("Extraction baseline is missing fixture provenance")
    generator = fixture.get("generator")
    seed = fixture.get("seed")
    documents = fixture.get("documents")
    manifest = fixture.get("manifest_sha256")
    if (
        not isinstance(generator, str)
        or not isinstance(seed, int)
        or not isinstance(documents, int)
        or not isinstance(manifest, str)
    ):
        raise SystemExit("Extraction baseline has incomplete fixture provenance")
    return generator, seed, documents, manifest


def _memory_for_selection(candidate: dict[str, Any]) -> int:
    """Prefer lower measured Ollama server residency among eligible candidates."""

    value = candidate.get("ollama_server_peak_resident_bytes", 0)
    return int(value)


def _digest_for(model: str, installed: list[dict[str, Any]]) -> str | None:
    canonical = model.removesuffix(":latest")
    for entry in installed:
        name = str(entry.get("name") or entry.get("model") or "").removesuffix(":latest")
        if name == canonical and isinstance(entry.get("digest"), str):
            return entry["digest"]
    return None


def _model_artifact(model: str, installed: list[dict[str, Any]]) -> dict[str, str | None]:
    canonical = _canonical_model_name(model)
    for entry in installed:
        name = _canonical_model_name(str(entry.get("name") or entry.get("model") or ""))
        if name == canonical:
            details = entry.get("details")
            quantization = (
                details.get("quantization_level")
                if isinstance(details, dict) and isinstance(details.get("quantization_level"), str)
                else None
            )
            return {"quantization": quantization}
    return {"quantization": None}


def _evaluation_fixture_metadata(labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Identify a direct unit-test evaluation corpus that has no source PNG manifest."""

    manifest = {"seed": 73, "documents": labels}
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    return {
        "generator": "eval.benchmark_extraction_models.evaluation_rows",
        "seed": 73,
        "documents": len(labels),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _canonical_model_name(model: str) -> str:
    return model.strip().removesuffix(":latest")


def _git_revision() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--fixtures", type=int, default=3)
    parser.add_argument(
        "--out", type=Path, default=Path("eval/results/extraction-model-decision.json")
    )
    parser.add_argument(
        "--baseline-out",
        type=Path,
        help="Write the selected candidate's real EvalReport for regression gating",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Compare the selected candidate against this existing EvalReport before writing",
    )
    arguments = parser.parse_args()
    if arguments.baseline is not None and not arguments.baseline.is_file():
        parser.error("--baseline must point to an existing EvalReport JSON file")
    if (
        arguments.baseline_out is not None
        and arguments.baseline_out.exists()
        and arguments.baseline is None
    ):
        parser.error("an existing --baseline-out requires --baseline before it can be replaced")
    if (
        arguments.baseline_out is not None
        and arguments.baseline_out.exists()
        and arguments.baseline is not None
        and arguments.baseline_out.resolve() != arguments.baseline.resolve()
    ):
        parser.error("an existing --baseline-out must also be the --baseline input")
    if (
        arguments.baseline_out is not None
        and arguments.baseline_out.resolve() == arguments.out.resolve()
    ):
        parser.error("--baseline-out must differ from --out")
    if arguments.baseline is not None and arguments.baseline.resolve() == arguments.out.resolve():
        parser.error("--baseline must differ from --out")

    input_baseline = _read_baseline(arguments.baseline) if arguments.baseline else None
    report = asyncio.run(
        benchmark(
            models=tuple(item.strip() for item in arguments.models.split(",") if item.strip()),
            ollama_url=arguments.ollama_url,
            fixture_count=arguments.fixtures,
        )
    )
    if input_baseline is not None:
        _reject_regression(_candidate_baseline(report), input_baseline)

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if arguments.baseline_out is not None:
        candidate_baseline = _candidate_baseline(report)
        arguments.baseline_out.parent.mkdir(parents=True, exist_ok=True)
        arguments.baseline_out.write_text(
            candidate_baseline.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
