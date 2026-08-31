"""Inert, bounded normalization for JSON, YAML, XML, and SVG."""

from __future__ import annotations

import hashlib
import html
import json
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

import yaml

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.normalized import (
    DocMetadata,
    ExtractedTable,
    NormalizedDocument,
    canonical_json,
    canonical_locator,
)
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

from .source_io import read_bounded_source
from .text import decode_text

_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")


class StructuredAdapter:
    supported_types: tuple[FileType, ...] = (
        FileType.JSON,
        FileType.JSONL,
        FileType.YAML,
        FileType.XML,
        FileType.SVG,
    )

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        detected_type = _context_file_type(context)
        raw = read_bounded_source(source, self.limits)
        text, charset = decode_text(raw, self.limits)
        if detected_type in {FileType.JSON, FileType.JSONL, FileType.YAML}:
            value = _parse_data(text, detected_type)
            nodes, depth = _measure_structure(value, self.limits)
            tables = _discover_data_tables(value)
            body = html.escape(canonical_json(value), quote=False)
        else:
            root = _parse_xml(raw)
            nodes, depth = _measure_xml(root, self.limits)
            tables = _discover_xml_tables(root)
            body = html.escape(" ".join(_iter_xml_text(root)), quote=False)
        output_bytes = len(body.encode("utf-8"))
        if output_bytes > self.limits.max_normalized_output_bytes:
            raise ResourceLimitExceeded(
                "max_normalized_output_bytes",
                self.limits.max_normalized_output_bytes,
                output_bytes,
            )
        return NormalizedDocument(
            markdown_body=body,
            metadata=DocMetadata(
                filename=source.filename,
                detected_type=detected_type,
                sha256=source.source_sha256,
                family=("markup" if detected_type is FileType.SVG else "structured"),
                canonical_mime=_context_text(context, "canonical_mime"),
                charset=charset,
                extra={
                    "document_format": detected_type.value,
                    "node_count": nodes,
                    "max_depth": depth,
                    "table_count": len(tables),
                    "rendering": "escaped_text",
                },
            ),
            tables=tables,
            embeddable=not tables,
        )

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            return self.normalize(
                ReadOnlySource(
                    handle.fileno(),
                    digest,
                    filename=meta.filename,
                    mime_type=meta.canonical_mime,
                ),
                AdapterContext(
                    adapter_key="legacy.structured",
                    metadata={
                        "detected_type": meta.detected_type.value,
                        "canonical_mime": meta.canonical_mime,
                    },
                ),
            )


def _parse_data(text: str, detected_type: FileType) -> Any:
    try:
        if detected_type is FileType.JSON:
            return json.loads(text)
        if detected_type is FileType.JSONL:
            return [
                json.loads(line)
                for line_number, line in enumerate(text.splitlines(), start=1)
                if line.strip()
            ]
        for token in yaml.scan(text):
            if isinstance(token, (yaml.AliasToken, yaml.AnchorToken, yaml.TagToken)):
                raise ValueError("YAML aliases, anchors, and custom tags are forbidden")
        return yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError, RecursionError) as error:
        raise ValueError(f"invalid {detected_type.value} structure") from error


def _parse_xml(raw: bytes) -> ET.Element:
    uppercase = raw[: 64 * 1024].upper()
    if any(marker in uppercase for marker in _XML_FORBIDDEN):
        raise ValueError("XML entities and document type declarations are forbidden")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError("invalid XML structure") from error


def _measure_structure(value: Any, limits: IngestLimits) -> tuple[int, int]:
    nodes = 0
    max_depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        max_depth = max(max_depth, depth)
        if nodes > limits.max_structured_nodes:
            raise ResourceLimitExceeded("max_structured_nodes", limits.max_structured_nodes, nodes)
        if depth > limits.max_recursion_depth:
            raise ResourceLimitExceeded("max_recursion_depth", limits.max_recursion_depth, depth)
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return nodes, max_depth


def _measure_xml(root: ET.Element, limits: IngestLimits) -> tuple[int, int]:
    nodes = 0
    max_depth = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        nodes += 1 + len(element.attrib)
        max_depth = max(max_depth, depth)
        if nodes > limits.max_structured_nodes:
            raise ResourceLimitExceeded("max_structured_nodes", limits.max_structured_nodes, nodes)
        if depth > limits.max_recursion_depth:
            raise ResourceLimitExceeded("max_recursion_depth", limits.max_recursion_depth, depth)
        stack.extend((child, depth + 1) for child in element)
    return nodes, max_depth


def _discover_data_tables(value: Any) -> list[ExtractedTable]:
    tables: list[ExtractedTable] = []
    stack: list[tuple[Any, tuple[str, ...]]] = [(value, ("root",))]
    while stack:
        current, path = stack.pop()
        if (
            isinstance(current, list)
            and current
            and all(isinstance(item, dict) for item in current)
        ):
            headers = tuple(
                dict.fromkeys(
                    str(key) for item in current for key in item if isinstance(item, dict)
                )
            )
            rows = [[_scalar_text(item.get(header)) for header in headers] for item in current]
            tables.append(
                ExtractedTable(
                    header=list(headers),
                    rows=rows,
                    source_location=canonical_locator("array", *path),
                )
            )
            continue
        if isinstance(current, dict):
            stack.extend(
                (item, (*path, str(key))) for key, item in reversed(tuple(current.items()))
            )
        elif isinstance(current, list):
            stack.extend(
                (item, (*path, str(index)))
                for index, item in reversed(tuple(enumerate(current, start=1)))
            )
    return tables


def _discover_xml_tables(root: ET.Element) -> list[ExtractedTable]:
    tables: list[ExtractedTable] = []
    for parent_ordinal, parent in enumerate(root.iter(), start=1):
        groups: dict[str, list[ET.Element]] = defaultdict(list)
        for child in parent:
            groups[_local_name(child.tag)].append(child)
        for tag, items in groups.items():
            if len(items) < 2 or not all(list(item) for item in items):
                continue
            headers = tuple(
                dict.fromkeys(_local_name(child.tag) for item in items for child in item)
            )
            rows = [
                [
                    next(
                        (
                            " ".join(candidate.itertext()).strip()
                            for candidate in item
                            if _local_name(candidate.tag) == header
                        ),
                        "",
                    )
                    for header in headers
                ]
                for item in items
            ]
            tables.append(
                ExtractedTable(
                    header=list(headers),
                    rows=rows,
                    source_location=canonical_locator("xml", parent_ordinal, tag),
                )
            )
    return tables


def _iter_xml_text(root: ET.Element) -> Iterator[str]:
    for value in root.itertext():
        cleaned = " ".join(value.split())
        if cleaned:
            yield cleaned


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return canonical_json(value)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        detected_type = FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("structured adapter context requires a detected type") from error
    if detected_type not in StructuredAdapter.supported_types:
        raise ValueError(f"structured adapter cannot handle {detected_type.value!r}")
    return detected_type


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) else None
