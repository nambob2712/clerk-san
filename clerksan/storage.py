"""Safe, portable paths and checksum helpers for immutable document artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path


class StoragePathError(ValueError):
    """A persisted artifact reference is outside the configured document store."""


class ArtifactIntegrityError(ValueError):
    """Persisted bytes do not match the immutable checksum recorded for an artifact."""


def relative_storage_path(storage_dir: Path, path: Path | str) -> str:
    """Return a portable, normalized artifact path below ``storage_dir``."""

    root = storage_dir.resolve()
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise StoragePathError(
            f"artifact is outside the configured document store: {path}"
        ) from error


def resolve_storage_path(storage_dir: Path, stored_path: str) -> Path:
    """Resolve a portable or legacy in-store path without allowing path traversal."""

    if not isinstance(stored_path, str) or not stored_path.strip():
        raise StoragePathError("artifact path must be a non-empty string")
    root = storage_dir.resolve()
    candidate = Path(stored_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise StoragePathError(
            f"artifact is outside the configured document store: {stored_path}"
        ) from error
    return resolved


def sha256_file(path: Path) -> str:
    """Stream a file's checksum without loading a document into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact_bytes(content: bytes, expected_sha256: str) -> bytes:
    """Return bytes only when they match their persisted immutable SHA-256 checksum."""

    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ArtifactIntegrityError("artifact checksum mismatch")
    return content


def verify_artifact_file(path: Path, expected_sha256: str) -> None:
    """Verify an on-disk artifact before reusing its content-addressed path."""

    if sha256_file(path) != expected_sha256:
        raise ArtifactIntegrityError("artifact checksum mismatch")


async def read_verified_artifact(
    read_bytes: Callable[[str], Awaitable[bytes]], content_path: str, expected_sha256: str
) -> bytes:
    """Read persisted bytes through a supplied boundary and verify their stored checksum."""

    return verify_artifact_bytes(await read_bytes(content_path), expected_sha256)
