"""Bounded DOCX normalization that preserves document structure and media links."""

from __future__ import annotations

import hashlib
import io
import re
import warnings
import zipfile
from collections.abc import Iterator

from docx import Document as load_document
from docx.document import Document as WordDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from PIL import Image, UnidentifiedImageError

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, check_image_pixels, safe_zip_members
from clerksan.ingest.normalized import (
    DocMetadata,
    ExtractedImage,
    ExtractedTable,
    NormalizedDocument,
)
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

from .source_io import read_bounded_source

_MIN_EMBEDDED_IMAGE_DIMENSION = 50


class DocxAdapter:
    """Convert a bounded DOCX into Markdown, structured tables, and media references."""

    supported_types: tuple[FileType, ...] = (FileType.DOCX,)

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        detected_type = _context_file_type(context)
        raw = read_bounded_source(source, self.limits)
        meta = DocMetadata(
            filename=source.filename,
            detected_type=detected_type,
            sha256=source.source_sha256,
            family="document",
            canonical_mime=_context_text(context, "canonical_mime"),
        )
        return self._normalize_bytes(raw, meta)

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        """Validate OOXML metadata before parsing it with ``python-docx``."""

        return self._normalize_bytes(raw, meta)

    def _normalize_bytes(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        """Normalize already-bounded bytes for the sandbox and legacy wrapper."""

        if meta.detected_type is not FileType.DOCX:
            raise ValueError(f"DOCX adapter cannot handle {meta.detected_type.value!r}")
        _validate_docx_archive(raw, self.limits)
        try:
            document = load_document(io.BytesIO(raw))
        except (PackageNotFoundError, OSError, zipfile.BadZipFile) as error:
            raise ValueError("invalid or unreadable DOCX upload") from error

        blocks: list[str] = []
        prose_blocks: list[str] = []
        tables: list[ExtractedTable] = []
        for block in _iter_body_blocks(document):
            if isinstance(block, Paragraph):
                markdown = _paragraph_markdown(block)
                if markdown:
                    blocks.append(markdown)
                    prose_blocks.append(markdown)
                continue

            matrix = _table_matrix(block)
            if not matrix:
                continue
            table_index = len(tables) + 1
            tables.append(
                ExtractedTable(
                    header=matrix[0],
                    rows=matrix[1:],
                    source_location=f"table:{table_index}",
                )
            )
            blocks.append(_markdown_table(matrix))

        images = extract_embedded_images(raw, limits=self.limits)
        extra = dict(meta.extra)
        extra.update(
            {
                "document_format": "docx",
                "embedded_image_count": len(images),
                "table_count": len(tables),
                "residual_markdown": "\n\n".join(prose_blocks),
            }
        )
        return NormalizedDocument(
            markdown_body="\n\n".join(blocks),
            metadata=meta.model_copy(update={"extra": extra}),
            images=images,
            tables=tables,
        )


def extract_embedded_images(
    docx_bytes: bytes, *, limits: IngestLimits | None = None
) -> list[ExtractedImage]:
    """Return distinct, sufficiently large ``word/media`` images for downstream OCR.

    The returned paths are content-addressed references, not extracted files.  The
    pipeline can persist the source bytes once and enqueue one OCR job per digest.
    """

    active_limits = limits or IngestLimits()
    images: list[ExtractedImage] = []
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
            safe_zip_members(archive, active_limits)
            for member in archive.infolist():
                if member.is_dir() or not member.filename.startswith("word/media/"):
                    continue
                image_bytes = archive.read(member)
                dimensions = _embedded_image_dimensions(image_bytes, active_limits)
                if dimensions is None:
                    continue
                width, height = dimensions
                if width < _MIN_EMBEDDED_IMAGE_DIMENSION or height < _MIN_EMBEDDED_IMAGE_DIMENSION:
                    continue
                digest = hashlib.sha256(image_bytes).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                images.append(
                    ExtractedImage(
                        sha256=digest,
                        content_path=f"embedded/sha256/{digest}",
                        width=width,
                        height=height,
                        source_location=member.filename,
                    )
                )
    except zipfile.BadZipFile as error:
        raise ValueError("invalid DOCX archive") from error
    return images


def _validate_docx_archive(raw: bytes, limits: IngestLimits) -> None:
    """Reject hostile ZIP metadata before any OOXML parser opens members."""

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            safe_zip_members(archive, limits)
    except zipfile.BadZipFile as error:
        raise ValueError("invalid DOCX archive") from error


def _iter_body_blocks(document: WordDocument) -> Iterator[Paragraph | Table]:
    """Yield paragraphs and tables in their original interleaved body order."""

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _paragraph_markdown(paragraph: Paragraph) -> str:
    text = _compact_text(paragraph.text)
    if not text:
        return ""
    level = _heading_level(paragraph)
    if level is not None:
        return f"{'#' * level} {text}"
    style_name = (paragraph.style.name or "").casefold()
    if "list bullet" in style_name:
        return f"- {text}"
    if "list number" in style_name:
        return f"1. {text}"
    return text


def _heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style
    identifiers = (style.name or "", style.style_id or "")
    for identifier in identifiers:
        match = re.search(r"(?:heading|見出し)\s*([1-9])", identifier, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _table_matrix(table: Table) -> list[list[str]]:
    rows = [[_compact_text(cell.text) for cell in row.cells] for row in table.rows]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    return [row + [""] * (width - len(row)) for row in rows]


def _markdown_table(matrix: list[list[str]]) -> str:
    header = [_escape_table_cell(value) or " " for value in matrix[0]]
    lines = [
        f"| {' | '.join(header)} |",
        f"| {' | '.join('---' for _ in header)} |",
    ]
    for row in matrix[1:]:
        values = [_escape_table_cell(value) for value in row]
        lines.append(f"| {' | '.join(values)} |")
    return "\n".join(lines)


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " / ")


def _compact_text(value: str) -> str:
    return " ".join(value.split())


def _embedded_image_dimensions(data: bytes, limits: IngestLimits) -> tuple[int, int] | None:
    """Inspect image headers without decoding arbitrary archive media into memory."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                check_image_pixels(width, height, limits)
                image.verify()
                return width, height
    except (UnidentifiedImageError, OSError):
        return None
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("embedded image exceeds Pillow safety limits") from error


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        return FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("DOCX adapter context requires a detected type") from error


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) else None
