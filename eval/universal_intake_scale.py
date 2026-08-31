"""Deterministic, non-personal scale fixture for universal delimited intake."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

FIXTURE_ROWS = 1_200
FIXTURE_COLUMNS = 13
FIXTURE_RELATIVE_PATH = "tests/fixtures/generated/universal-intake-1200x13.csv"
MANIFEST_RELATIVE_PATH = "tests/fixtures/generated/manifest.json"


def generate_scale_csv(*, rows: int = FIXTURE_ROWS, columns: int = FIXTURE_COLUMNS) -> bytes:
    """Return a stable UTF-8 CSV containing only visibly synthetic values."""

    if rows < 1 or columns < 1:
        raise ValueError("rows and columns must be positive")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([f"synthetic_field_{column:02d}" for column in range(1, columns + 1)])
    for row in range(1, rows + 1):
        writer.writerow([f"synthetic-r{row:04d}-c{column:02d}" for column in range(1, columns + 1)])
    return output.getvalue().encode("utf-8")


def build_fixture_manifest(payload: bytes) -> dict[str, Any]:
    """Describe the generated fixture without copying any cell values into metadata."""

    return {
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


def write_scale_fixture(repository_root: Path) -> tuple[Path, Path]:
    """Create or verify the committed generated fixture and provenance manifest."""

    root = repository_root.resolve()
    fixture = root / FIXTURE_RELATIVE_PATH
    manifest = root / MANIFEST_RELATIVE_PATH
    payload = generate_scale_csv()
    encoded_manifest = (
        json.dumps(build_fixture_manifest(payload), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_or_verify(fixture, payload)
    _write_or_verify(manifest, encoded_manifest)
    return fixture, manifest


def _write_or_verify(path: Path, expected: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise RuntimeError(f"generated fixture differs from deterministic output: {path.name}")
        return
    path.write_bytes(expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root that will receive the generated fixture",
    )
    arguments = parser.parse_args()
    fixture, manifest = write_scale_fixture(arguments.repository_root)
    print(json.dumps({"fixture": fixture.name, "manifest": manifest.name}, sort_keys=True))


if __name__ == "__main__":
    main()
