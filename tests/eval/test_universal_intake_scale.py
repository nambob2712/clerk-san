from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from eval.benchmark_universal_intake import (
    RSS_BUDGET_BYTES,
    TIME_BUDGET_SECONDS,
)
from eval.universal_intake_scale import (
    FIXTURE_COLUMNS,
    FIXTURE_RELATIVE_PATH,
    FIXTURE_ROWS,
    MANIFEST_RELATIVE_PATH,
    build_fixture_manifest,
    generate_scale_csv,
    write_scale_fixture,
)

ROOT = Path(__file__).resolve().parents[2]


def test_generated_scale_fixture_has_stable_shape_and_provenance() -> None:
    payload = generate_scale_csv()
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
    manifest = build_fixture_manifest(payload)

    assert len(rows) == FIXTURE_ROWS + 1
    assert all(len(row) == FIXTURE_COLUMNS for row in rows)
    assert manifest == {
        "format": 1,
        "generated": True,
        "generator": "eval.universal_intake_scale",
        "files": [
            {
                "path": FIXTURE_RELATIVE_PATH,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "rows": FIXTURE_ROWS,
                "columns": FIXTURE_COLUMNS,
            }
        ],
    }


def test_committed_generated_fixture_matches_generator_and_manifest() -> None:
    fixture = ROOT / FIXTURE_RELATIVE_PATH
    manifest_path = ROOT / MANIFEST_RELATIVE_PATH
    payload = generate_scale_csv()

    assert fixture.read_bytes() == payload
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == build_fixture_manifest(payload)


def test_fixture_writer_refuses_to_replace_changed_generated_data(tmp_path: Path) -> None:
    fixture, _ = write_scale_fixture(tmp_path)
    fixture.write_text("changed", encoding="utf-8")

    with pytest.raises(RuntimeError, match="differs from deterministic output"):
        write_scale_fixture(tmp_path)


def test_generated_scale_preserve_parse_stage_meets_local_resource_gate() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "eval.benchmark_universal_intake",
            "--repetitions",
            "1",
            "--warmups",
            "0",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)

    assert result["fixture"]["rows"] == FIXTURE_ROWS
    assert result["fixture"]["columns"] == FIXTURE_COLUMNS
    assert result["method"]["model_calls"] == 0
    assert result["runs"][0]["persisted_rows"] == FIXTURE_ROWS
    assert result["runs"][0]["temporary_files"] == 0
    assert result["summary"]["elapsed_seconds_max"] <= TIME_BUDGET_SECONDS
    assert result["summary"]["peak_rss_bytes_max"] <= RSS_BUDGET_BYTES
