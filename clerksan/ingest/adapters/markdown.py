"""Safe Markdown normalization that preserves body bytes and heading provenance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from typing import Any

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource, SandboxProtocolError

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")


class MarkdownAdapter:
    """Read UTF-8 Markdown without rendering or rewriting its semantic body."""

    supported_types: tuple[FileType, ...] = (FileType.MD,)

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        if context.metadata.get("detected_type") not in {None, FileType.MD.value}:
            raise ValueError("Markdown adapter requires detected_type md")
        source.verify_digest()
        raw = _read_descriptor(source, self.limits)
        meta = DocMetadata(
            filename=source.filename,
            detected_type=FileType.MD,
            sha256=source.source_sha256,
            family="text",
            canonical_mime=source.mime_type or "text/markdown",
            charset="utf-8",
        )
        return self._normalize_bytes(raw, meta)

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        if meta.detected_type is not FileType.MD:
            raise ValueError(f"Markdown adapter cannot handle {meta.detected_type.value!r}")
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            document = self.normalize(
                ReadOnlySource(
                    handle.fileno(),
                    digest,
                    filename=meta.filename,
                    mime_type=meta.canonical_mime or "text/markdown",
                ),
                AdapterContext(
                    adapter_key="legacy.markdown",
                    metadata={"detected_type": FileType.MD.value},
                ),
            )
        if meta.extra:
            document = document.model_copy(
                update={
                    "metadata": document.metadata.model_copy(
                        update={"extra": {**meta.extra, **document.metadata.extra}}
                    )
                }
            )
        return document

    def _normalize_bytes(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        if meta.detected_type is not FileType.MD:
            raise ValueError(f"Markdown adapter cannot handle {meta.detected_type.value!r}")
        try:
            source = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Markdown uploads must be valid UTF-8") from error
        front_matter, body = _split_front_matter(source)
        extra = dict(meta.extra)
        extra["front_matter"] = front_matter
        extra["outline"] = _outline(body)
        extra["document_format"] = "markdown"
        return NormalizedDocument(
            markdown_body=body,
            metadata=meta.model_copy(update={"extra": extra}),
        )


def _read_descriptor(source: ReadOnlySource, limits: IngestLimits) -> bytes:
    stat = os.fstat(source.fd)
    if stat.st_size > limits.max_upload_bytes:
        raise ResourceLimitExceeded("max_upload_bytes", limits.max_upload_bytes, stat.st_size)
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    offset = 0
    while offset < stat.st_size:
        chunk = os.pread(source.fd, min(1024 * 1024, stat.st_size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        digest.update(chunk)
        offset += len(chunk)
    if offset != stat.st_size or digest.hexdigest() != source.source_sha256:
        raise SandboxProtocolError("source changed while reading text input")
    return b"".join(chunks)


def _split_front_matter(source: str) -> tuple[dict[str, Any], str]:
    """Parse a deliberately small YAML-front-matter subset without a YAML runtime."""

    if not source.startswith("---\n") and not source.startswith("---\r\n"):
        return {}, source
    separator = "\r\n" if source.startswith("---\r\n") else "\n"
    lines = source.splitlines(keepends=True)
    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if closing_index is None:
        return {}, source
    front_matter = _parse_front_matter("".join(lines[1:closing_index]))
    body = "".join(lines[closing_index + 1 :])
    if separator == "\r\n" and body and not body.startswith("\r\n"):
        # No rewrite is performed; this merely makes the intentionally consumed front
        # matter boundary explicit when source used Windows line endings.
        return front_matter, body
    return front_matter, body


def _parse_front_matter(raw: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            continue
        result[key] = _scalar(value.strip())
    return result


def _scalar(value: str) -> Any:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value.strip("'\"")
    if isinstance(parsed, (str, int, float, bool, list, dict)) or parsed is None:
        return parsed
    return value


def _outline(body: str) -> list[dict[str, Any]]:
    path: list[str] = []
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(body.splitlines(), start=1):
        match = _HEADING.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        path = path[: level - 1]
        path.append(title)
        result.append({"level": level, "title": title, "path": list(path), "line": line_number})
    return result
