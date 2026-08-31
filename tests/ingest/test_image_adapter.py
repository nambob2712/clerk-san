from __future__ import annotations

import hashlib
import io
import tempfile

import pytest
from PIL import Image

from clerksan.ingest.adapters.image import ImageAdapter
from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.normalized import DocMetadata
from clerksan.ingest.parser_artifacts import PNG_SIGNATURE, ArtifactRole
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource
from clerksan.llm.ocr import OcrResult


class FakeOcr:
    name = "fake-ocr"

    def __init__(self) -> None:
        self.images: list[bytes] = []

    async def ocr(self, image_bytes: bytes) -> OcrResult:
        self.images.append(image_bytes)
        return OcrResult(
            text="領収書 合計 1,200円",
            engine=self.name,
            confidence_is_self_reported=True,
        )


def _jpeg_with_rotated_exif() -> bytes:
    image = Image.new("RGB", (2, 3), color="white")
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    image.close()
    return output.getvalue()


def _metadata(raw: bytes, *, extra: dict[str, object] | None = None) -> DocMetadata:
    return DocMetadata(
        filename="receipt.jpg",
        detected_type=FileType.JPEG,
        sha256=hashlib.sha256(raw).hexdigest(),
        extra=extra or {},
    )


def _sidecar_normalize(raw: bytes, detected_type: FileType, ocr: FakeOcr):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return ImageAdapter(ocr).normalize_with_artifacts(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename=f"fixture.{detected_type.value}",
            ),
            AdapterContext(
                detected_type.value,
                metadata={"detected_type": detected_type.value},
            ),
        )


async def test_image_adapter_normalizes_exif_runs_ocr_and_keeps_original_reference() -> None:
    raw = _jpeg_with_rotated_exif()
    ocr = FakeOcr()
    result = await ImageAdapter(ocr).adapt(
        raw,
        _metadata(raw, extra={"content_path": "documents/receipt/original.jpg"}),
    )

    assert result.markdown_body == "領収書 合計 1,200円"
    assert result.metadata.page_provenance == ["ocr"]
    assert result.metadata.extra["ocr_engine"] == "fake-ocr"
    assert result.metadata.extra["normalized_image_format"] == "png"
    assert result.images[0].content_path == "documents/receipt/original.jpg"
    assert (result.images[0].width, result.images[0].height) == (3, 2)
    assert result.images[0].sha256 == hashlib.sha256(raw).hexdigest()
    assert len(ocr.images) == 1
    assert ocr.images[0].startswith(b"\x89PNG\r\n\x1a\n")


async def test_image_adapter_rejects_dimensions_before_ocr() -> None:
    image = Image.new("RGB", (3, 2), color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    raw = output.getvalue()
    ocr = FakeOcr()
    adapter = ImageAdapter(
        ocr,
        limits=IngestLimits(
            max_image_width=2,
            max_image_height=10,
            max_image_pixels=20,
        ),
    )

    with pytest.raises(ResourceLimitExceeded, match="max_image_width"):
        await adapter.adapt(raw, _metadata(raw))

    assert ocr.images == []


async def test_image_adapter_uses_content_address_when_no_original_path_is_supplied() -> None:
    raw = _jpeg_with_rotated_exif()
    result = await ImageAdapter(FakeOcr()).adapt(raw, _metadata(raw))

    assert result.images[0].content_path == f"sha256/{hashlib.sha256(raw).hexdigest()}"


@pytest.mark.parametrize(
    ("detected_type", "pillow_format"),
    [
        (FileType.JPEG, "JPEG"),
        (FileType.PNG, "PNG"),
        (FileType.WEBP, "WEBP"),
        (FileType.BMP, "BMP"),
        (FileType.GIF, "GIF"),
        (FileType.TIFF, "TIFF"),
    ],
)
def test_sidecar_image_normalization_returns_one_sanitized_rgb_png_without_ocr(
    detected_type: FileType,
    pillow_format: str,
) -> None:
    output = io.BytesIO()
    image = Image.new("RGB", (4, 3), "navy")
    image.save(output, pillow_format)
    image.close()
    ocr = FakeOcr()

    result = _sidecar_normalize(output.getvalue(), detected_type, ocr)

    assert ocr.images == []
    assert result.normalized.metadata.page_provenance == ["ocr_required"]
    assert result.normalized.metadata.extra["ocr_required_pages"] == [1]
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.role is ArtifactRole.SANITIZED_IMAGE
    assert artifact.media_type == "image/png"
    assert artifact.data.startswith(PNG_SIGNATURE)
    assert artifact.data[25] == 2
    assert artifact.page_number == 1
    assert artifact.source_location == "image:1"
    assert artifact.ocr_required is True
    assert result.normalized.images[0].sha256 == hashlib.sha256(artifact.data).hexdigest()
    with Image.open(io.BytesIO(artifact.data)) as sanitized:
        assert sanitized.mode == "RGB"
        assert sanitized.info.get("exif") is None
        assert sanitized.info.get("icc_profile") is None


def test_sidecar_image_normalization_applies_exif_orientation_without_ocr() -> None:
    raw = _jpeg_with_rotated_exif()
    ocr = FakeOcr()

    result = _sidecar_normalize(raw, FileType.JPEG, ocr)

    assert ocr.images == []
    assert (result.artifacts[0].width, result.artifacts[0].height) == (3, 2)
    assert result.normalized.images[0].content_path == "artifact:sanitized-image:1"


def test_sidecar_image_normalization_rejects_mismatched_detected_format() -> None:
    raw = _jpeg_with_rotated_exif()

    with pytest.raises(ValueError, match="does not match"):
        _sidecar_normalize(raw, FileType.PNG, FakeOcr())
