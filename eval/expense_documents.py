"""Generate deterministic non-personal expense documents for local application testing."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

FIXTURE_MARKER = "CLERK-SAN SYNTHETIC - NON-PERSONAL"


@dataclass(frozen=True)
class ExpenseFixture:
    """One document plus the fields a reviewer should expect to see."""

    identifier: str
    filename: str
    mime_type: str
    source_format: str
    language: str
    document_class: str
    expense_kind: str
    transaction_date: str
    due_date: str | None
    total_amount: int
    currency: str
    counterparty: str
    body: tuple[str, ...]
    billing_period: str | None = None
    consumption_value: float | None = None
    consumption_unit: str | None = None
    scanned_variant: str | None = None

    def label(self, sha256: str) -> dict[str, Any]:
        """Return portable ground truth without exposing an implementation object."""

        return {
            "id": self.identifier,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "source_format": self.source_format,
            "language": self.language,
            "document_class": self.document_class,
            "expense_kind": self.expense_kind,
            "transaction_date": self.transaction_date,
            "due_date": self.due_date,
            "total_amount": self.total_amount,
            "currency": self.currency,
            "counterparty": self.counterparty,
            "billing_period": self.billing_period,
            "consumption_value": self.consumption_value,
            "consumption_unit": self.consumption_unit,
            "scanned_variant": self.scanned_variant,
            "sha256": sha256,
            "non_personal": True,
        }


def fixture_definitions() -> tuple[ExpenseFixture, ...]:
    """Return the fixed corpus spanning every supported expense category."""

    return (
        ExpenseFixture(
            identifier="en-retail-cafe",
            filename="en-retail-cafe.png",
            mime_type="image/png",
            source_format="image",
            language="en",
            document_class="receipt",
            expense_kind="retail",
            transaction_date="2026-07-01",
            due_date=None,
            total_amount=820,
            currency="JPY",
            counterparty="Synthetic City Cafe",
            body=(
                FIXTURE_MARKER,
                "RECEIPT",
                "Synthetic City Cafe",
                "Date: 2026-07-01",
                "Green tea x1                 820 JPY",
                "TOTAL:                       820 JPY",
                "Payment: TEST CARD",
            ),
            scanned_variant="clean",
        ),
        ExpenseFixture(
            identifier="en-tax-notice",
            filename="en-tax-notice.png",
            mime_type="image/png",
            source_format="image",
            language="en",
            document_class="bill",
            expense_kind="tax",
            transaction_date="2026-07-15",
            due_date="2026-08-31",
            total_amount=18400,
            currency="JPY",
            counterparty="Synthetic Municipal Tax Office",
            body=(
                FIXTURE_MARKER,
                "TAX NOTICE",
                "Synthetic Municipal Tax Office",
                "Notice date: 2026-07-15",
                "Due date: 2026-08-31",
                "Amount due:              18,400 JPY",
                "Reference: TEST-2026-0715",
            ),
            scanned_variant="soft-blur",
        ),
        ExpenseFixture(
            identifier="ja-electricity-july",
            filename="ja-electricity-july.pdf",
            mime_type="application/pdf",
            source_format="pdf",
            language="ja",
            document_class="recurring_bill",
            expense_kind="electricity",
            transaction_date="2026-07-15",
            due_date="2026-08-05",
            total_amount=4820,
            currency="JPY",
            counterparty="サンプル電力",
            body=(
                FIXTURE_MARKER,
                "電気料金 ご使用量のお知らせ",
                "サンプル電力",
                "2026年7月分",
                "ご使用量 182 kWh",
                "請求予定額 4,820円",
                "支払期限 2026年8月5日",
            ),
            billing_period="2026年7月分",
            consumption_value=182,
            consumption_unit="kWh",
        ),
        ExpenseFixture(
            identifier="ja-water-july",
            filename="ja-water-july.pdf",
            mime_type="application/pdf",
            source_format="pdf",
            language="ja",
            document_class="recurring_bill",
            expense_kind="water",
            transaction_date="2026-07-18",
            due_date="2026-08-10",
            total_amount=3500,
            currency="JPY",
            counterparty="サンプル水道局",
            body=(
                FIXTURE_MARKER,
                "水道料金 ご使用量のお知らせ",
                "サンプル水道局",
                "2026年7月分",
                "ご使用量 18 m3",
                "今回料金 3,500円",
                "支払期限 2026年8月10日",
            ),
            billing_period="2026年7月分",
            consumption_value=18,
            consumption_unit="m3",
        ),
        ExpenseFixture(
            identifier="ja-gas-july",
            filename="ja-gas-july.pdf",
            mime_type="application/pdf",
            source_format="pdf",
            language="ja",
            document_class="recurring_bill",
            expense_kind="gas",
            transaction_date="2026-07-20",
            due_date="2026-08-12",
            total_amount=6600,
            currency="JPY",
            counterparty="サンプルガス",
            body=(
                FIXTURE_MARKER,
                "ガス料金 ご使用量のお知らせ",
                "サンプルガス",
                "2026年7月分",
                "ご使用量 21.4 m3",
                "今回料金 6,600円",
                "支払期限 2026年8月12日",
            ),
            billing_period="2026年7月分",
            consumption_value=21.4,
            consumption_unit="m3",
        ),
        ExpenseFixture(
            identifier="ja-retail-market",
            filename="ja-retail-market.pdf",
            mime_type="application/pdf",
            source_format="pdf",
            language="ja",
            document_class="receipt",
            expense_kind="retail",
            transaction_date="2026-07-02",
            due_date=None,
            total_amount=1250,
            currency="JPY",
            counterparty="サンプル市場",
            body=(
                FIXTURE_MARKER,
                "領収書",
                "サンプル市場",
                "2026年7月2日",
                "食料品 1,250円",
                "合計 1,250円",
            ),
        ),
        ExpenseFixture(
            identifier="en-insurance-premium",
            filename="en-insurance-premium.pdf",
            mime_type="application/pdf",
            source_format="pdf",
            language="en",
            document_class="bill",
            expense_kind="insurance",
            transaction_date="2026-07-03",
            due_date="2026-07-31",
            total_amount=9200,
            currency="JPY",
            counterparty="Synthetic Mutual Insurance",
            body=(
                FIXTURE_MARKER,
                "INSURANCE PREMIUM PAYMENT NOTICE",
                "Synthetic Mutual Insurance",
                "Notice date: 2026-07-03",
                "Coverage period: 2026-08-01 to 2027-07-31",
                "Premium due: 9,200 JPY",
                "Due date: 2026-07-31",
            ),
        ),
        ExpenseFixture(
            identifier="en-rent-notice",
            filename="en-rent-notice.pdf",
            mime_type="application/pdf",
            source_format="pdf",
            language="en",
            document_class="bill",
            expense_kind="rent",
            transaction_date="2026-07-01",
            due_date="2026-07-27",
            total_amount=120000,
            currency="JPY",
            counterparty="Synthetic Property Management",
            body=(
                FIXTURE_MARKER,
                "RENT PAYMENT NOTICE",
                "Synthetic Property Management",
                "Rent period: July 2026",
                "Amount due: 120,000 JPY",
                "Due date: 2026-07-27",
            ),
        ),
        ExpenseFixture(
            identifier="en-subscription-notice",
            filename="en-subscription-notice.pdf",
            mime_type="application/pdf",
            source_format="pdf",
            language="en",
            document_class="bill",
            expense_kind="subscription",
            transaction_date="2026-07-05",
            due_date="2026-07-05",
            total_amount=1500,
            currency="JPY",
            counterparty="Synthetic Cloud Service",
            body=(
                FIXTURE_MARKER,
                "SUBSCRIPTION PAYMENT NOTICE",
                "Synthetic Cloud Service",
                "Service period: July 2026",
                "Amount due: 1,500 JPY",
                "Due date: 2026-07-05",
            ),
        ),
        ExpenseFixture(
            identifier="vi-telecom-internet",
            filename="vi-telecom-internet.md",
            mime_type="text/markdown",
            source_format="markdown",
            language="vi",
            document_class="bill",
            expense_kind="telecom",
            transaction_date="2026-07-08",
            due_date="2026-07-25",
            total_amount=320000,
            currency="VND",
            counterparty="Nhà mạng Mẫu",
            body=(
                "# Hóa đơn internet",
                FIXTURE_MARKER,
                "Nhà mạng Mẫu",
                "Kỳ thanh toán: tháng 07/2026",
                "Ngày lập: 2026-07-08",
                "Hạn thanh toán: 2026-07-25",
                "Cước viễn thông: 320.000 VND",
            ),
        ),
        ExpenseFixture(
            identifier="vi-tax-notice",
            filename="vi-tax-notice.md",
            mime_type="text/markdown",
            source_format="markdown",
            language="vi",
            document_class="bill",
            expense_kind="tax",
            transaction_date="2026-07-10",
            due_date="2026-08-31",
            total_amount=1250000,
            currency="VND",
            counterparty="Cơ quan Thuế Mẫu",
            body=(
                "# Thông báo nộp thuế thu nhập",
                FIXTURE_MARKER,
                "Cơ quan Thuế Mẫu",
                "Ngày thông báo: 2026-07-10",
                "Hạn nộp: 2026-08-31",
                "Số tiền phải nộp: 1.250.000 VND",
            ),
        ),
        ExpenseFixture(
            identifier="vi-retail-pharmacy",
            filename="vi-retail-pharmacy.md",
            mime_type="text/markdown",
            source_format="markdown",
            language="vi",
            document_class="receipt",
            expense_kind="retail",
            transaction_date="2026-07-11",
            due_date=None,
            total_amount=175000,
            currency="VND",
            counterparty="Nhà thuốc Mẫu",
            body=(
                "# Receipt / Hóa đơn bán lẻ",
                FIXTURE_MARKER,
                "Nhà thuốc Mẫu",
                "Ngày mua: 2026-07-11",
                "Thuốc mẫu: 175.000 VND",
                "Tổng cộng: 175.000 VND",
            ),
        ),
    )


def generate_expense_documents(out_dir: Path) -> list[dict[str, Any]]:
    """Render every fixture and write a manifest with its expected visible fields."""

    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} must be empty before generating expense fixtures")

    labels: list[dict[str, Any]] = []
    for fixture in fixture_definitions():
        path = out_dir / fixture.filename
        _render_fixture(fixture, path)
        labels.append(fixture.label(_sha256(path)))

    manifest = {
        "schema_version": 1,
        "non_personal": True,
        "documents": labels,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return labels


def _render_fixture(fixture: ExpenseFixture, path: Path) -> None:
    if fixture.source_format == "image":
        _render_image(fixture, path)
        return
    if fixture.source_format == "pdf":
        _render_pdf(fixture, path)
        return
    if fixture.source_format == "markdown":
        _render_markdown(fixture, path)
        return
    raise ValueError(f"unsupported fixture format: {fixture.source_format}")


def _render_image(fixture: ExpenseFixture, path: Path) -> None:
    image = Image.new("RGB", (900, 1120), "#f7f2e8")
    rendered: Image.Image | None = None
    try:
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (35, 35, 865, 1085),
            radius=18,
            fill="white",
            outline="#1f2937",
            width=3,
        )
        title_font = ImageFont.load_default(size=28)
        body_font = ImageFont.load_default(size=20)
        y = 85
        for index, line in enumerate(fixture.body):
            font = title_font if index in {1, 2} else body_font
            draw.text((85, y), line, fill="#111827", font=font)
            y += 68 if index in {1, 2} else 52
        draw.line((85, y + 10, 815, y + 10), fill="#9ca3af", width=2)
        draw.text(
            (85, y + 42),
            "Fixture only. No real account or personal data.",
            fill="#6b7280",
            font=body_font,
        )

        rendered = image
        if fixture.scanned_variant == "soft-blur":
            rendered = image.filter(ImageFilter.GaussianBlur(radius=0.35))
        rendered.save(path, format="PNG", compress_level=9)
    finally:
        if rendered is not None and rendered is not image:
            rendered.close()
        image.close()


def _render_pdf(fixture: ExpenseFixture, path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = _pdf_font(japanese=fixture.language == "ja")
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )

    commands = [
        "q",
        "0.97 0.95 0.91 rg 0 0 595 842 re f",
        "1 1 1 rg 0.12 0.16 0.23 RG 1 w 42 42 511 758 re B",
        "Q",
    ]
    for index, line in enumerate(fixture.body):
        y_position = 742 - index * 38
        if y_position < 82:
            raise ValueError(f"fixture text did not fit: {fixture.identifier}")
        commands.append(
            "BT /F1 14 Tf 0.07 0.1 0.15 rg "
            f"1 0 0 1 76 {y_position} Tm "
            f"{_pdf_text_operand(line, japanese=fixture.language == 'ja')} Tj ET"
        )

    content = DecodedStreamObject()
    content.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page.replace_contents(content)
    writer.add_metadata(
        {
            "/Title": fixture.identifier,
            "/Author": "Clerk-san",
            "/CreationDate": "D:20260701000000Z",
            "/ModDate": "D:20260701000000Z",
        }
    )
    writer.write(path)


def _pdf_font(*, japanese: bool) -> DictionaryObject:
    if not japanese:
        return DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )

    descendant = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/CIDFontType0"),
            NameObject("/BaseFont"): NameObject("/HeiseiMin-W3"),
            NameObject("/CIDSystemInfo"): DictionaryObject(
                {
                    NameObject("/Registry"): TextStringObject("Adobe"),
                    NameObject("/Ordering"): TextStringObject("Japan1"),
                    NameObject("/Supplement"): NumberObject(5),
                }
            ),
            NameObject("/DW"): NumberObject(1000),
        }
    )
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/HeiseiMin-W3"),
            NameObject("/Encoding"): NameObject("/UniJIS-UTF16-H"),
            NameObject("/DescendantFonts"): ArrayObject([descendant]),
        }
    )


def _pdf_text_operand(text: str, *, japanese: bool) -> str:
    if japanese:
        return f"<{text.encode('utf-16-be').hex().upper()}>"
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"({escaped})"


def _render_markdown(fixture: ExpenseFixture, path: Path) -> None:
    source = "\n".join(
        (
            "---",
            "fixture: non-personal",
            f"language: {fixture.language}",
            "---",
            "",
            *fixture.body,
            "",
        )
    )
    path.write_text(source, encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_output(path: Path, *, reset: bool) -> Path:
    target = path.resolve()
    if target.exists() and any(target.iterdir()):
        if not reset:
            raise SystemExit(f"{target} is not empty; rerun with --reset to replace its fixtures")
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".clerksan-expense-fixtures"))
    parser.add_argument("--reset", action="store_true")
    arguments = parser.parse_args()
    output_dir = _prepare_output(arguments.out, reset=arguments.reset)
    labels = generate_expense_documents(output_dir)
    print(f"Generated {len(labels)} non-personal expense fixtures in {output_dir}")


if __name__ == "__main__":
    main()
