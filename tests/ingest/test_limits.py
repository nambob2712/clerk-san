from __future__ import annotations

import stat
import zipfile
from io import BytesIO

import pytest

from clerksan.ingest.limits import (
    IngestLimits,
    ParseBudget,
    ResourceLimitExceeded,
    UnsafeArchiveMemberError,
    check_image_pixels,
    check_pdf_pages,
    check_upload_size,
    safe_zip_members,
)


def test_upload_pdf_and_image_bounds_expose_the_limit_name() -> None:
    limits = IngestLimits(
        max_upload_bytes=10,
        max_pdf_pages=2,
        max_image_pixels=10,
        max_image_width=5,
        max_image_height=5,
    )

    with pytest.raises(ResourceLimitExceeded, match="max_upload_bytes"):
        check_upload_size(11, limits)
    with pytest.raises(ResourceLimitExceeded, match="max_pdf_pages"):
        check_pdf_pages(10_000, limits)
    with pytest.raises(ResourceLimitExceeded, match="max_image_pixels"):
        check_image_pixels(4, 4, limits)
    with pytest.raises(ResourceLimitExceeded, match="max_image_width"):
        check_image_pixels(6, 1, limits)


@pytest.mark.parametrize(
    "name",
    [
        "../outside.txt",
        "/absolute.txt",
        "C:/windows.txt",
        "folder\\windows.txt",
        "bad\x00name.txt",
    ],
)
def test_zip_member_paths_are_safe_on_all_extracting_platforms(name: str) -> None:
    with pytest.raises(UnsafeArchiveMemberError):
        safe_zip_members([name], [(1, 1)], IngestLimits())


def test_zip_symlinks_are_rejected_from_zipinfo_metadata() -> None:
    link = zipfile.ZipInfo("linked-file")
    link.file_size = 1
    link.compress_size = 1
    link.external_attr = (stat.S_IFLNK | 0o777) << 16

    with pytest.raises(UnsafeArchiveMemberError, match="symbolic links"):
        safe_zip_members([link], limits=IngestLimits())


def test_zip_member_count_total_size_and_expansion_are_bounded() -> None:
    count_limits = IngestLimits(max_archive_members=1)
    with pytest.raises(ResourceLimitExceeded, match="max_archive_members"):
        safe_zip_members(["a", "b"], [(1, 1), (1, 1)], count_limits)

    size_limits = IngestLimits(max_archive_uncompressed_bytes=10)
    with pytest.raises(ResourceLimitExceeded, match="max_archive_uncompressed_bytes"):
        safe_zip_members(["a"], [(2, 11)], size_limits)

    ratio_limits = IngestLimits(max_archive_expansion_ratio=5)
    with pytest.raises(ResourceLimitExceeded, match="max_archive_expansion_ratio"):
        safe_zip_members(["a"], [(1, 6)], ratio_limits)


def test_nonempty_zero_compressed_member_is_rejected() -> None:
    with pytest.raises(ResourceLimitExceeded, match="max_archive_expansion_ratio"):
        safe_zip_members(["a"], [(0, 1)], IngestLimits())


def test_safe_zip_metadata_is_returned_without_extracting_any_member() -> None:
    names = safe_zip_members(
        ["word/document.xml", "word/media/image1.png"],
        [(100, 200), (10, 10)],
        IngestLimits(),
    )

    assert names == ["word/document.xml", "word/media/image1.png"]


def test_zipfile_object_and_infolist_forms_are_both_supported() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("word/document.xml", "<document />")

    with zipfile.ZipFile(buffer) as archive:
        assert safe_zip_members(archive, IngestLimits()) == ["word/document.xml"]
        assert safe_zip_members(archive.infolist(), IngestLimits()) == ["word/document.xml"]


def test_phase_one_aggregate_limit_defaults_are_explicit() -> None:
    limits = IngestLimits()

    assert limits.max_image_frames == 100
    assert limits.max_text_characters == 2_000_000
    assert limits.max_tabular_rows == 100_000
    assert limits.max_tabular_cells == 1_000_000
    assert limits.max_structured_nodes == 500_000
    assert limits.max_recursion_depth == 4
    assert limits.max_normalized_output_bytes == 50 * 1024 * 1024


def test_parse_budget_tracks_aggregate_resources_across_members() -> None:
    limits = IngestLimits(
        max_archive_uncompressed_bytes=10,
        max_image_pixels=12,
        max_image_frames=3,
        max_pdf_pages=2,
        max_text_characters=8,
        max_tabular_rows=4,
        max_tabular_cells=6,
        max_structured_nodes=5,
        max_archive_members=2,
        max_recursion_depth=2,
        max_normalized_output_bytes=9,
    )
    budget = ParseBudget(limits)

    budget.consume_bytes(4)
    budget.consume_bytes(6)
    budget.consume_pixels(5)
    budget.consume_pixels(7)
    budget.consume_frames(1)
    budget.consume_frames(2)
    budget.consume_pages(1)
    budget.consume_pages(1)
    budget.consume_characters(3)
    budget.consume_characters(5)
    budget.consume_rows(2)
    budget.consume_rows(2)
    budget.consume_cells(1)
    budget.consume_cells(5)
    budget.consume_nodes(2)
    budget.consume_nodes(3)
    budget.consume_parts(1)
    budget.consume_parts(1)
    budget.consume_nesting(2)
    budget.consume_normalized_output(4)
    budget.consume_normalized_output(5)

    assert budget.bytes_consumed == 10
    assert budget.pixels_consumed == 12
    assert budget.max_nesting == 2
    assert budget.normalized_output_bytes_consumed == 9


@pytest.mark.parametrize(
    ("method", "limit_name"),
    [
        ("consume_bytes", "max_archive_uncompressed_bytes"),
        ("consume_pixels", "max_image_pixels"),
        ("consume_frames", "max_image_frames"),
        ("consume_pages", "max_pdf_pages"),
        ("consume_characters", "max_text_characters"),
        ("consume_rows", "max_tabular_rows"),
        ("consume_cells", "max_tabular_cells"),
        ("consume_nodes", "max_structured_nodes"),
        ("consume_parts", "max_archive_members"),
        ("consume_nesting", "max_recursion_depth"),
        ("consume_normalized_output", "max_normalized_output_bytes"),
    ],
)
def test_parse_budget_rejects_the_first_aggregate_overrun(
    method: str,
    limit_name: str,
) -> None:
    limits = IngestLimits(
        max_archive_uncompressed_bytes=1,
        max_image_pixels=1,
        max_image_frames=1,
        max_pdf_pages=1,
        max_text_characters=1,
        max_tabular_rows=1,
        max_tabular_cells=1,
        max_structured_nodes=1,
        max_archive_members=1,
        max_recursion_depth=1,
        max_normalized_output_bytes=1,
    )
    budget = ParseBudget(limits)

    with pytest.raises(ResourceLimitExceeded) as error:
        getattr(budget, method)(2)

    assert error.value.limit_name == limit_name


def test_parse_budget_does_not_mutate_a_counter_after_a_failed_consume() -> None:
    budget = ParseBudget(IngestLimits(max_tabular_rows=2))
    budget.consume_rows(2)

    with pytest.raises(ResourceLimitExceeded):
        budget.consume_rows(1)

    assert budget.rows_consumed == 2
