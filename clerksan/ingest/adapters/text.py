"""Bounded inert normalization for plain text families."""

from __future__ import annotations

import hashlib
import html
import tempfile

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

from .source_io import read_bounded_source


class TextAdapter:
    supported_types: tuple[FileType, ...] = (
        FileType.MD,
        FileType.TXT,
        FileType.RST,
        FileType.LOG,
    )

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        detected_type = _context_file_type(context)
        raw = read_bounded_source(source, self.limits)
        body, charset = decode_text(raw, self.limits)
        return NormalizedDocument(
            markdown_body=html.escape(body, quote=False),
            metadata=DocMetadata(
                filename=source.filename,
                detected_type=detected_type,
                sha256=source.source_sha256,
                family="text",
                canonical_mime=_context_text(context, "canonical_mime") or "text/plain",
                charset=charset,
                extra={"document_format": detected_type.value, "rendering": "escaped_text"},
            ),
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
                    adapter_key="legacy.text",
                    metadata={
                        "detected_type": meta.detected_type.value,
                        "canonical_mime": meta.canonical_mime,
                    },
                ),
            )


def decode_text(raw: bytes, limits: IngestLimits) -> tuple[str, str]:
    if b"\x00" in raw and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("text input contains binary NUL bytes")
    candidates = (
        (("utf-8-sig", "utf-8"), ("utf-16", "utf-16"))
        if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))
        else (("utf-8", "utf-8"), ("cp932", "cp932"))
    )
    for codec, charset in candidates:
        try:
            text = raw.decode(codec)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            raise ValueError("text input contains decoded NUL characters")
        if len(text) > limits.max_text_characters:
            raise ResourceLimitExceeded(
                "max_text_characters", limits.max_text_characters, len(text)
            )
        controls = sum(ord(character) < 32 and character not in "\t\r\n" for character in text)
        if controls > max(2, len(text) // 100):
            raise ValueError("text input appears binary")
        return text, charset
    raise ValueError("text input is not valid UTF-8, UTF-16, or CP932")


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        detected_type = FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("text adapter context requires a detected type") from error
    if detected_type not in TextAdapter.supported_types:
        raise ValueError(f"text adapter cannot handle {detected_type.value!r}")
    return detected_type


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) else None
