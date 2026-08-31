"""Bounded inert RFC 5322 normalization with recursive attachment inspection."""

from __future__ import annotations

import hashlib
import html
import tempfile
import unicodedata
from dataclasses import dataclass, field
from email import policy
from email.errors import MessageDefect
from email.message import EmailMessage, Message
from email.parser import BytesParser

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ParseBudget, ResourceLimitExceeded
from clerksan.ingest.normalized import (
    DocMetadata,
    ExtractedTable,
    NormalizedDocument,
    canonical_locator,
)
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

from .archive import InspectedMember, inspect_attachment_bytes
from .html import parse_inert_html
from .source_io import read_bounded_source
from .text import decode_text

_HEADER_FIELDS = ("date", "from", "to", "cc", "subject", "message-id")
_CHARSET_ALIASES = {
    "ascii": "ascii",
    "cp932": "cp932",
    "iso-2022-jp": "iso2022_jp",
    "iso2022-jp": "iso2022_jp",
    "latin-1": "latin-1",
    "shift-jis": "cp932",
    "shift_jis": "cp932",
    "utf-16": "utf-16",
    "utf-8": "utf-8-sig",
    "utf8": "utf-8-sig",
    "windows-31j": "cp932",
}


@dataclass(slots=True)
class _EmailState:
    budget: ParseBudget
    bodies: list[str] = field(default_factory=list)
    body_locators: list[dict[str, int | str]] = field(default_factory=list)
    members: list[InspectedMember] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    attachment_names: dict[str, str] = field(default_factory=dict)
    attachment_count: int = 0
    part_ordinal: int = 0
    html_active_elements: int = 0
    html_external_references: int = 0


class EmailAdapter:
    supported_types: tuple[FileType, ...] = (FileType.EML,)

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        if _context_file_type(context) is not FileType.EML:
            raise ValueError("email adapter requires EML input")
        raw = read_bounded_source(source, self.limits)
        message = _parse_message(raw)
        state = _EmailState(ParseBudget(self.limits))
        decoded_headers = _inspect_headers(message, state.budget)
        _walk_message(message, self.limits, state, depth=1)

        header_lines = [
            f"{field.title()}: {html.escape(value, quote=False)}"
            for field, value in decoded_headers.items()
            if value
        ]
        attachment_lines = [
            f"- {html.escape(member.name, quote=False)} ({member.kind}, {member.size} bytes)"
            for member in state.members
        ]
        sections: list[str] = []
        if header_lines:
            sections.append("\n".join(header_lines))
        sections.extend(state.bodies)
        if attachment_lines:
            sections.append("Attachments:\n" + "\n".join(attachment_lines))
        body = "\n\n".join(sections)
        table_output_size = sum(
            len(value.encode("utf-8"))
            for table in state.tables
            for row in (table.header, *table.rows)
            for value in row
        )
        output_size = len(body.encode("utf-8")) + table_output_size
        if output_size > self.limits.max_normalized_output_bytes:
            raise ResourceLimitExceeded(
                "max_normalized_output_bytes",
                self.limits.max_normalized_output_bytes,
                output_size,
            )
        return NormalizedDocument(
            markdown_body=body,
            metadata=DocMetadata(
                filename=source.filename,
                detected_type=FileType.EML,
                sha256=source.source_sha256,
                family="email",
                canonical_mime=_context_text(context, "canonical_mime") or "message/rfc822",
                extra={
                    "document_format": "eml",
                    "headers": decoded_headers,
                    "part_count": state.budget.parts_consumed,
                    "attachment_count": state.attachment_count,
                    "inspected_member_count": len(state.members),
                    "table_count": len(state.tables),
                    "attachments": [member.as_json() for member in state.members],
                    "body_locators": state.body_locators,
                    "residual_markdown": body,
                    "max_nesting": state.budget.max_nesting,
                    "html_active_element_count": state.html_active_elements,
                    "html_external_reference_count": state.html_external_references,
                    "remote_fetch": "disabled",
                },
            ),
            tables=state.tables,
            embeddable=bool(state.bodies) and not state.tables,
        )

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            return self.normalize(
                ReadOnlySource(handle.fileno(), digest, filename=meta.filename),
                AdapterContext(
                    adapter_key="legacy.email",
                    metadata={
                        "detected_type": meta.detected_type.value,
                        "canonical_mime": meta.canonical_mime or "message/rfc822",
                    },
                ),
            )


def _parse_message(raw: bytes) -> EmailMessage:
    strict_policy = policy.default.clone(raise_on_defect=True)
    try:
        message = BytesParser(policy=strict_policy).parsebytes(raw)
    except (MessageDefect, ValueError, TypeError) as error:
        raise ValueError("invalid or defective EML structure") from error
    if not isinstance(message, EmailMessage):
        raise ValueError("EML parser did not produce an EmailMessage")
    _reject_defects(message)
    return message


def _walk_message(
    message: Message,
    limits: IngestLimits,
    state: _EmailState,
    *,
    depth: int,
) -> None:
    state.budget.consume_nesting(depth)
    state.budget.consume_parts(1)
    state.part_ordinal += 1
    ordinal = state.part_ordinal
    _reject_defects(message)
    if depth > 1:
        _inspect_headers(message, state.budget)

    if message.get_content_type() == "message/rfc822":
        nested_messages = _nested_messages(message)
        if not nested_messages:
            raise ValueError("message/rfc822 part has no nested message")
        for nested in nested_messages:
            nested_bytes = nested.as_bytes(policy=policy.default)
            state.budget.consume_bytes(len(nested_bytes))
            locator = canonical_locator("email", "part", ordinal, "message")
            attachment_name = _attachment_name(message, ordinal, default_suffix=".eml")
            _register_attachment_name(attachment_name, state)
            state.attachment_count += 1
            state.members.append(
                InspectedMember(
                    locator=locator,
                    name=attachment_name,
                    kind="email",
                    size=len(nested_bytes),
                    sha256=hashlib.sha256(nested_bytes).hexdigest(),
                    depth=depth,
                )
            )
            _walk_message(nested, limits, state, depth=depth + 1)
        return

    if message.is_multipart():
        children = list(message.iter_parts())
        if not children:
            raise ValueError("multipart EML part has no children")
        if message.get_content_subtype().casefold() == "alternative":
            _walk_alternative(children, limits, state, depth=depth + 1)
        else:
            for child in children:
                _walk_message(child, limits, state, depth=depth + 1)
        return

    disposition = message.get_content_disposition()
    content_type = message.get_content_type().casefold()
    if disposition == "attachment" or message.get_filename():
        _inspect_attachment(message, limits, state, depth, ordinal)
        return
    if content_type in {"text/plain", "text/html"}:
        _append_body(message, content_type, limits, state, ordinal)
        return
    if content_type.startswith("image/"):
        _inspect_attachment(message, limits, state, depth, ordinal)
        return
    raise ValueError(f"EML part type {content_type!r} is inspection_ambiguous")


def _walk_alternative(
    children: list[Message],
    limits: IngestLimits,
    state: _EmailState,
    *,
    depth: int,
) -> None:
    body_candidates = [
        child
        for child in children
        if child.get_content_disposition() != "attachment"
        and child.get_content_type().casefold() in {"text/plain", "text/html"}
    ]
    selected = next(
        (child for child in body_candidates if child.get_content_type() == "text/plain"),
        body_candidates[0] if body_candidates else None,
    )
    for child in children:
        if child is selected or child not in body_candidates:
            _walk_message(child, limits, state, depth=depth)
        else:
            _inspect_unselected_alternative(child, limits, state, depth=depth)


def _append_body(
    message: Message,
    content_type: str,
    limits: IngestLimits,
    state: _EmailState,
    ordinal: int,
    *,
    emit: bool = True,
) -> None:
    raw = _decoded_payload(message)
    state.budget.consume_bytes(len(raw))
    text, charset = _decode_email_text(raw, message.get_content_charset(), limits)
    state.budget.consume_characters(len(text))
    locator = canonical_locator("email", "part", ordinal, content_type.replace("/", "_"))
    if content_type == "text/html":
        parsed = parse_inert_html(text, limits)
        state.budget.consume_nodes(parsed.node_count)
        for table_number, table in enumerate(parsed.tables, start=1):
            state.budget.consume_rows(1 + len(table.rows))
            state.budget.consume_cells(len(table.header) + sum(len(row) for row in table.rows))
            if emit:
                state.budget.consume_normalized_output(
                    sum(
                        len(value.encode("utf-8"))
                        for row in (table.header, *table.rows)
                        for value in row
                    )
                )
                state.tables.append(
                    table.model_copy(
                        update={
                            "source_location": canonical_locator(
                                "email", "part", ordinal, "html_table", table_number
                            )
                        }
                    )
                )
        escaped = parsed.escaped_text
        state.html_active_elements += parsed.active_element_count
        state.html_external_references += parsed.external_reference_count
    else:
        escaped = html.escape(text, quote=False)
    if not emit:
        return
    state.budget.consume_normalized_output(len(escaped.encode("utf-8")))
    state.bodies.append(escaped)
    state.body_locators.append(
        {"locator": locator, "content_type": content_type, "charset": charset}
    )


def _inspect_attachment(
    message: Message,
    limits: IngestLimits,
    state: _EmailState,
    depth: int,
    ordinal: int,
) -> None:
    raw = _decoded_payload(message)
    name = _attachment_name(
        message,
        ordinal,
        default_suffix=_default_attachment_suffix(message.get_content_type()),
    )
    _register_attachment_name(name, state)
    state.attachment_count += 1
    locator = canonical_locator("email", "part", ordinal, "attachment", name)
    inspected = inspect_attachment_bytes(
        name,
        raw,
        limits=limits,
        budget=state.budget,
        depth=depth + 1,
        locator_prefix=locator,
    )
    state.members.extend(inspected.members)
    state.bodies.extend(inspected.escaped_text_parts)


def _inspect_unselected_alternative(
    message: Message,
    limits: IngestLimits,
    state: _EmailState,
    *,
    depth: int,
) -> None:
    """Validate every alternative while emitting only the selected representation."""

    state.budget.consume_nesting(depth)
    state.budget.consume_parts(1)
    state.part_ordinal += 1
    ordinal = state.part_ordinal
    _reject_defects(message)
    _inspect_headers(message, state.budget)
    if message.is_multipart():
        raise ValueError("nested multipart body alternative is inspection_ambiguous")
    content_type = message.get_content_type().casefold()
    _append_body(message, content_type, limits, state, ordinal, emit=False)


def _inspect_headers(message: Message, budget: ParseBudget) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in message.items():
        text = " ".join(str(value).split())
        if "\x00" in text or any(ord(char) < 32 for char in text):
            raise ValueError("EML header contains forbidden control characters")
        budget.consume_nodes(1)
        budget.consume_characters(len(name) + len(text))
        lowered = name.casefold()
        if lowered in _HEADER_FIELDS:
            previous = headers.get(lowered)
            headers[lowered] = f"{previous}, {text}" if previous else text
    return headers


def _decoded_payload(message: Message) -> bytes:
    try:
        payload = message.get_payload(decode=True)
    except (LookupError, UnicodeDecodeError, ValueError) as error:
        raise ValueError("invalid EML transfer encoding") from error
    if payload is None:
        raw_payload = message.get_payload()
        if isinstance(raw_payload, str):
            return raw_payload.encode("utf-8")
        raise ValueError("EML leaf part has no decodable payload")
    return payload


def _decode_email_text(
    raw: bytes,
    declared_charset: str | None,
    limits: IngestLimits,
) -> tuple[str, str]:
    codec = _CHARSET_ALIASES.get((declared_charset or "").casefold())
    if declared_charset and codec is None:
        raise ValueError(f"unsupported EML charset {declared_charset!r}")
    if codec is not None:
        try:
            text = raw.decode(codec)
        except UnicodeDecodeError as error:
            raise ValueError("EML body does not match its declared charset") from error
        if "\x00" in text:
            raise ValueError("EML body contains decoded NUL characters")
        if len(text) > limits.max_text_characters:
            raise ResourceLimitExceeded(
                "max_text_characters", limits.max_text_characters, len(text)
            )
        return text, codec
    return decode_text(raw, limits)


def _nested_messages(message: Message) -> list[Message]:
    payload = message.get_payload()
    if isinstance(payload, list) and all(isinstance(item, Message) for item in payload):
        return payload
    return []


def _reject_defects(message: Message) -> None:
    if message.defects:
        defect_names = ", ".join(type(defect).__name__ for defect in message.defects)
        raise ValueError(f"defective EML structure: {defect_names}")


def _attachment_name(
    message: Message,
    ordinal: int,
    *,
    default_suffix: str = "",
) -> str:
    filename = message.get_filename()
    if filename is None:
        filename = f"attachment-{ordinal}{default_suffix}"
    cleaned = " ".join(filename.split())
    if not cleaned or "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise ValueError("EML attachment filename must be a safe basename")
    if len(cleaned) > 255:
        raise ValueError("EML attachment filename exceeds bound")
    return cleaned


def _register_attachment_name(name: str, state: _EmailState) -> None:
    normalized = unicodedata.normalize("NFKC", name).casefold()
    previous = state.attachment_names.get(normalized)
    if previous is not None:
        raise ValueError(f"duplicate EML attachment name collides with {previous!r}")
    state.attachment_names[normalized] = name


def _default_attachment_suffix(content_type: str) -> str:
    return {
        "application/gzip": ".gz",
        "application/json": ".json",
        "application/pdf": ".pdf",
        "application/rtf": ".rtf",
        "application/x-tar": ".tar",
        "application/zip": ".zip",
        "text/csv": ".csv",
        "text/html": ".html",
        "text/plain": ".txt",
        "text/tab-separated-values": ".tsv",
    }.get(content_type.casefold(), "")


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        return FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("email adapter context requires a detected type") from error


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) and value else None
