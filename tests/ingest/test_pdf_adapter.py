from __future__ import annotations

import hashlib
import io
import json
import tempfile

import pytest
from pypdf import PdfWriter
from pypdf.annotations import Link
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from clerksan.config import Settings
from clerksan.ingest.adapters import pdf as pdf_adapter_module
from clerksan.ingest.adapters.pdf import PdfAdapter, page_needs_ocr
from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.normalized import DocMetadata
from clerksan.ingest.parser_artifacts import (
    PDF_PREVIEW_MANIFEST_MIME,
    PDF_PREVIEW_SCHEMA,
    ArtifactRole,
)
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource
from clerksan.llm.ocr import OcrResult


class FakeOcr:
    name = "fake-ocr"

    def __init__(self) -> None:
        self.images: list[bytes] = []

    async def ocr(self, image_bytes: bytes) -> OcrResult:
        self.images.append(image_bytes)
        return OcrResult(text="Scanned receipt page", engine=self.name)


def _hybrid_pdf() -> bytes:
    writer = PdfWriter()
    _add_text_page(writer, "Digital invoice page with a healthy text layer")
    writer.add_blank_page(width=612, height=792)
    return _writer_bytes(writer)


def _add_text_page(writer: PdfWriter, text: str) -> None:
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("ascii"))
    page.replace_contents(content)


def _writer_bytes(writer: PdfWriter) -> bytes:
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _metadata(raw: bytes) -> DocMetadata:
    return DocMetadata(
        filename="hybrid.pdf",
        detected_type=FileType.PDF,
        sha256=hashlib.sha256(raw).hexdigest(),
        extra={"content_path": "documents/hybrid/original.pdf"},
    )


def _settings(**overrides: object) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        pdf_min_chars_per_page=5,
        **overrides,
    )


def _sidecar_normalize(raw: bytes, *, limits: IngestLimits | None = None):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return PdfAdapter(FakeOcr(), _settings(), limits=limits).normalize_with_artifacts(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename="fixture.pdf",
            ),
            AdapterContext("pdf", metadata={"detected_type": "pdf"}),
        )


def _pdf_with_forbidden_action(action_name: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.root_object[NameObject("/OpenAction")] = DictionaryObject(
        {NameObject("/S"): NameObject(f"/{action_name}")}
    )
    return _writer_bytes(writer)


def _pdf_with_external_uri() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_annotation(
        page_number=0,
        annotation=Link(rect=(0, 0, 20, 20), url="https://example.invalid"),
    )
    return _writer_bytes(writer)


def _pdf_with_embedded_file() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_attachment("payload.txt", b"inert bytes are still forbidden here")
    return _writer_bytes(writer)


def _encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("user", owner_password="owner", algorithm="RC4-128")
    return _writer_bytes(writer)


def _pdf_requiring_repair() -> bytes:
    raw = _hybrid_pdf()
    marker = b"startxref\n"
    marker_index = raw.rfind(marker)
    assert marker_index >= 0
    offset_start = marker_index + len(marker)
    offset_end = raw.index(b"\n", offset_start)
    return raw[:offset_start] + b"0" + raw[offset_end:]


async def test_pdf_adapter_preserves_page_order_and_ocrs_only_scanned_pages() -> None:
    raw = _hybrid_pdf()
    ocr = FakeOcr()
    result = await PdfAdapter(ocr, _settings()).adapt(raw, _metadata(raw))

    assert result.markdown_body == (
        "Digital invoice page with a healthy text layer\n\n---\n\nScanned receipt page"
    )
    assert result.metadata.page_provenance == ["text_layer", "ocr"]
    assert result.metadata.extra["ocr_engine"] == "fake-ocr"
    assert result.metadata.extra["page_count"] == 2
    assert len(ocr.images) == 1
    assert len(result.images) == 1
    assert result.images[0].source_location == "page:2"
    assert result.images[0].content_path == f"sha256/{result.images[0].sha256}.png"


async def test_pdf_adapter_checks_page_count_before_rasterizing() -> None:
    raw = _hybrid_pdf()
    ocr = FakeOcr()

    with pytest.raises(ResourceLimitExceeded, match="max_pdf_pages"):
        await PdfAdapter(ocr, _settings(max_pdf_pages=1)).adapt(raw, _metadata(raw))

    assert ocr.images == []


@pytest.mark.parametrize(
    ("page_text", "min_chars", "ratio", "expected"),
    [
        ("Invoice total", 5, 0.3, False),
        ("令和8年 合計 1200円", 5, 0.3, False),
        ("1", 5, 0.3, True),
        ("ãƒ†ã‚¹ãƒˆ", 1, 0.3, True),
        ("valid � text", 1, 0.9, True),
    ],
)
def test_page_needs_ocr_detects_sparse_or_mojibake_text(
    page_text: str, min_chars: int, ratio: float, expected: bool
) -> None:
    assert page_needs_ocr(page_text, min_chars=min_chars, mojibake_ratio=ratio) is expected


def test_sidecar_pdf_returns_complete_ordered_page_set_and_manifest_without_ocr() -> None:
    raw = _hybrid_pdf()
    result = _sidecar_normalize(raw)

    assert result.normalized.metadata.page_provenance == ["text_layer", "ocr_required"]
    assert result.normalized.metadata.extra["ocr_required_pages"] == [2]
    assert result.normalized.metadata.extra["preview_status"] == "ready"
    assert [artifact.role for artifact in result.artifacts] == [
        ArtifactRole.PDF_PAGE,
        ArtifactRole.PDF_PAGE,
        ArtifactRole.PDF_PREVIEW_MANIFEST,
    ]
    assert [artifact.page_number for artifact in result.artifacts[:-1]] == [1, 2]
    assert [artifact.ocr_required for artifact in result.artifacts[:-1]] == [False, True]
    assert result.artifacts[-1].media_type == PDF_PREVIEW_MANIFEST_MIME
    manifest = json.loads(result.artifacts[-1].data)
    assert manifest["schema"] == PDF_PREVIEW_SCHEMA
    assert manifest["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert [page["page_number"] for page in manifest["pages"]] == [1, 2]
    assert result.normalized.images[0].source_location == "page:2"
    assert result.normalized.images[0].content_path == "artifact:pdf-page:2"


def test_sidecar_pdf_injected_partial_render_failure_exposes_no_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = pdf_adapter_module._rasterize_page
    calls = 0

    def fail_second_page(page, limits):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected render failure")
        return original(page, limits)

    monkeypatch.setattr(pdf_adapter_module, "_rasterize_page", fail_second_page)

    result = _sidecar_normalize(_hybrid_pdf())

    assert calls == 2
    assert result.artifacts == ()
    assert result.normalized.images == []
    assert result.normalized.metadata.extra["preview_status"] == "unavailable"
    assert result.normalized.metadata.extra["preview_unavailable_reason"] == ("render_unavailable")
    assert result.normalized.metadata.page_provenance == ["text_layer", "ocr_required"]
    assert result.normalized.metadata.extra["ocr_required_pages"] == [2]


def test_sidecar_pdf_render_budget_returns_explicit_unavailable_without_partial_set() -> None:
    result = _sidecar_normalize(
        _hybrid_pdf(),
        limits=IngestLimits(max_normalized_output_bytes=1024),
    )

    assert result.artifacts == ()
    assert result.normalized.metadata.extra["preview_status"] == "unavailable"
    assert result.normalized.metadata.extra["preview_unavailable_reason"] == (
        "render_budget_exceeded"
    )


def test_sidecar_pdf_rejects_cumulative_text_resource_exhaustion() -> None:
    with pytest.raises(ResourceLimitExceeded, match="max_text_characters"):
        _sidecar_normalize(
            _hybrid_pdf(),
            limits=IngestLimits(max_text_characters=5),
        )


def test_sidecar_pdf_rejects_structural_object_and_inspection_budget_exhaustion() -> None:
    with pytest.raises(ResourceLimitExceeded, match="max_structured_nodes"):
        _sidecar_normalize(
            _hybrid_pdf(),
            limits=IngestLimits(max_structured_nodes=1),
        )
    with pytest.raises(ResourceLimitExceeded, match="max_archive_uncompressed_bytes"):
        _sidecar_normalize(
            _hybrid_pdf(),
            limits=IngestLimits(max_archive_uncompressed_bytes=8),
        )


def test_sidecar_pdf_rejects_a_document_that_requires_structural_repair() -> None:
    with pytest.raises(ValueError, match="invalid|repaired"):
        _sidecar_normalize(_pdf_requiring_repair())


@pytest.mark.parametrize("action_name", ["JavaScript", "Launch", "XFA", "RichMedia"])
def test_sidecar_pdf_rejects_active_launch_xfa_and_richmedia(action_name: str) -> None:
    with pytest.raises(ValueError, match="active|external"):
        _sidecar_normalize(_pdf_with_forbidden_action(action_name))


@pytest.mark.parametrize(
    "fixture",
    [_encrypted_pdf, _pdf_with_external_uri, _pdf_with_embedded_file],
)
def test_sidecar_pdf_rejects_encrypted_external_and_embedded_structures(fixture) -> None:
    with pytest.raises(ValueError, match="encrypted|active|external|embedded"):
        _sidecar_normalize(fixture())
