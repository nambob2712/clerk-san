"""Crash-safe quarantine publication and reference-aware storage reconciliation."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from clerksan.storage import ArtifactIntegrityError, sha256_file, verify_artifact_file

_MANIFEST_VERSION = 1
_RESERVATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STORAGE_LOCK_FILENAME = ".storage.lock"
_STORAGE_LOCK_RETRY_SECONDS = 0.01


class ReferenceLookup(Protocol):
    """Persistence adapter boundary; implementations check every durable source reference."""

    def is_referenced(self, sha256: str) -> bool: ...


ReferenceChecker = Callable[[str], bool] | ReferenceLookup
Checkpoint = Callable[[str], None]


@dataclass(frozen=True)
class QuarantineReservation:
    storage_dir: Path
    reservation_id: str
    payload_path: Path
    manifest_path: Path

    @property
    def temporary_path(self) -> Path:
        """Readable alias for upload callers that refer to the bounded temporary file."""

        return self.payload_path


@dataclass(frozen=True)
class PublishedBlob:
    sha256: str
    path: Path
    relative_path: str
    created: bool


@dataclass(frozen=True)
class ReconcileReport:
    scanned: int = 0
    finalized: int = 0
    removed_payloads: int = 0
    removed_blobs: int = 0
    retained_referenced: int = 0
    retained_active: int = 0
    errors: tuple[str, ...] = ()


class _StorageLock:
    """One POSIX advisory lock file descriptor, released on close or process exit."""

    def __init__(self, storage_dir: Path, *, shared: bool) -> None:
        self._root = storage_dir.resolve()
        _mkdir_durable(self._root)
        self._lock_path = self._root / _STORAGE_LOCK_FILENAME
        self._descriptor = os.open(
            self._lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        self._operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        self._locked = False

    def try_acquire(self) -> bool:
        """Attempt a nonblocking flock acquisition without blocking the event loop."""

        try:
            fcntl.flock(self._descriptor, self._operation | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        self._locked = True
        return True

    def close(self) -> None:
        """Release the lease; closing the descriptor also protects crash recovery."""

        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            if self._locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _validate_lock_retry_seconds(value: float) -> float:
    retry_seconds = float(value)
    if retry_seconds < 0:
        raise ValueError("storage lock retry interval must not be negative")
    return retry_seconds


@contextmanager
def storage_lock(
    storage_dir: Path,
    *,
    shared: bool,
    retry_seconds: float = _STORAGE_LOCK_RETRY_SECONDS,
) -> Iterator[None]:
    """Hold a synchronous POSIX storage lease using nonblocking flock retries."""

    interval = _validate_lock_retry_seconds(retry_seconds)
    lease = _StorageLock(storage_dir, shared=shared)
    try:
        while not lease.try_acquire():
            time.sleep(interval)
        yield
    finally:
        lease.close()


@asynccontextmanager
async def async_storage_lock(
    storage_dir: Path,
    *,
    shared: bool,
    retry_seconds: float = _STORAGE_LOCK_RETRY_SECONDS,
) -> AsyncIterator[None]:
    """Hold a cancellation-safe POSIX lease without delegating blocking flock work."""

    interval = _validate_lock_retry_seconds(retry_seconds)
    lease = _StorageLock(storage_dir, shared=shared)
    try:
        while not lease.try_acquire():
            await asyncio.sleep(interval)
        yield
    finally:
        lease.close()


def reserve_quarantine(
    storage_dir: Path,
    *,
    reservation_id: str | None = None,
    now_ns: int | None = None,
) -> QuarantineReservation:
    """Create one durable reservation and empty bounded-upload target idempotently."""

    root = storage_dir.resolve()
    identifier = reservation_id or uuid4().hex
    _validate_reservation_id(identifier)
    payload_directory, manifest_directory = _ensure_layout(root)
    reservation = QuarantineReservation(
        storage_dir=root,
        reservation_id=identifier,
        payload_path=payload_directory / f"{identifier}.part",
        manifest_path=manifest_directory / f"{identifier}.json",
    )
    if reservation.manifest_path.exists():
        _load_manifest(reservation)
        return reservation

    try:
        descriptor = os.open(
            reservation.payload_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        pass
    else:
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(payload_directory)

    timestamp = _timestamp(now_ns)
    manifest = {
        "version": _MANIFEST_VERSION,
        "reservation_id": identifier,
        "state": "reserved",
        "created_at_ns": timestamp,
        "updated_at_ns": timestamp,
        "payload_path": _relative(root, reservation.payload_path),
        "sha256": None,
        "blob_path": None,
    }
    _atomic_write_manifest(reservation.manifest_path, manifest)
    return reservation


def publish_reserved_blob(
    reservation: QuarantineReservation,
    sha256: str | None = None,
    *,
    namespace: str = "originals",
    checkpoint: Checkpoint | None = None,
    now_ns: int | None = None,
) -> PublishedBlob:
    """Publish verified bytes without ever overwriting or compensating a digest blob.

    A hard link gives same-filesystem create-if-absent semantics.  Every manifest state is
    persisted before its filesystem transition so startup reconciliation can converge from
    each kill window.  Exceptions intentionally leave published blobs in place.
    """

    manifest = _load_manifest(reservation)
    root = reservation.storage_dir.resolve()
    namespace_path = _safe_namespace(root, namespace)
    _mkdir_durable(namespace_path)

    recorded_digest = manifest.get("sha256")
    if recorded_digest is not None:
        _validate_sha256(recorded_digest)
    if sha256 is not None:
        _validate_sha256(sha256)
    if recorded_digest is not None and sha256 is not None and recorded_digest != sha256:
        raise ArtifactIntegrityError("reservation digest mismatch")

    digest = sha256 or recorded_digest
    if digest is None:
        if not reservation.payload_path.is_file():
            raise FileNotFoundError("reserved payload is unavailable")
        digest = sha256_file(reservation.payload_path)
    _validate_sha256(digest)
    blob_path = namespace_path / digest
    relative_blob = _relative(root, blob_path)

    if manifest["state"] == "published":
        if manifest.get("blob_path") != relative_blob:
            raise ArtifactIntegrityError("reservation blob path mismatch")
        verify_artifact_file(blob_path, digest)
        return PublishedBlob(digest, blob_path, relative_blob, created=False)
    if manifest["state"] not in {"reserved", "publishing"}:
        raise ValueError(f"reservation cannot publish from state {manifest['state']!r}")

    if reservation.payload_path.exists():
        _fsync_file(reservation.payload_path)
        verify_artifact_file(reservation.payload_path, digest)
    elif not blob_path.exists():
        raise FileNotFoundError("reserved payload is unavailable")

    timestamp = _timestamp(now_ns)
    manifest.update(
        state="publishing",
        updated_at_ns=timestamp,
        sha256=digest,
        blob_path=relative_blob,
    )
    _atomic_write_manifest(reservation.manifest_path, manifest)
    _checkpoint(checkpoint, "publish_intent_persisted")

    created = False
    if blob_path.exists():
        verify_artifact_file(blob_path, digest)
    else:
        try:
            os.link(reservation.payload_path, blob_path)
            created = True
        except FileExistsError:
            verify_artifact_file(blob_path, digest)
        _fsync_file(blob_path)
        _fsync_directory(namespace_path)
    _checkpoint(checkpoint, "blob_published")

    manifest.update(state="published", updated_at_ns=timestamp)
    _atomic_write_manifest(reservation.manifest_path, manifest)
    _checkpoint(checkpoint, "publish_manifest_persisted")
    _unlink_file(reservation.payload_path)
    return PublishedBlob(digest, blob_path, relative_blob, created=created)


def finalize_reservation(reservation: QuarantineReservation) -> None:
    """Finalize a post-commit reservation without touching its published blob."""

    if not reservation.manifest_path.exists():
        return
    manifest = _load_manifest(reservation)
    manifest.update(state="finalized", updated_at_ns=time.time_ns())
    _atomic_write_manifest(reservation.manifest_path, manifest)
    _unlink_file(reservation.payload_path)
    _unlink_file(reservation.manifest_path)


def reconcile_reservations(
    storage_dir: Path,
    grace_period: float | timedelta,
    reference_checker: ReferenceChecker | None = None,
    *,
    is_referenced: Callable[[str], bool] | None = None,
    now_ns: int | None = None,
    lock_held: bool = False,
) -> ReconcileReport:
    """Converge abandoned reservations after grace and durable reference checks.

    Without a reference checker, published blobs are retained conservatively.  The
    reconciler never scans or deletes unmanifested content-addressed blobs.  Callers
    that need database snapshot consistency may hold an exclusive lease around that
    snapshot and pass ``lock_held=True`` for the cleanup portion.
    """

    if lock_held:
        return _reconcile_reservations_unlocked(
            storage_dir,
            grace_period,
            reference_checker,
            is_referenced=is_referenced,
            now_ns=now_ns,
        )
    with storage_lock(storage_dir, shared=False):
        return _reconcile_reservations_unlocked(
            storage_dir,
            grace_period,
            reference_checker,
            is_referenced=is_referenced,
            now_ns=now_ns,
        )


def _reconcile_reservations_unlocked(
    storage_dir: Path,
    grace_period: float | timedelta,
    reference_checker: ReferenceChecker | None = None,
    *,
    is_referenced: Callable[[str], bool] | None = None,
    now_ns: int | None = None,
) -> ReconcileReport:
    """Reconcile while the caller holds the exclusive storage lease."""

    if reference_checker is not None and is_referenced is not None:
        raise TypeError("reference checker was supplied twice")
    checker: ReferenceChecker | None = is_referenced or reference_checker
    grace_ns = _grace_nanoseconds(grace_period)
    current_ns = _timestamp(now_ns)
    root = storage_dir.resolve()
    payload_directory, manifest_directory = _ensure_layout(root)
    manifest_paths = sorted(manifest_directory.glob("*.json"))

    loaded: list[tuple[QuarantineReservation, dict[str, object], int]] = []
    errors: list[str] = []
    known_payloads: set[Path] = set()
    for manifest_path in manifest_paths:
        reservation_id = manifest_path.stem
        try:
            _validate_reservation_id(reservation_id)
            reservation = QuarantineReservation(
                root,
                reservation_id,
                payload_directory / f"{reservation_id}.part",
                manifest_path,
            )
            manifest = _load_manifest(reservation)
            updated_at = _manifest_timestamp(manifest)
            loaded.append((reservation, manifest, updated_at))
            known_payloads.add(reservation.payload_path)
        except (ArtifactIntegrityError, OSError, ValueError) as error:
            errors.append(f"{reservation_id}: {type(error).__name__}")

    active_digests = {
        str(manifest["sha256"])
        for _reservation, manifest, updated_at in loaded
        if manifest.get("sha256")
        and _age_ns(current_ns, updated_at) < grace_ns
        and manifest.get("state") in {"publishing", "published"}
    }

    finalized = 0
    removed_payloads = 0
    removed_blobs = 0
    retained_referenced = 0
    retained_active = 0
    for reservation, manifest, updated_at in loaded:
        age = _age_ns(current_ns, updated_at)
        if age < grace_ns:
            retained_active += 1
            continue
        state = manifest.get("state")
        if state in {"reserved", "finalized"}:
            removed_payloads += _unlink_file(reservation.payload_path)
            _unlink_file(reservation.manifest_path)
            finalized += 1
            continue
        if state not in {"publishing", "published"}:
            errors.append(f"{reservation.reservation_id}: invalid state")
            continue

        try:
            digest = str(manifest["sha256"])
            _validate_sha256(digest)
            blob_path = _manifest_blob_path(root, manifest, digest)
            if blob_path.exists():
                verify_artifact_file(blob_path, digest)
            if digest in active_digests:
                retained_active += 1
                continue
            if not blob_path.exists():
                removed_payloads += _unlink_file(reservation.payload_path)
                _unlink_file(reservation.manifest_path)
                finalized += 1
                continue
            if checker is None or _check_reference(checker, digest):
                retained_referenced += 1
                removed_payloads += _unlink_file(reservation.payload_path)
                _unlink_file(reservation.manifest_path)
                finalized += 1
                continue
            # Recheck immediately before unlink.  Concurrent uploads retain their own
            # manifest in ``active_digests``; committed sources are visible here.
            if _check_reference(checker, digest):
                retained_referenced += 1
                removed_payloads += _unlink_file(reservation.payload_path)
                _unlink_file(reservation.manifest_path)
                finalized += 1
                continue
            removed_blobs += _unlink_file(blob_path)
            removed_payloads += _unlink_file(reservation.payload_path)
            _unlink_file(reservation.manifest_path)
            finalized += 1
        except (ArtifactIntegrityError, OSError, ValueError) as error:
            errors.append(f"{reservation.reservation_id}: {type(error).__name__}")

    for payload_path in sorted(payload_directory.glob("*.part")):
        if payload_path in known_payloads:
            continue
        try:
            age = _age_ns(current_ns, payload_path.stat().st_mtime_ns)
            if age < grace_ns:
                retained_active += 1
                continue
            removed_payloads += _unlink_file(payload_path)
        except OSError as error:
            errors.append(f"{payload_path.stem}: {type(error).__name__}")

    for temporary_manifest in sorted(manifest_directory.glob(".*.tmp")):
        try:
            age = _age_ns(current_ns, temporary_manifest.stat().st_mtime_ns)
            if age < grace_ns:
                retained_active += 1
                continue
            _unlink_file(temporary_manifest)
        except OSError as error:
            errors.append(f"{temporary_manifest.stem}: {type(error).__name__}")

    return ReconcileReport(
        scanned=len(manifest_paths),
        finalized=finalized,
        removed_payloads=removed_payloads,
        removed_blobs=removed_blobs,
        retained_referenced=retained_referenced,
        retained_active=retained_active,
        errors=tuple(errors),
    )


def _ensure_layout(root: Path) -> tuple[Path, Path]:
    _mkdir_durable(root)
    quarantine = root / ".quarantine"
    payloads = quarantine / "payloads"
    manifests = quarantine / "manifests"
    _mkdir_durable(quarantine)
    _mkdir_durable(payloads)
    _mkdir_durable(manifests)
    return payloads, manifests


def _mkdir_durable(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _fsync_directory(created)
        if created.parent.exists():
            _fsync_directory(created.parent)


def _atomic_write_manifest(path: Path, manifest: dict[str, object]) -> None:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor: int | None = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _load_manifest(reservation: QuarantineReservation) -> dict[str, object]:
    try:
        value = json.loads(reservation.manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ArtifactIntegrityError("invalid reservation manifest") from error
    if not isinstance(value, dict) or value.get("version") != _MANIFEST_VERSION:
        raise ArtifactIntegrityError("unsupported reservation manifest")
    if value.get("reservation_id") != reservation.reservation_id:
        raise ArtifactIntegrityError("reservation identity mismatch")
    if value.get("payload_path") != _relative(reservation.storage_dir, reservation.payload_path):
        raise ArtifactIntegrityError("reservation payload path mismatch")
    if value.get("state") not in {"reserved", "publishing", "published", "finalized"}:
        raise ArtifactIntegrityError("invalid reservation state")
    return value


def _manifest_blob_path(root: Path, manifest: dict[str, object], digest: str) -> Path:
    stored = manifest.get("blob_path")
    if not isinstance(stored, str) or not stored:
        raise ArtifactIntegrityError("reservation blob path is missing")
    relative = PurePosixPath(stored)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.name != digest
        or relative.parts[0] == ".quarantine"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ArtifactIntegrityError("reservation blob path is not content-addressed")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ArtifactIntegrityError("reservation blob path escapes storage") from error
    return candidate


def _safe_namespace(root: Path, namespace: str) -> Path:
    relative = PurePosixPath(namespace)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("blob namespace must stay below storage")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("blob namespace must stay below storage") from error
    return candidate


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ArtifactIntegrityError("reservation path escapes storage") from error


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_file(path: Path) -> int:
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    _fsync_directory(path.parent)
    return 1


def _checkpoint(callback: Checkpoint | None, name: str) -> None:
    if callback is not None:
        callback(name)


def _check_reference(checker: ReferenceChecker, digest: str) -> bool:
    if callable(checker):
        return bool(checker(digest))
    return bool(checker.is_referenced(digest))


def _grace_nanoseconds(value: float | timedelta) -> int:
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    if seconds < 0:
        raise ValueError("grace period must not be negative")
    return int(seconds * 1_000_000_000)


def _timestamp(value: int | None) -> int:
    timestamp = time.time_ns() if value is None else value
    if timestamp < 0:
        raise ValueError("timestamp must not be negative")
    return timestamp


def _manifest_timestamp(manifest: dict[str, object]) -> int:
    value = manifest.get("updated_at_ns")
    if not isinstance(value, int) or value < 0:
        raise ArtifactIntegrityError("invalid reservation timestamp")
    return value


def _age_ns(now_ns: int, updated_at_ns: int) -> int:
    return max(0, now_ns - updated_at_ns)


def _validate_reservation_id(value: str) -> None:
    if not _RESERVATION_ID.fullmatch(value):
        raise ValueError("reservation id contains unsafe characters")


def _validate_sha256(value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
