"""Content-based file type detection for document ingestion.

The upload filename is useful only for deciding whether a UTF-8 text upload is an
intentional Markdown/text document.  Images, PDFs, and OOXML packages are always
classified from their bytes so a renamed file cannot select the wrong adapter.
"""

from __future__ import annotations

import enum
import io
import json
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from clerksan.ingest.limits import (
    IngestLimits,
    ResourceLimitExceeded,
    UnsafeArchiveMemberError,
    check_upload_size,
    safe_zip_central_directory,
)


class FileType(enum.StrEnum):
    """The document formats accepted by the ingest pipeline."""

    MD = "md"
    DOCX = "docx"
    XLSX = "xlsx"
    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"
    CSV = "csv"
    TSV = "tsv"
    TXT = "txt"
    RST = "rst"
    LOG = "log"
    JSON = "json"
    JSONL = "jsonl"
    YAML = "yaml"
    XML = "xml"
    HTML = "html"
    SVG = "svg"
    BMP = "bmp"
    GIF = "gif"
    TIFF = "tiff"
    PPTX = "pptx"
    ODT = "odt"
    ODP = "odp"
    ODS = "ods"
    RTF = "rtf"
    EML = "eml"
    ZIP = "zip"
    TAR = "tar"
    TGZ = "tgz"
    GZ = "gz"


LEGACY_FILE_TYPES: frozenset[FileType] = frozenset(
    {
        FileType.MD,
        FileType.DOCX,
        FileType.XLSX,
        FileType.PDF,
        FileType.JPEG,
        FileType.PNG,
        FileType.WEBP,
    }
)


@dataclass(frozen=True)
class DetectedFormat:
    """A content-derived format description for the dark universal policy.

    Declared filenames and MIME types are intentionally absent from the evidence.  They
    may be compared by a future client-facing layer, but they cannot promote bytes into a
    safer family or select an adapter.
    """

    family: str
    format: str
    canonical_mime: str
    charset: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.family or not self.format or not self.canonical_mime:
            raise ValueError("detected format fields must be non-empty")
        object.__setattr__(self, "evidence", tuple(self.evidence))


MIME_BY_FILE_TYPE: dict[FileType, str] = {
    FileType.MD: "text/markdown",
    FileType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    FileType.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    FileType.PDF: "application/pdf",
    FileType.JPEG: "image/jpeg",
    FileType.PNG: "image/png",
    FileType.WEBP: "image/webp",
    FileType.CSV: "text/csv",
    FileType.TSV: "text/tab-separated-values",
    FileType.TXT: "text/plain",
    FileType.RST: "text/x-rst",
    FileType.LOG: "text/plain",
    FileType.JSON: "application/json",
    FileType.JSONL: "application/x-ndjson",
    FileType.YAML: "application/yaml",
    FileType.XML: "application/xml",
    FileType.HTML: "text/html",
    FileType.SVG: "image/svg+xml",
    FileType.BMP: "image/bmp",
    FileType.GIF: "image/gif",
    FileType.TIFF: "image/tiff",
    FileType.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    FileType.ODT: "application/vnd.oasis.opendocument.text",
    FileType.ODP: "application/vnd.oasis.opendocument.presentation",
    FileType.ODS: "application/vnd.oasis.opendocument.spreadsheet",
    FileType.RTF: "application/rtf",
    FileType.EML: "message/rfc822",
    FileType.ZIP: "application/zip",
    FileType.TAR: "application/x-tar",
    FileType.TGZ: "application/gzip",
    FileType.GZ: "application/gzip",
}

_TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
_UNIVERSAL_TEXT_EXTENSIONS: dict[str, FileType] = {
    ".md": FileType.MD,
    ".markdown": FileType.MD,
    ".txt": FileType.TXT,
    ".rst": FileType.RST,
    ".log": FileType.LOG,
    ".json": FileType.JSON,
    ".jsonl": FileType.JSONL,
    ".ndjson": FileType.JSONL,
    ".yaml": FileType.YAML,
    ".yml": FileType.YAML,
    ".xml": FileType.XML,
    ".html": FileType.HTML,
    ".htm": FileType.HTML,
    ".svg": FileType.SVG,
    ".eml": FileType.EML,
}
_DELIMITED_EXTENSIONS: dict[str, FileType] = {
    ".csv": FileType.CSV,
    ".tsv": FileType.TSV,
}
_DOCX_CONTENT_TYPE = (
    b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
_XLSX_CONTENT_TYPE = b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_PPTX_CONTENT_TYPE = (
    b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
)
_MAX_CONTENT_TYPES_BYTES = 1_048_576


class UnsupportedFileError(ValueError):
    """A file's bytes do not identify one of the supported formats.

    ``claimed_name`` is retained for API clients and logs; ``detected_mime`` is
    deliberately derived from content rather than from the filename suffix.
    """

    def __init__(self, claimed_name: str, detected_mime: str) -> None:
        self.claimed_name = claimed_name
        self.detected_mime = detected_mime
        # ``detected_type`` is a useful alias for callers that present a typed error.
        self.detected_type = detected_mime
        super().__init__(f"unsupported file: {claimed_name!r} detected as {detected_mime!r}")


def detect_file_type(
    data: bytes,
    claimed_name: str,
    *,
    limits: IngestLimits | None = None,
) -> FileType:
    """Return the supported type detected from ``data``.

    A PNG named ``receipt.pdf`` is therefore an image upload and reaches the image
    adapter.  Text has no reliable magic bytes, so it is accepted only when the
    claimed name explicitly opts into a safe text extension and the bytes decode as
    UTF-8 without NULs.
    """

    detected = _detect_binary_type(data, limits or IngestLimits())
    if detected is not None:
        return detected

    if zipfile.is_zipfile(io.BytesIO(data)):
        raise UnsupportedFileError(claimed_name, "application/zip")

    if _is_safe_utf8_text(data) and _has_text_extension(claimed_name):
        return FileType.MD

    detected_mime = "text/plain" if _is_safe_utf8_text(data) else "application/octet-stream"
    raise UnsupportedFileError(claimed_name, detected_mime)


def detect_universal_file_type(
    data: bytes,
    claimed_name: str,
    *,
    limits: IngestLimits | None = None,
) -> FileType:
    """Detect a universal format without changing the legacy allowlist.

    Binary/container evidence is resolved first. Delimited text is admitted only
    when a matching filename extension refines otherwise inert text and the bytes
    decode under the bounded CSV/TSV encoding policy.
    """

    active_limits = limits or IngestLimits()
    detected = _detect_universal_binary_type(data, active_limits, claimed_name)
    if detected is not None:
        return detected
    delimited = _delimited_text_evidence(data, claimed_name)
    if delimited is not None:
        return delimited[0]
    text_type = _universal_text_evidence(data, claimed_name)
    if text_type is not None:
        return text_type
    detected_mime = (
        "text/plain" if _decode_safe_text(data) is not None else "application/octet-stream"
    )
    raise UnsupportedFileError(claimed_name, detected_mime)


def inspect_file(
    source: bytes | bytearray | memoryview | Path,
    declared_name: str | None = None,
    declared_mime: str | None = None,
    *,
    limits: IngestLimits | None = None,
    registry: Any | None = None,
) -> DetectedFormat:
    """Return bounded structural evidence without changing the live legacy allowlist.

    This is the Phase 1 dark inspection seam.  It recognizes the existing structural
    formats plus policy-relevant prohibited/ambiguous families, but it does not register
    adapters or make a file acceptable to ``detect_file_type``.  ``registry`` is accepted
    for the future Phase 2 call shape and deliberately has no influence on detection.
    """

    del declared_mime, registry
    active_limits = limits or IngestLimits()
    data = _bounded_inspection_bytes(source, active_limits)
    if not data:
        return DetectedFormat(
            family="empty",
            format="empty",
            canonical_mime="application/octet-stream",
            evidence=("size:0",),
        )

    executable = _executable_signature(data)
    if executable is not None:
        return DetectedFormat(
            family="executable",
            format=executable,
            canonical_mime="application/x-executable",
            evidence=(f"magic:{executable}",),
        )

    if data.startswith(b"%PDF-"):
        evidence = ["magic:pdf"]
        if b"/Encrypt" in data:
            evidence.append("structure:encrypted")
            return DetectedFormat(
                family="encrypted",
                format="pdf",
                canonical_mime=MIME_BY_FILE_TYPE[FileType.PDF],
                evidence=tuple(evidence),
            )
        if any(marker in data for marker in (b"/JavaScript", b"/OpenAction", b"/Launch")):
            evidence.append("structure:active-content")
            return DetectedFormat(
                family="active",
                format="pdf",
                canonical_mime=MIME_BY_FILE_TYPE[FileType.PDF],
                evidence=tuple(evidence),
            )
        return _detected_legacy(FileType.PDF, "document", "magic:pdf")

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _detected_legacy(FileType.PNG, "image", "magic:png")
    if data.startswith(b"\xff\xd8\xff"):
        return _detected_legacy(FileType.JPEG, "image", "magic:jpeg")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _detected_legacy(FileType.WEBP, "image", "magic:webp")
    for magic, file_type in (
        (b"BM", FileType.BMP),
        (b"GIF87a", FileType.GIF),
        (b"GIF89a", FileType.GIF),
        (b"II*\x00", FileType.TIFF),
        (b"MM\x00*", FileType.TIFF),
    ):
        if data.startswith(magic):
            return _detected_legacy(file_type, "image", f"magic:{file_type.value}")
    if data.lstrip().startswith(b"{\\rtf"):
        return _detected_legacy(FileType.RTF, "document", "magic:rtf")
    if _is_tar(data):
        return _detected_legacy(FileType.TAR, "container", "structure:tar")
    if data.startswith(b"\x1f\x8b"):
        suffix = PurePath(declared_name or "").suffix.lower()
        file_type = FileType.TGZ if suffix in {".tgz", ".tar.gz"} else FileType.GZ
        return _detected_legacy(file_type, "container", "magic:gzip")

    delimited = _delimited_text_evidence(data, declared_name)
    if delimited is not None:
        file_type, charset = delimited
        return DetectedFormat(
            family="tabular",
            format=file_type.value,
            canonical_mime=MIME_BY_FILE_TYPE[file_type],
            charset=charset,
            evidence=(f"text:{charset}", f"filename:{file_type.value}"),
        )

    media = _media_signature(data)
    if media is not None:
        family, format_name, canonical_mime = media
        return DetectedFormat(
            family=family,
            format=format_name,
            canonical_mime=canonical_mime,
            evidence=(f"magic:{format_name}",),
        )

    if zipfile.is_zipfile(io.BytesIO(data)):
        return _inspect_zip(data, active_limits)
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return DetectedFormat(
            family="malformed",
            format="zip",
            canonical_mime="application/zip",
            evidence=("magic:zip", "structure:invalid-central-directory"),
        )

    text_type = _universal_text_evidence(data, declared_name)
    if text_type is not None:
        if data.startswith(b"#!"):
            return DetectedFormat(
                family="active",
                format="script",
                canonical_mime="text/plain",
                charset="utf-8",
                evidence=("text:utf-8", "structure:shebang"),
            )
        family = {
            FileType.JSON: "structured",
            FileType.JSONL: "structured",
            FileType.YAML: "structured",
            FileType.XML: "structured",
            FileType.HTML: "markup",
            FileType.SVG: "markup",
            FileType.EML: "email",
        }.get(text_type, "text")
        decoded = _decode_safe_text(data)
        assert decoded is not None
        _, charset = decoded
        return DetectedFormat(
            family=family,
            format=text_type.value,
            canonical_mime=MIME_BY_FILE_TYPE[text_type],
            charset=charset,
            evidence=(f"text:{charset}", f"structure:{text_type.value}"),
        )

    return DetectedFormat(
        family="opaque",
        format="unknown",
        canonical_mime="application/octet-stream",
        evidence=("content:opaque",),
    )


def _detect_binary_type(data: bytes, limits: IngestLimits) -> FileType | None:
    if data.startswith(b"%PDF-"):
        return FileType.PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType.PNG
    if data.startswith(b"\xff\xd8\xff"):
        return FileType.JPEG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return FileType.WEBP
    if zipfile.is_zipfile(io.BytesIO(data)):
        return _detect_ooxml_type(data, limits)
    return None


def _bounded_inspection_bytes(
    source: bytes | bytearray | memoryview | Path,
    limits: IngestLimits,
) -> bytes:
    if isinstance(source, Path):
        with source.open("rb") as stream:
            data = stream.read(limits.max_upload_bytes + 1)
    elif isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    else:
        raise TypeError("source must be bytes-like or a pathlib.Path")
    check_upload_size(len(data), limits)
    return data


def _detected_legacy(file_type: FileType, family: str, evidence: str) -> DetectedFormat:
    return DetectedFormat(
        family=family,
        format=file_type.value,
        canonical_mime=MIME_BY_FILE_TYPE[file_type],
        evidence=(evidence,),
    )


def _inspect_zip(data: bytes, limits: IngestLimits) -> DetectedFormat:
    try:
        names = safe_zip_central_directory(data, limits)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if any(info.flag_bits & 0x1 for info in infos):
                return DetectedFormat(
                    family="encrypted",
                    format="zip",
                    canonical_mime="application/zip",
                    evidence=("magic:zip", "structure:encrypted-member"),
                )
    except ResourceLimitExceeded:
        raise
    except (OSError, UnsafeArchiveMemberError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return DetectedFormat(
            family="malformed",
            format="zip",
            canonical_mime="application/zip",
            evidence=("magic:zip", "structure:invalid-central-directory"),
        )

    lowercase_names = {name.lower() for name in names}
    if any(
        name.endswith("vbaproject.bin") or ("/embeddings/" in name and name.endswith(".bin"))
        for name in lowercase_names
    ):
        return DetectedFormat(
            family="active",
            format="ooxml",
            canonical_mime="application/zip",
            evidence=("magic:zip", "structure:active-package-member"),
        )

    detected = _detect_zip_package_type(data, limits)
    if detected is FileType.DOCX:
        return _detected_legacy(detected, "document", "structure:ooxml-docx")
    if detected is FileType.XLSX:
        return _detected_legacy(detected, "spreadsheet", "structure:ooxml-xlsx")
    if detected is FileType.PPTX:
        return _detected_legacy(detected, "presentation", "structure:ooxml-pptx")
    if detected in {FileType.ODT, FileType.ODP}:
        return _detected_legacy(detected, "document", f"structure:{detected.value}")
    if detected is FileType.ODS:
        return _detected_legacy(detected, "spreadsheet", "structure:ods")
    if any(name.endswith((".class", ".dex")) for name in lowercase_names):
        return DetectedFormat(
            family="executable",
            format="executable-zip",
            canonical_mime="application/zip",
            evidence=("magic:zip", "structure:executable-member"),
        )
    return DetectedFormat(
        family="container",
        format="zip",
        canonical_mime="application/zip",
        evidence=("magic:zip", "structure:container"),
    )


def _executable_signature(data: bytes) -> str | None:
    if data.startswith(b"\x7fELF"):
        return "elf"
    if data.startswith(b"MZ"):
        return "pe"
    if data.startswith((b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe")):
        return "mach-o"
    return None


def _media_signature(data: bytes) -> tuple[str, str, str] | None:
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio", "mp3", "audio/mpeg"
    if data.startswith(b"fLaC"):
        return "audio", "flac", "audio/flac"
    if data.startswith(b"OggS"):
        return "audio", "ogg", "audio/ogg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio", "wav", "audio/wav"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "video", "avi", "video/x-msvideo"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "matroska", "video/x-matroska"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1"}:
            format_name = "avif" if brand in {b"avif", b"avis"} else "heif"
            return "image", format_name, f"image/{format_name}"
        return "video", "mp4", "video/mp4"
    return None


def _detect_ooxml_type(data: bytes, limits: IngestLimits) -> FileType | None:
    """Recognize only complete DOCX/XLSX OOXML packages, never arbitrary ZIPs."""

    try:
        names = safe_zip_central_directory(data, limits)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            content_info = archive.getinfo("[Content_Types].xml")
            if content_info.file_size > _MAX_CONTENT_TYPES_BYTES:
                return None
            content_types = archive.read(content_info).lower()
    except (KeyError, OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None

    is_docx = "word/document.xml" in names and _DOCX_CONTENT_TYPE in content_types
    is_xlsx = "xl/workbook.xml" in names and _XLSX_CONTENT_TYPE in content_types
    if is_docx == is_xlsx:
        return None
    return FileType.DOCX if is_docx else FileType.XLSX


def _detect_universal_binary_type(
    data: bytes, limits: IngestLimits, claimed_name: str
) -> FileType | None:
    legacy = _detect_binary_type(data, limits)
    if legacy is not None:
        return legacy
    for magic, file_type in (
        (b"BM", FileType.BMP),
        (b"GIF87a", FileType.GIF),
        (b"GIF89a", FileType.GIF),
        (b"II*\x00", FileType.TIFF),
        (b"MM\x00*", FileType.TIFF),
    ):
        if data.startswith(magic):
            return file_type
    if data.lstrip().startswith(b"{\\rtf"):
        return FileType.RTF
    if _is_tar(data):
        return FileType.TAR
    if data.startswith(b"\x1f\x8b"):
        lowered = claimed_name.casefold()
        return FileType.TGZ if lowered.endswith((".tgz", ".tar.gz")) else FileType.GZ
    if zipfile.is_zipfile(io.BytesIO(data)):
        return _detect_zip_package_type(data, limits) or FileType.ZIP
    return None


def _detect_zip_package_type(data: bytes, limits: IngestLimits) -> FileType | None:
    legacy = _detect_ooxml_type(data, limits)
    if legacy is not None:
        return legacy
    try:
        names = safe_zip_central_directory(data, limits)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "[Content_Types].xml" in names:
                content_info = archive.getinfo("[Content_Types].xml")
                if content_info.file_size <= _MAX_CONTENT_TYPES_BYTES:
                    content_types = archive.read(content_info).lower()
                    if "ppt/presentation.xml" in names and _PPTX_CONTENT_TYPE in content_types:
                        return FileType.PPTX
            if "mimetype" in names:
                mimetype_info = archive.getinfo("mimetype")
                if mimetype_info.file_size <= 256:
                    mimetype = archive.read(mimetype_info).strip()
                    return {
                        b"application/vnd.oasis.opendocument.text": FileType.ODT,
                        b"application/vnd.oasis.opendocument.presentation": FileType.ODP,
                        b"application/vnd.oasis.opendocument.spreadsheet": FileType.ODS,
                    }.get(mimetype)
    except (KeyError, OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    return None


def _is_tar(data: bytes) -> bool:
    if len(data) < 512:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            archive.next()
    except (OSError, tarfile.TarError):
        return False
    return True


def _has_text_extension(claimed_name: str) -> bool:
    return PurePath(claimed_name).suffix.lower() in _TEXT_EXTENSIONS


def _universal_text_evidence(data: bytes, claimed_name: str | None) -> FileType | None:
    decoded = _decode_safe_text(data)
    if decoded is None:
        return None
    text, _ = decoded
    stripped = text.lstrip("\ufeff \t\r\n")
    folded = stripped[:512].casefold()
    if folded.startswith("<svg") or (folded.startswith("<?xml") and "<svg" in folded):
        return FileType.SVG
    if folded.startswith(("<!doctype html", "<html", "<head", "<body")):
        return FileType.HTML
    if folded.startswith("<?xml"):
        return FileType.XML

    suffix = PurePath(claimed_name or "").suffix.lower()
    requested = _UNIVERSAL_TEXT_EXTENSIONS.get(suffix)
    if requested is FileType.JSON:
        try:
            json.loads(text)
        except (json.JSONDecodeError, RecursionError):
            return FileType.JSON
        return FileType.JSON
    if requested is FileType.JSONL:
        return FileType.JSONL
    if requested is not None:
        return requested
    return FileType.TXT


def _decode_safe_text(data: bytes) -> tuple[str, str] | None:
    if not data:
        return "", "utf-8"
    if b"\x00" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    candidates = (
        (("utf-8-sig", "utf-8"), ("utf-16", "utf-16"))
        if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))
        else (("utf-8", "utf-8"), ("cp932", "cp932"))
    )
    for codec, charset in candidates:
        try:
            text = data.decode(codec)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            return None
        control_count = sum(ord(character) < 32 and character not in "\t\r\n" for character in text)
        if control_count > max(2, len(text) // 100):
            return None
        return text, charset
    return None


def _delimited_text_evidence(
    data: bytes,
    claimed_name: str | None,
) -> tuple[FileType, str] | None:
    if not claimed_name:
        return None
    file_type = _DELIMITED_EXTENSIONS.get(PurePath(claimed_name).suffix.lower())
    if file_type is None:
        return None
    if b"\x00" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    candidates = (
        (("utf-8-sig", "utf-8"), ("utf-16", "utf-16"))
        if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))
        else (("utf-8", "utf-8"), ("cp932", "cp932"))
    )
    for codec, charset in candidates:
        try:
            text = data.decode(codec)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            return None
        delimiter = "\t" if file_type is FileType.TSV else ","
        if delimiter not in text:
            return None
        return file_type, charset
    return None


def _is_safe_utf8_text(data: bytes) -> bool:
    """Text must be valid UTF-8 and must not contain an embedded NUL byte."""

    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return True
