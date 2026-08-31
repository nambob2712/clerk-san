"""Bounded inert extraction for ODT, ODP, and ODS OpenDocument packages."""

from __future__ import annotations

import hashlib
import html
import io
import stat
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import PurePosixPath

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import (
    IngestLimits,
    ParseBudget,
    ResourceLimitExceeded,
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

_ODF_TYPES = frozenset({FileType.ODT, FileType.ODP, FileType.ODS})
_MIMETYPE_BY_TYPE = {
    FileType.ODT: "application/vnd.oasis.opendocument.text",
    FileType.ODP: "application/vnd.oasis.opendocument.presentation",
    FileType.ODS: "application/vnd.oasis.opendocument.spreadsheet",
}
_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
)
_CORE_XML_MEMBERS = frozenset(
    {
        "content.xml",
        "meta.xml",
        "settings.xml",
        "styles.xml",
        "manifest.rdf",
        "META-INF/manifest.xml",
        "META-INF/documentsignatures.xml",
        "META-INF/macrosignatures.xml",
    }
)


class OdfAdapter:
    supported_types: tuple[FileType, ...] = tuple(sorted(_ODF_TYPES, key=str))

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        detected_type = _context_file_type(context)
        raw = read_bounded_source(source, self.limits)
        body, tables, provenance, extra = _parse_odf(raw, detected_type, self.limits)
        return NormalizedDocument(
            markdown_body=body,
            metadata=DocMetadata(
                filename=source.filename,
                detected_type=detected_type,
                sha256=source.source_sha256,
                family=(
                    "tabular"
                    if detected_type is FileType.ODS
                    else "presentation"
                    if detected_type is FileType.ODP
                    else "document"
                ),
                canonical_mime=(
                    _context_text(context, "canonical_mime") or _MIMETYPE_BY_TYPE[detected_type]
                ),
                page_provenance=provenance,
                extra={**extra, "residual_markdown": body},
            ),
            tables=tables,
            embeddable=(detected_type is not FileType.ODS and bool(body.strip())),
        )

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            return self.normalize(
                ReadOnlySource(handle.fileno(), digest, filename=meta.filename),
                AdapterContext(
                    adapter_key="legacy.odf",
                    metadata={
                        "detected_type": meta.detected_type.value,
                        "canonical_mime": meta.canonical_mime or "",
                    },
                ),
            )


def _parse_odf(
    raw: bytes,
    detected_type: FileType,
    limits: IngestLimits,
) -> tuple[str, list[ExtractedTable], list[str], dict[str, object]]:
    budget = ParseBudget(limits)
    roots: dict[str, ET.Element] = {}
    media: list[dict[str, int | str]] = []
    try:
        safe_zip_central_directory(raw, limits)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            safe_zip_members(archive, limits)
            infos = archive.infolist()
            _validate_collisions(info.filename for info in infos)
            if not infos or infos[0].filename != "mimetype":
                raise ValueError("OpenDocument mimetype must be the first package member")
            if infos[0].compress_type != zipfile.ZIP_STORED:
                raise ValueError("OpenDocument mimetype member must be uncompressed")
            for info in infos:
                if info.is_dir():
                    continue
                _validate_zip_info(info)
                _validate_package_member_name(info.filename)
                budget.consume_parts(1)
                data = _read_member(archive, info, budget)
                if info.filename == "mimetype":
                    try:
                        package_mimetype = data.decode("ascii")
                    except UnicodeDecodeError as error:
                        raise ValueError("OpenDocument mimetype is not ASCII") from error
                    if package_mimetype != _MIMETYPE_BY_TYPE[detected_type]:
                        raise ValueError("OpenDocument mimetype does not match detected type")
                elif info.filename in _CORE_XML_MEMBERS or info.filename.endswith((".xml", ".rdf")):
                    root = _parse_package_xml(info.filename, data, budget)
                    _reject_active_or_external_xml(info.filename, root)
                    roots[info.filename] = root
                elif _is_image(data):
                    width, height = inspect_image_bytes(data, limits=limits, budget=budget)
                    media.append(
                        {
                            "locator": canonical_locator("odf", "member", info.filename),
                            "name": info.filename,
                            "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "width": width,
                            "height": height,
                        }
                    )
                else:
                    raise UnsafeArchiveMemberError(
                        info.filename, "OpenDocument member type is inspection_ambiguous"
                    )
    except zipfile.BadZipFile as error:
        raise ValueError("invalid OpenDocument archive") from error

    content = roots.get("content.xml")
    if content is None:
        raise ValueError("OpenDocument package lacks content.xml")
    tables, table_text_nodes, formula_count = _extract_tables(content, budget)
    provenance: list[str] = []
    text_locators: list[dict[str, int | str]] = []
    if detected_type is FileType.ODP:
        prose = _extract_pages(content, table_text_nodes, budget, provenance, text_locators)
    else:
        prose = _extract_narrative(content, table_text_nodes, budget, text_locators)
    body = "\n\n".join(prose)
    budget.consume_normalized_output(len(body.encode("utf-8")))
    extra: dict[str, object] = {
        "document_format": detected_type.value,
        "table_count": len(tables),
        "formula_cell_count": formula_count,
        "formula_evaluation": "disabled",
        "external_references": "rejected",
        "active_content": "rejected",
        "media": media,
        "text_locators": text_locators,
    }
    return body, tables, provenance, extra


def _extract_tables(
    root: ET.Element, budget: ParseBudget
) -> tuple[list[ExtractedTable], set[int], int]:
    tables: list[ExtractedTable] = []
    table_text_nodes: set[int] = set()
    formula_count = 0
    for table_ordinal, table in enumerate(_elements_named(root, "table"), start=1):
        table_name = next(
            (value for key, value in table.attrib.items() if _local_name(key) == "name"),
            f"table-{table_ordinal}",
        )
        matrix: list[list[str]] = []
        for row in _rows_for_table(table):
            row_repeat = _bounded_repeat(
                row, "number-rows-repeated", budget.limits.max_tabular_rows
            )
            row_values: list[str] = []
            for cell in row:
                if _local_name(cell.tag) not in {"table-cell", "covered-table-cell"}:
                    continue
                column_repeat = _bounded_repeat(
                    cell, "number-columns-repeated", budget.limits.max_tabular_cells
                )
                formula = next(
                    (value for key, value in cell.attrib.items() if _local_name(key) == "formula"),
                    None,
                )
                text_nodes = list(_elements_named(cell, "p"))
                for node in text_nodes:
                    table_text_nodes.add(id(node))
                displayed = _compact(" ".join("".join(node.itertext()) for node in text_nodes))
                value = formula if formula is not None else _cell_literal(cell, displayed)
                if formula is not None:
                    formula_count += row_repeat * column_repeat
                budget.consume_characters(len(value) * row_repeat * column_repeat)
                if len(row_values) + column_repeat > budget.limits.max_tabular_cells:
                    raise ResourceLimitExceeded(
                        "max_tabular_cells",
                        budget.limits.max_tabular_cells,
                        len(row_values) + column_repeat,
                    )
                row_values.extend([value] * column_repeat)
            if not row_values:
                continue
            budget.consume_rows(row_repeat)
            budget.consume_cells(len(row_values) * row_repeat)
            matrix.extend([list(row_values) for _ in range(row_repeat)])
        if not matrix:
            continue
        width = max(len(row) for row in matrix)
        matrix = [row + [""] * (width - len(row)) for row in matrix]
        tables.append(
            ExtractedTable(
                header=matrix[0],
                rows=matrix[1:],
                source_location=canonical_locator("odf", "table", table_ordinal, table_name),
            )
        )
        budget.consume_normalized_output(
            sum(len(value.encode("utf-8")) for row in matrix for value in row)
        )
    return tables, table_text_nodes, formula_count


def _extract_pages(
    root: ET.Element,
    table_text_nodes: set[int],
    budget: ParseBudget,
    provenance: list[str],
    text_locators: list[dict[str, int | str]],
) -> list[str]:
    pages = list(_elements_named(root, "page"))
    budget.consume_pages(len(pages))
    result: list[str] = []
    for page_number, page in enumerate(pages, start=1):
        locator = canonical_locator("odf", "page", page_number)
        provenance.append(locator)
        values: list[str] = []
        for ordinal, node in enumerate(_narrative_nodes(page), start=1):
            if id(node) in table_text_nodes:
                continue
            value = _compact("".join(node.itertext()))
            if not value:
                continue
            budget.consume_characters(len(value))
            values.append(value)
            text_locators.append(
                {
                    "locator": canonical_locator("odf", "page", page_number, "text", ordinal),
                    "member": "content.xml",
                }
            )
        escaped = html.escape("\n".join(values), quote=False)
        result.append(f"Slide {page_number}\n{escaped}" if escaped else f"Slide {page_number}")
    return result


def _extract_narrative(
    root: ET.Element,
    table_text_nodes: set[int],
    budget: ParseBudget,
    text_locators: list[dict[str, int | str]],
) -> list[str]:
    result: list[str] = []
    for ordinal, node in enumerate(_narrative_nodes(root), start=1):
        if id(node) in table_text_nodes:
            continue
        value = _compact("".join(node.itertext()))
        if not value:
            continue
        budget.consume_characters(len(value))
        escaped = html.escape(value, quote=False)
        result.append(escaped)
        text_locators.append(
            {
                "locator": canonical_locator("odf", "text", ordinal),
                "member": "content.xml",
            }
        )
    return result


def _bounded_repeat(element: ET.Element, local_name: str, limit: int) -> int:
    raw_value = next(
        (value for key, value in element.attrib.items() if _local_name(key) == local_name),
        "1",
    )
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"OpenDocument {local_name} is invalid") from error
    if value < 1:
        raise ValueError(f"OpenDocument {local_name} must be positive")
    if value > limit:
        raise ResourceLimitExceeded(
            "max_tabular_rows" if "rows" in local_name else "max_tabular_cells",
            limit,
            value,
        )
    return value


def _cell_literal(cell: ET.Element, displayed: str) -> str:
    if displayed:
        return displayed
    values = {
        _local_name(key): value
        for key, value in cell.attrib.items()
        if _local_name(key)
        in {"boolean-value", "date-value", "string-value", "time-value", "value"}
    }
    for key in ("string-value", "value", "date-value", "time-value", "boolean-value"):
        if key in values:
            return values[key]
    return ""


def _parse_package_xml(name: str, data: bytes, budget: ParseBudget) -> ET.Element:
    if contains_forbidden_xml_declaration(data):
        raise UnsafeArchiveMemberError(name, "XML entities and doctypes are forbidden")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise UnsafeArchiveMemberError(name, "invalid OpenDocument XML") from error
    budget.consume_nodes(sum(1 + len(element.attrib) for element in root.iter()))
    return root


def _reject_active_or_external_xml(name: str, root: ET.Element) -> None:
    for element in root.iter():
        local = _local_name(element.tag).casefold()
        namespace = element.tag.partition("}")[0].casefold()
        if local in {"script", "scripts", "event-listener", "dde-link"}:
            raise UnsafeArchiveMemberError(name, "active OpenDocument element")
        if "script" in namespace:
            raise UnsafeArchiveMemberError(name, "script namespace is forbidden")
        if local in {"encryption-data", "encrypted-key"}:
            raise UnsafeArchiveMemberError(name, "encrypted OpenDocument content")
        for key, value in element.attrib.items():
            key_local = _local_name(key).casefold()
            if key_local.startswith("on"):
                raise UnsafeArchiveMemberError(name, "event attributes are forbidden")
            if key_local == "href" and _is_external_or_unsafe_href(value):
                raise UnsafeArchiveMemberError(name, "external or unsafe package reference")


def _is_external_or_unsafe_href(value: str) -> bool:
    normalized = value.strip()
    lowered = normalized.casefold()
    if not normalized or normalized.startswith("#"):
        return False
    if lowered.startswith(("//", "/", "http:", "https:", "ftp:", "file:", "data:")):
        return True
    return any(part == ".." for part in PurePosixPath(normalized).parts)


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
    if lowered.startswith(("basic/", "scripts/", "object ", "object/")) or "macro" in lowered:
        raise UnsafeArchiveMemberError(name, "active or embedded OpenDocument content")
    if lowered.endswith((".bin", ".exe", ".dll", ".js", ".class")):
        raise UnsafeArchiveMemberError(name, "active OpenDocument member")


def _validate_collisions(names) -> None:
    seen: dict[str, str] = {}
    for name in names:
        normalized = unicodedata.normalize("NFKC", name.rstrip("/")).casefold()
        if normalized in seen:
            raise UnsafeArchiveMemberError(name, f"name collides with {seen[normalized]!r}")
        seen[normalized] = name


def _narrative_nodes(root: ET.Element):
    return (element for element in root.iter() if _local_name(element.tag) in {"h", "p"})


def _rows_for_table(table: ET.Element):
    """Yield this table's rows while stopping before any nested table."""

    stack = list(reversed(tuple(table)))
    while stack:
        element = stack.pop()
        local = _local_name(element.tag)
        if local == "table":
            continue
        if local == "table-row":
            yield element
            continue
        stack.extend(reversed(tuple(element)))


def _elements_named(root: ET.Element, name: str):
    return (element for element in root.iter() if _local_name(element.tag) == name)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _is_image(data: bytes) -> bool:
    if data.startswith(b"RIFF"):
        return len(data) >= 12 and data[8:12] == b"WEBP"
    return any(data.startswith(signature) for signature in _IMAGE_SIGNATURES)


def _compact(value: str) -> str:
    return " ".join(value.split())


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        detected_type = FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("OpenDocument adapter context requires a detected type") from error
    if detected_type not in _ODF_TYPES:
        raise ValueError(f"OpenDocument adapter cannot handle {detected_type.value!r}")
    return detected_type


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) and value else None
