"""Small bounded RTF tokenizer that emits inert escaped text only."""

from __future__ import annotations

import hashlib
import html
import re
import tempfile
from dataclasses import dataclass

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ParseBudget, ResourceLimitExceeded
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument, canonical_locator
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

from .source_io import read_bounded_source

_RTF_HEADER = re.compile(rb"^\{\s*\\rtf[1-9][0-9]*\b")
_ACTIVE_CONTROLS = frozenset(
    {
        "bin",
        "datafield",
        "datastore",
        "ddeauto",
        "ddefield",
        "do",
        "field",
        "filetbl",
        "fldinst",
        "fldrslt",
        "htmlrtf",
        "htmltag",
        "include",
        "linkself",
        "linkstyles",
        "linkval",
        "objalias",
        "objclass",
        "objdata",
        "object",
        "objname",
        "objtime",
        "objupdate",
        "private",
        "shp",
        "shpinst",
        "xmlattrname",
        "xmlattrvalue",
        "xmlclose",
        "xmlname",
        "xmlnstbl",
        "xmlopen",
    }
)
_SKIP_DESTINATIONS = frozenset(
    {
        "annotation",
        "atnauthor",
        "atnid",
        "author",
        "category",
        "colortbl",
        "comment",
        "company",
        "creatim",
        "doccomm",
        "footer",
        "footerf",
        "footerl",
        "footerr",
        "fonttbl",
        "generator",
        "header",
        "headerf",
        "headerl",
        "headerr",
        "info",
        "keywords",
        "listoverridetable",
        "listtable",
        "operator",
        "pict",
        "printim",
        "revtim",
        "stylesheet",
        "subject",
        "title",
        "userprops",
    }
)
_CONTROL_TEXT = {
    "bullet": "•",
    "cell": "\t",
    "emdash": "—",
    "emspace": "\u2003",
    "endash": "–",
    "enspace": "\u2002",
    "line": "\n",
    "lquote": "‘",
    "par": "\n",
    "qmspace": "\u2005",
    "rdblquote": "”",
    "row": "\n",
    "rquote": "’",
    "tab": "\t",
    "ldblquote": "“",
}
_CODEPAGES = {
    932: "cp932",
    1252: "cp1252",
    65001: "utf-8",
}


@dataclass(slots=True)
class _GroupState:
    skip: bool = False
    starred: bool = False
    at_start: bool = True
    unicode_skip: int = 1
    codepage: str = "cp1252"

    def clone(self) -> _GroupState:
        return _GroupState(
            skip=self.skip,
            starred=False,
            at_start=True,
            unicode_skip=self.unicode_skip,
            codepage=self.codepage,
        )


class _RtfTokenizer:
    def __init__(self, raw: bytes, limits: IngestLimits) -> None:
        self.raw = raw
        self.limits = limits
        self.budget = ParseBudget(limits)
        self.stack: list[_GroupState] = []
        self.output: list[str] = []
        self.byte_buffer = bytearray()
        self.fallback_to_skip = 0
        self.pending_high_surrogate: int | None = None
        self.saw_rtf_control = False

    def parse(self) -> str:
        if not _RTF_HEADER.match(self.raw):
            raise ValueError("invalid RTF header")
        cursor = 0
        while cursor < len(self.raw):
            byte = self.raw[cursor]
            if byte == ord("{"):
                self._flush_bytes()
                parent = self.stack[-1] if self.stack else _GroupState()
                self.stack.append(parent.clone())
                self.budget.consume_nesting(len(self.stack))
                self.budget.consume_nodes(1)
                cursor += 1
                continue
            if byte == ord("}"):
                self._flush_bytes()
                if not self.stack:
                    raise ValueError("RTF has an unmatched closing group")
                self.stack.pop()
                self.budget.consume_nodes(1)
                cursor += 1
                continue
            if not self.stack:
                if chr(byte).isspace():
                    cursor += 1
                    continue
                raise ValueError("RTF content appears outside the root group")
            if byte == ord("\\"):
                if cursor + 1 >= len(self.raw) or self.raw[cursor + 1] != ord("'"):
                    self._flush_bytes()
                cursor = self._control(cursor + 1)
                continue
            self._literal_byte(byte)
            self.stack[-1].at_start = False
            cursor += 1
        self._flush_bytes()
        if self.stack:
            raise ValueError("RTF has an unterminated group")
        if not self.saw_rtf_control:
            raise ValueError("RTF version control is missing")
        self._flush_pending_surrogate()
        text = "".join(self.output)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        escaped = html.escape(text, quote=False)
        output_size = len(escaped.encode("utf-8"))
        if output_size > self.limits.max_normalized_output_bytes:
            raise ResourceLimitExceeded(
                "max_normalized_output_bytes",
                self.limits.max_normalized_output_bytes,
                output_size,
            )
        return escaped

    def _control(self, cursor: int) -> int:
        if cursor >= len(self.raw):
            raise ValueError("RTF ends with an incomplete control")
        state = self.stack[-1]
        symbol = self.raw[cursor]
        if symbol in b"\\{}":
            self.budget.consume_nodes(1)
            self._literal_byte(symbol)
            state.at_start = False
            return cursor + 1
        if symbol == ord("'"):
            if cursor + 2 >= len(self.raw):
                raise ValueError("RTF has an incomplete hexadecimal escape")
            token = self.raw[cursor + 1 : cursor + 3]
            try:
                value = int(token, 16)
            except ValueError as error:
                raise ValueError("RTF has an invalid hexadecimal escape") from error
            self.budget.consume_nodes(1)
            self._literal_byte(value)
            state.at_start = False
            return cursor + 3
        if symbol == ord("*"):
            state.starred = True
            self.budget.consume_nodes(1)
            return cursor + 1
        if symbol in b"~-_":
            mapped = {ord("~"): "\u00a0", ord("-"): "", ord("_"): "\u2011"}[symbol]
            self._append_character(mapped)
            self.budget.consume_nodes(1)
            state.at_start = False
            return cursor + 1
        if not chr(symbol).isalpha():
            self.budget.consume_nodes(1)
            state.at_start = False
            return cursor + 1

        word_start = cursor
        while cursor < len(self.raw) and chr(self.raw[cursor]).isalpha():
            cursor += 1
        word = self.raw[word_start:cursor].decode("ascii").casefold()
        sign = 1
        if cursor < len(self.raw) and self.raw[cursor] == ord("-"):
            sign = -1
            cursor += 1
        number_start = cursor
        while cursor < len(self.raw) and chr(self.raw[cursor]).isdigit():
            cursor += 1
        parameter = sign * int(self.raw[number_start:cursor]) if cursor > number_start else None
        if cursor < len(self.raw) and self.raw[cursor] == ord(" "):
            cursor += 1
        self.budget.consume_nodes(1)
        if word in _ACTIVE_CONTROLS:
            raise ValueError(f"RTF active control word \\{word} is forbidden")
        if state.starred or (state.at_start and word in _SKIP_DESTINATIONS):
            state.skip = True
        state.starred = False
        state.at_start = False

        if word == "rtf":
            self.saw_rtf_control = parameter is not None and parameter >= 1
        elif word == "uc":
            if parameter is None or not 0 <= parameter <= 8:
                raise ValueError("RTF Unicode fallback length is invalid")
            state.unicode_skip = parameter
        elif word == "u" and not state.skip:
            if parameter is None or not -32768 <= parameter <= 65535:
                raise ValueError("RTF Unicode escape is invalid")
            self._append_unicode(parameter)
            self.fallback_to_skip = state.unicode_skip
        elif word == "ansicpg":
            if parameter not in _CODEPAGES:
                raise ValueError("unsupported RTF ANSI codepage")
            state.codepage = _CODEPAGES[parameter]
        elif word == "ansi":
            state.codepage = "cp1252"
        elif word in {"mac", "pc", "pca"}:
            raise ValueError("unsupported legacy RTF codepage")
        elif word in _CONTROL_TEXT and not state.skip:
            self._append_character(_CONTROL_TEXT[word])
        return cursor

    def _literal_byte(self, value: int) -> None:
        if self.fallback_to_skip:
            self.fallback_to_skip -= 1
            return
        if self.stack and self.stack[-1].skip:
            return
        self.byte_buffer.append(value)

    def _flush_bytes(self) -> None:
        if not self.byte_buffer:
            return
        codec = self.stack[-1].codepage if self.stack else "cp1252"
        try:
            value = bytes(self.byte_buffer).decode(codec)
        except UnicodeDecodeError as error:
            raise ValueError(f"RTF text is invalid for {codec}") from error
        self.byte_buffer.clear()
        self._append_character(value)

    def _append_unicode(self, parameter: int) -> None:
        codepoint = parameter if parameter >= 0 else parameter + 65536
        if 0xD800 <= codepoint <= 0xDBFF:
            self._flush_pending_surrogate()
            self.pending_high_surrogate = codepoint
            return
        if 0xDC00 <= codepoint <= 0xDFFF:
            if self.pending_high_surrogate is None:
                raise ValueError("RTF contains an unpaired Unicode surrogate")
            high = self.pending_high_surrogate
            self.pending_high_surrogate = None
            combined = 0x10000 + ((high - 0xD800) << 10) + (codepoint - 0xDC00)
            self._append_character(chr(combined))
            return
        self._flush_pending_surrogate()
        self._append_character(chr(codepoint))

    def _flush_pending_surrogate(self) -> None:
        if self.pending_high_surrogate is not None:
            raise ValueError("RTF contains an unpaired Unicode surrogate")

    def _append_character(self, value: str) -> None:
        if not value:
            return
        if "\x00" in value:
            raise ValueError("RTF contains a NUL character")
        self.budget.consume_characters(len(value))
        self.output.append(value)


class RtfAdapter:
    supported_types: tuple[FileType, ...] = (FileType.RTF,)

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        if _context_file_type(context) is not FileType.RTF:
            raise ValueError("RTF adapter requires RTF input")
        raw = read_bounded_source(source, self.limits)
        tokenizer = _RtfTokenizer(raw, self.limits)
        body = tokenizer.parse()
        locators = [
            canonical_locator("rtf", "paragraph", ordinal)
            for ordinal, paragraph in enumerate(body.splitlines(), start=1)
            if paragraph.strip()
        ]
        return NormalizedDocument(
            markdown_body=body,
            metadata=DocMetadata(
                filename=source.filename,
                detected_type=FileType.RTF,
                sha256=source.source_sha256,
                family="document",
                canonical_mime=_context_text(context, "canonical_mime") or "application/rtf",
                charset="rtf-control-encoded",
                extra={
                    "document_format": "rtf",
                    "paragraph_locators": locators,
                    "max_group_depth": tokenizer.budget.max_nesting,
                    "node_count": tokenizer.budget.nodes_consumed,
                    "active_content": "rejected",
                    "rendering": "escaped_text",
                },
            ),
            embeddable=bool(body.strip()),
        )

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            return self.normalize(
                ReadOnlySource(handle.fileno(), digest, filename=meta.filename),
                AdapterContext(
                    adapter_key="legacy.rtf",
                    metadata={
                        "detected_type": meta.detected_type.value,
                        "canonical_mime": meta.canonical_mime or "application/rtf",
                    },
                ),
            )


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        return FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("RTF adapter context requires a detected type") from error


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) and value else None
