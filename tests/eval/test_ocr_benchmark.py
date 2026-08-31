from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from eval import benchmark_ocr
from eval.benchmark_ocr import _format_memory, _markdown, _select
from eval.metrics import process_peak_rss_bytes


def test_ocr_selection_ignores_unavailable_and_over_budget_candidates() -> None:
    assert _select(
        [
            {"engine": "missing", "status": "unavailable"},
            {
                "engine": "slow",
                "status": "available",
                "mean_field_recovery": 1.0,
                "seconds_per_page": 16,
            },
            {
                "engine": "local",
                "status": "available",
                "mean_field_recovery": 0.9,
                "seconds_per_page": 3,
            },
        ]
    ) == {
        "engine": "local",
        "model": None,
        "reason": "best available deterministic field recovery within the latency guardrail",
    }


def test_ocr_markdown_includes_actual_peak_memory_measurement() -> None:
    markdown = _markdown(
        {
            "criterion": "field recovery",
            "candidates": [
                {
                    "engine": "local",
                    "status": "available",
                    "mean_field_recovery": 1.0,
                    "seconds_per_page": 1.0,
                    "peak_rss_bytes": 64 * 1024 * 1024,
                }
            ],
            "selected": {"engine": "local"},
            "notes": "test",
        }
    )

    assert "| Peak RSS |" in markdown
    assert "64.0 MiB" in markdown
    assert "ru_maxrss" in markdown
    assert _format_memory(None) == "—"
    assert process_peak_rss_bytes() > 0


@pytest.mark.asyncio
async def test_ocr_benchmark_records_os_peak_rss(monkeypatch, tmp_path: Path) -> None:
    class FakeEngine:
        async def ocr(self, _: bytes) -> SimpleNamespace:
            return SimpleNamespace(text="North Cafe 2026-07-15 1234 T1234567890123")

    class FakeClient:
        async def loaded_models(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "gemma3:4b",
                    "size": 3 * 1024 * 1024 * 1024,
                    "details": {"quantization_level": "Q4_K_M"},
                }
            ]

    peaks = iter((100, 350))
    monkeypatch.setattr(benchmark_ocr, "get_ocr_engine", lambda *_: FakeEngine())
    monkeypatch.setattr(benchmark_ocr, "process_peak_rss_bytes", lambda: next(peaks))
    image = tmp_path / "fixture.png"
    image.write_bytes(b"fixture")

    report = await benchmark_ocr._benchmark_engine(
        "vision_llm",
        [
            {
                "image": image.name,
                "counterparty": "North Cafe",
                "transaction_date": "2026-07-15",
                "total_amount": 1234,
                "registration_number": "T1234567890123",
            }
        ],
        tmp_path,
        client=FakeClient(),  # type: ignore[arg-type]
        ocr_model="gemma3:4b",
        ollama_url="http://127.0.0.1:11434",
        model_artifact={"resolved_digest": "a" * 64, "quantization": "Q4_K_M"},
    )

    assert report["peak_rss_bytes"] == 350
    assert report["peak_rss_increase_bytes"] == 250
    assert report["ollama_server_peak_resident_bytes"] == 3 * 1024 * 1024 * 1024
    assert report["memory_budget_passed"] is True


@pytest.mark.asyncio
async def test_non_vision_ocr_benchmark_does_not_need_an_ollama_preflight(monkeypatch) -> None:
    class FakeClient:
        async def list_models(self) -> list[dict[str, object]]:
            raise AssertionError("non-vision engines must not contact Ollama")

        async def aclose(self) -> None:
            return None

    class FakeEngine:
        async def ocr(self, _: bytes) -> SimpleNamespace:
            return SimpleNamespace(text="North Cafe 2026-07-15 1234 T1234567890123")

    monkeypatch.setattr(benchmark_ocr, "OllamaClient", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr(benchmark_ocr, "get_ocr_engine", lambda *_args: FakeEngine())

    report = await benchmark_ocr.benchmark(engines=("yomitoku",), fixture_count=1)

    assert report["candidates"][0]["status"] == "available"
    assert report["candidates"][0]["model"] is None
