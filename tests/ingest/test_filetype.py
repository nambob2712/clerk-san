from __future__ import annotations

import tarfile
import zipfile
from io import BytesIO

import pytest

import clerksan.ingest.filetype as filetype
from clerksan.ingest.filetype import (
    LEGACY_FILE_TYPES,
    DetectedFormat,
    FileType,
    UnsupportedFileError,
    detect_file_type,
    detect_universal_file_type,
    inspect_file,
)
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded


def _ooxml_package(main_path: str, content_type: str, *, extra_members: int = 0) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            f'<Types><Override ContentType="{content_type}" /></Types>',
        )
        archive.writestr(main_path, "<document />")
        for index in range(extra_members):
            archive.writestr(f"word/custom/item-{index}.xml", "<item />")
    return buffer.getvalue()


def test_magic_bytes_win_over_misleading_filename() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"not-a-real-rendered-png"

    assert detect_file_type(png, "receipt.pdf") is FileType.PNG


@pytest.mark.parametrize(
    ("payload", "name", "expected"),
    [
        (b"%PDF-1.7\n", "scan.jpg", FileType.PDF),
        (b"\xff\xd8\xff\xe0", "scan.png", FileType.JPEG),
        (b"RIFF\x04\x00\x00\x00WEBP", "scan.pdf", FileType.WEBP),
        ("\ufeff# 領収書\n合計: 1000円\n".encode("utf-8"), "notes.markdown", FileType.MD),
    ],
)
def test_detects_supported_content(payload: bytes, name: str, expected: FileType) -> None:
    assert detect_file_type(payload, name) is expected


def test_distinguishes_docx_and_xlsx_by_ooxml_paths_and_content_types() -> None:
    docx = _ooxml_package(
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    )
    xlsx = _ooxml_package(
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    )

    assert detect_file_type(docx, "renamed.bin") is FileType.DOCX
    assert detect_file_type(xlsx, "renamed.docx") is FileType.XLSX


def test_ooxml_member_limit_is_checked_before_zipfile_opens_the_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _ooxml_package(
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        extra_members=1,
    )

    def unexpected_zipfile_open(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("member limit must be checked before ZipFile opens the archive")

    monkeypatch.setattr(filetype.zipfile, "ZipFile", unexpected_zipfile_open)

    with pytest.raises(ResourceLimitExceeded, match="max_archive_members") as error:
        detect_file_type(payload, "receipt.docx", limits=IngestLimits(max_archive_members=2))

    assert error.value.observed == 3


def test_plain_zip_named_docx_is_not_accepted() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "not an OOXML package")

    with pytest.raises(UnsupportedFileError) as error:
        detect_file_type(buffer.getvalue(), "invoice.docx")

    assert error.value.claimed_name == "invoice.docx"
    assert error.value.detected_mime == "application/zip"


def test_plain_text_requires_a_text_extension_and_valid_utf8() -> None:
    with pytest.raises(UnsupportedFileError) as extension_error:
        detect_file_type("領収書".encode(), "receipt.csv")
    assert extension_error.value.detected_mime == "text/plain"

    with pytest.raises(UnsupportedFileError) as encoding_error:
        detect_file_type(b"\xff\xfe\x00", "receipt.md")
    assert encoding_error.value.detected_mime == "application/octet-stream"


@pytest.mark.parametrize(
    ("payload", "name", "expected", "charset"),
    [
        (b"date,amount\n2026-08-22,1200\n", "transactions.csv", FileType.CSV, "utf-8"),
        ("日付\t金額\n2026-08-22\t1200\n".encode("cp932"), "取引.tsv", FileType.TSV, "cp932"),
        ("date,amount\n2026-08-22,1200\n".encode("utf-16"), "data.csv", FileType.CSV, "utf-16"),
    ],
)
def test_universal_delimited_detection_is_extension_refined_and_bounded(
    payload: bytes,
    name: str,
    expected: FileType,
    charset: str,
) -> None:
    assert detect_universal_file_type(payload, name) is expected
    detected = inspect_file(payload, declared_name=name)
    assert detected.family == "tabular"
    assert detected.format == expected.value
    assert detected.charset == charset


def test_legacy_detector_still_rejects_csv_while_universal_detector_accepts_it() -> None:
    payload = b"date,amount\n2026-08-22,1200\n"
    with pytest.raises(UnsupportedFileError):
        detect_file_type(payload, "transactions.csv")
    assert detect_universal_file_type(payload, "transactions.csv") is FileType.CSV


def test_text_with_nul_is_never_treated_as_markdown() -> None:
    with pytest.raises(UnsupportedFileError) as error:
        detect_file_type(b"# receipt\x00\n", "receipt.md")

    assert error.value.detected_mime == "application/octet-stream"


def test_legacy_file_type_enum_remains_the_original_seven_formats() -> None:
    assert LEGACY_FILE_TYPES == {
        FileType.MD,
        FileType.DOCX,
        FileType.XLSX,
        FileType.PDF,
        FileType.JPEG,
        FileType.PNG,
        FileType.WEBP,
    }


def test_dark_inspection_returns_structural_evidence_only() -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"bounded-dark-inspection"

    detected = inspect_file(
        payload,
        declared_name="misleading.pdf",
        declared_mime="application/pdf",
    )

    assert detected == DetectedFormat(
        family="image",
        format="png",
        canonical_mime="image/png",
        evidence=("magic:png",),
    )
    assert all("pdf" not in item for item in detected.evidence)


@pytest.mark.parametrize(
    ("payload", "family", "format_name"),
    [
        (b"", "empty", "empty"),
        (b"\x7fELF\x02\x01\x01", "executable", "elf"),
        (b"ID3\x04\x00\x00", "audio", "mp3"),
        (b"\x00\x00\x00\x18ftypmp42", "video", "mp4"),
        (b"\x00\x01\x02\x03", "opaque", "unknown"),
    ],
)
def test_dark_inspection_classifies_policy_families_without_new_live_formats(
    payload: bytes,
    family: str,
    format_name: str,
) -> None:
    detected = inspect_file(payload)

    assert detected.family == family
    assert detected.format == format_name


def test_gif_remains_unsupported_by_the_live_legacy_detector() -> None:
    with pytest.raises(UnsupportedFileError):
        detect_file_type(b"GIF89a\x01\x00\x01\x00", "image.gif")


@pytest.mark.parametrize(
    ("payload", "name", "expected"),
    [
        (b"GIF89a\x01\x00\x01\x00", "renamed.bin", FileType.GIF),
        (b"BM" + b"\x00" * 32, "renamed.bin", FileType.BMP),
        (b"II*\x00" + b"\x00" * 32, "renamed.bin", FileType.TIFF),
        (b"{\\rtf1 safe text}", "renamed.bin", FileType.RTF),
        (b'{"safe": true}', "data.json", FileType.JSON),
        ("plain 日本語".encode("cp932"), "notes.txt", FileType.TXT),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "wrong.txt", FileType.SVG),
        (b"<!doctype html><html><body>x</body></html>", "wrong.txt", FileType.HTML),
    ],
)
def test_universal_detector_adds_structural_families_without_legacy_drift(
    payload: bytes, name: str, expected: FileType
) -> None:
    assert detect_universal_file_type(payload, name) is expected
    if expected not in {FileType.JSON, FileType.TXT, FileType.SVG, FileType.HTML}:
        with pytest.raises(UnsupportedFileError):
            detect_file_type(payload, name)


def test_universal_detector_recognizes_pptx_odf_zip_tar_and_gzip() -> None:
    pptx = _ooxml_package(
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    )
    odf_buffer = BytesIO()
    with zipfile.ZipFile(odf_buffer, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", "<office:document-content />")
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("notes.txt", "safe")
    tar_buffer = BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
        info = tarfile.TarInfo("notes.txt")
        info.size = 4
        archive.addfile(info, BytesIO(b"safe"))

    assert detect_universal_file_type(pptx, "slides.bin") is FileType.PPTX
    assert detect_universal_file_type(odf_buffer.getvalue(), "sheet.bin") is FileType.ODS
    assert detect_universal_file_type(zip_buffer.getvalue(), "bundle.bin") is FileType.ZIP
    assert detect_universal_file_type(tar_buffer.getvalue(), "bundle.bin") is FileType.TAR
    assert detect_universal_file_type(b"\x1f\x8b" + b"x" * 20, "bundle.tgz") is FileType.TGZ
