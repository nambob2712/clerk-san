"""Benchmark local OCR candidates on deterministic, non-personal receipt fixtures."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from clerksan.config import Settings
from clerksan.llm.client import OllamaClient
from clerksan.llm.ocr import OcrEngine, OptionalOcrRuntimeUnavailable, get_ocr_engine
from eval.metrics import (
    LOCAL_MEMORY_BUDGET_BYTES,
    memory_budget_passes,
    ollama_residency_snapshot,
    process_peak_rss_bytes,
)
from eval.synthetic import generate

DEFAULT_ENGINES = ("vision_llm", "yomitoku", "paddleocr")
BENCHMARK_KEEP_ALIVE = "5m"


async def benchmark(
    *,
    engines: tuple[str, ...] = DEFAULT_ENGINES,
    ocr_model: str = "gemma3:4b",
    ollama_url: str = "http://127.0.0.1:11434",
    fixture_count: int = 8,
) -> dict[str, Any]:
    """Measure installed OCR candidates and make the choice criteria explicit."""

    with tempfile.TemporaryDirectory(prefix="clerksan-ocr-") as temporary:
        fixture_dir = Path(temporary)
        labels = generate(fixture_count, 42, fixture_dir)
        fixture = {
            "generator": "eval.synthetic.generate",
            "seed": 42,
            "documents": fixture_count,
            "manifest_sha256": _fixture_manifest_sha256(fixture_dir),
        }
        client = OllamaClient(
            Settings(
                database_url="sqlite+aiosqlite:///:memory:",
                ollama_url=ollama_url,
                ocr_model=ocr_model,
            ),
            request_keep_alive=BENCHMARK_KEEP_ALIVE,
        )
        vision_artifact: dict[str, str | None] | None = None
        vision_preflight_error: str | None = None
        vision_was_loaded = False
        vision_unloaded = False
        try:
            if "vision_llm" in engines:
                try:
                    installed = await client.list_models()
                    vision_artifact = _model_artifact(ocr_model, installed)
                    vision_was_loaded = _model_is_loaded(ocr_model, await client.loaded_models())
                except Exception as error:
                    vision_preflight_error = f"{type(error).__name__}: {error}"
            reports = []
            for engine_name in engines:
                if engine_name == "vision_llm" and vision_preflight_error is not None:
                    reports.append(
                        {
                            "engine": engine_name,
                            "model": ocr_model,
                            "status": "failed",
                            "reason": (
                                "Vision benchmark needs local Ollama model and residency evidence: "
                                f"{vision_preflight_error}"
                            ),
                        }
                    )
                    continue
                reports.append(
                    await _benchmark_engine(
                        engine_name,
                        labels,
                        fixture_dir,
                        client,
                        ocr_model=ocr_model,
                        ollama_url=ollama_url,
                        model_artifact=vision_artifact if engine_name == "vision_llm" else None,
                    )
                )
                if engine_name == "vision_llm" and not vision_was_loaded:
                    try:
                        await client.unload(ocr_model)
                        vision_unloaded = True
                    except Exception:
                        pass
        finally:
            if (
                "vision_llm" in engines
                and not vision_was_loaded
                and not vision_unloaded
                and vision_preflight_error is None
            ):
                try:
                    await client.unload(ocr_model)
                except Exception:
                    pass
            await client.aclose()
    selected = _select(reports)
    return {
        "decision_version": 1,
        "fixture_count": fixture_count,
        "fixture": fixture,
        "criterion": "highest field recovery among available candidates at <=15 seconds/page",
        "memory_budget_bytes": LOCAL_MEMORY_BUDGET_BYTES,
        "candidates": reports,
        "selected": selected,
        "git_revision": _git_revision(),
        "recorded_at": datetime.now(UTC).isoformat(),
        "notes": (
            "An unavailable optional runtime is reported, not treated as a zero-score model. "
            "Run this command again after installing YomiToku or PaddleOCR to compare it."
        ),
    }


async def _benchmark_engine(
    engine_name: str,
    labels: list[dict[str, Any]],
    fixture_dir: Path,
    client: OllamaClient,
    *,
    ocr_model: str,
    ollama_url: str,
    model_artifact: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        ollama_url=ollama_url,
        ocr_engine=engine_name,
        ocr_model=ocr_model,
    )
    engine: OcrEngine = get_ocr_engine(settings, client)
    process = psutil.Process()
    start_rss = process.memory_info().rss
    start_peak_rss = process_peak_rss_bytes()
    start = time.perf_counter()
    successes: list[dict[str, Any]] = []
    ollama_model_peak: int | None = None
    ollama_server_peak: int | None = None
    quantization = model_artifact.get("quantization") if model_artifact else None
    try:
        for label in labels:
            result = await engine.ocr((fixture_dir / label["image"]).read_bytes())
            successes.append(
                {
                    "counterparty": label["counterparty"] in result.text,
                    "transaction_date": label["transaction_date"] in result.text,
                    "total_amount": str(label["total_amount"]) in result.text,
                    "registration_number": label["registration_number"] in result.text,
                }
            )
            if engine_name == "vision_llm":
                snapshot = ollama_residency_snapshot(await client.loaded_models(), ocr_model)
                model_bytes = snapshot["model_resident_bytes"]
                server_bytes = snapshot["server_resident_bytes"]
                if model_bytes is None or server_bytes is None:
                    raise RuntimeError(
                        "Ollama /api/ps did not report resident memory for the OCR model"
                    )
                ollama_model_peak = max(ollama_model_peak or 0, model_bytes)
                ollama_server_peak = max(ollama_server_peak or 0, server_bytes)
                quantization = snapshot["quantization"] or quantization
    except OptionalOcrRuntimeUnavailable as error:
        return {"engine": engine_name, "status": "unavailable", "reason": str(error)}
    except Exception as error:
        return {
            "engine": engine_name,
            "status": "failed",
            "reason": f"{type(error).__name__}: {error}",
        }
    elapsed = time.perf_counter() - start
    totals = {
        field: sum(int(row[field]) for row in successes) / len(successes)
        for field in ("counterparty", "transaction_date", "total_amount", "registration_number")
    }
    peak_rss = process_peak_rss_bytes()
    budget_passed = (
        memory_budget_passes(ollama_server_peak) if engine_name == "vision_llm" else None
    )
    return {
        "engine": engine_name,
        "model": ocr_model if engine_name == "vision_llm" else None,
        "resolved_digest": model_artifact.get("resolved_digest") if model_artifact else None,
        "quantization": quantization,
        "status": (
            "available" if engine_name != "vision_llm" or budget_passed else "over_memory_budget"
        ),
        "field_recovery": totals,
        "mean_field_recovery": round(sum(totals.values()) / len(totals), 4),
        "seconds_per_page": round(elapsed / len(labels), 3),
        "peak_rss_bytes": peak_rss,
        "peak_rss_increase_bytes": max(0, peak_rss - start_peak_rss),
        "process_rss_delta_bytes": process.memory_info().rss - start_rss,
        "ollama_model_peak_resident_bytes": ollama_model_peak,
        "ollama_server_peak_resident_bytes": ollama_server_peak,
        "memory_budget_passed": budget_passed,
    }


def _select(reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        report
        for report in reports
        if report.get("status") == "available"
        and report.get("seconds_per_page", float("inf")) <= 15
    ]
    if not candidates:
        return None
    winner = max(
        candidates,
        key=lambda report: (
            float(report["mean_field_recovery"]),
            -float(report["seconds_per_page"]),
        ),
    )
    return {
        "engine": winner["engine"],
        "model": winner.get("model"),
        "reason": "best available deterministic field recovery within the latency guardrail",
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# OCR benchmark",
        "",
        f"Criterion: {report['criterion']}.",
        "",
        "| Engine | Status | Mean field recovery | Seconds/page | Peak RSS |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for candidate in report["candidates"]:
        lines.append(
            "| {engine} | {status} | {recovery} | {seconds} | {peak_rss} |".format(
                engine=candidate["engine"],
                status=candidate["status"],
                recovery=candidate.get("mean_field_recovery", "—"),
                seconds=candidate.get("seconds_per_page", "—"),
                peak_rss=_format_memory(candidate.get("peak_rss_bytes")),
            )
        )
        if candidate.get("reason"):
            lines.append(f"\n- {candidate['engine']}: {candidate['reason']}")
    fixture = report.get("fixture")
    lines.extend(("", "## Reproducibility"))
    if isinstance(fixture, dict):
        lines.extend(
            (
                f"- Fixture generator: `{fixture.get('generator', 'unknown')}`.",
                f"- Fixture seed/documents: `{fixture.get('seed', 'unknown')}` / "
                f"`{fixture.get('documents', 'unknown')}`.",
                f"- Fixture manifest SHA-256: `{fixture.get('manifest_sha256', 'unknown')}`.",
            )
        )
    lines.append(f"- Git revision: `{report.get('git_revision', 'unknown')}`.")
    lines.append(f"- Recorded at: `{report.get('recorded_at', 'unknown')}`.")
    for candidate in report["candidates"]:
        if candidate.get("model"):
            lines.append(
                "- {engine}: model `{model}`, digest `{digest}`, quantization `{quantization}`, "
                "Ollama server resident peak {resident}.".format(
                    engine=candidate["engine"],
                    model=candidate["model"],
                    digest=candidate.get("resolved_digest") or "unknown",
                    quantization=candidate.get("quantization") or "unknown",
                    resident=_format_memory(candidate.get("ollama_server_peak_resident_bytes")),
                )
            )
    lines.extend(
        (
            "",
            "## Selection",
            "",
            json.dumps(report["selected"], ensure_ascii=False, indent=2),
            "",
            report["notes"],
            "",
            "Peak RSS is the OS-reported benchmark-process lifetime maximum (ru_maxrss), "
            "not a model-server measurement. Vision candidates also record the Ollama "
            "`/api/ps` server resident peak and must stay within the configured 16 GiB "
            "budget. The compatibility process_rss_delta_bytes field is reported only in "
            "the JSON result.",
        )
    )
    return "\n".join(lines) + "\n"


def _format_memory(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value / (1024 * 1024):.1f} MiB"


def _fixture_manifest_sha256(fixture_dir: Path) -> str:
    return hashlib.sha256((fixture_dir / "manifest.json").read_bytes()).hexdigest()


def _model_artifact(model: str, installed: list[dict[str, Any]]) -> dict[str, str | None]:
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
            digest = entry.get("digest")
            return {
                "resolved_digest": digest if isinstance(digest, str) and digest else None,
                "quantization": quantization,
            }
    return {"resolved_digest": None, "quantization": None}


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engines", default=",".join(DEFAULT_ENGINES))
    parser.add_argument("--ocr-model", default="gemma3:4b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--fixtures", type=int, default=8)
    parser.add_argument("--out", type=Path, default=Path("eval/results/ocr_benchmark.md"))
    arguments = parser.parse_args()
    report = asyncio.run(
        benchmark(
            engines=tuple(name.strip() for name in arguments.engines.split(",") if name.strip()),
            ocr_model=arguments.ocr_model,
            ollama_url=arguments.ollama_url,
            fixture_count=arguments.fixtures,
        )
    )
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
