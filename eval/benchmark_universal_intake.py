"""Measure deterministic preserve, CSV parse, and SQL staging without model latency."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil
import sqlalchemy
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.db.models import Base, SpreadsheetRow
from clerksan.db.repositories import DocumentRepo
from clerksan.ingest.adapters.delimited import DelimitedAdapter
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource
from clerksan.ingest.staging import stage_tabular_rows
from clerksan.ingest.storage_reconcile import (
    finalize_reservation,
    publish_reserved_blob,
    reserve_quarantine,
)
from eval.universal_intake_scale import FIXTURE_COLUMNS, FIXTURE_ROWS, generate_scale_csv

TIME_BUDGET_SECONDS = 5.0
RSS_BUDGET_BYTES = 256 * 1024 * 1024


@contextmanager
def _peak_rss_sampler(interval_seconds: float = 0.002) -> Iterator[dict[str, int]]:
    process = psutil.Process()
    baseline = process.memory_info().rss
    state = {"baseline": baseline, "peak": baseline}
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(interval_seconds):
            state["peak"] = max(state["peak"], process.memory_info().rss)

    thread = threading.Thread(target=sample, name="clerksan-scale-rss", daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state["peak"] = max(state["peak"], process.memory_info().rss)
        stop.set()
        thread.join(timeout=1)


async def run_once(payload: bytes) -> dict[str, Any]:
    """Run the measured structural path once in a fresh local database and store."""

    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="clerksan-generated-scale-") as temporary:
        root = Path(temporary)
        storage = root / "doc_store"
        engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'clerksan.sqlite'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            with _peak_rss_sampler() as rss:
                started = time.perf_counter()
                reservation = reserve_quarantine(storage)
                reservation.payload_path.write_bytes(payload)
                published = publish_reserved_blob(reservation, digest)

                async with factory() as session:
                    document_id = await DocumentRepo(session).create_with_raw(
                        filename="generated-scale.csv",
                        content_path=published.relative_path,
                        sha256=digest,
                        mime="text/csv",
                        detected_family="delimited",
                        detected_format="csv",
                    )
                    descriptor = os.open(published.path, os.O_RDONLY)
                    try:
                        normalized = DelimitedAdapter().normalize(
                            ReadOnlySource(
                                descriptor,
                                digest,
                                filename="generated-scale.csv",
                                mime_type="text/csv",
                            ),
                            AdapterContext("delimited.csv", metadata={"detected_type": "csv"}),
                        )
                    finally:
                        os.close(descriptor)
                    staged = await stage_tabular_rows(
                        document_id,
                        None,
                        1,
                        normalized.tables,
                        session,
                    )
                    await session.commit()
                finalize_reservation(reservation)
                elapsed = time.perf_counter() - started

            async with factory() as session:
                persisted = int(
                    await session.scalar(select(func.count()).select_from(SpreadsheetRow)) or 0
                )
            quarantine_files = (
                [path for path in (storage / ".quarantine").rglob("*") if path.is_file()]
                if (storage / ".quarantine").exists()
                else []
            )
        finally:
            await engine.dispose()

    if staged != FIXTURE_ROWS or persisted != FIXTURE_ROWS:
        raise AssertionError("generated scale rows were not preserved through SQL staging")
    if len(normalized.tables) != 1 or len(normalized.tables[0].header) != FIXTURE_COLUMNS:
        raise AssertionError("generated scale fixture shape changed during parsing")
    if quarantine_files:
        raise AssertionError("generated scale run left temporary quarantine files")
    return {
        "elapsed_seconds": round(elapsed, 6),
        "peak_rss_bytes": rss["peak"],
        "peak_rss_delta_bytes": max(0, rss["peak"] - rss["baseline"]),
        "staged_rows": staged,
        "persisted_rows": persisted,
        "columns": len(normalized.tables[0].header),
        "model_calls": 0,
        "temporary_files": 0,
    }


async def benchmark(*, repetitions: int = 3, warmups: int = 1) -> dict[str, Any]:
    if repetitions < 1 or warmups < 0:
        raise ValueError("repetitions must be positive and warmups must not be negative")
    payload = generate_scale_csv()
    for _ in range(warmups):
        await run_once(payload)
    runs = [await run_once(payload) for _ in range(repetitions)]
    elapsed_values = [float(run["elapsed_seconds"]) for run in runs]
    peak_values = [int(run["peak_rss_bytes"]) for run in runs]
    return {
        "fixture": {
            "generated": True,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "rows": FIXTURE_ROWS,
            "columns": FIXTURE_COLUMNS,
            "bytes": len(payload),
        },
        "method": {
            "scope": "preserve_parse_stage",
            "warmups": warmups,
            "repetitions": repetitions,
            "model_calls": 0,
            "database": "SQLite",
        },
        "runtime": {
            "python": platform.python_version(),
            "sqlalchemy": sqlalchemy.__version__,
            "machine": platform.machine(),
            "logical_cpus": psutil.cpu_count(logical=True),
            "memory_bytes": psutil.virtual_memory().total,
        },
        "runs": runs,
        "summary": {
            "elapsed_seconds_min": min(elapsed_values),
            "elapsed_seconds_median": statistics.median(elapsed_values),
            "elapsed_seconds_max": max(elapsed_values),
            "peak_rss_bytes_max": max(peak_values),
            "time_budget_seconds": TIME_BUDGET_SECONDS,
            "rss_budget_bytes": RSS_BUDGET_BYTES,
            "time_budget_passed": max(elapsed_values) <= TIME_BUDGET_SECONDS,
            "rss_budget_passed": max(peak_values) <= RSS_BUDGET_BYTES,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--json-out", type=Path)
    arguments = parser.parse_args()
    result = asyncio.run(benchmark(repetitions=arguments.repetitions, warmups=arguments.warmups))
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.json_out is not None:
        arguments.json_out.parent.mkdir(parents=True, exist_ok=True)
        arguments.json_out.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    if not all((result["summary"]["time_budget_passed"], result["summary"]["rss_budget_passed"])):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
