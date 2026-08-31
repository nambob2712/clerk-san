"""Bounded, filesystem-free inspection for supported archive containers."""

from __future__ import annotations

import hashlib
import html
import io
import stat
import tarfile
import tempfile
import unicodedata
import warnings
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath

from PIL import Image, UnidentifiedImageError

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import (
    IngestLimits,
    ParseBudget,
    ResourceLimitExceeded,
    UnsafeArchiveMemberError,
    check_image_pixels,
    safe_zip_central_directory,
    safe_zip_members,
)
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument, canonical_locator
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

from .source_io import read_bounded_source
from .text import decode_text

_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_CONTAINER_TYPES = frozenset({FileType.ZIP, FileType.TAR, FileType.TGZ, FileType.GZ})
_TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".htm",
        ".html",
        ".json",
        ".jsonl",
        ".log",
        ".md",
        ".rels",
        ".rst",
        ".rtf",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_XML_SUFFIXES = frozenset({".rels", ".xml"})
_ACTIVE_SUFFIXES = frozenset(
    {
        ".apk",
        ".app",
        ".bat",
        ".cgi",
        ".chm",
        ".class",
        ".cmd",
        ".com",
        ".desktop",
        ".dll",
        ".docm",
        ".dotm",
        ".exe",
        ".hta",
        ".jar",
        ".js",
        ".lnk",
        ".msi",
        ".php",
        ".pl",
        ".potm",
        ".ppam",
        ".pptm",
        ".ps1",
        ".py",
        ".rb",
        ".reg",
        ".scr",
        ".sh",
        ".sldm",
        ".so",
        ".vbs",
        ".wasm",
        ".xlam",
        ".xll",
        ".xlsm",
    }
)
_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
    b"RIFF",
)
_ACTIVE_MAGIC = (
    b"MZ",
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
)
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_ACTIVE_RTF = (
    b"\\bin",
    b"\\field",
    b"\\fldinst",
    b"\\include",
    b"\\objdata",
    b"\\object",
)


@dataclass(frozen=True, slots=True)
class InspectedMember:
    """One fully inspected member, represented only by inert provenance."""

    locator: str
    name: str
    kind: str
    size: int
    sha256: str
    depth: int

    def as_json(self) -> dict[str, int | str]:
        return {
            "locator": self.locator,
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "depth": self.depth,
        }


@dataclass(slots=True)
class ArchiveInspection:
    members: list[InspectedMember] = field(default_factory=list)
    escaped_text_parts: list[str] = field(default_factory=list)


class ArchiveAdapter:
    """Inspect archives in memory without extracting, executing, or fetching."""

    supported_types: tuple[FileType, ...] = tuple(sorted(_CONTAINER_TYPES, key=str))

    def __init__(self, *, limits: IngestLimits | None = None) -> None:
        self.limits = limits or IngestLimits()

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        detected_type = _context_file_type(context)
        raw = read_bounded_source(source, self.limits)
        budget = ParseBudget(self.limits)
        inspection = ArchiveInspection()
        _inspect_container(
            raw,
            detected_type,
            source.filename,
            self.limits,
            budget,
            inspection,
            depth=1,
            locator_prefix=canonical_locator("archive", "root"),
        )
        manifest_lines = [
            f"- {html.escape(member.name, quote=False)} ({member.kind}, {member.size} bytes)"
            for member in inspection.members
        ]
        body_parts = ["Archive members:", *manifest_lines]
        if inspection.escaped_text_parts:
            body_parts.extend(("", "Inert text:", *inspection.escaped_text_parts))
        body = "\n".join(body_parts)
        output_size = len(body.encode("utf-8"))
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
                detected_type=detected_type,
                sha256=source.source_sha256,
                family="container",
                canonical_mime=_context_text(context, "canonical_mime"),
                extra={
                    "document_format": detected_type.value,
                    "member_count": len(inspection.members),
                    "members": [member.as_json() for member in inspection.members],
                    "aggregate_uncompressed_bytes": budget.bytes_consumed,
                    "max_nesting": budget.max_nesting,
                    "filesystem_extraction": "disabled",
                    "active_content": "rejected",
                },
            ),
            embeddable=bool(inspection.escaped_text_parts),
        )

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryFile() as handle:
            handle.write(raw)
            handle.flush()
            return self.normalize(
                ReadOnlySource(handle.fileno(), digest, filename=meta.filename),
                AdapterContext(
                    adapter_key="legacy.archive",
                    metadata={
                        "detected_type": meta.detected_type.value,
                        "canonical_mime": meta.canonical_mime or "",
                    },
                ),
            )


def inspect_attachment_bytes(
    name: str,
    data: bytes,
    *,
    limits: IngestLimits,
    budget: ParseBudget,
    depth: int,
    locator_prefix: str,
) -> ArchiveInspection:
    """Positively classify one recursive attachment under a shared budget."""

    inspection = ArchiveInspection()
    _inspect_member_payload(
        name,
        data,
        limits,
        budget,
        inspection,
        depth=depth,
        locator=locator_prefix,
    )
    return inspection


def _inspect_container(
    data: bytes,
    detected_type: FileType,
    source_name: str,
    limits: IngestLimits,
    budget: ParseBudget,
    inspection: ArchiveInspection,
    *,
    depth: int,
    locator_prefix: str,
) -> None:
    budget.consume_nesting(depth)
    if detected_type is FileType.ZIP:
        _inspect_zip(data, limits, budget, inspection, depth, locator_prefix)
    elif detected_type is FileType.TAR:
        _inspect_tar(data, limits, budget, inspection, depth, locator_prefix)
    elif detected_type is FileType.TGZ:
        unpacked, gzip_name = _decompress_one_gzip(data, limits, budget)
        member_name = gzip_name or _strip_gzip_suffix(source_name) or "archive.tar"
        _validate_member_name(member_name)
        _inspect_tar(unpacked, limits, budget, inspection, depth, locator_prefix)
    elif detected_type is FileType.GZ:
        unpacked, gzip_name = _decompress_one_gzip(data, limits, budget)
        member_name = gzip_name or _strip_gzip_suffix(source_name) or "member"
        _validate_member_name(member_name)
        _inspect_member_payload(
            member_name,
            unpacked,
            limits,
            budget,
            inspection,
            depth=depth + 1,
            locator=_extend_locator(locator_prefix, "gzip", member_name),
            bytes_already_consumed=True,
        )
    else:  # pragma: no cover - protected by context validation
        raise ValueError(f"unsupported archive type {detected_type.value!r}")


def _inspect_zip(
    data: bytes,
    limits: IngestLimits,
    budget: ParseBudget,
    inspection: ArchiveInspection,
    depth: int,
    locator_prefix: str,
) -> None:
    try:
        safe_zip_central_directory(data, limits)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            safe_zip_members(archive, limits)
            infos = archive.infolist()
            _validate_name_collisions((info.filename, info.is_dir()) for info in infos)
            for ordinal, info in enumerate(infos, start=1):
                if info.is_dir():
                    continue
                _validate_zip_info(info)
                budget.consume_parts(1)
                locator = _extend_locator(locator_prefix, "zip", ordinal, info.filename)
                try:
                    with archive.open(info, "r") as stream:
                        payload = _read_exact_member(stream, info.file_size, limits, budget)
                except (RuntimeError, zipfile.BadZipFile, zlib.error) as error:
                    raise UnsafeArchiveMemberError(
                        info.filename, "invalid, encrypted, or corrupt member"
                    ) from error
                _inspect_member_payload(
                    info.filename,
                    payload,
                    limits,
                    budget,
                    inspection,
                    depth=depth + 1,
                    locator=locator,
                    bytes_already_consumed=True,
                )
    except zipfile.BadZipFile as error:
        raise ValueError("invalid ZIP archive") from error


def _inspect_tar(
    data: bytes,
    limits: IngestLimits,
    budget: ParseBudget,
    inspection: ArchiveInspection,
    depth: int,
    locator_prefix: str,
) -> None:
    names: list[tuple[str, bool]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for ordinal, info in enumerate(archive, start=1):
                if ordinal > limits.max_archive_members:
                    raise ResourceLimitExceeded(
                        "max_archive_members", limits.max_archive_members, ordinal
                    )
                _validate_member_name(info.name)
                names.append((info.name, info.isdir()))
                if info.isdir():
                    continue
                if not info.isfile() or info.issparse():
                    raise UnsafeArchiveMemberError(
                        info.name, "links, sparse entries, and device entries are forbidden"
                    )
                if info.size < 0:
                    raise UnsafeArchiveMemberError(info.name, "negative member size")
                budget.consume_parts(1)
                stream = archive.extractfile(info)
                if stream is None:
                    raise UnsafeArchiveMemberError(info.name, "member is unreadable")
                with stream:
                    payload = _read_exact_member(stream, info.size, limits, budget)
                _inspect_member_payload(
                    info.name,
                    payload,
                    limits,
                    budget,
                    inspection,
                    depth=depth + 1,
                    locator=_extend_locator(locator_prefix, "tar", ordinal, info.name),
                    bytes_already_consumed=True,
                )
        _validate_name_collisions(names)
    except (tarfile.ReadError, EOFError) as error:
        raise ValueError("invalid TAR archive") from error


def _inspect_member_payload(
    name: str,
    data: bytes,
    limits: IngestLimits,
    budget: ParseBudget,
    inspection: ArchiveInspection,
    *,
    depth: int,
    locator: str,
    bytes_already_consumed: bool = False,
) -> None:
    _validate_member_name(name)
    budget.consume_nesting(depth)
    if not bytes_already_consumed:
        budget.consume_bytes(len(data))
    reason = _active_payload_reason(name, data)
    if reason is not None:
        raise UnsafeArchiveMemberError(name, reason)

    nested_type = _container_type(data, name)
    if nested_type is not None:
        _append_member(inspection, locator, name, "container", data, depth)
        _inspect_container(
            data,
            nested_type,
            name,
            limits,
            budget,
            inspection,
            depth=depth,
            locator_prefix=locator,
        )
        return

    suffix = PurePosixPath(name).suffix.casefold()
    if _has_image_signature(data):
        inspect_image_bytes(data, limits=limits, budget=budget)
        _append_member(inspection, locator, name, "image", data, depth)
        return
    if _is_inert_pdf(data):
        _append_member(inspection, locator, name, "pdf", data, depth)
        return
    if _is_known_package_metadata(name, data):
        _append_member(inspection, locator, name, "package_metadata", data, depth)
        return
    if suffix in _TEXT_SUFFIXES:
        if suffix in _XML_SUFFIXES:
            _validate_inert_xml(name, data, budget)
        if suffix == ".rtf":
            _validate_inert_rtf(name, data)
        text, _ = decode_text(data, limits)
        budget.consume_characters(len(text))
        escaped = html.escape(text, quote=False)
        budget.consume_normalized_output(len(escaped.encode("utf-8")))
        inspection.escaped_text_parts.append(f"[{html.escape(name, quote=False)}]\n{escaped}")
        _append_member(inspection, locator, name, "text", data, depth)
        return
    raise UnsafeArchiveMemberError(name, "member type is inspection_ambiguous")


def _decompress_one_gzip(
    data: bytes, limits: IngestLimits, budget: ParseBudget
) -> tuple[bytes, str | None]:
    if not data.startswith(b"\x1f\x8b\x08"):
        raise ValueError("invalid GZip stream")
    header_name = _gzip_header_name(data)
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output: list[bytes] = []
    cursor = 0
    try:
        while cursor < len(data):
            chunk = data[cursor : cursor + 64 * 1024]
            cursor += len(chunk)
            pending = chunk
            while pending:
                remaining = limits.max_archive_uncompressed_bytes - budget.bytes_consumed
                if remaining < 0:
                    raise ResourceLimitExceeded(
                        "max_archive_uncompressed_bytes",
                        limits.max_archive_uncompressed_bytes,
                        budget.bytes_consumed,
                    )
                produced = decompressor.decompress(pending, remaining + 1)
                pending = decompressor.unconsumed_tail
                if produced:
                    budget.consume_bytes(len(produced))
                    output.append(produced)
                if decompressor.unused_data:
                    raise UnsafeArchiveMemberError(
                        header_name or "<gzip>", "concatenated or trailing GZip data"
                    )
                if pending and not produced and remaining == 0:
                    raise ResourceLimitExceeded(
                        "max_archive_uncompressed_bytes",
                        limits.max_archive_uncompressed_bytes,
                        budget.bytes_consumed + 1,
                    )
        produced = decompressor.flush(
            limits.max_archive_uncompressed_bytes - budget.bytes_consumed + 1
        )
        if produced:
            budget.consume_bytes(len(produced))
            output.append(produced)
    except zlib.error as error:
        raise ValueError("invalid GZip stream") from error
    if not decompressor.eof:
        raise ValueError("truncated GZip stream")
    result = b"".join(output)
    compressed_size = max(1, len(data))
    ratio = len(result) / compressed_size
    if ratio > limits.max_archive_expansion_ratio:
        raise ResourceLimitExceeded(
            "max_archive_expansion_ratio", limits.max_archive_expansion_ratio, ratio
        )
    return result, header_name


def _read_exact_member(
    stream, expected_size: int, limits: IngestLimits, budget: ParseBudget
) -> bytes:
    if expected_size > limits.max_archive_uncompressed_bytes:
        raise ResourceLimitExceeded(
            "max_archive_uncompressed_bytes",
            limits.max_archive_uncompressed_bytes,
            expected_size,
        )
    chunks: list[bytes] = []
    observed = 0
    while observed < expected_size:
        chunk = stream.read(min(64 * 1024, expected_size - observed))
        if not chunk:
            break
        observed += len(chunk)
        budget.consume_bytes(len(chunk))
        chunks.append(chunk)
    if observed != expected_size or stream.read(1):
        raise UnsafeArchiveMemberError("<member>", "declared size does not match content")
    return b"".join(chunks)


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x41:
        raise UnsafeArchiveMemberError(info.filename, "encrypted ZIP members are forbidden")
    if info.compress_type not in _ZIP_METHODS:
        raise UnsafeArchiveMemberError(info.filename, "unsupported compression method")
    unix_mode = info.external_attr >> 16
    file_kind = stat.S_IFMT(unix_mode)
    if file_kind and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)):
        raise UnsafeArchiveMemberError(info.filename, "non-regular ZIP member")


def _validate_name_collisions(names: list[tuple[str, bool]] | object) -> None:
    seen: dict[str, tuple[str, bool]] = {}
    for name, is_dir in names:  # type: ignore[union-attr]
        _validate_member_name(name)
        normalized = unicodedata.normalize("NFKC", name.rstrip("/")).casefold()
        previous = seen.get(normalized)
        if previous is not None:
            raise UnsafeArchiveMemberError(
                name,
                f"normalized name collides with {previous[0]!r}",
            )
        seen[normalized] = (name, is_dir)


def _validate_member_name(name: str) -> None:
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeArchiveMemberError(name, "empty, NUL, or backslash path")
    if name.startswith(("/", "//")) or PureWindowsPath(name).is_absolute():
        raise UnsafeArchiveMemberError(name, "absolute path")
    if len(name) > 1024:
        raise UnsafeArchiveMemberError(name, "member name exceeds bound")
    raw_parts = name.rstrip("/").split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise UnsafeArchiveMemberError(name, "traversal or ambiguous path")
    path = PurePosixPath(*raw_parts)
    if ":" in path.parts[0]:
        raise UnsafeArchiveMemberError(name, "drive-qualified path")


def _active_payload_reason(name: str, data: bytes) -> str | None:
    lowered = name.casefold()
    suffix = PurePosixPath(lowered).suffix
    if suffix in _ACTIVE_SUFFIXES:
        return "active or executable member type"
    if any(data.startswith(magic) for magic in _ACTIVE_MAGIC):
        return "active executable or OLE content"
    if "vbaproject" in lowered or "/embeddings/" in f"/{lowered}":
        return "macro or embedded active content"
    uppercase = data[: min(len(data), 2 * 1024 * 1024)].upper()
    if suffix in _XML_SUFFIXES and contains_forbidden_xml_declaration(data):
        return "XML entity or document type declaration"
    if suffix == ".rels" and b'TARGETMODE="EXTERNAL"' in uppercase:
        return "external package relationship"
    return None


def _validate_inert_xml(name: str, data: bytes, budget: ParseBudget) -> None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise UnsafeArchiveMemberError(name, "invalid XML member") from error
    node_count = 0
    for element in root.iter():
        node_count += 1 + len(element.attrib)
        if _local_name(element.tag) == "Relationship":
            target_mode = element.attrib.get("TargetMode", "").casefold()
            target = element.attrib.get("Target", "").strip().casefold()
            if target_mode == "external" or target.startswith(
                ("//", "http:", "https:", "ftp:", "file:")
            ):
                raise UnsafeArchiveMemberError(name, "external package relationship")
    budget.consume_nodes(node_count)


def _validate_inert_rtf(name: str, data: bytes) -> None:
    lowered = data.casefold()
    if any(marker in lowered for marker in _ACTIVE_RTF):
        raise UnsafeArchiveMemberError(name, "active RTF control word")


def _is_known_package_metadata(name: str, data: bytes) -> bool:
    lowered = name.casefold()
    if lowered in {"mimetype", "[content_types].xml"}:
        if lowered.endswith(".xml"):
            uppercase = data.upper()
            if contains_forbidden_xml_declaration(data):
                raise UnsafeArchiveMemberError(name, "forbidden package XML declaration")
            active_markers = (
                b"MACROENABLED",
                b"VND.MS-OFFICE.VBAPROJECT",
                b"OLEOBJECT",
                b"ACTIVEX",
            )
            if any(marker in uppercase for marker in active_markers):
                raise UnsafeArchiveMemberError(name, "active package content type")
            try:
                ET.fromstring(data)
            except ET.ParseError as error:
                raise UnsafeArchiveMemberError(name, "invalid package metadata") from error
        return True
    return False


def inspect_image_bytes(
    data: bytes,
    *,
    limits: IngestLimits,
    budget: ParseBudget,
) -> tuple[int, int]:
    """Validate one inert raster without retaining decoder objects or pixels."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                first_width, first_height = image.size
                frame_count = getattr(image, "n_frames", 1)
                budget.consume_frames(frame_count)
                for frame in range(frame_count):
                    image.seek(frame)
                    width, height = image.size
                    check_image_pixels(width, height, limits)
                    budget.consume_pixels(width * height)
                image.verify()
                return first_width, first_height
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("invalid or truncated image member") from error
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("image member exceeds decoder safety limits") from error


def contains_forbidden_xml_declaration(data: bytes) -> bool:
    """Scan a bounded XML member without allocating a second full-size uppercase copy."""

    overlap = max(len(marker) for marker in _XML_FORBIDDEN) - 1
    tail = b""
    for cursor in range(0, len(data), 64 * 1024):
        window = tail + data[cursor : cursor + 64 * 1024].upper()
        if any(marker in window for marker in _XML_FORBIDDEN):
            return True
        tail = window[-overlap:]
    return False


def _has_image_signature(data: bytes) -> bool:
    if data.startswith(b"RIFF"):
        return len(data) >= 12 and data[8:12] == b"WEBP"
    return any(data.startswith(signature) for signature in _IMAGE_SIGNATURES[:-1])


def _is_inert_pdf(data: bytes) -> bool:
    if not data.startswith(b"%PDF-"):
        return False
    if b"%%EOF" not in data[-1024:]:
        raise ValueError("truncated PDF member")
    active_markers = (
        b"/AA",
        b"/EmbeddedFile",
        b"/GoToR",
        b"/ImportData",
        b"/JavaScript",
        b"/Launch",
        b"/OpenAction",
        b"/RichMedia",
        b"/SubmitForm",
        b"/URI",
        b"/XFA",
    )
    lowered = data.casefold()
    if any(marker.casefold() in lowered for marker in active_markers):
        raise ValueError("PDF member contains active or external content")
    return True


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _container_type(data: bytes, name: str) -> FileType | None:
    stream = io.BytesIO(data)
    if zipfile.is_zipfile(stream):
        return FileType.ZIP
    if data.startswith(b"\x1f\x8b\x08"):
        suffix = PurePosixPath(name).suffix.casefold()
        is_tar_gzip = suffix == ".tgz" or name.casefold().endswith(".tar.gz")
        return FileType.TGZ if is_tar_gzip else FileType.GZ
    if _looks_like_tar(data):
        return FileType.TAR
    return None


def _looks_like_tar(data: bytes) -> bool:
    if len(data) < 512:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            next(iter(archive), None)
        return True
    except (tarfile.ReadError, EOFError):
        return False


def _gzip_header_name(data: bytes) -> str | None:
    if len(data) < 10:
        raise ValueError("truncated GZip header")
    flags = data[3]
    if flags & 0xE0:
        raise UnsafeArchiveMemberError("<gzip>", "reserved GZip flags are set")
    cursor = 10
    if flags & 0x04:
        if cursor + 2 > len(data):
            raise ValueError("truncated GZip extra header")
        length = int.from_bytes(data[cursor : cursor + 2], "little")
        cursor += 2 + length
    name: str | None = None
    if flags & 0x08:
        end = data.find(b"\x00", cursor, min(len(data), cursor + 1025))
        if end < 0:
            raise UnsafeArchiveMemberError("<gzip>", "unterminated GZip member name")
        try:
            name = data[cursor:end].decode("latin-1")
        except UnicodeDecodeError as error:  # pragma: no cover - latin-1 is total
            raise UnsafeArchiveMemberError("<gzip>", "invalid GZip member name") from error
        _validate_member_name(name)
        cursor = end + 1
    if flags & 0x10:
        end = data.find(b"\x00", cursor, min(len(data), cursor + 4097))
        if end < 0:
            raise UnsafeArchiveMemberError("<gzip>", "unterminated GZip comment")
        cursor = end + 1
    if flags & 0x02:
        cursor += 2
    if cursor > len(data):
        raise ValueError("truncated GZip header")
    return name


def _strip_gzip_suffix(name: str) -> str:
    lowered = name.casefold()
    if lowered.endswith(".tar.gz"):
        return name[:-3]
    if lowered.endswith((".tgz", ".gz")):
        return name.rsplit(".", 1)[0]
    return ""


def _append_member(
    inspection: ArchiveInspection,
    locator: str,
    name: str,
    kind: str,
    data: bytes,
    depth: int,
) -> None:
    inspection.members.append(
        InspectedMember(
            locator=locator,
            name=name,
            kind=kind,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            depth=depth,
        )
    )


def _extend_locator(prefix: str, kind: str, *parts: str | int) -> str:
    return f"{prefix}/{canonical_locator(kind, *parts)}"


def _context_file_type(context: AdapterContext) -> FileType:
    try:
        detected_type = FileType(context.metadata.get("detected_type", ""))
    except ValueError as error:
        raise ValueError("archive adapter context requires a detected type") from error
    if detected_type not in _CONTAINER_TYPES:
        raise ValueError(f"archive adapter cannot handle {detected_type.value!r}")
    return detected_type


def _context_text(context: AdapterContext, key: str) -> str | None:
    value = context.metadata.get(key)
    return value if isinstance(value, str) and value else None
