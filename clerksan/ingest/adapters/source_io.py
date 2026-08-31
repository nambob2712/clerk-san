"""Shared bounded reads for digest-bound parser descriptors."""

from __future__ import annotations

import hashlib
import os
import stat

from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.parser_runner import ReadOnlySource, SandboxProtocolError


def read_bounded_source(source: ReadOnlySource, limits: IngestLimits) -> bytes:
    """Read exactly one verified regular-file descriptor without accepting a path."""

    metadata = os.fstat(source.fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise SandboxProtocolError("parser source must be a regular file")
    if metadata.st_size > limits.max_upload_bytes:
        raise ResourceLimitExceeded("max_upload_bytes", limits.max_upload_bytes, metadata.st_size)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    offset = 0
    while offset < metadata.st_size:
        chunk = os.pread(source.fd, min(1024 * 1024, metadata.st_size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        digest.update(chunk)
        offset += len(chunk)
    if offset != metadata.st_size or digest.hexdigest() != source.source_sha256:
        raise SandboxProtocolError("parser source changed while reading")
    return b"".join(chunks)
