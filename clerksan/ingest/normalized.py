"""The JSON-safe, format-neutral document representation used after ingestion."""

from __future__ import annotations

import enum
import hashlib
import json
from typing import Any
from urllib.parse import quote
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from clerksan.ingest.filetype import FileType

PDF_PREVIEW_MANIFEST_MIME = "application/vnd.clerksan.preview-manifest+json"


def canonical_json(value: Any) -> str:
    """Return the one JSON spelling used by structural and candidate digests."""

    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value is not canonical JSON") from error


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_locator(kind: str, *parts: str | int) -> str:
    """Build an unambiguous, stable locator without provider-specific semantics."""

    clean_kind = kind.strip().casefold()
    if not clean_kind or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in clean_kind
    ):
        raise ValueError("locator kind must be a nonblank lowercase token")
    encoded: list[str] = []
    for part in parts:
        text = str(part)
        if not text:
            raise ValueError("locator parts must not be blank")
        encoded.append(quote(text, safe="._-~"))
    return "/".join((clean_kind, *encoded))


class _StrictModel(BaseModel):
    """Reject accidental fields so adapter boundaries remain explicit."""

    model_config = ConfigDict(extra="forbid")


class ExtractedImage(_StrictModel):
    """A persisted image reference; binary image content never enters JSON."""

    sha256: str
    content_path: str
    width: int | None = None
    height: int | None = None
    source_location: str | None = None

    @field_validator("content_path")
    @classmethod
    def _path_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("content_path must not be empty")
        return value


class ExtractedTable(_StrictModel):
    """A table retained as cells instead of flattened prose."""

    header: list[str]
    rows: list[list[str]]
    source_location: str | None = None


class DocMetadata(_StrictModel):
    """Provenance shared by all normalized document formats."""

    filename: str
    detected_type: FileType
    sha256: str
    family: str | None = None
    canonical_mime: str | None = None
    charset: str | None = None
    truncated: bool = False
    page_provenance: list[str] = Field(default_factory=list)
    extra: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("sha256")
    @classmethod
    def _sha256_is_canonical(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


class PdfPreviewStatus(enum.StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"


class PdfPreviewPageDescriptor(_StrictModel):
    page_number: int = Field(ge=1)
    artifact_id: UUID
    sha256: str
    mime: str = "image/png"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    byte_size: int = Field(gt=0)

    @field_validator("sha256")
    @classmethod
    def _page_sha256_is_canonical(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("mime")
    @classmethod
    def _page_mime_is_png(cls, value: str) -> str:
        if value != "image/png":
            raise ValueError("PDF preview pages must be inert PNG images")
        return value


class PdfPreviewManifest(_StrictModel):
    schema_version: int = 1
    document_id: UUID
    source_file_id: UUID
    source_version: int = Field(ge=1)
    source_sha256: str
    page_count: int = Field(ge=0)
    status: PdfPreviewStatus
    pages: list[PdfPreviewPageDescriptor] = Field(default_factory=list)
    manifest_sha256: str
    unavailable_reason: str | None = None

    @field_validator("source_sha256", "manifest_sha256")
    @classmethod
    def _manifest_sha256_is_canonical(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("digest must be a lowercase SHA-256 value")
        return value

    @model_validator(mode="after")
    def _complete_or_explicitly_unavailable(self) -> PdfPreviewManifest:
        if self.status is PdfPreviewStatus.READY:
            expected = list(range(1, self.page_count + 1))
            actual = [page.page_number for page in self.pages]
            if self.page_count < 1 or actual != expected:
                raise ValueError("ready PDF preview must contain every ordered page")
            if self.unavailable_reason is not None:
                raise ValueError("ready PDF preview cannot have an unavailable reason")
        else:
            if self.pages:
                raise ValueError("unavailable PDF preview must not expose partial pages")
            if not self.unavailable_reason:
                raise ValueError("unavailable PDF preview requires a stable reason")
        return self


class NormalizedDocument(_StrictModel):
    """Markdown, provenance, images, and tables emitted by every adapter."""

    markdown_body: str
    metadata: DocMetadata
    images: list[ExtractedImage] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    embeddable: bool = True
