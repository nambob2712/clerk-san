"""Bounded, literal CSV/TSV normalization.

Delimited files are treated as data.  In particular, values beginning with a
spreadsheet formula prefix are never evaluated or rewritten.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
from dataclasses import dataclass

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.normalized import DocMetadata, ExtractedTable, NormalizedDocument
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource, SandboxProtocolError


@dataclass(frozen=True, slots=True)
class DelimitedRow:
    """One row with a stable one-based source locator."""

    ordinal: int
    cells: tuple[str, ...]

    @property
    def source_location(self) -> str:
        return f"row:{self.ordinal}"


@dataclass(frozen=True, slots=True)
class DelimitedEvidence:
    """Deterministic evidence recorded alongside a normalized table."""

    delimiter: str
    quotechar: str
    encoding: str
    row_count: int
    cell_count: int
    header: tuple[str, ...]
    duplicate_headers: tuple[str, ...] = ()
    ragged_rows: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "delimiter": self.delimiter,
            "quotechar": self.quotechar,
            "encoding": self.encoding,
            "row_count": self.row_count,
            "cell_count": self.cell_count,
            "header": list(self.header),
            "duplicate_headers": list(self.duplicate_headers),
            "ragged_rows": self.ragged_rows,
        }


class DelimitedAdapter:
    """Parse a bounded CSV or TSV stream without semantic coercion."""

    # CSV/TSV enum members are added by the universal file-type contract.  Keeping
    # this optional lets the legacy seven-type runtime import safely while the
    # capability remains dark.
    supported_types: tuple[FileType, ...] = tuple(
        file_type
        for file_type in (getattr(FileType, "CSV", None), getattr(FileType, "TSV", None))
        if isinstance(file_type, FileType)
    )

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        """Normalize a digest-bound descriptor; no host path or legacy raw API is used."""

        detected_type = _detected_type(context)
        if detected_type.value not in {"csv", "tsv"}:
            raise ValueError(f"delimited adapter cannot handle {detected_type.value!r}")
        source.verify_digest()
        raw = _read_source(source, self.limits)
        text, encoding = decode_delimited(raw, limits=self.limits)
        delimiter = "\t" if detected_type.value == "tsv" else detect_delimiter(text)
        rows = list(iter_delimited_rows(text, delimiter=delimiter, limits=self.limits))
        if not rows:
            header: list[str] = []
            data_rows: list[list[str]] = []
        else:
            header = _unique_headers(list(rows[0].cells))
            data_rows = [list(row.cells) for row in rows[1:]]
        duplicate_headers = tuple(
            name
            for name, count in _header_counts(list(rows[0].cells) if rows else []).items()
            if count > 1
        )
        width = len(header)
        ragged = sum(1 for row in data_rows if len(row) != width)
        evidence = DelimitedEvidence(
            delimiter=delimiter,
            quotechar='"',
            encoding=encoding,
            row_count=len(data_rows),
            cell_count=sum(len(row) for row in data_rows),
            header=tuple(header),
            duplicate_headers=duplicate_headers,
            ragged_rows=ragged,
        )
        extra = {
            "document_format": detected_type.value,
            "delimited_evidence": evidence.as_dict(),
            "row_provenance": [row.source_location for row in rows[1:]],
            "spreadsheet_row_embedding": "disabled",
        }
        filename = source.filename
        body = f"Delimited {filename}; {len(data_rows)} data rows; {width} columns."
        return NormalizedDocument(
            markdown_body=body,
            metadata=DocMetadata(
                filename=filename,
                detected_type=detected_type,
                sha256=source.source_sha256,
                extra=extra,
            ),
            tables=[
                ExtractedTable(
                    header=header,
                    rows=data_rows,
                    source_location="delimited:table:1",
                )
            ]
            if rows
            else [],
            embeddable=False,
        )

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        """Compatibility wrapper for callers not yet migrated to the FD seam."""

        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            source = ReadOnlySource(
                handle.fileno(),
                digest,
                filename=meta.filename,
                mime_type=None,
            )
            context = AdapterContext(
                adapter_key="legacy.delimited",
                metadata={"detected_type": meta.detected_type.value},
            )
            document = self.normalize(source, context)
            if meta.extra:
                document = document.model_copy(
                    update={
                        "metadata": document.metadata.model_copy(
                            update={"extra": {**meta.extra, **document.metadata.extra}}
                        )
                    }
                )
            return document


def _detected_type(context: AdapterContext) -> FileType:
    value = context.metadata.get("detected_type")
    try:
        return FileType(value or "")
    except ValueError as error:
        raise ValueError("delimited context requires detected_type csv or tsv") from error


def _read_source(source: ReadOnlySource, limits: IngestLimits) -> bytes:
    """Read only the already-open descriptor, bounded by the upload limit."""

    stat = os.fstat(source.fd)
    if not stat.st_mode or stat.st_size > limits.max_upload_bytes:
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
        raise SandboxProtocolError("source changed while reading delimited input")
    return b"".join(chunks)


def decode_delimited(raw: bytes, *, limits: IngestLimits | None = None) -> tuple[str, str]:
    """Decode BOM-marked Unicode, UTF-8, then deterministic CP932 fallback."""

    active = limits or IngestLimits()
    if len(raw) > active.max_upload_bytes:
        raise ResourceLimitExceeded("max_upload_bytes", active.max_upload_bytes, len(raw))
    if b"\x00" in raw and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("delimited input contains binary NUL bytes")
    candidates = (
        (("utf-8-sig", "utf-8"), ("utf-16", "utf-16"))
        if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))
        else (("utf-8", "utf-8"), ("cp932", "cp932"))
    )
    for codec, evidence in candidates:
        try:
            return raw.decode(codec), evidence
        except UnicodeDecodeError:
            continue
    raise ValueError("delimited input is not valid UTF-8, UTF-16, or CP932")


def detect_delimiter(text: str) -> str:
    """Choose a delimiter deterministically from the first bounded sample."""

    sample = text[: 64 * 1024]
    lines = [line for line in sample.splitlines() if line.strip()][:20]
    scores = {
        delimiter: sum(line.count(delimiter) for line in lines)
        for delimiter in (",", ";", "\t", "|")
    }
    best = max(scores, key=lambda delimiter: (scores[delimiter], delimiter == ","))
    if scores[best] == 0:
        raise ValueError("could not determine CSV delimiter")
    return best


def iter_delimited_rows(
    text: str, *, delimiter: str, limits: IngestLimits | None = None
) -> list[DelimitedRow]:
    """Read rows with strict CSV quoting and aggregate row/cell limits."""

    active = limits or IngestLimits()
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"', strict=True
    )
    rows: list[DelimitedRow] = []
    cells = 0
    try:
        for ordinal, values in enumerate(reader, start=1):
            if ordinal > active.max_tabular_rows:
                raise ResourceLimitExceeded("max_tabular_rows", active.max_tabular_rows, ordinal)
            cells += len(values)
            if cells > active.max_tabular_cells:
                raise ResourceLimitExceeded("max_tabular_cells", active.max_tabular_cells, cells)
            rows.append(DelimitedRow(ordinal, tuple(values)))
    except csv.Error as error:
        raise ValueError("invalid delimited quoting") from error
    return rows


def _header_counts(headers: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for header in headers:
        key = header.strip() or "column"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _unique_headers(headers: list[str]) -> list[str]:
    used: dict[str, int] = {}
    result: list[str] = []
    for index, header in enumerate(headers, start=1):
        base = header.strip() or f"column_{index}"
        used[base] = used.get(base, 0) + 1
        result.append(base if used[base] == 1 else f"{base}__{used[base]}")
    return result
