from __future__ import annotations

import gzip
import hashlib
import io
import stat
import tarfile
import tempfile
import zipfile

import pytest
from PIL import Image

from clerksan.ingest.adapters.archive import ArchiveAdapter
from clerksan.ingest.limits import (
    IngestLimits,
    ResourceLimitExceeded,
    UnsafeArchiveMemberError,
)
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource


def _normalize(
    raw: bytes,
    detected_type: str,
    *,
    filename: str | None = None,
    limits: IngestLimits | None = None,
):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return ArchiveAdapter(limits=limits).normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename=filename or f"source.{detected_type}",
            ),
            AdapterContext(
                adapter_key=f"universal.{detected_type}",
                metadata={"detected_type": detected_type},
            ),
        )


def _zip(entries: list[tuple[str, bytes]], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 3), "white").save(output, "PNG")
    return output.getvalue()


def _tar(entries: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for info, data in entries:
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data) if data else None)
    return output.getvalue()


def test_zip_mixed_safe_members_are_inert_and_keep_recursive_provenance() -> None:
    raw = _zip([("notes/a.txt", b"safe <text>"), ("images/p.png", _png())])
    result = _normalize(raw, "zip")

    assert result.metadata.extra["member_count"] == 2
    assert result.metadata.extra["filesystem_extraction"] == "disabled"
    assert "safe &lt;text&gt;" in result.markdown_body
    members = result.metadata.extra["members"]
    assert {member["kind"] for member in members} == {"text", "image"}
    assert all(member["locator"].startswith("archive/root/zip/") for member in members)


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "/absolute.txt", "C:/drive.txt", "a//b.txt", "a/./b.txt"],
)
def test_zip_traversal_and_absolute_paths_are_rejected(name: str) -> None:
    with pytest.raises(UnsafeArchiveMemberError):
        _normalize(_zip([(name, b"no")]), "zip")


def test_zip_symlink_case_collision_and_unsupported_method_are_rejected() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "target")
    with pytest.raises(UnsafeArchiveMemberError, match="symbolic"):
        _normalize(output.getvalue(), "zip")

    with pytest.raises(UnsafeArchiveMemberError, match="collides"):
        _normalize(_zip([("A.txt", b"one"), ("a.txt", b"two")]), "zip")

    with pytest.raises(UnsafeArchiveMemberError, match="compression method"):
        _normalize(_zip([("a.txt", b"safe")], compression=zipfile.ZIP_BZIP2), "zip")


def test_zip_encryption_flag_and_ambiguous_member_poison_the_container() -> None:
    encrypted = bytearray(_zip([("a.txt", b"safe")], compression=zipfile.ZIP_STORED))
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        cursor = 0
        while (cursor := encrypted.find(signature, cursor)) >= 0:
            flag_slice = slice(cursor + flag_offset, cursor + flag_offset + 2)
            flags = int.from_bytes(encrypted[flag_slice], "little")
            encrypted[flag_slice] = (flags | 1).to_bytes(2, "little")
            cursor += 4
    with pytest.raises(UnsafeArchiveMemberError, match="encrypted"):
        _normalize(bytes(encrypted), "zip")

    with pytest.raises(UnsafeArchiveMemberError, match="inspection_ambiguous"):
        _normalize(_zip([("unknown.bin", b"not-classified")]), "zip")


@pytest.mark.parametrize(
    "entry_type",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE],
)
def test_tar_links_devices_and_fifos_are_rejected(entry_type: bytes) -> None:
    info = tarfile.TarInfo("unsafe")
    info.type = entry_type
    if entry_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
        info.linkname = "target"
    with pytest.raises(UnsafeArchiveMemberError, match="forbidden"):
        _normalize(_tar([(info, b"")]), "tar")


def test_tgz_and_single_member_gzip_are_inspected_without_filesystem_extraction() -> None:
    member = tarfile.TarInfo("inside.txt")
    tgz_result = _normalize(gzip.compress(_tar([(member, b"tar text")])), "tgz")
    assert "tar text" in tgz_result.markdown_body

    output = io.BytesIO()
    with gzip.GzipFile(filename="one.txt", mode="wb", fileobj=output, mtime=0) as stream:
        stream.write(b"gzip text")
    gz_result = _normalize(output.getvalue(), "gz", filename="source.gz")
    assert gz_result.metadata.extra["member_count"] == 1
    assert "gzip text" in gz_result.markdown_body


def test_recursive_archive_uses_one_aggregate_byte_and_depth_budget() -> None:
    inner = _zip([("inside.txt", b"12345")], compression=zipfile.ZIP_STORED)
    outer = _zip([("inner.zip", inner)], compression=zipfile.ZIP_STORED)
    with pytest.raises(ResourceLimitExceeded, match="max_archive_uncompressed_bytes"):
        _normalize(
            outer,
            "zip",
            limits=IngestLimits(max_archive_uncompressed_bytes=len(inner) + 4),
        )
    with pytest.raises(ResourceLimitExceeded, match="max_recursion_depth"):
        _normalize(outer, "zip", limits=IngestLimits(max_recursion_depth=1))
