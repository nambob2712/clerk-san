from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image

from clerksan.config import Settings
from clerksan.db.models import DocumentClass
from clerksan.extract.classifier import classify_by_keywords
from clerksan.ingest.adapters.markdown import MarkdownAdapter
from clerksan.ingest.adapters.pdf import PdfAdapter
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata
from eval.expense_documents import FIXTURE_MARKER, generate_expense_documents


class _NeverOcr:
    name = "never-ocr"

    async def ocr(self, image_bytes: bytes):
        del image_bytes
        raise AssertionError("healthy fixture PDFs must use their text layers")


def _metadata(path: Path, source_format: str) -> DocMetadata:
    detected_type = FileType.PDF if source_format == "pdf" else FileType.MD
    return DocMetadata(
        filename=path.name,
        detected_type=detected_type,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def test_expense_fixture_manifest_covers_documents_languages_and_formats(tmp_path: Path) -> None:
    labels = generate_expense_documents(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["non_personal"] is True
    assert manifest["documents"] == labels
    assert len(labels) == 12
    assert {label["expense_kind"] for label in labels} == {
        "retail",
        "electricity",
        "water",
        "gas",
        "telecom",
        "tax",
        "insurance",
        "rent",
        "subscription",
    }
    assert {label["language"] for label in labels} == {"en", "ja", "vi"}
    assert {label["source_format"] for label in labels} == {"image", "pdf", "markdown"}
    assert len({label["sha256"] for label in labels}) == len(labels)

    for label in labels:
        path = tmp_path / label["filename"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == label["sha256"]
        assert label["non_personal"] is True
        if label["source_format"] == "image":
            with Image.open(path) as image:
                image.verify()
                assert image.size == (900, 1120)
        elif label["source_format"] == "pdf":
            with pdfium.PdfDocument(path) as document:
                assert len(document) == 1
                page = document[0]
                try:
                    text_page = page.get_textpage()
                    try:
                        normalized_text = " ".join(text_page.get_text_bounded().split())
                    finally:
                        text_page.close()
                finally:
                    page.close()
            assert " ".join(FIXTURE_MARKER.split()) in normalized_text
        else:
            assert FIXTURE_MARKER in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_text_layer_expense_fixtures_route_without_a_local_model(tmp_path: Path) -> None:
    labels = generate_expense_documents(tmp_path)
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    pdf_adapter = PdfAdapter(_NeverOcr(), settings)
    markdown_adapter = MarkdownAdapter()

    for label in labels:
        if label["source_format"] == "image":
            continue
        path = tmp_path / label["filename"]
        metadata = _metadata(path, label["source_format"])
        if label["source_format"] == "pdf":
            normalized = await pdf_adapter.adapt(path.read_bytes(), metadata)
            assert normalized.metadata.page_provenance == ["text_layer"]
        else:
            normalized = await markdown_adapter.adapt(path.read_bytes(), metadata)

        classification = classify_by_keywords(normalized)
        assert classification.label is DocumentClass(label["document_class"]), label["id"]
