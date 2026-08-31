"""Deterministically generate non-personal receipt fixtures and labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from clerksan.extract.tax_id import registration_check_digit

_MERCHANTS = ("Sample Store", "North Cafe", "Local Market", "City Utility")
_DEGRADATIONS = ("clean", "rotate", "blur", "jpeg")


def _registration_number(randomizer: random.Random) -> str:
    base = "".join(str(randomizer.randrange(10)) for _ in range(12))
    return f"T{registration_check_digit(base)}{base}"


def _receipt_label(index: int, randomizer: random.Random) -> dict[str, Any]:
    transaction_date = date(2026, 1, 1) + timedelta(days=randomizer.randrange(365))
    subtotal = randomizer.randrange(500, 10_000)
    tax_rate = randomizer.choice((8, 10))
    tax_amount = round(subtotal * tax_rate / 100)
    total = subtotal + tax_amount
    return {
        "id": f"synthetic-{index:04d}",
        "class": "receipt",
        "transaction_date": transaction_date.isoformat(),
        "total_amount": total,
        "tax_rate_lines": [{"rate": tax_rate, "amount": tax_amount}],
        "counterparty": randomizer.choice(_MERCHANTS),
        "registration_number": _registration_number(randomizer),
        "currency": "JPY",
        "degradation": randomizer.choice(_DEGRADATIONS),
    }


def _render_receipt(label: dict[str, Any]) -> Image.Image:
    """Render an intentionally plain, non-personal receipt image."""

    image = Image.new("RGB", (720, 520), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    lines = (
        "CLERK-SAN SYNTHETIC RECEIPT",
        f"Merchant: {label['counterparty']}",
        f"Date: {label['transaction_date']}",
        f"Registration: {label['registration_number']}",
        f"Tax: {label['tax_rate_lines'][0]['rate']}% / {label['tax_rate_lines'][0]['amount']} JPY",
        f"TOTAL: {label['total_amount']} JPY",
    )
    y = 50
    for line in lines:
        draw.text((48, y), line, fill="black", font=font)
        y += 55
    return image


def _degrade(image: Image.Image, mode: str) -> Image.Image:
    if mode == "rotate":
        return image.rotate(2, resample=Image.Resampling.BICUBIC, fillcolor="white")
    if mode == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=0.7))
    return image


def _write_png(image: Image.Image, path: Path, *, jpeg_source: bool) -> None:
    if jpeg_source:
        temporary = path.with_suffix(".jpg")
        image.save(temporary, format="JPEG", quality=40, optimize=False, progressive=False)
        with Image.open(temporary) as compressed:
            compressed.convert("RGB").save(path, format="PNG", compress_level=9)
        temporary.unlink()
        return
    image.save(path, format="PNG", compress_level=9)


def generate(n: int, seed: int, out_dir: Path) -> list[dict[str, Any]]:
    """Render ``n`` deterministic PNGs plus labels and a manifest under ``out_dir``."""

    if n < 1:
        raise ValueError("n must be greater than zero")
    out_dir.mkdir(parents=True, exist_ok=True)
    randomizer = random.Random(seed)
    records: list[dict[str, Any]] = []
    for index in range(n):
        label = _receipt_label(index, randomizer)
        image = _render_receipt(label)
        try:
            degraded = _degrade(image, label["degradation"])
            try:
                image_name = f"{index:04d}.png"
                image_path = out_dir / image_name
                _write_png(degraded, image_path, jpeg_source=label["degradation"] == "jpeg")
            finally:
                if degraded is not image:
                    degraded.close()
        finally:
            image.close()

        label["image"] = image_name
        label["sha256"] = hashlib.sha256(image_path.read_bytes()).hexdigest()
        label_path = out_dir / f"{index:04d}.json"
        label_path.write_text(
            json.dumps(label, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        records.append(label)

    manifest = {"seed": seed, "documents": records}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("eval/fixtures/synthetic"))
    arguments = parser.parse_args()
    records = generate(arguments.n, arguments.seed, arguments.out)
    print(f"Generated {len(records)} deterministic synthetic receipts in {arguments.out}")


if __name__ == "__main__":
    main()
