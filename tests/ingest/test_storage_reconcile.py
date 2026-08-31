from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from clerksan.ingest import storage_reconcile
from clerksan.ingest.storage_reconcile import (
    async_storage_lock,
    finalize_reservation,
    publish_reserved_blob,
    reconcile_reservations,
    reserve_quarantine,
    storage_lock,
)


def _reserve_bytes(storage: Path, payload: bytes, reservation_id: str):
    reservation = reserve_quarantine(storage, reservation_id=reservation_id, now_ns=1)
    reservation.payload_path.write_bytes(payload)
    return reservation


def _reconcile_after_confirmed_lock_contention(
    storage_dir: str,
    lock_blocked: multiprocessing.synchronize.Event,
    reference_established: multiprocessing.synchronize.Event,
    completed: multiprocessing.synchronize.Event,
    outcomes: multiprocessing.queues.Queue,
) -> None:
    """Run the real reconciler after proving its exclusive flock is currently blocked."""

    descriptor = os.open(Path(storage_dir) / ".storage.lock", os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            lock_blocked.set()
    finally:
        os.close(descriptor)

    try:
        if acquired:
            outcomes.put(("exclusive lock unexpectedly acquired",))
            return
        report = reconcile_reservations(
            Path(storage_dir),
            grace_period=0,
            is_referenced=lambda _digest: reference_established.is_set(),
        )
        outcomes.put(report)
    except BaseException as error:
        outcomes.put((type(error).__name__, str(error)))
    finally:
        completed.set()


def test_reservation_and_content_addressed_publish_are_idempotent(tmp_path: Path) -> None:
    payload = b"immutable original"
    digest = hashlib.sha256(payload).hexdigest()
    reservation = _reserve_bytes(tmp_path, payload, "upload-a")

    published = publish_reserved_blob(reservation, digest)
    replay = publish_reserved_blob(reservation, digest)

    assert published.path == replay.path == tmp_path / "originals" / digest
    assert published.path.read_bytes() == payload
    assert published.created is True
    assert replay.created is False
    manifest = json.loads(reservation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "published"
    assert manifest["sha256"] == digest

    finalize_reservation(reservation)
    finalize_reservation(reservation)
    assert published.path.read_bytes() == payload
    assert not reservation.manifest_path.exists()


def test_publish_kill_point_never_synchronously_deletes_a_blob(tmp_path: Path) -> None:
    payload = b"survive request failure"
    digest = hashlib.sha256(payload).hexdigest()
    reservation = _reserve_bytes(tmp_path, payload, "upload-killed")

    def kill_after_blob(checkpoint: str) -> None:
        if checkpoint == "blob_published":
            raise RuntimeError("simulated SIGKILL boundary")

    with pytest.raises(RuntimeError, match="SIGKILL"):
        publish_reserved_blob(reservation, digest, checkpoint=kill_after_blob)

    blob = tmp_path / "originals" / digest
    assert blob.read_bytes() == payload

    report = reconcile_reservations(
        tmp_path,
        grace_period=0,
        is_referenced=lambda _digest: False,
        now_ns=10,
    )

    assert report.removed_blobs == 1
    assert not blob.exists()
    assert not reservation.manifest_path.exists()


def test_reconciler_honors_grace_and_reference_checks(tmp_path: Path) -> None:
    payload = b"committed source"
    digest = hashlib.sha256(payload).hexdigest()
    reservation = _reserve_bytes(tmp_path, payload, "upload-committed")
    published = publish_reserved_blob(reservation, digest, now_ns=2)

    too_early = reconcile_reservations(
        tmp_path,
        grace_period=10,
        is_referenced=lambda _digest: False,
        now_ns=5_000_000_000,
    )
    assert too_early.retained_active == 1
    assert published.path.exists()

    referenced = reconcile_reservations(
        tmp_path,
        grace_period=0,
        is_referenced=lambda candidate: candidate == digest,
        now_ns=20_000_000_000,
    )
    assert referenced.retained_referenced == 1
    assert published.path.read_bytes() == payload
    assert not reservation.manifest_path.exists()


def test_concurrent_same_digest_reservation_prevents_unreferenced_cleanup(tmp_path: Path) -> None:
    payload = b"same digest concurrent upload"
    digest = hashlib.sha256(payload).hexdigest()
    old = _reserve_bytes(tmp_path, payload, "upload-old")
    published = publish_reserved_blob(old, digest, now_ns=2)
    active = _reserve_bytes(tmp_path, payload, "upload-active")
    publish_reserved_blob(active, digest, now_ns=9_000_000_000)

    report = reconcile_reservations(
        tmp_path,
        grace_period=5,
        is_referenced=lambda _digest: False,
        now_ns=10_000_000_000,
    )

    assert report.retained_active >= 1
    assert published.path.read_bytes() == payload


def test_cancelled_async_storage_lock_waiter_releases_its_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempted = threading.Event()
    original_try_acquire = storage_reconcile._StorageLock.try_acquire

    def recording_try_acquire(lock: storage_reconcile._StorageLock) -> bool:
        acquired = original_try_acquire(lock)
        if not acquired:
            attempted.set()
        return acquired

    monkeypatch.setattr(
        storage_reconcile._StorageLock,
        "try_acquire",
        recording_try_acquire,
    )

    async def cancel_waiter() -> None:
        async def wait_for_shared_lease() -> None:
            async with async_storage_lock(tmp_path, shared=True, retry_seconds=0.001):
                raise AssertionError("the exclusive lease should prevent acquisition")

        task = asyncio.create_task(wait_for_shared_lease())
        for _ in range(100):
            if attempted.is_set():
                break
            await asyncio.sleep(0.001)
        assert attempted.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with storage_lock(tmp_path, shared=False):
        asyncio.run(cancel_waiter())

    descriptor = os.open(tmp_path / ".storage.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        pytest.fail(f"cancelled waiter left the storage lease locked: {error}")
    finally:
        os.close(descriptor)


def test_reconciler_waits_for_shared_lease_and_keeps_an_eventually_referenced_blob(
    tmp_path: Path,
) -> None:
    """An exclusive startup cleanup cannot cross a live upload's shared lease."""

    payload = b"shared lease survives reference commit"
    digest = hashlib.sha256(payload).hexdigest()
    reservation = _reserve_bytes(tmp_path, payload, "upload-live")
    published = publish_reserved_blob(reservation, digest, now_ns=1)

    context = multiprocessing.get_context("spawn")
    lock_blocked = context.Event()
    reference_established = context.Event()
    completed = context.Event()
    outcomes = context.Queue()
    process = context.Process(
        target=_reconcile_after_confirmed_lock_contention,
        args=(
            str(tmp_path),
            lock_blocked,
            reference_established,
            completed,
            outcomes,
        ),
    )

    try:
        with storage_lock(tmp_path, shared=True):
            process.start()
            assert lock_blocked.wait(timeout=10)
            assert not completed.wait(timeout=0.25)
            assert published.path.read_bytes() == payload
            reference_established.set()

        process.join(timeout=10)
        assert process.exitcode == 0
        assert completed.is_set()
        report = outcomes.get(timeout=1)
        assert getattr(report, "removed_blobs", None) == 0
        assert getattr(report, "retained_referenced", None) == 1
        assert published.path.read_bytes() == payload
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)


@pytest.mark.parametrize(
    "kill_at",
    [
        "publish_intent_persisted",
        "blob_published",
        "publish_manifest_persisted",
    ],
)
def test_each_publish_kill_window_converges_idempotently(
    tmp_path: Path,
    kill_at: str,
) -> None:
    payload = f"payload-{kill_at}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    reservation = _reserve_bytes(tmp_path, payload, f"upload-{kill_at}")

    def kill(checkpoint: str) -> None:
        if checkpoint == kill_at:
            raise RuntimeError(checkpoint)

    with pytest.raises(RuntimeError, match=kill_at):
        publish_reserved_blob(reservation, digest, checkpoint=kill, now_ns=2)

    first = reconcile_reservations(
        tmp_path,
        grace_period=0,
        is_referenced=lambda _digest: False,
        now_ns=10,
    )
    second = reconcile_reservations(
        tmp_path,
        grace_period=0,
        is_referenced=lambda _digest: False,
        now_ns=11,
    )

    assert first.errors == ()
    assert second.scanned == 0
    assert not reservation.payload_path.exists()
    assert not reservation.manifest_path.exists()
    assert not (tmp_path / "originals" / digest).exists()


def test_reconciler_never_replaces_or_removes_a_referenced_same_digest_blob(
    tmp_path: Path,
) -> None:
    payload = b"shared immutable bytes"
    digest = hashlib.sha256(payload).hexdigest()
    first = _reserve_bytes(tmp_path, payload, "upload-first")
    second = _reserve_bytes(tmp_path, payload, "upload-second")

    first_publish = publish_reserved_blob(first, digest)
    second_publish = publish_reserved_blob(second, digest)

    assert first_publish.created is True
    assert second_publish.created is False
    assert first_publish.path == second_publish.path

    reconcile_reservations(
        tmp_path,
        grace_period=0,
        is_referenced=lambda _digest: True,
        now_ns=10,
    )

    assert first_publish.path.read_bytes() == payload


def test_reconciler_rejects_a_manifest_path_that_targets_unrelated_storage(
    tmp_path: Path,
) -> None:
    payload = b"same bytes do not make an arbitrary path a managed blob"
    digest = hashlib.sha256(payload).hexdigest()
    reservation = _reserve_bytes(tmp_path, payload, "upload-tampered")
    published = publish_reserved_blob(reservation, digest)
    victim = tmp_path / "important" / "source-record"
    victim.parent.mkdir()
    victim.write_bytes(payload)
    manifest = json.loads(reservation.manifest_path.read_text(encoding="utf-8"))
    manifest["blob_path"] = "important/source-record"
    reservation.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = reconcile_reservations(
        tmp_path,
        grace_period=0,
        is_referenced=lambda _digest: False,
        now_ns=10,
    )

    assert report.errors == ("upload-tampered: ArtifactIntegrityError",)
    assert victim.read_bytes() == payload
    assert published.path.read_bytes() == payload


def test_reconciler_removes_an_abandoned_atomic_manifest_temporary(tmp_path: Path) -> None:
    reserve_quarantine(tmp_path, reservation_id="layout", now_ns=1)
    temporary = tmp_path / ".quarantine" / "manifests" / ".dead.json.token.tmp"
    temporary.write_bytes(b"partial")

    reconcile_reservations(
        tmp_path,
        grace_period=0,
        is_referenced=lambda _digest: False,
        now_ns=10,
    )

    assert not temporary.exists()
