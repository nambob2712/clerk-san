"""Provider-neutral, inert PPTX structure extraction from a bounded OOXML ZIP."""

from __future__ import annotations

import hashlib
import html
import io
import posixpath
import re
import stat
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import (
    IngestLimits,
    ParseBudget,
    UnsafeArchiveMemberError,
    safe_zip_central_directory,
    safe_zip_members,
)
from clerksan.ingest.normalized import (
    DocMetadata,
    ExtractedTable,
    NormalizedDocument,
    canonical_locator,
)
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

from .archive import contains_forbidden_xml_declaration, inspect_image_bytes
from .source_io import read_bounded_source

_PRESENTATION_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
)
_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_ACTIVE_NAME_PARTS = (
    "embeddings/",
    "vbaproject",
    "activex/",
    "ctrlprops/",
)
_ACTIVE_CONTENT_MARKERS = (
    "macroenabled",
    "vnd.ms-office.vbaproject",
    "oleobject",
    "activex",
)
_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
)


class PptxAdapter:
    supported_types: tuple[FileType, ...] = (FileType.PPTX,)

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        if _context_file_type(context) is not FileType.PPTX:
            raise ValueError("PPTX adapter requires PPTX input")
        raw = read_bounded_source(source, self.limits)
        parsed = _parse_pptx(raw, self.limits)
        return NormalizedDocument(
            markdown_body=parsed[0],
            metadata=DocMetadata(
                filename=source.filename,
                detected_type=FileType.PPTX,
                sha256=source.source_sha256,
                family="presentation",
                canonical_mime=(
                    _context_text(context, "canonical_mime")
                    or "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                ),
                page_provenance=parsed[2],
                extra={
                    "document_format": "pptx",
                    "slide_count": len(parsed[2]),
                    "table_count": len(parsed[1]),
                    "media": parsed[3],
                    "text_locators": parsed[4],
                    "residual_markdown": parsed[0] if parsed[4] else "",
                    "formula_evaluation": "disabled",
                    "external_relationships": "rejected",
                    "active_content": "rejected",
                },
            ),
            tables=parsed[1],
            embeddable=bool(parsed[0].strip()),
        )

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            return self.normalize(
                ReadOnlySource(handle.fileno(), digest, filename=meta.filename),
                AdapterContext(
                    adapter_key="legacy.pptx",
                    metadata={
                        "detected_type": meta.detected_type.value,
                        "canonical_mime": meta.canonical_mime or "",
                    },
                ),
            )


def _parse_pptx(
    raw: bytes, limits: IngestLimits
) -> tuple[
    str,
    list[ExtractedTable],
    list[str],
    list[dict[str, int | str]],
    list[dict[str, int | str]],
]:
    budget = ParseBudget(limits)
    roots: dict[str, ET.Element] = {}
    media: list[dict[str, int | str]] = []
    try:
        safe_zip_central_directory(raw, limits)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            safe_zip_members(archive, limits)
            infos = archive.infolist()
            _validate_collisions(info.filename for info in infos)
            for info in infos:
                if info.is_dir():
                    continue
                _validate_zip_info(info)
                _validate_package_member_name(info.filename)
                budget.consume_parts(1)
                data = _read_member(archive, info, budget)
                lowered = info.filename.casefold()
                if any(part in lowered for part in _ACTIVE_NAME_PARTS):
                    raise ValueError("PPTX contains embedded or active content")
                if info.filename.endswith((".xml", ".rels")) or (
                    info.filename == "[Content_Types].xml"
                ):
                    root = _parse_package_xml(info.filename, data, budget)
                    _reject_external_relationships(info.filename, root)
                    roots[info.filename] = root
                elif _is_image(data):
                    width, height = inspect_image_bytes(data, limits=limits, budget=budget)
                    media.append(
                        {
                            "locator": canonical_locator("pptx", "member", info.filename),
                            "name": info.filename,
                            "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "width": width,
                            "height": height,
                        }
                    )
                else:
                    raise UnsafeArchiveMemberError(
                        info.filename, "PPTX member type is inspection_ambiguous"
                    )
    except zipfile.BadZipFile as error:
        raise ValueError("invalid PPTX archive") from error

    content_types = roots.get("[Content_Types].xml")
    if content_types is None or not _has_presentation_content_type(content_types):
        raise ValueError("PPTX package lacks the presentation content type")
    slide_names = _ordered_slide_names(roots)
    if not slide_names:
        raise ValueError("PPTX package has no slides")
    budget.consume_pages(len(slide_names))

    prose_sections: list[str] = []
    tables: list[ExtractedTable] = []
    provenance: list[str] = []
    text_locators: list[dict[str, int | str]] = []
    for slide_number, slide_name in enumerate(slide_names, start=1):
        root = roots.get(slide_name)
        if root is None:
            raise ValueError(f"PPTX slide relationship targets missing member {slide_name!r}")
        slide_locator = canonical_locator("pptx", "slide", slide_number, slide_name)
        provenance.append(slide_locator)
        table_text_nodes: set[int] = set()
        for table_number, table in enumerate(_elements_named(root, "tbl"), start=1):
            matrix, text_node_ids = _pptx_table_matrix(table, budget)
            table_text_nodes.update(text_node_ids)
            if not matrix:
                continue
            width = max(len(row) for row in matrix)
            matrix = [row + [""] * (width - len(row)) for row in matrix]
            tables.append(
                ExtractedTable(
                    header=matrix[0],
                    rows=matrix[1:],
                    source_location=canonical_locator(
                        "pptx", "slide", slide_number, "table", table_number
                    ),
                )
            )
            budget.consume_normalized_output(
                sum(len(value.encode("utf-8")) for row in matrix for value in row)
            )
        slide_text: list[str] = []
        for ordinal, element in enumerate(_elements_named(root, "t"), start=1):
            if id(element) in table_text_nodes:
                continue
            text = _compact(element.text or "")
            if not text:
                continue
            budget.consume_characters(len(text))
            slide_text.append(text)
            text_locators.append(
                {
                    "locator": canonical_locator("pptx", "slide", slide_number, "text", ordinal),
                    "member": slide_name,
                }
            )
        escaped_text = html.escape("\n".join(slide_text), quote=False)
        heading = f"Slide {slide_number}"
        section = f"{heading}\n{escaped_text}" if escaped_text else heading
        budget.consume_normalized_output(len(section.encode("utf-8")))
        prose_sections.append(section)
    body = "\n\n".join(prose_sections)
    return body, tables, provenance, media, text_locators


def _ordered_slide_names(roots: dict[str, ET.Element]) -> list[str]:
    presentation = roots.get("ppt/presentation.xml")
    relationships = roots.get("ppt/_rels/presentation.xml.rels")
    if presentation is None or relationships is None:
        return sorted(
            (name for name in roots if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=_slide_number,
        )
    targets = {
        element.attrib.get("Id", ""): element.attrib.get("Target", "")
        for element in relationships.iter()
        if _local_name(element.tag) == "Relationship"
    }
    result: list[str] = []
    for slide_id in _elements_named(presentation, "sldId"):
        relationship_id = next(
            (
                value
                for key, value in slide_id.attrib.items()
                if key.startswith("{") and _local_name(key) == "id"
            ),
            None,
        )
        if relationship_id is None:
            relationship_id = next(
                (
                    value
                    for key, value in slide_id.attrib.items()
                    if key.casefold() in {"r:id", "relationshipid"}
                ),
                None,
            )
        target = targets.get(relationship_id or "")
        if not target:
            raise ValueError("PPTX slide relationship is incomplete")
        result.append(_resolve_package_target("ppt/presentation.xml", target))
    if len(result) != len(set(result)):
        raise ValueError("PPTX slide relationships contain duplicate targets")
    return result


def _pptx_table_matrix(table: ET.Element, budget: ParseBudget) -> tuple[list[list[str]], set[int]]:
    rows: list[list[str]] = []
    text_node_ids: set[int] = set()
    for row in (child for child in table if _local_name(child.tag) == "tr"):
        values: list[str] = []
        budget.consume_rows(1)
        for cell in (child for child in row if _local_name(child.tag) == "tc"):
            budget.consume_cells(1)
            pieces: list[str] = []
            for text_node in _elements_named(cell, "t"):
                text_node_ids.add(id(text_node))
                value = _compact(text_node.text or "")
                if value:
                    budget.consume_characters(len(value))
                    pieces.append(value)
            values.append(" ".join(pieces))
        if values:
            rows.append(values)
    return rows, text_node_ids


def _parse_package_xml(name: str, data: bytes, budget: ParseBudget) -> ET.Element:
    if contains_forbidden_xml_declaration(data):
        raise UnsafeArchiveMemberError(name, "XML entities and doctypes are forbidden")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise UnsafeArchiveMemberError(name, "invalid PPTX XML") from error
    node_count = sum(1 + len(element.attrib) for element in root.iter())
    budget.consume_nodes(node_count)
    return root


def _reject_external_relationships(name: str, root: ET.Element) -> None:
    for element in root.iter():
        local = _local_name(element.tag).casefold()
        if local in {"oleobj", "control", "custdata", "audiofile", "videofile"}:
            raise UnsafeArchiveMemberError(name, "active or embedded PPTX XML")
        if _local_name(element.tag) != "Relationship":
            continue
        if element.attrib.get("TargetMode", "").casefold() == "external":
            raise UnsafeArchiveMemberError(name, "external relationships are forbidden")
        target = element.attrib.get("Target", "")
        relationship_type = element.attrib.get("Type", "").casefold()
        if any(
            marker in relationship_type
            for marker in ("oleobject", "activex", "vbaproject", "/package")
        ):
            raise UnsafeArchiveMemberError(name, "active package relationship")
        if target.casefold().startswith(("http:", "https:", "ftp:", "file:", "//")):
            raise UnsafeArchiveMemberError(name, "external relationship target")


def _has_presentation_content_type(root: ET.Element) -> bool:
    values = " ".join(
        value.casefold() for element in root.iter() for value in element.attrib.values()
    )
    if any(marker in values for marker in _ACTIVE_CONTENT_MARKERS):
        raise ValueError("PPTX package advertises macro or active content")
    return _PRESENTATION_CONTENT_TYPE in values


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: ParseBudget) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    try:
        with archive.open(info) as stream:
            while observed < info.file_size:
                chunk = stream.read(min(64 * 1024, info.file_size - observed))
                if not chunk:
                    break
                observed += len(chunk)
                budget.consume_bytes(len(chunk))
                chunks.append(chunk)
            if observed != info.file_size or stream.read(1):
                raise UnsafeArchiveMemberError(info.filename, "member size mismatch")
    except (RuntimeError, zipfile.BadZipFile, zlib.error) as error:
        raise UnsafeArchiveMemberError(info.filename, "corrupt or encrypted member") from error
    return b"".join(chunks)


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x41:
        raise UnsafeArchiveMemberError(info.filename, "encrypted member")
    if info.compress_type not in _ZIP_METHODS:
        raise UnsafeArchiveMemberError(info.filename, "unsupported compression method")
    mode = info.external_attr >> 16
    file_kind = stat.S_IFMT(mode)
    if file_kind and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise UnsafeArchiveMemberError(info.filename, "non-regular package member")


def _validate_package_member_name(name: str) -> None:
    lowered = name.casefold()
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise UnsafeArchiveMemberError(name, "unsafe package path")
    if any(part in {"", ".", ".."} for part in name.rstrip("/").split("/")):
        raise UnsafeArchiveMemberError(name, "unsafe package path")
    if lowered.endswith((".bin", ".exe", ".dll", ".js")):
        raise UnsafeArchiveMemberError(name, "active package member")


def _validate_collisions(names) -> None:
    seen: dict[str, str] = {}
    for name in names:
        normalized = unicodedata.normalize("NFKC", name.rstrip("/")).casefold()
        if normalized in seen:
            raise UnsafeArchiveMemberError(name, f"name collides with {seen[normalized]!r}")
        seen[normalized] = name


def _resolve_package_target(base: str, target: str) -> str:
    if not target or target.startswith(("/", "\\")) or ":" in target.split("/", 1)[0]:
        raise ValueError("PPTX relationship target is unsafe")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError("PPTX relationship target escapes the package")
    return resolved


def _elements_named(root: ET.Element, name: str):
    return (element for element in root.iter() if _local_name(element.tag) == name)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _slide_number(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _is_image(data: bytes) -> bool:
    if data.startswith(b"RIFF"):
        return len(data) >= 12 and data[8:12] == b"WEBP"
    return any(data.startswith(signature) for signature in _IMAGE_SIGNATURES)


def _compact(value: str) -> str:
    return " ".join(value.split())


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        return FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("PPTX adapter context requires a detected type") from error


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) and value else None
