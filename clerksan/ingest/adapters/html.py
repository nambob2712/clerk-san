"""Bounded inert HTML normalization without rendering or remote fetches."""

from __future__ import annotations

import hashlib
import html
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.normalized import (
    DocMetadata,
    ExtractedTable,
    NormalizedDocument,
    canonical_locator,
)
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

from .source_io import read_bounded_source
from .text import decode_text

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_SUPPRESSED_TAGS = frozenset({"script", "style"})
_ACTIVE_TAGS = frozenset(
    {"applet", "audio", "embed", "form", "iframe", "object", "script", "video"}
)
_REMOTE_ATTRIBUTES = frozenset(
    {"action", "background", "formaction", "href", "poster", "src", "srcset", "xlink:href"}
)


@dataclass(frozen=True, slots=True)
class InertHtmlResult:
    escaped_text: str
    tables: tuple[ExtractedTable, ...]
    node_count: int
    max_depth: int
    active_element_count: int
    event_attribute_count: int
    external_reference_count: int
    text_locators: tuple[dict[str, int | str], ...]


@dataclass(slots=True)
class _TableState:
    ordinal: int
    line: int
    rows: list[list[str]] = field(default_factory=list)
    current_row: list[str] | None = None
    current_cell: list[str] | None = None


class _InertHtmlParser(HTMLParser):
    def __init__(self, limits: IngestLimits) -> None:
        super().__init__(convert_charrefs=True)
        self.limits = limits
        self.stack: list[tuple[str, bool]] = []
        self.suppression_depth = 0
        self.node_count = 0
        self.max_depth = 0
        self.active_element_count = 0
        self.event_attribute_count = 0
        self.external_reference_count = 0
        self.text_parts: list[str] = []
        self.text_locators: list[dict[str, int | str]] = []
        self.tables: list[ExtractedTable] = []
        self.table: _TableState | None = None
        self.row_count = 0
        self.cell_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        self._consume_nodes(1 + len(attrs))
        suppressed = tag in _SUPPRESSED_TAGS
        if tag in _ACTIVE_TAGS:
            self.active_element_count += 1
        if suppressed:
            self.suppression_depth += 1
        for name, value in attrs:
            lowered = name.casefold()
            if lowered.startswith("on"):
                self.event_attribute_count += 1
            if lowered in _REMOTE_ATTRIBUTES and value and _is_external_reference(value):
                self.external_reference_count += 1

        if tag == "table":
            if self.table is not None:
                raise ValueError("nested HTML tables are not supported")
            self.table = _TableState(len(self.tables) + 1, self.getpos()[0])
        elif self.table is not None and tag == "tr":
            if self.table.current_row is not None:
                self._finish_row()
            self.table.current_row = []
        elif self.table is not None and tag in {"td", "th"}:
            if self.table.current_row is None:
                self.table.current_row = []
            if self.table.current_cell is not None:
                self._finish_cell()
            self.table.current_cell = []

        if tag not in _VOID_TAGS:
            self.stack.append((tag, suppressed))
            self.max_depth = max(self.max_depth, len(self.stack))
            if len(self.stack) > self.limits.max_recursion_depth:
                raise ResourceLimitExceeded(
                    "max_recursion_depth", self.limits.max_recursion_depth, len(self.stack)
                )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.table is not None and tag in {"td", "th"}:
            self._finish_cell()
        elif self.table is not None and tag == "tr":
            self._finish_row()
        elif self.table is not None and tag == "table":
            self._finish_table()

        matching_index = next(
            (index for index in range(len(self.stack) - 1, -1, -1) if self.stack[index][0] == tag),
            None,
        )
        if matching_index is None:
            return
        removed = self.stack[matching_index:]
        del self.stack[matching_index:]
        self.suppression_depth -= sum(suppressed for _, suppressed in removed)

    def handle_data(self, data: str) -> None:
        self._consume_nodes(1)
        if self.suppression_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self.table is not None and self.table.current_cell is not None:
            self.table.current_cell.append(cleaned)
            return
        self.text_parts.append(cleaned)
        line, column = self.getpos()
        self.text_locators.append(
            {
                "locator": canonical_locator("html", "text", len(self.text_parts)),
                "line": line,
                "column": column,
            }
        )

    def handle_comment(self, data: str) -> None:
        del data
        self._consume_nodes(1)

    def handle_decl(self, decl: str) -> None:
        del decl
        self._consume_nodes(1)

    def handle_pi(self, data: str) -> None:
        del data
        self._consume_nodes(1)

    def close(self) -> None:
        super().close()
        if self.table is not None:
            self._finish_cell()
            self._finish_row()
            self._finish_table()

    def _finish_cell(self) -> None:
        assert self.table is not None
        if self.table.current_cell is None:
            return
        assert self.table.current_row is not None
        self.cell_count += 1
        if self.cell_count > self.limits.max_tabular_cells:
            raise ResourceLimitExceeded(
                "max_tabular_cells", self.limits.max_tabular_cells, self.cell_count
            )
        self.table.current_row.append(" ".join(self.table.current_cell))
        self.table.current_cell = None

    def _finish_row(self) -> None:
        assert self.table is not None
        self._finish_cell()
        if self.table.current_row is None:
            return
        if self.table.current_row:
            self.row_count += 1
            if self.row_count > self.limits.max_tabular_rows:
                raise ResourceLimitExceeded(
                    "max_tabular_rows", self.limits.max_tabular_rows, self.row_count
                )
            self.table.rows.append(self.table.current_row)
        self.table.current_row = None

    def _finish_table(self) -> None:
        assert self.table is not None
        self._finish_row()
        table = self.table
        self.table = None
        if not table.rows:
            return
        width = max(len(row) for row in table.rows)
        rows = [row + [""] * (width - len(row)) for row in table.rows]
        self.tables.append(
            ExtractedTable(
                header=rows[0],
                rows=rows[1:],
                source_location=canonical_locator("html", "table", table.ordinal, table.line),
            )
        )

    def _consume_nodes(self, amount: int) -> None:
        self.node_count += amount
        if self.node_count > self.limits.max_structured_nodes:
            raise ResourceLimitExceeded(
                "max_structured_nodes", self.limits.max_structured_nodes, self.node_count
            )


def parse_inert_html(text: str, limits: IngestLimits) -> InertHtmlResult:
    """Parse HTML as inert tokens; no URL or active element is interpreted."""

    parser = _InertHtmlParser(limits)
    try:
        parser.feed(text)
        parser.close()
    except RecursionError as error:
        raise ValueError("HTML nesting is invalid") from error
    escaped = html.escape(" ".join(parser.text_parts), quote=False)
    table_output_size = sum(
        len(value.encode("utf-8"))
        for table in parser.tables
        for row in (table.header, *table.rows)
        for value in row
    )
    output_size = len(escaped.encode("utf-8")) + table_output_size
    if output_size > limits.max_normalized_output_bytes:
        raise ResourceLimitExceeded(
            "max_normalized_output_bytes", limits.max_normalized_output_bytes, output_size
        )
    return InertHtmlResult(
        escaped_text=escaped,
        tables=tuple(parser.tables),
        node_count=parser.node_count,
        max_depth=parser.max_depth,
        active_element_count=parser.active_element_count,
        event_attribute_count=parser.event_attribute_count,
        external_reference_count=parser.external_reference_count,
        text_locators=tuple(parser.text_locators),
    )


class HtmlAdapter:
    supported_types: tuple[FileType, ...] = (FileType.HTML,)

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        if _context_file_type(context) is not FileType.HTML:
            raise ValueError("HTML adapter requires HTML input")
        raw = read_bounded_source(source, self.limits)
        text, charset = decode_text(raw, self.limits)
        parsed = parse_inert_html(text, self.limits)
        return NormalizedDocument(
            markdown_body=parsed.escaped_text,
            metadata=DocMetadata(
                filename=source.filename,
                detected_type=FileType.HTML,
                sha256=source.source_sha256,
                family="markup",
                canonical_mime=_context_text(context, "canonical_mime") or "text/html",
                charset=charset,
                extra={
                    "document_format": "html",
                    "node_count": parsed.node_count,
                    "max_depth": parsed.max_depth,
                    "active_element_count": parsed.active_element_count,
                    "event_attribute_count": parsed.event_attribute_count,
                    "external_reference_count": parsed.external_reference_count,
                    "text_locators": list(parsed.text_locators),
                    "residual_markdown": parsed.escaped_text,
                    "rendering": "escaped_text",
                    "remote_fetch": "disabled",
                },
            ),
            tables=list(parsed.tables),
            embeddable=not parsed.tables,
        )

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            return self.normalize(
                ReadOnlySource(handle.fileno(), digest, filename=meta.filename),
                AdapterContext(
                    adapter_key="legacy.html",
                    metadata={
                        "detected_type": meta.detected_type.value,
                        "canonical_mime": meta.canonical_mime or "text/html",
                    },
                ),
            )


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        value = FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("HTML adapter context requires a detected type") from error
    return value


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) else None


def _is_external_reference(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized.startswith(("//", "http:", "https:", "ftp:", "file:", "data:", "javascript:"))
