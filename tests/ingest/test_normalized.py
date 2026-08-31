from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import (
    DocMetadata,
    ExtractedImage,
    ExtractedTable,
    NormalizedDocument,
    PdfPreviewManifest,
    PdfPreviewPageDescriptor,
    PdfPreviewStatus,
    canonical_digest,
    canonical_json,
    canonical_locator,
)


def test_normalized_document_round_trips_japanese_tables_images_and_large_body() -> None:
    document = NormalizedDocument(
        markdown_body=("# 令和8年7月の領収書\n合計 12,345円\n" * 200_000),
        metadata=DocMetadata(
            filename="領収書.png",
            detected_type=FileType.PNG,
            sha256="a" * 64,
            page_provenance=["ocr"],
            extra={"ocr_engine": "vision_llm", "outline": ["領収書", "明細"]},
        ),
        images=[
            ExtractedImage(
                sha256="b" * 64,
                content_path="files/original/receipt.png",
                width=1200,
                height=900,
                source_location="page:1",
            )
        ],
        tables=[
            ExtractedTable(
                header=["日付", "金額"],
                rows=[["2026-07-13", "12,345円"], ["2026-07-14", "500円"]],
                source_location="page:1",
            )
        ],
    )

    encoded = document.model_dump_json()
    restored = NormalizedDocument.model_validate_json(encoded)

    assert len(encoded) > 5_000_000
    assert restored == document
    assert restored.tables[0].header == ["日付", "金額"]


@pytest.mark.parametrize(
    "payload",
    [
        {"sha256": "a", "content_path": "x", "binary": "not allowed"},
        {"filename": "x", "detected_type": "png", "sha256": "a", "unknown": "no"},
        {
            "markdown_body": "x",
            "metadata": {"filename": "x", "detected_type": "md", "sha256": "a"},
            "surprise": True,
        },
    ],
)
def test_normalized_contract_rejects_unknown_fields(payload: dict[str, object]) -> None:
    if "content_path" in payload:
        model = ExtractedImage
    elif "markdown_body" in payload:
        model = NormalizedDocument
    else:
        model = DocMetadata

    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_image_reference_requires_a_real_storage_path() -> None:
    with pytest.raises(ValidationError, match="content_path"):
        ExtractedImage(sha256="a", content_path="")


def test_pdf_preview_manifest_requires_a_complete_ordered_page_set() -> None:
    document_id = uuid4()
    source_file_id = uuid4()
    pages = [
        PdfPreviewPageDescriptor(
            page_number=number,
            artifact_id=uuid4(),
            sha256=f"{number:x}" * 64,
            width=1200,
            height=1600,
            byte_size=1000,
        )
        for number in (1, 2)
    ]
    manifest = PdfPreviewManifest(
        document_id=document_id,
        source_file_id=source_file_id,
        source_version=1,
        source_sha256="a" * 64,
        page_count=2,
        status=PdfPreviewStatus.READY,
        pages=pages,
        manifest_sha256="b" * 64,
    )
    assert [page.page_number for page in manifest.pages] == [1, 2]

    with pytest.raises(ValidationError, match="every ordered page"):
        PdfPreviewManifest(
            document_id=document_id,
            source_file_id=source_file_id,
            source_version=1,
            source_sha256="a" * 64,
            page_count=2,
            status=PdfPreviewStatus.READY,
            pages=pages[:1],
            manifest_sha256="b" * 64,
        )


def test_unavailable_pdf_preview_never_exposes_partial_pages() -> None:
    with pytest.raises(ValidationError, match="partial pages"):
        PdfPreviewManifest(
            document_id=uuid4(),
            source_file_id=uuid4(),
            source_version=1,
            source_sha256="a" * 64,
            page_count=2,
            status=PdfPreviewStatus.UNAVAILABLE,
            pages=[
                PdfPreviewPageDescriptor(
                    page_number=1,
                    artifact_id=uuid4(),
                    sha256="c" * 64,
                    width=1200,
                    height=1600,
                    byte_size=1000,
                )
            ],
            manifest_sha256="b" * 64,
            unavailable_reason="resource_limit_exceeded",
        )


def test_canonical_json_and_locator_are_stable_and_unambiguous() -> None:
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    assert canonical_digest({"b": 2, "a": 1}) == canonical_digest({"a": 1, "b": 2})
    assert canonical_locator("table", "sheet / 1", 2) == "table/sheet%20%2F%201/2"


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        canonical_json({"amount": float("nan")})
