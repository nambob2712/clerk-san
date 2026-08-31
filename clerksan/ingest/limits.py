"""Resource bounds shared by every document ingestion path."""

from __future__ import annotations

import re
import stat
import struct
import zipfile
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clerksan.config import Settings


Number = int | float


class ResourceLimitExceeded(ValueError):
    """An input exceeded a configured resource bound.

    API routes can safely map this exception to a 413 response while exposing the
    stable ``limit_name`` to clients and tests.
    """

    def __init__(self, limit_name: str, limit: Number, observed: Number) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.observed = observed
        super().__init__(f"{limit_name}: observed {observed} > limit {limit}")


class UnsafeArchiveMemberError(ValueError):
    """A ZIP member is unsafe to inspect or extract."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"unsafe archive member {name!r}: {reason}")


@dataclass(frozen=True)
class IngestLimits:
    """Bounds sized for the documented 16 GB local deployment."""

    max_upload_bytes: int = 25 * 1024 * 1024
    max_pdf_pages: int = 100
    max_image_pixels: int = 40_000_000
    max_image_width: int = 12_000
    max_image_height: int = 12_000
    max_archive_members: int = 2_000
    max_archive_uncompressed_bytes: int = 200 * 1024 * 1024
    max_archive_expansion_ratio: float = 100.0
    max_image_frames: int = 100
    max_text_characters: int = 2_000_000
    max_tabular_rows: int = 100_000
    max_tabular_cells: int = 1_000_000
    max_structured_nodes: int = 500_000
    max_recursion_depth: int = 4
    max_normalized_output_bytes: int = 50 * 1024 * 1024

    @classmethod
    def from_settings(cls, settings: Settings) -> IngestLimits:
        """Build bounds from the one typed runtime configuration object."""

        defaults = cls()
        return cls(
            max_upload_bytes=settings.max_upload_bytes,
            max_pdf_pages=settings.max_pdf_pages,
            max_image_pixels=settings.max_image_pixels,
            max_image_width=settings.max_image_width,
            max_image_height=settings.max_image_height,
            max_archive_members=settings.max_archive_members,
            max_archive_uncompressed_bytes=settings.max_archive_uncompressed_bytes,
            max_archive_expansion_ratio=settings.max_archive_expansion_ratio,
            max_image_frames=getattr(settings, "max_image_frames", defaults.max_image_frames),
            max_text_characters=getattr(
                settings, "max_text_characters", defaults.max_text_characters
            ),
            max_tabular_rows=getattr(settings, "max_tabular_rows", defaults.max_tabular_rows),
            max_tabular_cells=getattr(settings, "max_tabular_cells", defaults.max_tabular_cells),
            max_structured_nodes=getattr(
                settings, "max_structured_nodes", defaults.max_structured_nodes
            ),
            max_recursion_depth=getattr(
                settings, "max_recursion_depth", defaults.max_recursion_depth
            ),
            max_normalized_output_bytes=getattr(
                settings,
                "max_normalized_output_bytes",
                defaults.max_normalized_output_bytes,
            ),
        )


@dataclass
class ParseBudget:
    """Mutable aggregate counters shared by every member of one source parse.

    Phase 1 only lands these cooperative primitives.  Recursive universal inspection
    remains disabled until the sandboxed Phase 2 runner owns their execution boundary.
    """

    limits: IngestLimits = field(default_factory=IngestLimits)
    bytes_consumed: int = 0
    pixels_consumed: int = 0
    frames_consumed: int = 0
    pages_consumed: int = 0
    characters_consumed: int = 0
    nodes_consumed: int = 0
    rows_consumed: int = 0
    cells_consumed: int = 0
    parts_consumed: int = 0
    normalized_output_bytes_consumed: int = 0
    max_nesting: int = 0
    _current_nesting: int = field(default=0, init=False, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> ParseBudget:
        return cls(IngestLimits.from_settings(settings))

    def consume_bytes(self, amount: int) -> None:
        self._consume("bytes_consumed", amount, "max_archive_uncompressed_bytes")

    def consume_pixels(self, amount: int) -> None:
        self._consume("pixels_consumed", amount, "max_image_pixels")

    def consume_frames(self, amount: int) -> None:
        self._consume("frames_consumed", amount, "max_image_frames")

    def consume_pages(self, amount: int) -> None:
        self._consume("pages_consumed", amount, "max_pdf_pages")

    def consume_characters(self, amount: int) -> None:
        self._consume("characters_consumed", amount, "max_text_characters")

    def consume_text_characters(self, amount: int) -> None:
        self.consume_characters(amount)

    def consume_nodes(self, amount: int) -> None:
        self._consume("nodes_consumed", amount, "max_structured_nodes")

    def consume_structured_nodes(self, amount: int) -> None:
        self.consume_nodes(amount)

    def consume_rows(self, amount: int) -> None:
        self._consume("rows_consumed", amount, "max_tabular_rows")

    def consume_tabular_rows(self, amount: int) -> None:
        self.consume_rows(amount)

    def consume_cells(self, amount: int) -> None:
        self._consume("cells_consumed", amount, "max_tabular_cells")

    def consume_tabular_cells(self, amount: int) -> None:
        self.consume_cells(amount)

    def consume_parts(self, amount: int) -> None:
        self._consume("parts_consumed", amount, "max_archive_members")

    def consume_archive_members(self, amount: int) -> None:
        self.consume_parts(amount)

    def consume_nesting(self, depth: int) -> None:
        _require_nonnegative("nesting depth", depth)
        if depth > self.limits.max_recursion_depth:
            raise ResourceLimitExceeded(
                "max_recursion_depth", self.limits.max_recursion_depth, depth
            )
        self.max_nesting = max(self.max_nesting, depth)

    def consume_recursion_depth(self, depth: int) -> None:
        self.consume_nesting(depth)

    def consume_normalized_output(self, amount: int) -> None:
        self._consume("normalized_output_bytes_consumed", amount, "max_normalized_output_bytes")

    def consume_normalized_bytes(self, amount: int) -> None:
        """Compatibility alias for adapters that name the produced resource directly."""

        self.consume_normalized_output(amount)

    def consume_normalized_output_bytes(self, amount: int) -> None:
        self.consume_normalized_output(amount)

    @contextmanager
    def nested(self):
        """Track a recursive member with balanced depth even when parsing raises."""

        next_depth = self._current_nesting + 1
        self.consume_nesting(next_depth)
        self._current_nesting = next_depth
        try:
            yield self
        finally:
            self._current_nesting -= 1

    def _consume(self, counter_name: str, amount: int, limit_name: str) -> None:
        _require_nonnegative(counter_name.replace("_", " "), amount)
        observed = getattr(self, counter_name) + amount
        limit = getattr(self.limits, limit_name)
        if observed > limit:
            raise ResourceLimitExceeded(limit_name, limit, observed)
        setattr(self, counter_name, observed)


def check_upload_size(size: int, limits: IngestLimits) -> None:
    """Reject an oversized upload before parsing or decoding it."""

    _require_nonnegative("upload size", size)
    if size > limits.max_upload_bytes:
        raise ResourceLimitExceeded("max_upload_bytes", limits.max_upload_bytes, size)


def check_pdf_pages(page_count: int, limits: IngestLimits) -> None:
    """Reject PDFs before page rasterization can begin."""

    _require_nonnegative("PDF page count", page_count)
    if page_count > limits.max_pdf_pages:
        raise ResourceLimitExceeded("max_pdf_pages", limits.max_pdf_pages, page_count)


def check_image_pixels(width: int, height: int, limits: IngestLimits) -> None:
    """Reject implausibly large dimensions before image decode allocation."""

    if width < 1 or height < 1:
        raise ValueError("image dimensions must be greater than zero")
    if width > limits.max_image_width:
        raise ResourceLimitExceeded("max_image_width", limits.max_image_width, width)
    if height > limits.max_image_height:
        raise ResourceLimitExceeded("max_image_height", limits.max_image_height, height)

    pixels = width * height
    if pixels > limits.max_image_pixels:
        raise ResourceLimitExceeded("max_image_pixels", limits.max_image_pixels, pixels)


def safe_zip_members(
    members: zipfile.ZipFile | Sequence[zipfile.ZipInfo] | Sequence[str],
    sizes: IngestLimits | Sequence[tuple[int, int]] | None = None,
    limits: IngestLimits | None = None,
) -> list[str]:
    """Validate ZIP paths and sizes before opening any member.

    Pass a ``ZipFile`` or ``ZipFile.infolist()`` to preserve symlink metadata.  The legacy
    ``names, [(compressed_size, uncompressed_size)], limits`` form remains usable for
    callers that only have metadata snapshots; it cannot detect symlinks because the
    attributes are unavailable in that representation.
    """

    if isinstance(sizes, IngestLimits):
        if limits is not None:
            raise TypeError("limits was supplied twice")
        limits = sizes
        sizes = None
    if limits is None:
        raise TypeError("limits is required")
    if isinstance(members, zipfile.ZipFile):
        members = members.infolist()
    if len(members) > limits.max_archive_members:
        raise ResourceLimitExceeded("max_archive_members", limits.max_archive_members, len(members))

    validator = _ArchiveSafetyValidator(limits)
    for member in _coerce_members(members, sizes):
        validator.add(member)
    return validator.finish()


def safe_zip_central_directory(data: bytes, limits: IngestLimits) -> set[str]:
    """Validate ZIP metadata without constructing an unbounded ``ZipInfo`` list.

    ``zipfile.ZipFile`` eagerly expands the whole central directory into Python objects.
    The upload boundary calls this helper first, so an archive with too many members is
    rejected from its end record before any OOXML parser or ``ZipFile`` instance runs.
    """

    try:
        end_record = zipfile._EndRecData(BytesIO(data))
    except (OSError, zipfile.BadZipFile) as error:
        raise UnsafeArchiveMemberError("<archive>", "invalid ZIP central directory") from error
    if end_record is None:
        raise UnsafeArchiveMemberError("<archive>", "invalid ZIP central directory")

    disk_number = end_record[zipfile._ECD_DISK_NUMBER]
    directory_disk = end_record[zipfile._ECD_DISK_START]
    entry_count = int(end_record[zipfile._ECD_ENTRIES_TOTAL])
    entries_on_disk = int(end_record[zipfile._ECD_ENTRIES_THIS_DISK])
    if disk_number != 0 or directory_disk != 0 or entries_on_disk != entry_count:
        raise UnsafeArchiveMemberError("<archive>", "multi-disk archives are not supported")
    if entry_count > limits.max_archive_members:
        raise ResourceLimitExceeded("max_archive_members", limits.max_archive_members, entry_count)

    directory_size = int(end_record[zipfile._ECD_SIZE])
    directory_end = int(end_record[zipfile._ECD_LOCATION])
    directory_start = directory_end - directory_size
    if directory_start < 0 or directory_end > len(data):
        raise UnsafeArchiveMemberError("<archive>", "invalid central directory bounds")

    validator = _ArchiveSafetyValidator(limits)
    names: set[str] = set()
    cursor = directory_start
    for _ in range(entry_count):
        if cursor + zipfile.sizeCentralDir > directory_end:
            raise UnsafeArchiveMemberError("<archive>", "truncated central directory")
        entry = struct.unpack_from(zipfile.structCentralDir, data, cursor)
        if entry[zipfile._CD_SIGNATURE] != zipfile.stringCentralDir:
            raise UnsafeArchiveMemberError("<archive>", "invalid central directory entry")

        name_size = entry[zipfile._CD_FILENAME_LENGTH]
        extra_size = entry[zipfile._CD_EXTRA_FIELD_LENGTH]
        comment_size = entry[zipfile._CD_COMMENT_LENGTH]
        name_start = cursor + zipfile.sizeCentralDir
        name_end = name_start + name_size
        extra_end = name_end + extra_size
        entry_end = extra_end + comment_size
        if entry_end > directory_end:
            raise UnsafeArchiveMemberError("<archive>", "truncated central directory entry")

        name = _decode_zip_member_name(data[name_start:name_end], entry[zipfile._CD_FLAG_BITS])
        compressed_size, uncompressed_size = _zip_member_sizes(
            name,
            entry[zipfile._CD_COMPRESSED_SIZE],
            entry[zipfile._CD_UNCOMPRESSED_SIZE],
            data[name_end:extra_end],
        )
        validator.add(
            _ArchiveMember(
                name=name,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                is_symlink=stat.S_ISLNK(entry[zipfile._CD_EXTERNAL_FILE_ATTRIBUTES] >> 16),
            )
        )
        names.add(name)
        cursor = entry_end

    if cursor != directory_end:
        raise UnsafeArchiveMemberError("<archive>", "invalid central directory length")
    validator.finish()
    return names


@dataclass(frozen=True)
class _ArchiveMember:
    name: str
    compressed_size: int
    uncompressed_size: int
    is_symlink: bool = False


class _ArchiveSafetyValidator:
    """Apply member and aggregate limits while metadata is streamed."""

    def __init__(self, limits: IngestLimits) -> None:
        self.limits = limits
        self.member_count = 0
        self.total_compressed = 0
        self.total_uncompressed = 0
        self.names: list[str] = []

    def add(self, member: _ArchiveMember) -> None:
        self.member_count += 1
        if self.member_count > self.limits.max_archive_members:
            raise ResourceLimitExceeded(
                "max_archive_members", self.limits.max_archive_members, self.member_count
            )
        _validate_member_path(member.name)
        if member.is_symlink:
            raise UnsafeArchiveMemberError(member.name, "symbolic links are not allowed")
        if member.compressed_size < 0 or member.uncompressed_size < 0:
            raise UnsafeArchiveMemberError(member.name, "negative member size")

        _check_member_expansion(member, self.limits)
        self.total_compressed += member.compressed_size
        self.total_uncompressed += member.uncompressed_size
        if self.total_uncompressed > self.limits.max_archive_uncompressed_bytes:
            raise ResourceLimitExceeded(
                "max_archive_uncompressed_bytes",
                self.limits.max_archive_uncompressed_bytes,
                self.total_uncompressed,
            )
        self.names.append(member.name)

    def finish(self) -> list[str]:
        if self.total_uncompressed > 0 and self.total_compressed == 0:
            raise ResourceLimitExceeded(
                "max_archive_expansion_ratio", self.limits.max_archive_expansion_ratio, float("inf")
            )
        if self.total_compressed > 0:
            total_ratio = self.total_uncompressed / self.total_compressed
            if total_ratio > self.limits.max_archive_expansion_ratio:
                raise ResourceLimitExceeded(
                    "max_archive_expansion_ratio",
                    self.limits.max_archive_expansion_ratio,
                    total_ratio,
                )
        return self.names


def _decode_zip_member_name(raw_name: bytes, flags: int) -> str:
    encoding = "utf-8" if flags & zipfile._MASK_UTF_FILENAME else "cp437"
    try:
        return raw_name.decode(encoding)
    except UnicodeDecodeError as error:
        raise UnsafeArchiveMemberError("<archive>", "invalid member name encoding") from error


def _zip_member_sizes(
    name: str,
    compressed_size: int,
    uncompressed_size: int,
    extra: bytes,
) -> tuple[int, int]:
    """Resolve ZIP64 sizes when the central-directory fields use sentinels."""

    zip64_sentinel = 0xFFFFFFFF
    if compressed_size != zip64_sentinel and uncompressed_size != zip64_sentinel:
        return compressed_size, uncompressed_size

    payload = _zip64_extra_payload(name, extra)
    cursor = 0
    if uncompressed_size == zip64_sentinel:
        uncompressed_size, cursor = _read_zip64_size(name, payload, cursor)
    if compressed_size == zip64_sentinel:
        compressed_size, cursor = _read_zip64_size(name, payload, cursor)
    return compressed_size, uncompressed_size


def _zip64_extra_payload(name: str, extra: bytes) -> bytes:
    cursor = 0
    while cursor < len(extra):
        if cursor + 4 > len(extra):
            raise UnsafeArchiveMemberError(name, "invalid ZIP64 metadata")
        header_id, size = struct.unpack_from("<HH", extra, cursor)
        payload_start = cursor + 4
        payload_end = payload_start + size
        if payload_end > len(extra):
            raise UnsafeArchiveMemberError(name, "invalid ZIP64 metadata")
        if header_id == 0x0001:
            return extra[payload_start:payload_end]
        cursor = payload_end
    raise UnsafeArchiveMemberError(name, "missing ZIP64 metadata")


def _read_zip64_size(name: str, payload: bytes, cursor: int) -> tuple[int, int]:
    if cursor + 8 > len(payload):
        raise UnsafeArchiveMemberError(name, "invalid ZIP64 metadata")
    return struct.unpack_from("<Q", payload, cursor)[0], cursor + 8


def _coerce_members(
    members: Sequence[zipfile.ZipInfo] | Sequence[str],
    sizes: Sequence[tuple[int, int]] | None,
) -> list[_ArchiveMember]:
    if not members:
        if sizes:
            raise ValueError("archive member names and sizes must have the same length")
        return []

    first = members[0]
    if isinstance(first, zipfile.ZipInfo):
        if sizes is not None:
            raise ValueError("sizes must not be supplied with ZipInfo members")
        if not all(isinstance(info, zipfile.ZipInfo) for info in members):
            raise TypeError("members must all be ZipInfo objects")
        zip_members = members  # type: ignore[assignment]
        return [
            _ArchiveMember(
                name=info.filename,
                compressed_size=info.compress_size,
                uncompressed_size=info.file_size,
                is_symlink=stat.S_ISLNK(info.external_attr >> 16),
            )
            for info in zip_members
        ]

    if sizes is None or len(members) != len(sizes):
        raise ValueError("archive member names and sizes must have the same length")
    if not all(isinstance(name, str) for name in members):
        raise TypeError("members must be ZipInfo objects or path strings")
    return [
        _ArchiveMember(name, compressed_size, uncompressed_size)
        for name, (compressed_size, uncompressed_size) in zip(members, sizes, strict=True)
    ]


def _validate_member_path(name: str) -> None:
    if not name:
        raise UnsafeArchiveMemberError(name, "empty path")
    if "\x00" in name:
        raise UnsafeArchiveMemberError(name, "NUL byte")
    if "\\" in name:
        raise UnsafeArchiveMemberError(name, "backslash path separators are not allowed")
    if name.startswith(("/", "//")) or PureWindowsPath(name).is_absolute():
        raise UnsafeArchiveMemberError(name, "absolute path")
    if re.match(r"^[A-Za-z]:", name):
        raise UnsafeArchiveMemberError(name, "drive-qualified path")

    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeArchiveMemberError(name, "traversal or ambiguous path component")


def _check_member_expansion(member: _ArchiveMember, limits: IngestLimits) -> None:
    if member.uncompressed_size > 0 and member.compressed_size == 0:
        raise ResourceLimitExceeded(
            "max_archive_expansion_ratio", limits.max_archive_expansion_ratio, float("inf")
        )
    if member.compressed_size == 0:
        return
    ratio = member.uncompressed_size / member.compressed_size
    if ratio > limits.max_archive_expansion_ratio:
        raise ResourceLimitExceeded(
            "max_archive_expansion_ratio", limits.max_archive_expansion_ratio, ratio
        )


def _require_nonnegative(label: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{label} must not be negative")
