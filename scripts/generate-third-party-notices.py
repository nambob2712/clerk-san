#!/usr/bin/env python3
"""Generate the deterministic source-lock reference notice index."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-license-policy.py"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=ROOT / "THIRD_PARTY_NOTICES.md",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    output = _parser().parse_args(argv).output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp.{os.getpid()}")
    try:
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--write-notices", str(temporary)],
            cwd=ROOT,
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
