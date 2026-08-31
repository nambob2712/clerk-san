"""Safety helpers used by the native Clerk-san preview launcher.

The shell launcher owns process lifecycle.  This module keeps JSON readiness
parsing and SQLite rollback preparation in Python so both behaviors can be
tested without duplicating application rules in shell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from clerksan.config import Settings
from clerksan.tools.backup import ManifestError, restore_sqlite, snapshot_sqlite, verify_manifest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_CONTRACT_FILES = (
    _PROJECT_ROOT / "clerksan" / "db" / "models.py",
    _PROJECT_ROOT / "clerksan" / "db" / "sqlite_schema.py",
)
_SAFE_MODEL_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_WORKER_REASON_CODES = frozenset(
    {"registry_mismatch", "sandbox_unavailable", "worker_capability_stale"}
)
_CORE_REASON_CODES = frozenset(
    {
        "configuration_unavailable",
        "database_unavailable",
        "local_data_needs_upgrade",
        "sandbox_unavailable",
        "storage_unavailable",
    }
)
_PROCESSING_REASON_CODES = frozenset(
    {
        "embedding_digest_mismatch",
        "model_unavailable",
        "ollama_unavailable",
        "registry_mismatch",
        "required_model_missing",
        "sandbox_unavailable",
        "worker_capability_stale",
    }
)
_MODEL_REASON_CODES = frozenset(
    {"embedding_digest_mismatch", "ollama_unavailable", "required_model_missing"}
)
_RUNTIME_STORAGE_NAMES = frozenset({".quarantine", ".storage.lock"})


class LocalPreviewError(RuntimeError):
    """A launcher safety contract could not be established."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise LocalPreviewError("SQLite rollback artifact could not be read safely") from None
    return digest.hexdigest()


def schema_contract_digest(paths: tuple[Path, ...] = _SCHEMA_CONTRACT_FILES) -> str:
    """Bind the rollback decision to the code that owns the SQLite schema."""

    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise LocalPreviewError("SQLite schema contract source is missing or unsafe")
        relative = path.relative_to(_PROJECT_ROOT).as_posix().encode("utf-8")
        try:
            content = path.read_bytes()
        except OSError:
            raise LocalPreviewError("SQLite schema contract source is missing or unsafe") from None
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def sqlite_schema_digest(database: Path) -> str:
    """Return a content-free digest of the database's declared schema."""

    if not database.is_file() or database.is_symlink():
        raise LocalPreviewError("SQLite database is missing or unsafe")
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT type, name, tbl_name, COALESCE(sql, '') "
                "FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name, tbl_name"
            ).fetchall()
    except sqlite3.Error as error:
        raise LocalPreviewError("SQLite schema could not be inspected safely") from error
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_safe_directory(path: Path) -> Path:
    absolute = path.absolute()
    existing_parent = absolute
    missing: list[Path] = []
    while not existing_parent.exists() and not existing_parent.is_symlink():
        missing.append(existing_parent)
        if existing_parent.parent == existing_parent:
            break
        existing_parent = existing_parent.parent
    if existing_parent.is_symlink() or not existing_parent.is_dir():
        raise LocalPreviewError("SQLite rollback state directory is unsafe")
    for candidate in reversed(missing):
        candidate.mkdir()
    cursor = absolute
    while cursor != existing_parent:
        if cursor.is_symlink() or not cursor.is_dir():
            raise LocalPreviewError("SQLite rollback state directory is unsafe")
        cursor = cursor.parent
    if absolute.is_symlink() or not absolute.is_dir():
        raise LocalPreviewError("SQLite rollback state directory is unsafe")
    return absolute.resolve()


def _data_identity(database: Path) -> str:
    return hashlib.sha256(str(database.parent.resolve()).encode("utf-8")).hexdigest()


def _read_schema_state(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise LocalPreviewError("SQLite schema state record is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LocalPreviewError("SQLite schema state record is invalid") from error
    if not isinstance(payload, dict) or set(payload) != {"contract", "schema"}:
        raise LocalPreviewError("SQLite schema state record is invalid")
    values = {key: value for key, value in payload.items() if isinstance(value, str)}
    invalid_digest = any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values.values())
    if len(values) != 2 or invalid_digest:
        raise LocalPreviewError("SQLite schema state record is invalid")
    return values


def _manifest_signature(manifest: dict[str, Any]) -> tuple[tuple[str, str, int], ...]:
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise LocalPreviewError("SQLite rollback manifest is invalid")
    signature: list[tuple[str, str, int]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise LocalPreviewError("SQLite rollback manifest is invalid")
        path = entry.get("path")
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(size, int):
            raise LocalPreviewError("SQLite rollback manifest is invalid")
        signature.append((path, digest, size))
    return tuple(sorted(signature))


def _quick_check(database: Path) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        result = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError:
        raise LocalPreviewError("SQLite rollback database failed its integrity check") from None
    finally:
        if connection is not None:
            connection.close()
    if result != [("ok",)]:
        raise LocalPreviewError("SQLite rollback database failed its integrity check")


def _normalized_sqlite_digest(database: Path, scratch: Path) -> str:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".database-check-", dir=scratch)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as source:
            with sqlite3.connect(temporary) as destination:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode = DELETE")
        _quick_check(temporary)
        return _sha256_file(temporary)
    except (OSError, sqlite3.DatabaseError):
        raise LocalPreviewError("SQLite rollback could not be matched to current data") from None
    finally:
        for artifact in (
            temporary,
            Path(f"{temporary}-journal"),
            Path(f"{temporary}-shm"),
            Path(f"{temporary}-wal"),
        ):
            artifact.unlink(missing_ok=True)


def _current_store_signature(storage: Path) -> tuple[tuple[str, str, int], ...]:
    signature: list[tuple[str, str, int]] = []
    try:
        for path in sorted(storage.rglob("*")):
            relative = path.relative_to(storage)
            if relative.parts and relative.parts[0] in _RUNTIME_STORAGE_NAMES:
                continue
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise LocalPreviewError("SQLite document store changed or became unsafe")
            if path.is_file():
                signature.append(
                    (
                        f"doc_store/{relative.as_posix()}",
                        _sha256_file(path),
                        path.stat().st_size,
                    )
                )
    except OSError:
        raise LocalPreviewError("SQLite document store changed or became unsafe") from None
    return tuple(signature)


def _snapshot_matches_current(
    snapshot: Path,
    manifest: dict[str, Any],
    database: Path,
    storage: Path,
    scratch: Path,
) -> None:
    signature = _manifest_signature(manifest)
    snapshot_store = tuple(entry for entry in signature if entry[0].startswith("doc_store/"))
    if snapshot_store != _current_store_signature(storage):
        raise LocalPreviewError("SQLite rollback could not be matched to current data")
    snapshot_digest = _normalized_sqlite_digest(snapshot / "database.sqlite", scratch)
    current_digest = _normalized_sqlite_digest(database, scratch)
    if snapshot_digest != current_digest:
        raise LocalPreviewError("SQLite rollback could not be matched to current data")


def _verify_restoreability(snapshot: Path, scratch: Path) -> None:
    restore_root = Path(tempfile.mkdtemp(prefix=".restore-check-", dir=scratch))
    try:
        restored_database = restore_root / "database.sqlite"
        restored_store = restore_root / "doc_store"
        restore_sqlite(snapshot, restored_database, restored_store)
        _quick_check(restored_database)
    except (ManifestError, OSError, LocalPreviewError):
        raise LocalPreviewError("SQLite rollback restoreability check failed") from None
    finally:
        shutil.rmtree(restore_root, ignore_errors=True)


def _verified_snapshot(snapshot: Path) -> dict[str, Any]:
    try:
        manifest = verify_manifest(snapshot)
    except (ManifestError, OSError):
        raise LocalPreviewError("SQLite rollback manifest verification failed") from None
    database = snapshot / "database.sqlite"
    if database.is_symlink() or not database.is_file():
        raise LocalPreviewError("SQLite rollback is missing database.sqlite")
    _quick_check(database)
    return manifest


def _publish_or_reuse_snapshot(
    candidate: Path,
    manifest: dict[str, Any],
    backups: Path,
    name_prefix: str,
    scratch: Path,
) -> Path:
    signature = _manifest_signature(manifest)
    for existing in backups.iterdir():
        if not existing.name.startswith(f"{name_prefix}-"):
            continue
        if existing.is_symlink() or not existing.is_dir():
            continue
        try:
            existing_manifest = _verified_snapshot(existing)
        except LocalPreviewError:
            continue
        if _manifest_signature(existing_manifest) == signature:
            _verify_restoreability(existing, scratch)
            return existing

    destination = backups / f"{name_prefix}-{uuid4().hex}"
    try:
        os.rename(candidate, destination)
    except OSError:
        raise LocalPreviewError("SQLite rollback could not be published safely") from None
    return destination


def prepare_sqlite_upgrade(database: Path, storage: Path, state_root: Path) -> str:
    """Create and verify a rollback snapshot when code/schema state may upgrade."""

    database = database.absolute()
    storage = storage.absolute()
    if not storage.is_dir() or storage.is_symlink():
        raise LocalPreviewError("SQLite document store is missing or unsafe")
    state_root = _ensure_safe_directory(state_root)
    contract = schema_contract_digest()
    schema = sqlite_schema_digest(database)
    identity = _data_identity(database)
    state_dir = _ensure_safe_directory(state_root / "schema-state")
    state_path = state_dir / f"{identity}.json"
    recorded = _read_schema_state(state_path)
    if recorded == {"contract": contract, "schema": schema}:
        return "SQLite rollback: current schema state already verified"

    backups = _ensure_safe_directory(state_root / "pre-upgrade-backups")
    try:
        staging = Path(tempfile.mkdtemp(prefix=".rollback-staging-", dir=backups))
    except OSError:
        raise LocalPreviewError("SQLite rollback staging could not be created safely") from None
    candidate = staging / "snapshot"
    try:
        try:
            snapshot_sqlite(database, storage, candidate)
        except (ManifestError, OSError, sqlite3.Error):
            raise LocalPreviewError(
                "SQLite rollback snapshot could not be created safely"
            ) from None
        manifest = _verified_snapshot(candidate)
        _snapshot_matches_current(candidate, manifest, database, storage, staging)
        _verify_restoreability(candidate, staging)
        manifest_digest = _sha256_file(candidate / "manifest.json")
        prefix = f"{identity}-{contract[:16]}-{schema[:16]}-{manifest_digest[:32]}"
        destination = _publish_or_reuse_snapshot(candidate, manifest, backups, prefix, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    manifest_digest = _sha256_file(destination / "manifest.json")
    return f"SQLite rollback: verified ({manifest_digest})"


def mark_sqlite_upgrade(database: Path, state_root: Path) -> None:
    """Record the post-start schema only after API core readiness succeeds."""

    database = database.absolute()
    state_root = _ensure_safe_directory(state_root)
    state_dir = _ensure_safe_directory(state_root / "schema-state")
    destination = state_dir / f"{_data_identity(database)}.json"
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise LocalPreviewError("SQLite schema state record is unsafe")
    payload = {
        "contract": schema_contract_digest(),
        "schema": sqlite_schema_digest(database),
    }
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.tmp-", dir=state_dir
        )
    except OSError:
        raise LocalPreviewError("SQLite schema state could not be recorded safely") from None
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError:
        raise LocalPreviewError("SQLite schema state could not be recorded safely") from None
    finally:
        temporary.unlink(missing_ok=True)


def _safe_reasons(payload: dict[str, Any]) -> tuple[str, ...]:
    values = payload.get("processing_reason_codes")
    if not isinstance(values, list):
        return ("readiness_contract_invalid",)
    model_values = payload.get("model_reason_codes", [])
    if not isinstance(model_values, list):
        return ("readiness_contract_invalid",)
    reasons = {
        value if isinstance(value, str) and value in _PROCESSING_REASON_CODES else "unreported"
        for value in [*values, *model_values]
    }
    if reasons.intersection(_MODEL_REASON_CODES):
        reasons.discard("model_unavailable")
    return tuple(sorted(reasons))


def _safe_core_reasons(payload: dict[str, Any]) -> tuple[str, ...]:
    candidates: list[object] = [payload.get("code")]
    detail = payload.get("detail")
    if isinstance(detail, dict):
        candidates.append(detail.get("reason_code"))
        values = detail.get("core_reason_codes", [])
        if isinstance(values, list):
            candidates.extend(values)
    safe = {value for value in candidates if isinstance(value, str) and value in _CORE_REASON_CODES}
    return tuple(sorted(safe))


def _core_ready(payload: dict[str, Any]) -> bool:
    return (
        payload.get("status") == "ready"
        and payload.get("demo_mode") is False
        and payload.get("intake_ready") is True
        and payload.get("review_ready") is True
    )


def _has_fresh_worker_evidence(payload: dict[str, Any]) -> bool:
    age = payload.get("worker_capability_lease_age_seconds")
    return (
        isinstance(age, (int, float))
        and not isinstance(age, bool)
        and math.isfinite(age)
        and age >= 0
        and isinstance(registry := payload.get("worker_registry_digest"), str)
        and _SHA256.fullmatch(registry) is not None
        and isinstance(capabilities := payload.get("worker_capabilities_digest"), str)
        and _SHA256.fullmatch(capabilities) is not None
    )


def readiness_message(payload: dict[str, Any], mode: str) -> tuple[int, str]:
    """Return a stable exit code/message without echoing raw readiness data."""

    if not _core_ready(payload):
        reasons = _safe_core_reasons(payload)
        if reasons:
            return 2, f"Core: unavailable ({', '.join(reasons)})"
        return 2, "Core: unavailable or unsafe (inspect /ready locally)"
    if mode == "core":
        return 0, "Core: ready for local intake and review (demo mode: false)"

    reasons = _safe_reasons(payload)
    reason_text = ", ".join(reasons) if reasons else "no reported reason"
    if payload.get("processing_ready") is True and _has_fresh_worker_evidence(payload):
        processing_code = 0
        processing_text = "Processing: ready with a fresh worker capability lease"
    elif _has_fresh_worker_evidence(payload) and not _WORKER_REASON_CODES.intersection(reasons):
        processing_code = 3
        processing_text = f"Processing: unavailable ({reason_text})"
    else:
        processing_code = 4
        processing_text = f"Processing: waiting for valid worker/model evidence ({reason_text})"
    if mode == "processing":
        return processing_code, processing_text
    return (0 if processing_code in {0, 3, 4} else processing_code), (
        "Core: ready for local intake and review (demo mode: false)\n" + processing_text
    )


def _load_readiness() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as error:
        raise LocalPreviewError("Readiness response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise LocalPreviewError("Readiness response is not an object")
    return payload


def _validated_model_name(value: object, *, source: str) -> str:
    if not isinstance(value, str) or not _SAFE_MODEL_NAME.fullmatch(value):
        raise LocalPreviewError(f"{source} model configuration is invalid")
    return value


def required_models(settings: Settings | None = None) -> tuple[str, ...]:
    """Return configured model tags only after line-safe validation."""

    try:
        active_settings = settings or Settings()
        models = active_settings.required_models
    except Exception:  # noqa: BLE001 - configuration diagnostics must stay redacted
        raise LocalPreviewError("Runtime model configuration is invalid") from None
    return tuple(_validated_model_name(model, source="Runtime") for model in models)


def ollama_models(payload: object) -> tuple[str, ...]:
    """Extract exact line-safe model names from an Ollama ``/api/tags`` response."""

    if not isinstance(payload, dict) or set(payload) != {"models"}:
        raise LocalPreviewError("Ollama model response is invalid")
    entries = payload.get("models")
    if not isinstance(entries, list):
        raise LocalPreviewError("Ollama model response is invalid")
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise LocalPreviewError("Ollama model response is invalid")
        name = entry.get("name", entry.get("model"))
        try:
            validated = _validated_model_name(name, source="Ollama")
        except LocalPreviewError:
            raise LocalPreviewError("Ollama model response is invalid") from None
        model_alias = entry.get("model")
        if model_alias is not None and model_alias != validated:
            raise LocalPreviewError("Ollama model response is invalid")
        if validated not in names:
            names.append(validated)
    return tuple(names)


def _load_ollama_models() -> tuple[str, ...]:
    try:
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        raise LocalPreviewError("Ollama model response is invalid") from None
    return ollama_models(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    readiness = commands.add_parser("readiness")
    readiness.add_argument("mode", choices=("core", "processing", "status"))
    prepare = commands.add_parser("prepare-sqlite-upgrade")
    prepare.add_argument("--database", type=Path, required=True)
    prepare.add_argument("--storage", type=Path, required=True)
    prepare.add_argument("--state-root", type=Path, required=True)
    mark = commands.add_parser("mark-sqlite-upgrade")
    mark.add_argument("--database", type=Path, required=True)
    mark.add_argument("--state-root", type=Path, required=True)
    commands.add_parser("required-models")
    commands.add_parser("ollama-models")
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        if arguments.command == "readiness":
            code, message = readiness_message(_load_readiness(), arguments.mode)
            print(message)
            raise SystemExit(code)
        if arguments.command == "prepare-sqlite-upgrade":
            print(
                prepare_sqlite_upgrade(arguments.database, arguments.storage, arguments.state_root)
            )
            return
        if arguments.command == "mark-sqlite-upgrade":
            mark_sqlite_upgrade(arguments.database, arguments.state_root)
            print("SQLite schema state: recorded after core readiness")
            return
        models = (
            required_models() if arguments.command == "required-models" else _load_ollama_models()
        )
        if models:
            print("\n".join(models))
    except LocalPreviewError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
