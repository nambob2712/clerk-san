from __future__ import annotations

import hashlib
import tempfile

import pytest

from clerksan.ingest.adapters.markdown import MarkdownAdapter
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource


@pytest.mark.asyncio
async def test_markdown_preserves_body_and_records_front_matter_heading_paths() -> None:
    raw = (
        b'---\ntitle: Local notes\ntags: ["receipts", "japan"]\n---\n'
        b"# Expenses\n\n## June\n| Merchant | Amount |\n| --- | ---: |\n| Shop | 100 |\n"
    )
    document = await MarkdownAdapter().adapt(
        raw,
        DocMetadata(filename="notes.md", detected_type=FileType.MD, sha256="a" * 64),
    )

    assert document.markdown_body == (
        "# Expenses\n\n## June\n| Merchant | Amount |\n| --- | ---: |\n| Shop | 100 |\n"
    )
    assert document.metadata.extra["front_matter"] == {
        "title": "Local notes",
        "tags": ["receipts", "japan"],
    }
    assert document.metadata.extra["outline"][-1]["path"] == ["Expenses", "June"]


@pytest.mark.asyncio
async def test_markdown_rejects_wrong_type() -> None:
    with pytest.raises(ValueError, match="cannot handle"):
        await MarkdownAdapter().adapt(
            b"# heading",
            DocMetadata(filename="bad.pdf", detected_type=FileType.PDF, sha256="a" * 64),
        )


def test_markdown_normalize_uses_digest_bound_readonly_source() -> None:
    raw = b"# Notes\n\nLiteral content\n"
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        document = MarkdownAdapter().normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename="notes.md",
                mime_type="text/markdown",
            ),
            AdapterContext(
                adapter_key="text.markdown",
                metadata={"detected_type": "md"},
            ),
        )

    assert document.markdown_body == "# Notes\n\nLiteral content\n"
    assert document.metadata.family == "text"
    assert document.metadata.charset == "utf-8"
