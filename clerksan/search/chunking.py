"""Heading-aware, token-bounded chunks for local semantic retrieval."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from clerksan.ingest.normalized import NormalizedDocument

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_LATIN_WORD = re.compile(r"[A-Za-z0-9_]+")


class ChunkingError(ValueError):
    """A document contains an indivisible unit that cannot fit the configured budget."""


class Chunk(BaseModel):
    """A stable chunk identity within one normalized document version."""

    seq: int = Field(ge=0)
    heading_path: str
    text: str = Field(min_length=1)
    token_count: int = Field(ge=1)


def estimate_tokens(text: str) -> int:
    """Use a deterministic CJK-aware estimate without loading a second tokenizer.

    Japanese characters count individually, Latin words are approximated in four
    character pieces, and punctuation counts as one token.  The estimate deliberately
    errs high, keeping the worker's embedding batches bounded on the target machine.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    total = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if _CJK.fullmatch(character):
            total += 1
            index += 1
            continue
        word = _LATIN_WORD.match(text, index)
        if word is not None:
            total += max(1, math.ceil(len(word.group()) / 4))
            index = word.end()
            continue
        total += 1
        index += 1
    return total


def chunk_document(doc: NormalizedDocument, *, max_tokens: int = 480) -> list[Chunk]:
    """Split markdown at headings and paragraphs while preserving whole tables."""

    if max_tokens < 1:
        raise ValueError("max_tokens must be greater than zero")
    chunks: list[Chunk] = []
    for heading_path, lines in _sections(doc.markdown_body):
        chunks.extend(_chunk_section(heading_path, lines, max_tokens))
    return [chunk.model_copy(update={"seq": index}) for index, chunk in enumerate(chunks)]


def _sections(body: str) -> Iterable[tuple[str, list[str]]]:
    path: list[str] = []
    lines: list[str] = []
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading is None:
            lines.append(line)
            continue
        if any(item.strip() for item in lines):
            yield " > ".join(path), lines
        level = len(heading.group(1))
        title = heading.group(2).strip()
        path = path[: level - 1]
        path.append(title)
        lines = []
    if any(item.strip() for item in lines):
        yield " > ".join(path), lines


def _chunk_section(heading_path: str, lines: list[str], max_tokens: int) -> list[Chunk]:
    blocks = _paragraph_blocks(lines)
    pieces: list[str] = []
    for block, is_table in blocks:
        block_tokens = estimate_tokens(block)
        if block_tokens <= max_tokens:
            pieces.append(block)
            continue
        if is_table:
            raise ChunkingError(
                f"table under {heading_path or 'preamble'!r} exceeds the token budget"
            )
        pieces.extend(_split_large_paragraph(block, max_tokens))

    result: list[Chunk] = []
    current: list[str] = []
    current_tokens = 0
    for piece in pieces:
        piece_tokens = estimate_tokens(piece)
        separator_tokens = 1 if current else 0
        if current and current_tokens + separator_tokens + piece_tokens > max_tokens:
            result.append(_make_chunk(heading_path, current))
            current = []
            current_tokens = 0
        current.append(piece)
        current_tokens += piece_tokens + separator_tokens
    if current:
        result.append(_make_chunk(heading_path, current))
    return result


def _paragraph_blocks(lines: list[str]) -> list[tuple[str, bool]]:
    blocks: list[tuple[str, bool]] = []
    current: list[str] = []
    current_is_table = False

    def flush() -> None:
        nonlocal current, current_is_table
        text = "\n".join(current).strip()
        if text:
            blocks.append((text, current_is_table))
        current = []
        current_is_table = False

    for line in lines:
        if not line.strip():
            flush()
            continue
        is_table_line = _is_table_line(line)
        if current and is_table_line != current_is_table:
            flush()
        current.append(line)
        current_is_table = is_table_line
    flush()
    return blocks


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _split_large_paragraph(text: str, max_tokens: int) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s*", text) if part.strip()]
    if len(sentences) <= 1:
        return _hard_split(text, max_tokens)

    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        sentence_tokens = estimate_tokens(sentence)
        if sentence_tokens > max_tokens:
            if current:
                pieces.append(" ".join(current))
                current = []
                current_tokens = 0
            pieces.extend(_hard_split(sentence, max_tokens))
            continue
        separator_tokens = 1 if current else 0
        if current and current_tokens + separator_tokens + sentence_tokens > max_tokens:
            pieces.append(" ".join(current))
            current = []
            current_tokens = 0
        current.append(sentence)
        current_tokens += sentence_tokens + separator_tokens
    if current:
        pieces.append(" ".join(current))
    return pieces


def _hard_split(text: str, max_tokens: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and estimate_tokens(candidate) > max_tokens:
            pieces.append(current.strip())
            current = character
        else:
            current = candidate
    if current.strip():
        pieces.append(current.strip())
    return pieces


def _make_chunk(heading_path: str, parts: list[str]) -> Chunk:
    text = "\n\n".join(parts).strip()
    return Chunk(seq=0, heading_path=heading_path, text=text, token_count=estimate_tokens(text))
