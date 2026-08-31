"""Bounded, descriptor-backed outputs produced by the parser sidecar.

Artifact bytes never enter the JSON control channel.  The control message carries
only strict metadata, while the bytes travel in anonymous file descriptors passed
with ``SCM_RIGHTS``.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
import stat
import struct
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from PIL import Image, UnidentifiedImageError

from clerksan.ingest.limits import IngestLimits, check_image_pixels
from clerksan.ingest.normalized import NormalizedDocument, canonical_json

PNG_MIME = "image/png"
PDF_PREVIEW_MANIFEST_MIME = "application/vnd.clerksan.pdf-preview+json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ARTIFACT_SCHEMA_VERSION = 1
PDF_PREVIEW_SCHEMA = "clerksan.parser-pdf-preview"
PDF_PREVIEW_SCHEMA_VERSION = 1
MAX_ARTIFACT_FDS = 128
MAX_PDF_PREVIEW_MANIFEST_BYTES = 256 * 1024


class ArtifactRole(StrEnum):
    SANITIZED_IMAGE = "sanitized_image"
    PDF_PAGE = "pdf_page"
    PDF_PREVIEW_MANIFEST = "pdf_preview_manifest"


class ParserArtifactError(ValueError):
    """An output artifact or descriptor failed the sealed transport contract."""


@dataclass(frozen=True, slots=True)
class GeneratedArtifact:
    """In-child artifact bytes awaiting anonymous descriptor publication."""

    role: ArtifactRole
    media_type: str
    data: bytes
    page_number: int | None = None
    width: int | None = None
    height: int | None = None
    source_location: str | None = None
    ocr_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise ParserArtifactError("generated artifact bytes must be non-empty")
        _validate_role_fields(
            self.role,
            self.media_type,
            self.page_number,
            self.width,
            self.height,
            self.source_location,
            self.ocr_required,
        )
        _validate_media_bytes(
            self.role,
            self.media_type,
            self.data,
            width=self.width,
            height=self.height,
        )


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """Strict JSON metadata bound positionally to one received descriptor."""

    schema_version: int
    ordinal: int
    role: ArtifactRole
    media_type: str
    byte_size: int
    sha256: str
    source_sha256: str
    page_number: int | None
    width: int | None
    height: int | None
    source_location: str | None
    ocr_required: bool
    seal_supported: bool
    sealed: bool

    @classmethod
    def from_mapping(cls, value: object) -> ArtifactDescriptor:
        keys = {
            "schema_version",
            "ordinal",
            "role",
            "media_type",
            "byte_size",
            "sha256",
            "source_sha256",
            "page_number",
            "width",
            "height",
            "source_location",
            "ocr_required",
            "seal_supported",
            "sealed",
        }
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ParserArtifactError("artifact descriptor does not match the protocol schema")
        try:
            descriptor = cls(
                schema_version=value["schema_version"],
                ordinal=value["ordinal"],
                role=ArtifactRole(value["role"]),
                media_type=value["media_type"],
                byte_size=value["byte_size"],
                sha256=value["sha256"],
                source_sha256=value["source_sha256"],
                page_number=value["page_number"],
                width=value["width"],
                height=value["height"],
                source_location=value["source_location"],
                ocr_required=value["ocr_required"],
                seal_supported=value["seal_supported"],
                sealed=value["sealed"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ParserArtifactError("artifact descriptor contains invalid values") from error
        descriptor.validate()
        return descriptor

    def validate(self) -> None:
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ParserArtifactError("unsupported artifact descriptor schema")
        for name, value in (("ordinal", self.ordinal), ("byte_size", self.byte_size)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ParserArtifactError(f"artifact {name} must be a positive integer")
        if not _is_sha256(self.sha256) or not _is_sha256(self.source_sha256):
            raise ParserArtifactError("artifact digests must be lowercase SHA-256 values")
        if not isinstance(self.ocr_required, bool):
            raise ParserArtifactError("artifact OCR linkage must be boolean")
        if not isinstance(self.seal_supported, bool) or not isinstance(self.sealed, bool):
            raise ParserArtifactError("artifact seal evidence must be boolean")
        if self.sealed and not self.seal_supported:
            raise ParserArtifactError("an artifact cannot be sealed without seal support")
        _validate_role_fields(
            self.role,
            self.media_type,
            self.page_number,
            self.width,
            self.height,
            self.source_location,
            self.ocr_required,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ordinal": self.ordinal,
            "role": self.role.value,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "source_sha256": self.source_sha256,
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "source_location": self.source_location,
            "ocr_required": self.ocr_required,
            "seal_supported": self.seal_supported,
            "sealed": self.sealed,
        }


@dataclass(frozen=True, slots=True)
class ParserArtifact:
    descriptor: ArtifactDescriptor
    data: bytes


@dataclass(frozen=True, slots=True)
class ParserRunResult:
    normalized: NormalizedDocument
    artifacts: tuple[ParserArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class AdapterRunResult:
    """Adapter output retained inside the isolated child until FDs are created."""

    normalized: NormalizedDocument
    artifacts: tuple[GeneratedArtifact, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))


@dataclass(frozen=True, slots=True)
class BackendRunResult:
    """Raw backend response plus still-open received artifact descriptors."""

    payload: Mapping[str, Any]
    artifact_fds: tuple[int, ...] = ()


def create_sealed_artifact_fd(artifact: GeneratedArtifact) -> tuple[int, bool, bool]:
    """Write one artifact to an anonymous file and seal it when supported."""

    fd: int | None = None
    try:
        if kernel_sealing_available():
            flags = getattr(os, "MFD_CLOEXEC", 0) | os.MFD_ALLOW_SEALING
            fd = os.memfd_create("clerksan-parser-artifact", flags)
        else:
            with tempfile.TemporaryFile() as handle:
                fd = os.dup(handle.fileno())
            fcntl.fcntl(fd, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
        _write_all(fd, artifact.data)
        os.lseek(fd, 0, os.SEEK_SET)
        seal_supported, sealed = _seal_descriptor(fd)
        return fd, seal_supported, sealed
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise


def descriptor_for_generated(
    artifact: GeneratedArtifact,
    *,
    ordinal: int,
    source_sha256: str,
    seal_supported: bool,
    sealed: bool,
) -> ArtifactDescriptor:
    descriptor = ArtifactDescriptor(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        ordinal=ordinal,
        role=artifact.role,
        media_type=artifact.media_type,
        byte_size=len(artifact.data),
        sha256=hashlib.sha256(artifact.data).hexdigest(),
        source_sha256=source_sha256,
        page_number=artifact.page_number,
        width=artifact.width,
        height=artifact.height,
        source_location=artifact.source_location,
        ocr_required=artifact.ocr_required,
        seal_supported=seal_supported,
        sealed=sealed,
    )
    descriptor.validate()
    return descriptor


def validate_received_artifacts(
    metadata: object,
    descriptors: Sequence[int],
    *,
    source_sha256: str,
    limits: IngestLimits,
) -> tuple[ParserArtifact, ...]:
    """Re-hash and read an exact bounded metadata/FD set without closing it."""

    if not isinstance(metadata, list):
        raise ParserArtifactError("artifact metadata must be a list")
    if len(metadata) != len(descriptors):
        raise ParserArtifactError("artifact metadata/descriptor cardinality mismatch")
    maximum_count = min(MAX_ARTIFACT_FDS, limits.max_pdf_pages + 1)
    if len(descriptors) > maximum_count:
        raise ParserArtifactError("artifact descriptor count exceeds configured limit")

    parsed: list[ParserArtifact] = []
    total_size = 0
    for expected_ordinal, (raw_descriptor, fd) in enumerate(
        zip(metadata, descriptors, strict=True), start=1
    ):
        descriptor = ArtifactDescriptor.from_mapping(raw_descriptor)
        if descriptor.ordinal != expected_ordinal:
            raise ParserArtifactError("artifact ordinals must be complete and ordered")
        if descriptor.source_sha256 != source_sha256:
            raise ParserArtifactError("artifact source digest binding mismatch")
        if descriptor.role in {ArtifactRole.SANITIZED_IMAGE, ArtifactRole.PDF_PAGE}:
            if descriptor.width is None or descriptor.height is None:
                raise ParserArtifactError("raster artifact dimensions are missing")
            try:
                check_image_pixels(descriptor.width, descriptor.height, limits)
            except ValueError as error:
                raise ParserArtifactError(
                    "raster artifact dimensions exceed configured limits"
                ) from error
        descriptor_stat = os.fstat(fd)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise ParserArtifactError("artifact descriptor must reference a regular file")
        if descriptor_stat.st_size != descriptor.byte_size:
            raise ParserArtifactError("artifact declared size does not match its descriptor")
        total_size += descriptor.byte_size
        if total_size > limits.max_normalized_output_bytes:
            raise ParserArtifactError("artifact output exceeds configured limit")
        actual_supported, actual_sealed = descriptor_seal_state(fd)
        if kernel_sealing_available() and not actual_supported:
            raise ParserArtifactError("artifact descriptor is not sealable on this platform")
        if (
            descriptor.seal_supported != actual_supported
            or descriptor.sealed != actual_sealed
            or (actual_supported and not actual_sealed)
        ):
            raise ParserArtifactError("artifact seal evidence mismatch")
        data = _pread_exact(fd, descriptor.byte_size)
        if hashlib.sha256(data).hexdigest() != descriptor.sha256:
            raise ParserArtifactError("artifact digest mismatch")
        _validate_media_bytes(
            descriptor.role,
            descriptor.media_type,
            data,
            width=descriptor.width,
            height=descriptor.height,
        )
        parsed.append(ParserArtifact(descriptor, data))
    return tuple(parsed)


def validate_result_artifact_set(
    adapter_key: str,
    normalized: NormalizedDocument,
    artifacts: Sequence[ParserArtifact],
    *,
    source_sha256: str,
) -> None:
    """Validate adapter-specific completeness and OCR/page linkage."""

    if normalized.metadata.sha256 != source_sha256:
        raise ParserArtifactError("normalized output source digest binding mismatch")

    image_keys = {"jpeg", "png", "webp", "bmp", "gif", "tiff"}
    if adapter_key in image_keys:
        if len(artifacts) != 1:
            raise ParserArtifactError("image parser must return exactly one sanitized artifact")
        artifact = artifacts[0]
        descriptor = artifact.descriptor
        if (
            descriptor.role is not ArtifactRole.SANITIZED_IMAGE
            or descriptor.page_number != 1
            or descriptor.source_location != "image:1"
            or not descriptor.ocr_required
        ):
            raise ParserArtifactError("sanitized image artifact linkage is invalid")
        if normalized.metadata.page_provenance != ["ocr_required"]:
            raise ParserArtifactError("image normalized output lacks OCR-required provenance")
        if normalized.metadata.extra.get("ocr_required_pages") != [1]:
            raise ParserArtifactError("image normalized output lacks OCR page linkage")
        if len(normalized.images) != 1:
            raise ParserArtifactError("image normalized output lacks sanitized image linkage")
        image = normalized.images[0]
        if (
            image.sha256 != descriptor.sha256
            or image.content_path != "artifact:sanitized-image:1"
            or image.width != descriptor.width
            or image.height != descriptor.height
            or image.source_location != descriptor.source_location
        ):
            raise ParserArtifactError("sanitized image metadata does not match its artifact")
        return

    if adapter_key == "pdf":
        _validate_pdf_artifact_set(normalized, artifacts, source_sha256=source_sha256)
        return

    if artifacts:
        raise ParserArtifactError("this parser format is not allowed to return artifacts")


def build_pdf_preview_manifest(source_sha256: str, pages: Sequence[GeneratedArtifact]) -> bytes:
    """Build the immutable parser-level page manifest before FD creation."""

    if not _is_sha256(source_sha256):
        raise ParserArtifactError("PDF preview source digest is invalid")
    expected_pages = list(range(1, len(pages) + 1))
    if [page.page_number for page in pages] != expected_pages:
        raise ParserArtifactError("PDF preview pages must be complete and ordered")
    payload = {
        "schema": PDF_PREVIEW_SCHEMA,
        "version": PDF_PREVIEW_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "page_count": len(pages),
        "pages": [
            {
                "page_number": page.page_number,
                "sha256": hashlib.sha256(page.data).hexdigest(),
                "media_type": page.media_type,
                "byte_size": len(page.data),
                "width": page.width,
                "height": page.height,
                "source_location": page.source_location,
                "ocr_required": page.ocr_required,
            }
            for page in pages
        ],
    }
    return canonical_json(payload).encode("utf-8")


def kernel_sealing_available() -> bool:
    return bool(
        hasattr(os, "memfd_create")
        and hasattr(os, "MFD_ALLOW_SEALING")
        and all(
            hasattr(fcntl, name)
            for name in (
                "F_ADD_SEALS",
                "F_GET_SEALS",
                "F_SEAL_GROW",
                "F_SEAL_SEAL",
                "F_SEAL_SHRINK",
                "F_SEAL_WRITE",
            )
        )
    )


def descriptor_seal_state(fd: int) -> tuple[bool, bool]:
    if not hasattr(fcntl, "F_GET_SEALS"):
        return False, False
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTTY, errno.ENOSYS}:
            return False, False
        raise
    required = _required_seals()
    return True, flags & required == required


def _seal_descriptor(fd: int) -> tuple[bool, bool]:
    if not kernel_sealing_available():
        return descriptor_seal_state(fd)
    required = _required_seals()
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, required)
    return descriptor_seal_state(fd)


def _required_seals() -> int:
    return (
        getattr(fcntl, "F_SEAL_SEAL", 0)
        | getattr(fcntl, "F_SEAL_SHRINK", 0)
        | getattr(fcntl, "F_SEAL_GROW", 0)
        | getattr(fcntl, "F_SEAL_WRITE", 0)
    )


def _validate_pdf_artifact_set(
    normalized: NormalizedDocument,
    artifacts: Sequence[ParserArtifact],
    *,
    source_sha256: str,
) -> None:
    extra = normalized.metadata.extra
    status = extra.get("preview_status")
    if status == "unavailable":
        if artifacts:
            raise ParserArtifactError("unavailable PDF preview must expose no artifacts")
        if not isinstance(extra.get("preview_unavailable_reason"), str):
            raise ParserArtifactError("unavailable PDF preview requires a stable reason")
        if normalized.images:
            raise ParserArtifactError("unavailable PDF preview cannot expose image locators")
        return
    if status != "ready":
        raise ParserArtifactError("PDF normalized output lacks explicit preview status")
    page_count = extra.get("preview_page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ParserArtifactError("ready PDF preview requires a positive page count")
    if len(artifacts) != page_count + 1:
        raise ParserArtifactError("PDF preview artifact set is incomplete")
    pages = artifacts[:-1]
    manifest = artifacts[-1]
    if manifest.descriptor.role is not ArtifactRole.PDF_PREVIEW_MANIFEST:
        raise ParserArtifactError("PDF preview manifest must be the final artifact")
    if [item.descriptor.page_number for item in pages] != list(range(1, page_count + 1)):
        raise ParserArtifactError("PDF preview page ordinals are incomplete")
    if any(item.descriptor.role is not ArtifactRole.PDF_PAGE for item in pages):
        raise ParserArtifactError("PDF preview contains a non-page artifact")
    if len(normalized.metadata.page_provenance) != page_count:
        raise ParserArtifactError("PDF page provenance cardinality mismatch")
    for page, provenance in zip(pages, normalized.metadata.page_provenance, strict=True):
        should_ocr = provenance == "ocr_required"
        if page.descriptor.ocr_required != should_ocr:
            raise ParserArtifactError("PDF artifact OCR linkage mismatch")
    expected_ocr_pages = [
        page.descriptor.page_number for page in pages if page.descriptor.ocr_required
    ]
    if extra.get("ocr_required_pages") != expected_ocr_pages:
        raise ParserArtifactError("PDF normalized OCR page metadata is incomplete")
    if len(normalized.images) != len(expected_ocr_pages):
        raise ParserArtifactError("PDF normalized image linkage is incomplete")
    normalized_ocr_pages: list[int] = []
    page_by_number = {page.descriptor.page_number: page for page in pages}
    for image in normalized.images:
        location = image.source_location
        if not isinstance(location, str) or not location.startswith("page:"):
            raise ParserArtifactError("PDF normalized image locator is invalid")
        try:
            page_number = int(location.removeprefix("page:"))
        except ValueError as error:
            raise ParserArtifactError("PDF normalized image locator is invalid") from error
        page = page_by_number.get(page_number)
        if page is None or not page.descriptor.ocr_required:
            raise ParserArtifactError("PDF normalized image linkage is invalid")
        if (
            image.content_path != f"artifact:pdf-page:{page_number}"
            or image.sha256 != page.descriptor.sha256
            or image.width != page.descriptor.width
            or image.height != page.descriptor.height
        ):
            raise ParserArtifactError("PDF normalized image metadata is invalid")
        normalized_ocr_pages.append(page_number)
    if normalized_ocr_pages != expected_ocr_pages:
        raise ParserArtifactError("PDF normalized image linkage is incomplete")
    try:
        manifest_payload = json.loads(manifest.data)
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise ParserArtifactError("PDF preview manifest is invalid JSON") from error
    expected_payload = {
        "schema": PDF_PREVIEW_SCHEMA,
        "version": PDF_PREVIEW_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "page_count": page_count,
        "pages": [
            {
                "page_number": page.descriptor.page_number,
                "sha256": page.descriptor.sha256,
                "media_type": page.descriptor.media_type,
                "byte_size": page.descriptor.byte_size,
                "width": page.descriptor.width,
                "height": page.descriptor.height,
                "source_location": page.descriptor.source_location,
                "ocr_required": page.descriptor.ocr_required,
            }
            for page in pages
        ],
    }
    if manifest_payload != expected_payload:
        raise ParserArtifactError("PDF preview manifest does not match its page descriptors")
    if manifest.data != canonical_json(expected_payload).encode("utf-8"):
        raise ParserArtifactError("PDF preview manifest is not canonical JSON")
    if extra.get("preview_manifest_sha256") != manifest.descriptor.sha256:
        raise ParserArtifactError("PDF preview manifest digest linkage mismatch")


def _validate_role_fields(
    role: ArtifactRole,
    media_type: object,
    page_number: object,
    width: object,
    height: object,
    source_location: object,
    ocr_required: object,
) -> None:
    if not isinstance(role, ArtifactRole):
        raise ParserArtifactError("artifact role is invalid")
    if not isinstance(media_type, str):
        raise ParserArtifactError("artifact media type is invalid")
    if role in {ArtifactRole.SANITIZED_IMAGE, ArtifactRole.PDF_PAGE}:
        if media_type != PNG_MIME:
            raise ParserArtifactError("raster parser artifacts must be PNG")
        for name, value in (
            ("page_number", page_number),
            ("width", width),
            ("height", height),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ParserArtifactError(f"raster artifact {name} must be positive")
        expected_location = (
            "image:1" if role is ArtifactRole.SANITIZED_IMAGE else f"page:{page_number}"
        )
        if source_location != expected_location:
            raise ParserArtifactError("raster artifact source location is invalid")
        if role is ArtifactRole.SANITIZED_IMAGE and ocr_required is not True:
            raise ParserArtifactError("sanitized image artifact must require OCR")
    else:
        if media_type != PDF_PREVIEW_MANIFEST_MIME:
            raise ParserArtifactError("PDF preview manifest media type is invalid")
        if any(value is not None for value in (page_number, width, height, source_location)):
            raise ParserArtifactError("PDF preview manifest cannot claim a page locator")
        if ocr_required is not False:
            raise ParserArtifactError("PDF preview manifest cannot require OCR")


def _validate_media_bytes(
    role: ArtifactRole,
    media_type: str,
    data: bytes,
    *,
    width: int | None,
    height: int | None,
) -> None:
    if role in {ArtifactRole.SANITIZED_IMAGE, ArtifactRole.PDF_PAGE}:
        if media_type != PNG_MIME or len(data) < 26 or not data.startswith(PNG_SIGNATURE):
            raise ParserArtifactError("artifact declared as PNG has invalid bytes")
        if data[12:16] != b"IHDR":
            raise ParserArtifactError("artifact PNG lacks an IHDR header")
        actual_width, actual_height = struct.unpack(">II", data[16:24])
        if (actual_width, actual_height) != (width, height):
            raise ParserArtifactError("artifact PNG dimensions do not match metadata")
        if data[25] != 2:
            raise ParserArtifactError("sanitized parser PNG must use RGB color")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if (
                    image.format != "PNG"
                    or image.mode != "RGB"
                    or image.size != (width, height)
                    or int(getattr(image, "n_frames", 1)) != 1
                ):
                    raise ParserArtifactError("sanitized parser PNG is not a single RGB image")
                image.verify()
        except (OSError, SyntaxError, UnidentifiedImageError) as error:
            raise ParserArtifactError("artifact declared as PNG has invalid bytes") from error
        return
    if media_type != PDF_PREVIEW_MANIFEST_MIME:
        raise ParserArtifactError("artifact media type is not permitted")
    if len(data) > MAX_PDF_PREVIEW_MANIFEST_BYTES:
        raise ParserArtifactError("PDF preview manifest exceeds its protocol limit")
    try:
        decoded = json.loads(data)
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise ParserArtifactError("PDF preview manifest is invalid JSON") from error
    if not isinstance(decoded, Mapping):
        raise ParserArtifactError("PDF preview manifest must be a JSON object")


def _pread_exact(fd: int, size: int) -> bytes:
    output = bytearray()
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(1024 * 1024, size - offset), offset)
        if not chunk:
            break
        output.extend(chunk)
        offset += len(chunk)
    if offset != size:
        raise ParserArtifactError("artifact descriptor ended before its declared size")
    return bytes(output)


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written < 1:
            raise OSError("artifact descriptor write made no progress")
        offset += written


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
