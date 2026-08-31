"""Evaluate extraction predictions, R2 query evidence, or fixture integrity.

Without ``--predictions`` this command deliberately performs only fixture-integrity
validation.  It never copies labels into an extraction score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.metrics import EvalReport, field_accuracy, hit_rate_at_k, regressions, routing_accuracy
from eval.synthetic import generate

DEFAULT_SYNTHETIC_COUNT = 8
DEFAULT_SYNTHETIC_SEED = 42


def _load_labels(fixtures: Path) -> list[dict[str, Any]]:
    manifest = fixtures / "manifest.json"
    if manifest.exists():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        records = payload.get("documents", [])
        if not isinstance(records, list):
            raise ValueError(f"Fixture manifest {manifest} must contain a documents list")
        return _validate_records(records, source=manifest)

    records: list[dict[str, Any]] = []
    for path in sorted(fixtures.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Fixture label {path} must contain a JSON object")
        if "image" not in payload:
            payload = {**payload, **_infer_image(path)}
        records.append(payload)
    return _validate_records(records, source=fixtures)


def _validate_records(records: list[Any], *, source: Path) -> list[dict[str, Any]]:
    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"Fixture records in {source} must be JSON objects")
    return [dict(record) for record in records]


def _infer_image(label_path: Path) -> dict[str, str]:
    for suffix in (".png", ".jpg", ".jpeg", ".pdf"):
        candidate = label_path.with_suffix(suffix)
        if candidate.exists():
            return {"image": candidate.name}
    return {}


def _validate_fixture_integrity(fixtures: Path, labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify label/image linkage and optional recorded SHA-256 values."""

    root = fixtures.resolve()
    failures: list[str] = []
    for index, label in enumerate(labels):
        label_id = str(label.get("id", index))
        image = label.get("image")
        if not isinstance(image, str) or not image:
            failures.append(f"{label_id}: missing image reference")
            continue
        image_path = (root / image).resolve()
        try:
            image_path.relative_to(root)
        except ValueError:
            failures.append(f"{label_id}: image escapes fixture directory")
            continue
        if not image_path.is_file():
            failures.append(f"{label_id}: missing image {image}")
            continue
        expected_digest = label.get("sha256")
        if isinstance(expected_digest, str) and expected_digest:
            actual_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
            if actual_digest != expected_digest:
                failures.append(f"{label_id}: SHA-256 mismatch for {image}")
    if failures:
        details = "; ".join(failures[:5])
        remaining = len(failures) - 5
        if remaining > 0:
            details += f"; and {remaining} more"
        raise ValueError(f"Fixture integrity failed: {details}")
    return {"verified": True, "documents": len(labels), "directory": str(fixtures)}


def _load_predictions(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("predictions")
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"Prediction file {path} must be a JSON array of objects")
    return list(payload)


def _load_query_evidence(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("query_evidence", payload.get("queries", payload.get("results")))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"Query evidence {path} must be a JSON array of objects")
    return list(payload)


def _query_metrics(
    evidence: list[dict[str, Any]], *, k: int
) -> tuple[float | None, float | None, dict[str, int]]:
    """Extract retrieval and routing metrics from a single inspectable result file."""

    ranked_documents: list[list[str]] = []
    gold_documents: list[str] = []
    predicted_routes: list[str] = []
    expected_routes: list[str] = []
    for index, item in enumerate(evidence):
        retrieved = item.get(
            "retrieved_documents", item.get("retrieved_ids", item.get("retrieved"))
        )
        gold = item.get("gold_document")
        if retrieved is not None or gold is not None:
            if not isinstance(gold, str) or not gold:
                raise ValueError(f"Query evidence entry {index} needs a non-empty gold_document")
            if not isinstance(retrieved, list) or not all(
                isinstance(document, str) for document in retrieved
            ):
                raise ValueError(
                    f"Query evidence entry {index} needs retrieved_documents as an array of IDs"
                )
            gold_documents.append(gold)
            ranked_documents.append(retrieved)

        expected = item.get("expected_route")
        actual = item.get("actual_route", item.get("predicted_route", item.get("route")))
        if expected is not None or actual is not None:
            if not isinstance(expected, str) or not expected:
                raise ValueError(f"Query evidence entry {index} needs a non-empty expected_route")
            if not isinstance(actual, str) or not actual:
                raise ValueError(
                    f"Query evidence entry {index} needs actual_route, predicted_route, or route"
                )
            expected_routes.append(expected)
            predicted_routes.append(actual)

    if not gold_documents and not expected_routes:
        raise ValueError("Query evidence contains neither retrieval ranks nor routing decisions")
    return (
        hit_rate_at_k(ranked_documents, gold_documents, k=k) if gold_documents else None,
        routing_accuracy(predicted_routes, expected_routes) if expected_routes else None,
        {
            "queries": len(evidence),
            "retrieval_queries": len(gold_documents),
            "routing_queries": len(expected_routes),
        },
    )


def _git_revision() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def evaluate(
    labels: list[dict[str, Any]],
    predictions: list[dict[str, Any]] | None,
    *,
    mode: str,
    k: int = 5,
    query_evidence: list[dict[str, Any]] | None = None,
    fixture_integrity: dict[str, Any] | None = None,
) -> EvalReport:
    """Build an explicit report without ever treating labels as predictions."""

    retrieval_rate: float | None = None
    route_rate: float | None = None
    query_meta: dict[str, int] | None = None
    if query_evidence is not None:
        retrieval_rate, route_rate, query_meta = _query_metrics(query_evidence, k=k)
    return EvalReport(
        fields=field_accuracy(predictions, labels) if predictions is not None else [],
        retrieval_hit_rate_at_k=retrieval_rate,
        k=k if retrieval_rate is not None else None,
        routing_accuracy=route_rate,
        meta={
            "mode": mode,
            "prediction_evaluation": predictions is not None,
            "git_revision": _git_revision(),
            "documents": len(labels),
            "fixture_integrity": fixture_integrity,
            "query_evidence": query_meta,
        },
    )


def _prepare_fixtures(
    arguments: argparse.Namespace,
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None, str]:
    if arguments.fixtures is not None:
        if not arguments.fixtures.is_dir():
            raise ValueError(f"Fixture directory does not exist: {arguments.fixtures}")
        return arguments.fixtures, None, "provided"
    if arguments.generate_synthetic is not None:
        generate(arguments.fixture_count, arguments.fixture_seed, arguments.generate_synthetic)
        return arguments.generate_synthetic, None, "generated-persistent"
    temporary = tempfile.TemporaryDirectory(prefix="clerksan-eval-")
    fixture_dir = Path(temporary.name)
    generate(arguments.fixture_count, arguments.fixture_seed, fixture_dir)
    return fixture_dir, temporary, "generated-temporary"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_source = parser.add_mutually_exclusive_group()
    fixture_source.add_argument(
        "--fixtures", type=Path, help="Existing directory of labels and images"
    )
    fixture_source.add_argument(
        "--generate-synthetic",
        type=Path,
        help="Write deterministic non-personal receipt fixtures to this directory",
    )
    parser.add_argument("--fixture-count", type=int, default=DEFAULT_SYNTHETIC_COUNT)
    parser.add_argument("--fixture-seed", type=int, default=DEFAULT_SYNTHETIC_SEED)
    parser.add_argument(
        "--predictions", type=Path, help="JSON predictions aligned with fixture labels"
    )
    parser.add_argument(
        "--query-results",
        type=Path,
        help="JSON retrieval ranks and/or routing decisions; see eval/fixtures/README.md",
    )
    parser.add_argument("--k", type=int, default=5, help="Retrieval cutoff for --query-results")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--out", type=Path, default=Path("eval/results"))
    parser.add_argument("--allow-regression", action="store_true")
    arguments = parser.parse_args()

    if arguments.fixture_count < 1:
        parser.error("--fixture-count must be positive")
    if arguments.k < 1:
        parser.error("--k must be positive")
    if arguments.baseline and not arguments.predictions and not arguments.query_results:
        parser.error(
            "--baseline needs --predictions and/or --query-results, not fixture integrity alone"
        )

    fixture_dir, temporary, fixture_origin = _prepare_fixtures(arguments)
    try:
        labels = _load_labels(fixture_dir)
        if not labels:
            raise SystemExit(f"No fixture labels found under {fixture_dir}")
        integrity = _validate_fixture_integrity(fixture_dir, labels)
        integrity["origin"] = fixture_origin
        predictions = _load_predictions(arguments.predictions) if arguments.predictions else None
        query_evidence = (
            _load_query_evidence(arguments.query_results) if arguments.query_results else None
        )
        report = evaluate(
            labels,
            predictions,
            mode=(
                "predictions"
                if predictions is not None
                else "query-evidence"
                if query_evidence is not None
                else "fixture-integrity"
            ),
            k=arguments.k,
            query_evidence=query_evidence,
            fixture_integrity=integrity,
        )

        baseline_path = arguments.baseline
        if baseline_path and baseline_path.exists() and not arguments.allow_regression:
            baseline = EvalReport.model_validate_json(baseline_path.read_text(encoding="utf-8"))
            dropped = regressions(report, baseline)
            if dropped:
                print(f"Regression beyond two points: {', '.join(dropped)}", file=sys.stderr)
                raise SystemExit(1)

        arguments.out.mkdir(parents=True, exist_ok=True)
        report_kind = (
            "extraction"
            if predictions is not None
            else "query"
            if query_evidence
            else "fixture-integrity"
        )
        report_path = arguments.out / f"{report_kind}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        if predictions is None and query_evidence is None:
            print(
                "No extraction predictions supplied; this is fixture-integrity, "
                "not an accuracy score."
            )
        elif predictions is None:
            print("No extraction predictions supplied; the report contains query evidence only.")
        print(report.model_dump_json(indent=2))
        print(f"Wrote {report_path}")
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    main()
