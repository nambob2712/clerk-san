#!/usr/bin/env python3
"""Run one real local receipt through OCR, extraction, review, verification, and search."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from clerksan.config import Settings
from clerksan.db.models import Base
from clerksan.db.repositories import DocumentRepo
from clerksan.ingest.pipeline import build_default_dependencies, process_document
from clerksan.review.queue import approve, pending
from clerksan.search.indexer import (
    build_default_dependencies as build_search_dependencies,
)
from clerksan.search.indexer import index_document, search
from eval.synthetic import generate


async def run_demo(settings: Settings, output_dir: Path) -> dict[str, Any]:
    """Execute the real local stages and return only inspectable, non-personal evidence."""

    fixture_dir = output_dir / "fixture"
    label = generate(1, 73, fixture_dir)[0]
    image_path = fixture_dir / label["image"]
    raw = image_path.read_bytes()
    original_path = output_dir / "doc_store" / "originals" / f"{label['sha256']}.png"
    original_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.write_bytes(raw)

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    ingest = build_default_dependencies(settings)
    indexing = build_search_dependencies(settings)
    try:
        async with session_factory() as session:
            document_id = await DocumentRepo(session).create_with_raw(
                filename=image_path.name,
                content_path=original_path.relative_to(settings.storage_dir).as_posix(),
                sha256=hashlib.sha256(raw).hexdigest(),
                mime="image/png",
            )
            await process_document(session, {"document_id": str(document_id)}, dependencies=ingest)
            await session.commit()

        async with session_factory() as session:
            await index_document(session, {"document_id": str(document_id)}, dependencies=indexing)
            await session.commit()

        async with session_factory() as session:
            review_items = await pending(
                session, confidence_threshold=settings.confidence_threshold
            )
            item = next(
                candidate for candidate in review_items if candidate["document_id"] == document_id
            )
            verified_id = await approve(
                session,
                item["extraction_id"],
                expected_version=item["version"],
                corrections={},
                reviewer="local-demo",
            )
            document = await DocumentRepo(session).get(document_id)
            await session.commit()

        async with session_factory() as session:
            hits = await search(
                session,
                f"{label['counterparty']} {label['transaction_date']}",
                dependencies=indexing,
            )
        return {
            "document_id": str(document_id),
            "verified_id": str(verified_id),
            "expected": {
                field: label[field]
                for field in (
                    "counterparty",
                    "transaction_date",
                    "total_amount",
                    "registration_number",
                )
            },
            "extracted": document["extracted"]["payload"],
            "verified": document["verified"],
            "search_hits": [hit.model_dump(mode="json") for hit in hits],
            "storage": str(output_dir),
        }
    finally:
        await ingest.client.aclose()
        await indexing.client.aclose()
        await engine.dispose()


def _prepare_output(path: Path, *, reset: bool) -> Path:
    target = path.resolve()
    if target.exists() and any(target.iterdir()):
        if not reset:
            raise SystemExit(
                f"{target} is not empty; rerun with --reset to replace this demo output"
            )
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".clerksan-demo"))
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ocr-model", default="gemma3:4b")
    parser.add_argument("--extract-model", default="qwen2.5:7b")
    arguments = parser.parse_args()
    output_dir = _prepare_output(arguments.out, reset=arguments.reset)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{output_dir / 'clerksan.sqlite'}",
        storage_dir=output_dir / "doc_store",
        ollama_url=arguments.ollama_url,
        ocr_model=arguments.ocr_model,
        extract_model=arguments.extract_model,
        router_model=arguments.extract_model,
    )
    result = asyncio.run(run_demo(settings, output_dir))
    report_path = output_dir / "demo-result.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
