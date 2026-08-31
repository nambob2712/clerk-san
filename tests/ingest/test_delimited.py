from __future__ import annotations

import hashlib
import os

import pytest

from clerksan.ingest.adapters.delimited import DelimitedAdapter, decode_delimited, detect_delimiter
from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.normalized import DocMetadata
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource, SandboxProtocolError

CSV_AVAILABLE = hasattr(FileType, "CSV")


def _meta(file_type: FileType = FileType.CSV) -> DocMetadata:
    return DocMetadata(
        filename=f"transactions.{file_type.value}", detected_type=file_type, sha256="a" * 64
    )


@pytest.mark.skipif(not CSV_AVAILABLE, reason="universal CSV file type is not enabled yet")
@pytest.mark.asyncio
async def test_csv_preserves_formulas_quoted_newlines_and_duplicate_headers() -> None:
    raw = b'Date,Amount,Amount,Note\n2026-08-22,=SUM(A1:A2),+100,"line 1\nline 2"\n'
    document = await DelimitedAdapter().adapt(raw, _meta())
    table = document.tables[0]
    assert table.header == ["Date", "Amount", "Amount__2", "Note"]
    assert table.rows[0][1] == "=SUM(A1:A2)"
    assert table.rows[0][2] == "+100"
    assert table.rows[0][3] == "line 1\nline 2"
    assert document.metadata.extra["row_provenance"] == ["row:2"]


@pytest.mark.skipif(not CSV_AVAILABLE, reason="universal TSV file type is not enabled yet")
@pytest.mark.asyncio
async def test_tsv_cp932_and_ragged_rows_have_stable_evidence() -> None:
    raw = "日付\t金額\t摘要\n2026-08-22\t1200\t\n".encode("cp932")
    document = await DelimitedAdapter().adapt(raw, _meta(FileType.TSV))
    evidence = document.metadata.extra["delimited_evidence"]
    assert evidence["encoding"] == "cp932"
    assert evidence["delimiter"] == "\t"
    assert evidence["ragged_rows"] == 0
    assert document.tables[0].rows == [["2026-08-22", "1200", ""]]


def test_decoder_honors_utf16_bom_and_bounds() -> None:
    text, encoding = decode_delimited("a,b\n1,2\n".encode("utf-16"))
    assert text.startswith("a,b")
    assert encoding == "utf-16"
    with pytest.raises(ResourceLimitExceeded, match="max_upload_bytes"):
        decode_delimited(b"a,b\n", limits=IngestLimits(max_upload_bytes=2))


def test_delimiter_detection_is_deterministic() -> None:
    assert detect_delimiter("a;b\n1;2\n") == ";"
    assert detect_delimiter("a,b\n1,2\n") == ","


@pytest.mark.skipif(not CSV_AVAILABLE, reason="universal CSV file type is not enabled yet")
def test_normalize_reads_digest_bound_descriptor_and_preserves_literal_cells(tmp_path) -> None:
    raw = b"Date,Amount\n2026-08-22,=SUM(A1:A2)\n"
    path = tmp_path / "transactions.csv"
    path.write_bytes(raw)
    fd = os.open(path, os.O_RDONLY)
    try:
        source = ReadOnlySource(fd, hashlib.sha256(raw).hexdigest(), filename="transactions.csv")
        document = DelimitedAdapter().normalize(
            source,
            AdapterContext("delimited.csv", metadata={"detected_type": "csv"}),
        )
        assert document.metadata.sha256 == source.source_sha256
        assert document.tables[0].rows == [["2026-08-22", "=SUM(A1:A2)"]]
    finally:
        os.close(fd)


@pytest.mark.skipif(not CSV_AVAILABLE, reason="universal CSV file type is not enabled yet")
def test_normalize_rejects_changed_descriptor(tmp_path) -> None:
    path = tmp_path / "transactions.csv"
    path.write_bytes(b"a,b\n1,2\n")
    fd = os.open(path, os.O_RDONLY)
    try:
        source = ReadOnlySource(fd, "0" * 64, filename="transactions.csv")
        with pytest.raises(SandboxProtocolError, match="digest"):
            DelimitedAdapter().normalize(
                source,
                AdapterContext("delimited.csv", metadata={"detected_type": "csv"}),
            )
    finally:
        os.close(fd)
