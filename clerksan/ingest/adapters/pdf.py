"""Hybrid PDF normalization with a text-layer-first OCR policy."""

from __future__ import annotations

import hashlib
import io
import math
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

if TYPE_CHECKING:
    import pypdfium2 as pdfium

from clerksan.config import Settings
from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import (
    IngestLimits,
    ResourceLimitExceeded,
    check_image_pixels,
    check_pdf_pages,
)
from clerksan.ingest.normalized import (
    DocMetadata,
    ExtractedImage,
    NormalizedDocument,
    canonical_json,
)
from clerksan.ingest.parser_artifacts import (
    MAX_ARTIFACT_FDS,
    PDF_PREVIEW_MANIFEST_MIME,
    PNG_MIME,
    AdapterRunResult,
    ArtifactRole,
    GeneratedArtifact,
    build_pdf_preview_manifest,
)
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource
from clerksan.llm.ocr import OcrEngine

from .source_io import read_bounded_source

_MOJIBAKE_MARKERS = frozenset(
    "\ufffd\u00c3\u00c2\u00e2\u00e3\u00e6\u00e5\u00e7\u00f0\u00ef\u00a4\u00a2\u00ac"
)
_RASTER_DPI = 300
_FORBIDDEN_PDF_NAMES = frozenset(
    {
        "/AA",
        "/EmbeddedFile",
        "/EmbeddedFiles",
        "/EF",
        "/FS",
        "/Filespec",
        "/GoToE",
        "/GoToR",
        "/ImportData",
        "/JavaScript",
        "/JS",
        "/Launch",
        "/OpenAction",
        "/RichMedia",
        "/SubmitForm",
        "/URI",
        "/URL",
        "/XFA",
    }
)


def _pdfium_module():
    """Load PDFium only inside the bounded parser child.

    The sidecar forks for every untrusted parse. Importing native graphics
    runtimes in its parent first is unsafe on macOS and unnecessary on Linux.
    """

    import pypdfium2

    return pypdfium2


def page_needs_ocr(page_text: str, *, min_chars: int, mojibake_ratio: float) -> bool:
    """Return whether a PDF text layer is too sparse or visibly corrupt.

    The character count excludes whitespace and punctuation, so a page with only a
    decorative page number still reaches OCR.  The second condition catches common
    UTF-8-as-Latin-1 artifacts while allowing ordinary Japanese and Latin text.
    """

    if min_chars < 1:
        raise ValueError("min_chars must be greater than zero")
    if not 0 <= mojibake_ratio <= 1:
        raise ValueError("mojibake_ratio must be between zero and one")

    meaningful = [character for character in page_text if not character.isspace()]
    text_characters = sum(character.isalnum() for character in meaningful)
    if text_characters < min_chars:
        return True
    if "\ufffd" in page_text:
        return True
    if not meaningful:
        return True
    markers = sum(character in _MOJIBAKE_MARKERS for character in meaningful)
    return markers / len(meaningful) >= mojibake_ratio


class PdfAdapter:
    """Use the text layer for healthy pages and OCR only the pages that need it."""

    supported_types: tuple[FileType, ...] = (FileType.PDF,)

    def __init__(
        self,
        ocr: OcrEngine,
        settings: Settings,
        *,
        limits: IngestLimits | None = None,
    ) -> None:
        self.ocr = ocr
        self.settings = settings
        self.limits = limits or IngestLimits.from_settings(settings)

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        """Return bounded PDF structure for direct adapter callers."""

        return self.normalize_with_artifacts(source, context).normalized

    def normalize_with_artifacts(
        self, source: ReadOnlySource, context: AdapterContext
    ) -> AdapterRunResult:
        """Return text plus an all-or-none inert page-render artifact set."""

        detected_type = _context_file_type(context)
        if detected_type is not FileType.PDF:
            raise ValueError(f"PDF adapter cannot handle {detected_type.value!r}")
        raw = read_bounded_source(source, self.limits)
        document = _open_pdf(raw, self.limits)

        try:
            page_bodies: list[str] = []
            page_provenance: list[str] = []
            text_characters = 0
            for page_index in range(len(document)):
                page = document[page_index]
                try:
                    page_text = _extract_page_text(page)
                    text_characters += len(page_text)
                    if text_characters > self.limits.max_text_characters:
                        raise ResourceLimitExceeded(
                            "max_text_characters",
                            self.limits.max_text_characters,
                            text_characters,
                        )
                    if page_needs_ocr(
                        page_text,
                        min_chars=self.settings.pdf_min_chars_per_page,
                        mojibake_ratio=self.settings.pdf_mojibake_ratio,
                    ):
                        page_bodies.append("")
                        page_provenance.append("ocr_required")
                    else:
                        page_bodies.append(page_text)
                        page_provenance.append("text_layer")
                finally:
                    page.close()

            if len(document) + 1 > MAX_ARTIFACT_FDS:
                return AdapterRunResult(
                    normalized=_pdf_normalized_document(
                        source,
                        context,
                        page_bodies,
                        page_provenance,
                        preview_status="unavailable",
                        unavailable_reason="render_budget_exceeded",
                    )
                )

            try:
                pages = _render_preview_pages(
                    document,
                    page_provenance,
                    self.limits,
                )
                manifest_bytes = build_pdf_preview_manifest(source.source_sha256, pages)
                total_output = sum(len(page.data) for page in pages) + len(manifest_bytes)
                if total_output > self.limits.max_normalized_output_bytes:
                    raise ResourceLimitExceeded(
                        "max_normalized_output_bytes",
                        self.limits.max_normalized_output_bytes,
                        total_output,
                    )
            except ResourceLimitExceeded:
                return AdapterRunResult(
                    normalized=_pdf_normalized_document(
                        source,
                        context,
                        page_bodies,
                        page_provenance,
                        preview_status="unavailable",
                        unavailable_reason="render_budget_exceeded",
                    )
                )
            except (MemoryError, OSError, RuntimeError, ValueError):
                return AdapterRunResult(
                    normalized=_pdf_normalized_document(
                        source,
                        context,
                        page_bodies,
                        page_provenance,
                        preview_status="unavailable",
                        unavailable_reason="render_unavailable",
                    )
                )
        finally:
            document.close()

        manifest = GeneratedArtifact(
            role=ArtifactRole.PDF_PREVIEW_MANIFEST,
            media_type=PDF_PREVIEW_MANIFEST_MIME,
            data=manifest_bytes,
        )
        normalized = _pdf_normalized_document(
            source,
            context,
            page_bodies,
            page_provenance,
            preview_status="ready",
            pages=pages,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
        aggregate_output = len(
            canonical_json(normalized.model_dump(mode="json")).encode("utf-8")
        ) + sum(len(artifact.data) for artifact in (*pages, manifest))
        if aggregate_output > self.limits.max_normalized_output_bytes:
            return AdapterRunResult(
                normalized=_pdf_normalized_document(
                    source,
                    context,
                    page_bodies,
                    page_provenance,
                    preview_status="unavailable",
                    unavailable_reason="render_budget_exceeded",
                )
            )
        return AdapterRunResult(normalized=normalized, artifacts=(*pages, manifest))

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        """Adapt each PDF page in source order without rasterizing healthy text."""

        if meta.detected_type is not FileType.PDF:
            raise ValueError(f"PDF adapter cannot handle {meta.detected_type.value!r}")

        document = _open_pdf(raw, self.limits)

        try:
            page_bodies: list[str] = []
            page_provenance: list[str] = []
            images: list[ExtractedImage] = []
            text_characters = 0

            for page_index in range(len(document)):
                page_number = page_index + 1
                page = document[page_index]
                try:
                    text = _extract_page_text(page)
                    text_characters += len(text)
                    if text_characters > self.limits.max_text_characters:
                        raise ResourceLimitExceeded(
                            "max_text_characters",
                            self.limits.max_text_characters,
                            text_characters,
                        )
                    if page_needs_ocr(
                        text,
                        min_chars=self.settings.pdf_min_chars_per_page,
                        mojibake_ratio=self.settings.pdf_mojibake_ratio,
                    ):
                        image_bytes, width, height = _rasterize_page(page, self.limits)
                        result = await self.ocr.ocr(image_bytes)
                        page_bodies.append(result.text.strip())
                        page_provenance.append("ocr")
                        digest = hashlib.sha256(image_bytes).hexdigest()
                        images.append(
                            ExtractedImage(
                                sha256=digest,
                                content_path=f"sha256/{digest}.png",
                                width=width,
                                height=height,
                                source_location=f"page:{page_number}",
                            )
                        )
                    else:
                        page_bodies.append(text)
                        page_provenance.append("text_layer")
                finally:
                    page.close()
        finally:
            document.close()

        extra = dict(meta.extra)
        extra.update(
            {
                "ocr_engine": self.ocr.name,
                "raster_dpi": _RASTER_DPI,
                "page_count": len(page_provenance),
            }
        )
        normalized_meta = meta.model_copy(
            update={"page_provenance": page_provenance, "extra": extra}
        )
        return NormalizedDocument(
            markdown_body="\n\n---\n\n".join(page_bodies),
            metadata=normalized_meta,
            images=images,
        )


def _open_pdf(raw: bytes, limits: IngestLimits) -> pdfium.PdfDocument:
    """Open a PDF only after strict, bounded structural inspection succeeds."""

    try:
        reader = PdfReader(
            io.BytesIO(raw),
            strict=True,
            root_object_recovery_limit=0,
        )
    except (PyPdfError, OSError, ValueError) as error:
        raise ValueError("invalid or structurally repaired PDF upload") from error

    try:
        if reader.is_encrypted:
            raise ValueError("encrypted PDF uploads are not supported")
        try:
            page_count = len(reader.pages)
        except (PyPdfError, OSError, ValueError) as error:
            raise ValueError("invalid or structurally repaired PDF upload") from error
        check_pdf_pages(page_count, limits)
        _validate_pdf_security(reader, limits)
    finally:
        reader.close()

    pdfium = _pdfium_module()
    try:
        document = pdfium.PdfDocument(raw)
    except (pdfium.PdfiumError, OSError, TypeError, ValueError) as error:
        raise ValueError("invalid or unreadable PDF upload") from error
    if len(document) != page_count:
        document.close()
        raise ValueError("PDF parsers disagree on the page count")
    return document


def _extract_page_text(page: pdfium.PdfPage) -> str:
    text_page = page.get_textpage()
    try:
        return text_page.get_text_bounded().replace("\r\n", "\n").strip()
    finally:
        text_page.close()


def _rasterize_page(page: pdfium.PdfPage, limits: IngestLimits) -> tuple[bytes, int, int]:
    """Render one bounded PDF page at a readable OCR resolution."""

    scale = _RASTER_DPI / 72
    width = math.ceil(page.get_width() * scale)
    height = math.ceil(page.get_height() * scale)
    if not _finite_positive((width, height)):
        raise ValueError("PDF page has invalid dimensions")
    check_image_pixels(width, height, limits)

    bitmap = page.render(scale=scale, rev_byteorder=True)
    try:
        check_image_pixels(bitmap.width, bitmap.height, limits)
        image = bitmap.to_pil()
        try:
            output = io.BytesIO()
            image.save(output, format="PNG", compress_level=9)
            return output.getvalue(), bitmap.width, bitmap.height
        finally:
            image.close()
    finally:
        bitmap.close()


def _render_preview_pages(
    document: pdfium.PdfDocument,
    page_provenance: list[str],
    limits: IngestLimits,
) -> tuple[GeneratedArtifact, ...]:
    pages: list[GeneratedArtifact] = []
    output_bytes = 0
    for page_number in range(1, len(document) + 1):
        page = document[page_number - 1]
        try:
            page_bytes, width, height = _rasterize_page(page, limits)
        finally:
            page.close()
        output_bytes += len(page_bytes)
        if output_bytes > limits.max_normalized_output_bytes:
            raise ResourceLimitExceeded(
                "max_normalized_output_bytes",
                limits.max_normalized_output_bytes,
                output_bytes,
            )
        pages.append(
            GeneratedArtifact(
                role=ArtifactRole.PDF_PAGE,
                media_type=PNG_MIME,
                data=page_bytes,
                page_number=page_number,
                width=width,
                height=height,
                source_location=f"page:{page_number}",
                ocr_required=page_provenance[page_number - 1] == "ocr_required",
            )
        )
    return tuple(pages)


def _pdf_normalized_document(
    source: ReadOnlySource,
    context: AdapterContext,
    page_bodies: list[str],
    page_provenance: list[str],
    *,
    preview_status: str,
    pages: tuple[GeneratedArtifact, ...] = (),
    manifest_sha256: str | None = None,
    unavailable_reason: str | None = None,
) -> NormalizedDocument:
    ocr_pages = [
        page.page_number for page in pages if page.ocr_required and page.page_number is not None
    ]
    images = [
        ExtractedImage(
            sha256=hashlib.sha256(page.data).hexdigest(),
            content_path=f"artifact:pdf-page:{page.page_number}",
            width=page.width,
            height=page.height,
            source_location=page.source_location,
        )
        for page in pages
        if page.ocr_required
    ]
    extra: dict[str, object] = {
        "page_count": len(page_provenance),
        "raster_dpi": _RASTER_DPI,
        "ocr_required": "ocr_required" in page_provenance,
        "ocr_required_pages": ocr_pages
        if preview_status == "ready"
        else [
            index
            for index, provenance in enumerate(page_provenance, start=1)
            if provenance == "ocr_required"
        ],
        "preview_status": preview_status,
    }
    if preview_status == "ready":
        extra.update(
            {
                "preview_page_count": len(pages),
                "preview_manifest_sha256": manifest_sha256,
            }
        )
    else:
        extra["preview_unavailable_reason"] = unavailable_reason
    return NormalizedDocument(
        markdown_body="\n\n---\n\n".join(page_bodies),
        metadata=DocMetadata(
            filename=source.filename,
            detected_type=FileType.PDF,
            sha256=source.source_sha256,
            family="document",
            canonical_mime=_context_text(context, "canonical_mime"),
            page_provenance=page_provenance,
            extra=extra,
        ),
        images=images,
        embeddable=bool("".join(page_bodies).strip()),
    )


def _validate_pdf_security(reader: PdfReader, limits: IngestLimits) -> None:
    """Reject active, embedded, external, and oversized PDF object structures."""

    references = {
        (generation, object_id)
        for generation, entries in reader.xref.items()
        if generation != 65535
        for object_id in entries
        if object_id > 0
    }
    references.update((0, object_id) for object_id in reader.xref_objStm)
    object_count = max((object_id for _, object_id in references), default=0) + 1
    if object_count > limits.max_structured_nodes:
        raise ResourceLimitExceeded(
            "max_structured_nodes", limits.max_structured_nodes, object_count
        )

    inspected_bytes = 0
    for generation, object_id in sorted(references):
        try:
            value = reader.get_object(IndirectObject(object_id, generation, reader))
        except (PyPdfError, OSError, RecursionError, ValueError) as error:
            raise ValueError("PDF object structure is unreadable") from error
        if value is None:
            raise ValueError("PDF object structure is unreadable")
        inspected_bytes = _inspect_pdf_value(value, inspected_bytes, set())
        if inspected_bytes > limits.max_archive_uncompressed_bytes:
            raise ResourceLimitExceeded(
                "max_archive_uncompressed_bytes",
                limits.max_archive_uncompressed_bytes,
                inspected_bytes,
            )


def _inspect_pdf_value(value: object, inspected_bytes: int, seen: set[int]) -> int:
    """Count bounded structural data and reject forbidden PDF names."""

    if isinstance(value, NameObject):
        if value in _FORBIDDEN_PDF_NAMES:
            raise ValueError("active, external, or embedded PDF structures are not accepted")
        return inspected_bytes + len(value.encode("utf-8", errors="replace"))
    if isinstance(value, IndirectObject):
        return inspected_bytes + len(f"{value.idnum} {value.generation} R")
    if isinstance(value, (DictionaryObject, ArrayObject)):
        identity = id(value)
        if identity in seen:
            return inspected_bytes
        seen.add(identity)
        members = value.items() if isinstance(value, DictionaryObject) else enumerate(value)
        for key, member in members:
            inspected_bytes = _inspect_pdf_value(key, inspected_bytes, seen)
            inspected_bytes = _inspect_pdf_value(member, inspected_bytes, seen)
        return inspected_bytes
    if isinstance(value, bytes):
        return inspected_bytes + len(value)
    return inspected_bytes + len(str(value).encode("utf-8", errors="replace"))


def _finite_positive(dimensions: Iterable[int]) -> bool:
    return all(math.isfinite(dimension) and dimension > 0 for dimension in dimensions)


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        return FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("PDF adapter context requires a detected type") from error


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) else None
