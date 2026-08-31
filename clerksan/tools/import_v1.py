"""Idempotently import legacy CSV history into the Clerk-san review lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from clerksan.config import Settings
from clerksan.db.engine import get_engine, get_session
from clerksan.db.models import Base, DocumentClass, DocumentStatus
from clerksan.db.repositories import DocumentRepo, ExtractionRepo
from clerksan.storage import relative_storage_path


async def import_v1_data(
    csv_path: Path,
    images_dir: Path,
    *,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Import legacy rows as pending review records without inventing missing images.

    When an old image is missing, the immutable original is a small JSON manifest that
    records the source CSV row and the missing path.  The corresponding extraction has
    ``_import_flags: ["missing_image"]`` so reviewers can see the evidence rather than
    receiving a fabricated image or an already-verified record.
    """

    active_settings = settings or Settings()
    csv_path = csv_path.expanduser().resolve()
    images_dir = images_dir.expanduser().resolve()
    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)
    active_settings.storage_dir.mkdir(parents=True, exist_ok=True)
    if active_settings.is_sqlite:
        async with get_engine(active_settings).begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    totals = {"imported": 0, "skipped": 0, "missing_images": 0}
    async with get_session(active_settings) as session:
        documents = DocumentRepo(session)
        extractions = ExtractionRepo(session)
        for row in _rows(csv_path):
            original, filename, mime, missing_image = _legacy_original(row, images_dir)
            sha256 = hashlib.sha256(original).hexdigest()
            if await documents.find_by_sha256(sha256):
                totals["skipped"] += 1
                continue
            content_path = _preserve_original(
                active_settings.storage_dir, original, sha256, filename
            )
            document_id = await documents.create_with_raw(
                filename=filename,
                content_path=relative_storage_path(active_settings.storage_dir, content_path),
                sha256=sha256,
                mime=mime,
                doc_class=DocumentClass.RECEIPT,
            )
            payload = _legacy_payload(row, missing_image=missing_image)
            await extractions.add(
                document_id,
                payload=payload,
                field_confidences={
                    "transaction_date": 1.0,
                    "total_amount": 1.0,
                    "counterparty": 1.0,
                    "currency": 1.0,
                    "expense_category": 1.0,
                },
                model_name="v1_import",
                prompt_version="v1-csv",
                actor="v1-import",
            )
            await documents.set_status(document_id, DocumentStatus.IN_REVIEW)
            totals["imported"] += 1
            totals["missing_images"] += int(missing_image)
    return totals


def _rows(csv_path: Path) -> Iterable[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def _legacy_original(row: dict[str, str], images_dir: Path) -> tuple[bytes, str, str, bool]:
    raw_path = (row.get("Image_Path") or "").replace("\\", "/")
    candidate = Path(raw_path)
    paths = [images_dir / candidate.name]
    if raw_path:
        paths.append(images_dir / candidate)
    image = next((path for path in paths if path.is_file()), None)
    if image is not None:
        return image.read_bytes(), image.name, _mime_for(image), False

    manifest = {
        "legacy_id": row.get("ID", ""),
        "missing_image_path": raw_path,
        "row": {
            key: row.get(key, "")
            for key in ("Timestamp", "Date", "Total_Amount", "Currency", "Category", "Merchant")
        },
    }
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        ),
        f"legacy-{row.get('ID') or 'row'}.missing-image.json",
        "application/json",
        True,
    )


def _preserve_original(storage_dir: Path, raw: bytes, sha256: str, filename: str) -> Path:
    suffix = Path(filename).suffix.lower() or ".bin"
    target = storage_dir / "originals" / f"{sha256}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _legacy_payload(row: dict[str, str], *, missing_image: bool) -> dict[str, Any]:
    line_items = _line_items(row.get("Line_Items", ""))
    payload: dict[str, Any] = {
        "transaction_date": _value(row.get("Date"), "v1 CSV:Date"),
        "total_amount": _value(_number(row.get("Total_Amount")), "v1 CSV:Total_Amount"),
        "counterparty": _value(row.get("Merchant"), "v1 CSV:Merchant"),
        "currency": _value(row.get("Currency"), "v1 CSV:Currency"),
        "expense_category": _value(row.get("Category"), "v1 CSV:Category"),
        "line_items": line_items,
        "_legacy_id": row.get("ID", ""),
    }
    if missing_image:
        payload["_import_flags"] = ["missing_image"]
    return payload


def _value(value: Any, source_span: str) -> dict[str, Any]:
    return {"value": value, "confidence": 1.0, "source_span": source_span}


def _number(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _line_items(raw: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _mime_for(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")


def main() -> None:
    """CLI entry point for a review-safe, repeatable legacy import."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("images_dir", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(import_v1_data(arguments.csv_path, arguments.images_dir))))


if __name__ == "__main__":
    main()
