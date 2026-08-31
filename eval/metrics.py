"""Extraction and retrieval metrics with explicit, type-aware equality rules."""

from __future__ import annotations

import datetime as dt
import resource
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

LOCAL_MEMORY_BUDGET_BYTES = 16 * 1024 * 1024 * 1024


class FieldAccuracy(BaseModel):
    field: str
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


class EvalReport(BaseModel):
    fields: list[FieldAccuracy] = Field(default_factory=list)
    retrieval_hit_rate_at_k: float | None = None
    k: int | None = None
    routing_accuracy: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _normal_text(value: Any) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _same_field(field: str, predicted: Any, expected: Any) -> bool:
    predicted = _unwrap(predicted)
    expected = _unwrap(expected)
    if predicted is None or expected is None:
        return predicted is expected
    if field in {"total_amount", "tax_8_amount", "tax_10_amount"}:
        try:
            return abs(Decimal(str(predicted)) - Decimal(str(expected))) <= Decimal("0.5")
        except (InvalidOperation, ValueError):
            return False
    if field.endswith("date"):
        try:
            return dt.date.fromisoformat(str(predicted)) == dt.date.fromisoformat(str(expected))
        except ValueError:
            return False
    if field == "registration_number":
        return _normal_text(predicted).replace("-", "") == _normal_text(expected).replace("-", "")
    if isinstance(expected, (dict, list)):
        return predicted == expected
    return _normal_text(predicted) == _normal_text(expected)


def field_accuracy(
    predictions: list[dict[str, Any]], labels: list[dict[str, Any]]
) -> list[FieldAccuracy]:
    """Compare aligned records, ignoring label metadata such as fixture file names."""

    if len(predictions) != len(labels):
        raise ValueError("predictions and labels must be aligned and have equal length")
    fields = sorted(
        {
            field
            for label in labels
            for field in label
            if field not in {"id", "class", "image", "sha256", "degradation"}
        }
    )
    results: list[FieldAccuracy] = []
    for field in fields:
        result = FieldAccuracy(field=field)
        for prediction, label in zip(predictions, labels, strict=True):
            if field not in label:
                continue
            result.total += 1
            if _same_field(field, prediction.get(field), label[field]):
                result.correct += 1
        results.append(result)
    return results


def hit_rate_at_k(results: list[list[str]], gold_ids: list[str], k: int = 5) -> float:
    """Compute the share of queries whose gold document appears in the top ``k``."""

    if k < 1:
        raise ValueError("k must be greater than zero")
    if len(results) != len(gold_ids):
        raise ValueError("results and gold_ids must have equal length")
    if not gold_ids:
        return 0.0
    hits = sum(gold in result[:k] for result, gold in zip(results, gold_ids, strict=True))
    return hits / len(gold_ids)


def routing_accuracy(predicted_routes: list[str], expected_routes: list[str]) -> float:
    """Return exact route accuracy for aligned query-routing decisions."""

    if len(predicted_routes) != len(expected_routes):
        raise ValueError("predicted_routes and expected_routes must have equal length")
    if not expected_routes:
        return 0.0
    return sum(
        predicted == expected
        for predicted, expected in zip(predicted_routes, expected_routes, strict=True)
    ) / len(expected_routes)


def process_peak_rss_bytes() -> int:
    """Return the OS-reported peak RSS for this process in bytes.

    ``ru_maxrss`` is KiB on Linux and bytes on macOS.  Both supported local
    environments expose an OS-maintained maximum, so this is deliberately not a
    start/end RSS delta.
    """

    maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return maximum if sys.platform == "darwin" else maximum * 1024


def ollama_residency_snapshot(loaded_models: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """Describe Ollama's own reported resident-model memory for ``model``.

    ``GET /api/ps`` is available both for a host service and an Ollama container,
    unlike a host-process RSS sample.  Its ``size`` field is therefore the portable
    measurement used for the local 16 GiB model-residency budget.  It is intentionally
    reported separately from :func:`process_peak_rss_bytes`, which describes only the
    benchmark Python process.
    """

    canonical = _canonical_model_name(model)
    matching: dict[str, Any] | None = None
    sizes: list[int] = []
    for entry in loaded_models:
        size = _nonnegative_int(entry.get("size"))
        if size is not None:
            sizes.append(size)
        entry_name = str(entry.get("name") or entry.get("model") or "")
        if _canonical_model_name(entry_name) == canonical:
            matching = entry

    model_size = _nonnegative_int(matching.get("size")) if matching is not None else None
    details = matching.get("details") if isinstance(matching, dict) else None
    quantization = (
        details.get("quantization_level")
        if isinstance(details, dict) and isinstance(details.get("quantization_level"), str)
        else None
    )
    return {
        "model_resident_bytes": model_size,
        "server_resident_bytes": sum(sizes) if sizes else None,
        "quantization": quantization,
        "model_loaded": matching is not None,
    }


def memory_budget_passes(
    observed_bytes: int | None, budget_bytes: int = LOCAL_MEMORY_BUDGET_BYTES
) -> bool:
    """Return true only for a measured non-negative residency within the budget."""

    return observed_bytes is not None and observed_bytes <= budget_bytes


def _canonical_model_name(model: str) -> str:
    return model.strip().removesuffix(":latest")


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def regressions(
    report: EvalReport, baseline: EvalReport, *, maximum_drop: float = 0.02
) -> list[str]:
    """Return fields whose accuracy fell by more than the permitted absolute drop."""

    baseline_fields = {field.field: field.accuracy for field in baseline.fields}
    failures: list[str] = []
    for field in report.fields:
        previous = baseline_fields.get(field.field)
        if previous is not None and previous - field.accuracy > maximum_drop:
            failures.append(field.field)
    if (
        report.retrieval_hit_rate_at_k is not None
        and baseline.retrieval_hit_rate_at_k is not None
        and report.k == baseline.k
        and baseline.retrieval_hit_rate_at_k - report.retrieval_hit_rate_at_k > maximum_drop
    ):
        failures.append(f"retrieval_hit_rate_at_{report.k}")
    if (
        report.routing_accuracy is not None
        and baseline.routing_accuracy is not None
        and baseline.routing_accuracy - report.routing_accuracy > maximum_drop
    ):
        failures.append("routing_accuracy")
    return failures
