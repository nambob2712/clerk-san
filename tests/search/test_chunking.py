from __future__ import annotations

import pytest

from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.search.chunking import ChunkingError, chunk_document, estimate_tokens


def _document(body: str) -> NormalizedDocument:
    return NormalizedDocument(
        markdown_body=body,
        metadata=DocMetadata(filename="notes.md", detected_type=FileType.MD, sha256="a" * 64),
    )


def test_heading_aware_chunks_keep_breadcrumbs_and_tables_intact() -> None:
    document = _document(
        "# Expenses\n\nIntro text.\n\n## June\n\n"
        "| Merchant | Amount |\n| --- | ---: |\n| Needle Shop | 1200 |\n\n"
        "### Notes\n\nClient gift receipt."
    )

    chunks = chunk_document(document, max_tokens=60)

    assert [chunk.heading_path for chunk in chunks] == [
        "Expenses",
        "Expenses > June",
        "Expenses > June > Notes",
    ]
    assert "| Needle Shop | 1200 |" in chunks[1].text
    assert all(chunk.token_count <= 60 for chunk in chunks)
    assert [chunk.seq for chunk in chunks] == [0, 1, 2]


def test_token_estimate_is_cjk_aware_and_long_paragraph_is_soft_split() -> None:
    assert estimate_tokens("領収書") >= 3
    chunks = chunk_document(_document("# H\n\n" + ("長い文章です。" * 20)), max_tokens=12)
    assert len(chunks) > 1
    assert all(chunk.token_count <= 12 for chunk in chunks)


def test_indivisible_table_over_budget_fails_loudly() -> None:
    table = "| A | B |\n| --- | --- |\n| " + ("領" * 40) + " | 1 |"
    with pytest.raises(ChunkingError, match="table"):
        chunk_document(_document("# H\n\n" + table), max_tokens=10)
