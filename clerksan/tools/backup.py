"""Checksum-manifest snapshots for the local SQLite demo and Compose backup scripts."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import enum
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Text, inspect, text

from clerksan.config import Settings
from clerksan.db.engine import get_engine
from clerksan.db.migrate import discover_migrations
from clerksan.ingest.storage_reconcile import async_storage_lock, reconcile_reservations
from clerksan.storage import sha256_file

MANIFEST_NAME = "manifest.json"
DATABASE_INVENTORY_NAME = "database-inventory.json"
_RUNTIME_STORAGE_NAMES = frozenset({".quarantine", ".storage.lock"})
# ``schema_migrations`` is represented separately by the inventory's migration
# manifest. Every SQLAlchemy application table must otherwise be inventoried or
# explicitly classified here.
_INVENTORY_EXCLUDED_TABLES = {
    "worker_capability_leases": (
        "transient heartbeat/capability evidence; maintenance preflight requires "
        "zero live leases and restarted workers recreate it"
    ),
}
_ENGINE_INTERNAL_TABLES = {
    "sqlite": frozenset({"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"}),
    "postgresql": frozenset(),
}
_INVENTORY_TABLES = (
    "documents",
    "document_files",
    "source_intakes",
    "upload_idempotency_reservations",
    "embedded_media",
    "schema_mappings",
    "mapping_sets",
    "mapping_set_entries",
    "extraction_batches",
    "extracted_records",
    "candidate_review_decisions",
    "verified_records",
    "audit_log",
    "jobs",
    "chunks",
    "spreadsheet_rows",
    "issuers",
    "recurring_bills",
    "duplicate_flags",
)
_TABLE_INTRODUCTIONS = {
    "0001_core_documents.sql": frozenset(
        {"documents", "document_files", "extracted_records", "verified_records"}
    ),
    "0002_audit_log.sql": frozenset({"audit_log"}),
    "0003_jobs.sql": frozenset({"jobs"}),
    "0004_chunks_pgvector.sql": frozenset({"chunks"}),
    "0005_recurring_bills.sql": frozenset({"issuers", "recurring_bills"}),
    "0006_format_staging.sql": frozenset({"embedded_media", "spreadsheet_rows"}),
    "0015_universal_intake.sql": frozenset(
        {
            "source_intakes",
            "upload_idempotency_reservations",
            "worker_capability_leases",
        }
    ),
    "0016_extraction_batches_and_mappings.sql": frozenset(
        {
            "schema_mappings",
            "mapping_sets",
            "mapping_set_entries",
            "extraction_batches",
        }
    ),
    "0017_candidate_review_decisions.sql": frozenset(
        {"candidate_review_decisions", "duplicate_flags"}
    ),
}


class ManifestError(RuntimeError):
    """A backup manifest is missing, malformed, or does not match its files."""


class MaintenancePreflightError(RuntimeError):
    """The stopped Compose deployment is not safe to snapshot yet."""


class DatabaseInventoryError(RuntimeError):
    """A sanitized logical database/store inventory could not be built or matched."""


@dataclass(frozen=True, slots=True)
class MaintenancePreflightResult:
    """Non-sensitive evidence that the maintenance fence is closed."""

    references: int
    reconciled: int


@dataclass(frozen=True, slots=True)
class _InventoryTableMetadata:
    primary_keys: tuple[str, ...]
    columns: tuple[str, ...]
    non_nullable: frozenset[str]
    text_columns: frozenset[str]
    datetime_columns: frozenset[str]
    timezone_columns: frozenset[str]
    defaults: tuple[tuple[str, str], ...]


_SAFE_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_MIGRATION_FILENAME = re.compile(r"\A[0-9]{4}_[A-Za-z0-9][A-Za-z0-9_.-]*\.sql\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_RESTORE_PREVIOUS_ENTRY = re.compile(r"\A\.restore-previous-[1-9][0-9]*\Z")


def build_manifest(root: Path) -> dict[str, Any]:
    """Describe every regular file under ``root`` using safe relative paths and digests."""

    _reject_regular_file_tree(root, "backup source")
    root = root.resolve()
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path == root / MANIFEST_NAME:
            continue
        relative = path.relative_to(root).as_posix()
        files.append({"path": relative, "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return {"format": 1, "files": files}


def write_manifest(root: Path, *, storage_root: Path | None = None) -> Path:
    _reject_symlink_input(root, "backup destination")
    root.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(root)
    if storage_root is not None:
        manifest["storage_root"] = str(storage_root.resolve())
    path = root / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return path


def verify_manifest(root: Path) -> dict[str, Any]:
    """Verify exact file set, size, and checksum before any restore operation."""

    _reject_regular_file_tree(root, "backup source")
    root = root.resolve()
    path = root / MANIFEST_NAME
    if path.is_symlink():
        raise ManifestError(f"backup contains a symbolic link: {MANIFEST_NAME}")
    if not path.is_file():
        raise ManifestError(f"missing {MANIFEST_NAME}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"invalid backup manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise ManifestError("unsupported backup manifest format")
    entries = manifest.get("files")
    if manifest.get("format") != 1 or not isinstance(entries, list):
        raise ManifestError("unsupported backup manifest format")
    storage_root = manifest.get("storage_root")
    if storage_root is not None and not isinstance(storage_root, str):
        raise ManifestError("invalid backup storage_root")

    expected: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ManifestError("manifest file entry is not an object")
        relative = entry.get("path")
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if not isinstance(relative, str) or not _safe_relative_path(relative):
            raise ManifestError(f"unsafe manifest path: {relative!r}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ManifestError(f"invalid checksum for {relative}")
        if not isinstance(size, int) or size < 0:
            raise ManifestError(f"invalid byte count for {relative}")
        if relative in expected:
            raise ManifestError(f"duplicate manifest path: {relative}")
        expected.add(relative)
        source = root / relative
        if _has_symlink_component(root, Path(relative)):
            raise ManifestError(f"backup contains a symbolic link: {relative}")
        if not source.is_file():
            raise ManifestError(f"missing backed-up file: {relative}")
        if source.stat().st_size != size:
            raise ManifestError(f"size mismatch for {relative}")
        if sha256_file(source) != digest:
            raise ManifestError(f"checksum mismatch for {relative}")

    items = list(root.rglob("*"))
    actual = {
        item.relative_to(root).as_posix()
        for item in items
        if item.is_file() and item != root / MANIFEST_NAME
    }
    if actual != expected:
        raise ManifestError("backup contains files not represented by its manifest")
    return manifest


def snapshot_sqlite(database: Path, storage_dir: Path, destination: Path) -> Path:
    """Create a non-destructive, checksum-verified local-demo backup."""

    _reject_symlink_input(database, "SQLite database")
    _reject_symlink_input(storage_dir, "SQLite storage directory")
    _reject_symlink_input(destination, "backup destination")
    database = database.resolve()
    storage_dir = storage_dir.resolve()
    destination = destination.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {database}")
    _reject_regular_file_tree(storage_dir, "SQLite backup source")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"backup destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    _snapshot_sqlite_database(database, destination / "database.sqlite")
    if storage_dir.exists():
        _copy_storage_tree(storage_dir, destination / "doc_store")
    return write_manifest(destination, storage_root=storage_dir)


def _copy_storage_tree(source: Path, destination: Path) -> None:
    """Copy durable store content while omitting lock/quarantine runtime state."""

    source = source.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        if Path(directory).resolve() != source:
            return set()
        return set(names).intersection(_RUNTIME_STORAGE_NAMES)

    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)


async def maintenance_preflight(
    settings: Settings | None = None,
    *,
    wait_seconds: float = 0,
    poll_seconds: float = 0.25,
) -> MaintenancePreflightResult:
    """Prove stopped writers left no live lease or quarantine reservation."""

    active_settings = settings or Settings()
    if wait_seconds < 0:
        raise ValueError("maintenance wait must not be negative")
    if poll_seconds <= 0:
        raise ValueError("maintenance poll interval must be positive")
    deadline = time.monotonic() + wait_seconds
    configured_root = active_settings.storage_dir
    if configured_root.is_symlink() or (configured_root.exists() and not configured_root.is_dir()):
        raise MaintenancePreflightError("document store is not a safe directory")
    root = configured_root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    async with async_storage_lock(root, shared=False):
        while True:
            active_jobs, fresh_workers = await _active_lease_counts(active_settings)
            if active_jobs == 0 and fresh_workers == 0:
                break
            if time.monotonic() >= deadline:
                raise MaintenancePreflightError(
                    "maintenance fence still has active job or worker leases"
                )
            await asyncio.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

        engine = get_engine(active_settings)
        async with engine.connect() as connection:
            async with connection.begin():
                tables = await connection.run_sync(_table_names)
                references = (
                    {
                        str(value)
                        for value in await connection.scalars(
                            text("SELECT sha256 FROM document_files")
                        )
                    }
                    if "document_files" in tables
                    else set()
                )
                active_jobs, fresh_workers = await _lease_counts(connection, tables)
                if active_jobs or fresh_workers:
                    raise MaintenancePreflightError(
                        "maintenance fence changed while leases were being checked"
                    )
                report = reconcile_reservations(
                    root,
                    0,
                    references.__contains__,
                    lock_held=True,
                )

        if report.errors or report.retained_active:
            raise MaintenancePreflightError(
                "storage reconciliation retained active or invalid reservations"
            )
        if _quarantine_entry_count(root):
            raise MaintenancePreflightError("storage quarantine is not empty after reconciliation")
        return MaintenancePreflightResult(
            references=len(references),
            reconciled=report.finalized,
        )


async def _active_lease_counts(settings: Settings) -> tuple[int, int]:
    engine = get_engine(settings)
    async with engine.connect() as connection:
        tables = await connection.run_sync(_table_names)
        return await _lease_counts(connection, tables)


def _table_names(connection: Any) -> frozenset[str]:
    return frozenset(str(name) for name in inspect(connection).get_table_names())


async def _lease_counts(connection: Any, tables: frozenset[str]) -> tuple[int, int]:
    jobs = (
        int(
            await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM jobs "
                    "WHERE status = 'running' AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at > CURRENT_TIMESTAMP"
                )
            )
            or 0
        )
        if "jobs" in tables
        else 0
    )
    workers = (
        int(
            await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM worker_capability_leases "
                    "WHERE expires_at > CURRENT_TIMESTAMP"
                )
            )
            or 0
        )
        if "worker_capability_leases" in tables
        else 0
    )
    return jobs, workers


def _quarantine_entry_count(root: Path) -> int:
    quarantine = root / ".quarantine"
    if not quarantine.exists():
        return 0
    return sum(1 for path in quarantine.rglob("*") if path.is_symlink() or not path.is_dir())


async def build_database_inventory(
    settings: Settings | None = None,
    *,
    excluded_restore_entry: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic logical inventory without emitting user data values."""

    active_settings = settings or Settings()
    try:
        engine = get_engine(active_settings)
        async with engine.connect() as connection:
            table_metadata = await connection.run_sync(_inventory_table_metadata)
            dialect_name = connection.dialect.name
            _validate_inventory_structure(table_metadata, dialect_name=dialect_name)
            migrations = await _migration_inventory(connection, table_metadata)
            _validate_inventory_generation(
                table_metadata,
                migrations,
                dialect_name=dialect_name,
            )
            table_inventory: list[dict[str, Any]] = []
            for table_name in _INVENTORY_TABLES:
                metadata = table_metadata.get(table_name)
                if metadata is None:
                    table_inventory.append(
                        {
                            "table": table_name,
                            "present": False,
                            "rows": 0,
                            "identity_sha256": None,
                        }
                    )
                    continue
                if not metadata.primary_keys or not metadata.columns:
                    raise DatabaseInventoryError(
                        "database inventory table has no deterministic identity"
                    )
                columns = ", ".join(_quote_identifier(column) for column in metadata.columns)
                order = ", ".join(_quote_identifier(column) for column in metadata.primary_keys)
                rows = (
                    await connection.execute(
                        text(
                            f"SELECT {columns} FROM {_quote_identifier(table_name)} "
                            f"ORDER BY {order}"
                        )
                    )
                ).all()
                primary_key_positions = tuple(
                    metadata.columns.index(column) for column in metadata.primary_keys
                )
                identities = [
                    [_stable_value(row[position]) for position in primary_key_positions]
                    for row in rows
                ]
                contents = [[_stable_value(value) for value in row] for row in rows]
                table_inventory.append(
                    {
                        "table": table_name,
                        "present": True,
                        "rows": len(rows),
                        "identity_sha256": _canonical_digest(identities),
                        "content_sha256": _canonical_digest(contents),
                    }
                )

            schema_objects = await connection.run_sync(_schema_object_inventory)

            artifacts = await _artifact_inventory(
                connection,
                active_settings.storage_dir.resolve(),
                table_metadata,
            )
        storage = _storage_inventory(
            active_settings.storage_dir.resolve(),
            excluded_restore_entry=excluded_restore_entry,
        )
    except DatabaseInventoryError:
        raise
    except Exception:  # noqa: BLE001 - suppress private paths/values from diagnostics
        raise DatabaseInventoryError("could not build database inventory") from None

    return {
        "format": 1,
        "migrations": migrations,
        "schema_objects": schema_objects,
        "tables": table_inventory,
        "artifacts": artifacts,
        "storage": storage,
    }


def _inventory_table_metadata(connection: Any) -> dict[str, _InventoryTableMetadata]:
    inspector = inspect(connection)
    result: dict[str, _InventoryTableMetadata] = {}
    for table_name in inspector.get_table_names():
        primary_key = inspector.get_pk_constraint(table_name).get("constrained_columns") or []
        reflected_columns = inspector.get_columns(table_name)
        columns = tuple(str(column["name"]) for column in reflected_columns)
        result[table_name] = _InventoryTableMetadata(
            primary_keys=tuple(str(column) for column in primary_key),
            columns=columns,
            non_nullable=frozenset(
                str(column["name"])
                for column in reflected_columns
                if column.get("nullable") is False
            ),
            text_columns=frozenset(
                str(column["name"])
                for column in reflected_columns
                if isinstance(column.get("type"), Text)
            ),
            datetime_columns=frozenset(
                str(column["name"])
                for column in reflected_columns
                if isinstance(column.get("type"), DateTime)
            ),
            timezone_columns=frozenset(
                str(column["name"])
                for column in reflected_columns
                if getattr(column.get("type"), "timezone", False) is True
            ),
            defaults=tuple(
                (str(column["name"]), str(column["default"]).strip())
                for column in reflected_columns
                if column.get("default") is not None
            ),
        )
    return result


def _validate_inventory_structure(
    table_metadata: dict[str, _InventoryTableMetadata], *, dialect_name: str
) -> None:
    engine_tables = _ENGINE_INTERNAL_TABLES.get(dialect_name)
    if engine_tables is None:
        raise DatabaseInventoryError("database inventory dialect is unsupported")
    allowed = (
        set(_INVENTORY_TABLES)
        | set(_INVENTORY_EXCLUDED_TABLES)
        | {"schema_migrations"}
        | set(engine_tables)
    )
    if set(table_metadata).difference(allowed):
        raise DatabaseInventoryError("database has an unclassified database table")

    migration_metadata = table_metadata.get("schema_migrations")
    if migration_metadata is None:
        if dialect_name == "postgresql":
            raise DatabaseInventoryError("database migration ledger is missing")
        return
    defaults = dict(migration_metadata.defaults)
    applied_at_default = re.sub(r"\s+", "", defaults.get("applied_at", "")).lower()
    type_shape_valid = (
        {"filename", "checksum", "applied_at"}.issubset(migration_metadata.text_columns)
        if dialect_name == "sqlite"
        else (
            {"filename", "checksum"}.issubset(migration_metadata.text_columns)
            and "applied_at" in migration_metadata.datetime_columns
            and "applied_at" in migration_metadata.timezone_columns
        )
    )
    default_valid = (
        applied_at_default in {"current_timestamp", "(current_timestamp)"}
        if dialect_name == "sqlite"
        else applied_at_default in {"now()", "current_timestamp", "(current_timestamp)"}
    )
    if (
        set(migration_metadata.columns) != {"filename", "checksum", "applied_at"}
        or migration_metadata.primary_keys != ("filename",)
        or not {"checksum", "applied_at"}.issubset(migration_metadata.non_nullable)
        or set(defaults) != {"applied_at"}
        or not type_shape_valid
        or not default_valid
    ):
        raise DatabaseInventoryError("database migration ledger is malformed")


def _validate_inventory_generation(
    table_metadata: dict[str, _InventoryTableMetadata],
    migrations: list[dict[str, str]],
    *,
    dialect_name: str,
) -> None:
    # SQLite Base/create_all and historical local-demo databases intentionally have
    # no migration ledger. Once a ledger exists, its exact applied prefix owns the
    # physical application-table generation on both engines.
    if not migrations:
        return
    expected = {"schema_migrations"}
    for migration in migrations:
        expected.update(_TABLE_INTRODUCTIONS.get(migration["filename"], ()))
    engine_tables = _ENGINE_INTERNAL_TABLES[dialect_name]
    actual = set(table_metadata).difference(engine_tables)
    if actual != expected:
        raise DatabaseInventoryError("database tables do not match the migration ledger")


async def _migration_inventory(
    connection: Any,
    table_metadata: dict[str, _InventoryTableMetadata],
) -> list[dict[str, str]]:
    if "schema_migrations" not in table_metadata:
        return []
    rows = (
        await connection.execute(
            text("SELECT filename, checksum FROM schema_migrations ORDER BY filename")
        )
    ).all()
    migrations: list[dict[str, str]] = []
    for filename, checksum in rows:
        if (
            not isinstance(filename, str)
            or _MIGRATION_FILENAME.fullmatch(filename) is None
            or not isinstance(checksum, str)
            or _SHA256.fullmatch(checksum) is None
        ):
            raise DatabaseInventoryError("database migration ledger is malformed")
        migrations.append({"filename": filename, "checksum": checksum})
    expected = [
        {
            "filename": path.name,
            "checksum": hashlib.sha256(
                path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
        }
        for path in discover_migrations()
    ]
    if not migrations or migrations != expected[: len(migrations)]:
        raise DatabaseInventoryError("database migration ledger is malformed")
    return migrations


def _schema_object_inventory(connection: Any) -> dict[str, Any]:
    dialect_name = connection.dialect.name
    if dialect_name == "sqlite":
        definitions = _sqlite_schema_object_definitions(connection)
    elif dialect_name == "postgresql":
        definitions = _postgresql_schema_object_definitions(connection)
    else:
        raise DatabaseInventoryError("database inventory dialect is unsupported")
    definitions.sort(key=lambda definition: _canonical_json(definition))
    return {
        "count": len(definitions),
        "sha256": _canonical_digest(definitions),
    }


def _sqlite_schema_object_definitions(connection: Any) -> list[list[object]]:
    rows = connection.execute(
        text(
            "/* inventory:sqlite-schema */ "
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view') "
            "AND substr(name, 1, 7) <> 'sqlite_' AND sql IS NOT NULL "
            "ORDER BY type, name, tbl_name"
        )
    ).all()
    return [[_stable_value(value) for value in row] for row in rows]


def _postgresql_schema_object_definitions(connection: Any) -> list[list[object]]:
    queries = (
        (
            "trigger",
            """/* inventory:postgresql-trigger */
            SELECT namespace.nspname, relation.relname, trigger_metadata.tgname,
                   trigger_metadata.tgenabled,
                   pg_get_triggerdef(trigger_metadata.oid, true)
              FROM pg_trigger AS trigger_metadata
              JOIN pg_class AS relation ON relation.oid = trigger_metadata.tgrelid
              JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
             WHERE namespace.nspname = 'public'
               AND NOT trigger_metadata.tgisinternal
             ORDER BY namespace.nspname, relation.relname, trigger_metadata.tgname
            """,
        ),
        (
            "index",
            """/* inventory:postgresql-index */
            SELECT namespace.nspname, relation.relname, index_relation.relname,
                   index_metadata.indisvalid, index_metadata.indisready,
                   index_metadata.indislive,
                   pg_get_indexdef(index_relation.oid, 0, false)
              FROM pg_index AS index_metadata
              JOIN pg_class AS relation ON relation.oid = index_metadata.indrelid
              JOIN pg_class AS index_relation
                ON index_relation.oid = index_metadata.indexrelid
              JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
             WHERE namespace.nspname = 'public'
             ORDER BY namespace.nspname, relation.relname, index_relation.relname
            """,
        ),
        (
            "constraint",
            """/* inventory:postgresql-constraint */
            SELECT namespace.nspname, relation.relname, constraint_metadata.conname,
                   constraint_metadata.convalidated,
                   pg_get_constraintdef(constraint_metadata.oid, true)
              FROM pg_constraint AS constraint_metadata
              JOIN pg_class AS relation ON relation.oid = constraint_metadata.conrelid
              JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
             WHERE namespace.nspname = 'public'
             ORDER BY namespace.nspname, relation.relname, constraint_metadata.conname
            """,
        ),
        (
            "function",
            """/* inventory:postgresql-function */
            SELECT function_namespace.nspname, function_metadata.proname,
                   pg_get_function_identity_arguments(function_metadata.oid),
                   pg_get_functiondef(function_metadata.oid)
              FROM pg_proc AS function_metadata
              JOIN pg_namespace AS function_namespace
                ON function_namespace.oid = function_metadata.pronamespace
             WHERE function_namespace.nspname = 'public'
               AND function_metadata.prokind IN ('f', 'p')
               AND NOT EXISTS (
                    SELECT 1
                      FROM pg_depend AS dependency
                     WHERE dependency.classid = 'pg_proc'::regclass
                       AND dependency.objid = function_metadata.oid
                       AND dependency.deptype = 'e'
               )
             ORDER BY function_namespace.nspname, function_metadata.proname,
                      pg_get_function_identity_arguments(function_metadata.oid)
            """,
        ),
    )
    definitions: list[list[object]] = []
    for object_kind, query in queries:
        for row in connection.execute(text(query)).all():
            definitions.append([object_kind, *(_stable_value(value) for value in row)])
    return definitions


async def _artifact_inventory(
    connection: Any,
    storage_root: Path,
    table_metadata: dict[str, _InventoryTableMetadata],
) -> dict[str, Any]:
    identities: list[list[str]] = []
    for table_name in ("document_files", "embedded_media"):
        if table_name not in table_metadata:
            continue
        rows = (
            await connection.execute(
                text(f"SELECT id, sha256, content_path FROM {table_name} ORDER BY id")
            )
        ).all()
        for row_id, digest, raw_path in rows:
            path = _storage_artifact_path(storage_root, raw_path)
            try:
                if sha256_file(path) != str(digest):
                    raise DatabaseInventoryError("database inventory artifact checksum mismatch")
            except (OSError, ValueError) as error:
                raise DatabaseInventoryError(
                    "database inventory artifact verification failed"
                ) from error
            identities.append([table_name, _stable_value(row_id), str(digest)])
    return {
        "rows": len(identities),
        "identity_sha256": _canonical_digest(identities),
    }


def _storage_artifact_path(storage_root: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise DatabaseInventoryError("database inventory has an invalid artifact reference")
    value = Path(raw_path)
    candidate = value.resolve() if value.is_absolute() else (storage_root / value).resolve()
    try:
        candidate.relative_to(storage_root)
    except ValueError as error:
        raise DatabaseInventoryError(
            "database inventory artifact reference escapes storage"
        ) from error
    if not candidate.is_file() or candidate.is_symlink():
        raise DatabaseInventoryError("database inventory artifact is unavailable")
    return candidate


def _storage_inventory(
    root: Path,
    *,
    excluded_restore_entry: str | None = None,
) -> dict[str, Any]:
    excluded = _validated_restore_exclusion(root, excluded_restore_entry)
    _reject_storage_inventory_tree(root, excluded)
    entries: list[list[object]] = []
    if root.exists():
        for path in _storage_inventory_paths(root, excluded):
            relative = path.relative_to(root)
            if relative.parts[0] in _RUNTIME_STORAGE_NAMES or path.is_dir():
                continue
            entries.append([relative.as_posix(), sha256_file(path), path.stat().st_size])
    return {
        "files": len(entries),
        "bytes": sum(int(entry[2]) for entry in entries),
        "identity_sha256": _canonical_digest(entries),
    }


def _storage_inventory_paths(root: Path, excluded: str | None) -> list[Path]:
    paths: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.name == excluded:
            continue
        paths.append(child)
        if child.is_dir():
            paths.extend(sorted(child.rglob("*")))
    return paths


def _validated_restore_exclusion(root: Path, value: str | None) -> str | None:
    if value is None:
        return None
    if (
        _RESTORE_PREVIOUS_ENTRY.fullmatch(value) is None
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise DatabaseInventoryError("database inventory restore exclusion is invalid")
    candidate = root / value
    state_file = candidate / ".restore-state"
    try:
        valid = (
            not candidate.is_symlink()
            and candidate.is_dir()
            and not state_file.is_symlink()
            and state_file.is_file()
            and state_file.read_bytes() == b"replacement-active\n"
        )
    except OSError:
        valid = False
    if not valid:
        raise DatabaseInventoryError("database inventory restore exclusion is invalid")
    return value


def _reject_storage_inventory_tree(root: Path, excluded: str | None) -> None:
    _reject_symlink_input(root, "document store")
    if not root.exists():
        if excluded is not None:
            raise DatabaseInventoryError("database inventory restore exclusion is invalid")
        return
    if not root.is_dir():
        raise ManifestError(f"document store must be a directory: {root}")
    for child in root.iterdir():
        if child.name == excluded:
            continue
        if child.is_symlink():
            raise ManifestError(f"document store contains a symbolic link: {child}")
        if child.is_dir():
            _reject_regular_file_tree(child, "document store")
        elif not child.is_file():
            raise ManifestError(f"document store contains an unsupported filesystem entry: {child}")


def verify_database_inventory(expected_path: Path, actual: dict[str, Any]) -> None:
    """Compare an on-disk backup inventory without exposing mismatch values."""

    try:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DatabaseInventoryError("backup database inventory is invalid") from None
    expected_encoded = _canonical_json(expected)
    actual_encoded = _canonical_json(actual)
    if not hmac.compare_digest(expected_encoded, actual_encoded):
        raise DatabaseInventoryError("restored database/store inventory does not match backup")


def _quote_identifier(value: str) -> str:
    if _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise DatabaseInventoryError("database inventory has an unsafe identifier")
    return f'"{value}"'


def _stable_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"float": repr(value)}
        return value
    if isinstance(value, Decimal):
        return {"decimal": format(value, "f")}
    if isinstance(value, dt.datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
        return {"datetime": normalized.astimezone(dt.UTC).isoformat()}
    if isinstance(value, (dt.date, dt.time)):
        return {type(value).__name__: value.isoformat()}
    if isinstance(value, UUID):
        return {"uuid": str(value)}
    if isinstance(value, enum.Enum):
        return {"enum": _stable_value(value.value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        return {
            "bytes": hashlib.sha256(payload).hexdigest(),
            "length": len(payload),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    return {"scalar": str(value)}


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _snapshot_sqlite_database(source: Path, destination: Path) -> None:
    """Copy a SQLite snapshot through SQLite so committed WAL frames are included."""

    staged = destination.with_name(f".{destination.name}.snapshot-{uuid4().hex}")
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_connection:
            with sqlite3.connect(staged) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.execute("PRAGMA journal_mode = DELETE")
        os.replace(staged, destination)
    finally:
        for artifact in (
            staged,
            Path(f"{staged}-journal"),
            Path(f"{staged}-shm"),
            Path(f"{staged}-wal"),
        ):
            artifact.unlink(missing_ok=True)


def restore_sqlite(source: Path, database: Path, storage_dir: Path) -> None:
    """Restore a verified local-demo snapshot with portable artifact references.

    The entire replacement is staged before either live target is touched. Absolute
    artifact paths from older snapshots are rebased to the requested store when the
    manifest records their original root; new snapshots keep portable relative paths.
    """

    manifest = verify_manifest(source)
    _reject_symlink_input(database, "SQLite database")
    _reject_symlink_input(storage_dir, "SQLite storage directory")
    source = source.resolve()
    database_source = source / "database.sqlite"
    if not database_source.is_file():
        raise ManifestError("backup does not contain database.sqlite")
    database = database.resolve()
    storage_dir = storage_dir.resolve()
    rewrites = _sqlite_path_rewrites(
        database_source,
        source_root=_manifest_storage_root(manifest),
        destination_root=storage_dir,
    )

    database.parent.mkdir(parents=True, exist_ok=True)
    storage_dir.parent.mkdir(parents=True, exist_ok=True)
    staged_database = database.parent / f".{database.name}.restore-{uuid4().hex}"
    staged_store_parent = Path(
        tempfile.mkdtemp(prefix=f".{storage_dir.name}.restore-", dir=storage_dir.parent)
    )
    staged_store = staged_store_parent / storage_dir.name
    source_store = source / "doc_store"
    try:
        shutil.copy2(database_source, staged_database)
        _apply_sqlite_path_rewrites(staged_database, rewrites)
        _validate_sqlite_staging_database(staged_database)
        if source_store.exists():
            shutil.copytree(source_store, staged_store)
        else:
            staged_store.mkdir()
        _replace_restore_targets(staged_database, database, staged_store, storage_dir)
    finally:
        staged_database.unlink(missing_ok=True)
        shutil.rmtree(staged_store_parent, ignore_errors=True)


def _manifest_storage_root(manifest: dict[str, Any]) -> Path | None:
    root = manifest.get("storage_root")
    return Path(root).resolve() if isinstance(root, str) and root else None


def _sqlite_path_rewrites(
    database: Path, *, source_root: Path | None, destination_root: Path
) -> list[tuple[str, int, str]]:
    """Plan safe absolute-to-relative artifact rewrites without mutating a backup."""

    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        raise ManifestError("could not inspect SQLite backup artifact paths") from None
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        rewrites: list[tuple[str, int, str]] = []
        for table in ("document_files", "embedded_media"):
            if table not in tables:
                continue
            for row_id, raw_path in connection.execute(f"SELECT rowid, content_path FROM {table}"):
                if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                    continue
                resolved = Path(raw_path).resolve()
                roots = tuple(root for root in (source_root, destination_root) if root is not None)
                relative = next(
                    (resolved.relative_to(root) for root in roots if _is_within(resolved, root)),
                    None,
                )
                if relative is None:
                    raise ManifestError(
                        f"artifact path in backup cannot be safely rebased: {raw_path}"
                    )
                rewrites.append((table, int(row_id), relative.as_posix()))
        return rewrites
    except sqlite3.DatabaseError:
        raise ManifestError("could not inspect SQLite backup artifact paths") from None
    finally:
        connection.close()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _apply_sqlite_path_rewrites(database: Path, rewrites: list[tuple[str, int, str]]) -> None:
    if not rewrites:
        return
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        append_only_trigger_sql: str | None = None
        if any(table == "document_files" for table, _, _ in rewrites):
            trigger_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = ? AND tbl_name = 'document_files'",
                ("document_files_append_only_sqlite_update",),
            ).fetchone()
            if trigger_row is not None:
                append_only_trigger_sql = trigger_row[0]
                if not isinstance(append_only_trigger_sql, str) or not append_only_trigger_sql:
                    raise ManifestError(
                        "could not preserve the document-file append-only trigger during restore"
                    )
                connection.execute("DROP TRIGGER document_files_append_only_sqlite_update")
        for table, row_id, relative in rewrites:
            cursor = connection.execute(
                f"UPDATE {table} SET content_path = ? WHERE rowid = ?", (relative, row_id)
            )
            if cursor.rowcount != 1:
                raise ManifestError("could not locate an artifact path selected for restore")
        if append_only_trigger_sql is not None:
            connection.execute(append_only_trigger_sql)
        connection.commit()
    except (ManifestError, sqlite3.DatabaseError) as error:
        connection.rollback()
        if isinstance(error, ManifestError):
            raise
        raise ManifestError(f"could not rebase restored artifact paths: {error}") from error
    finally:
        connection.close()


def _validate_sqlite_staging_database(database: Path) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        result = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError:
        raise ManifestError("could not inspect SQLite staging database") from None
    finally:
        if connection is not None:
            connection.close()
    if result != [("ok",)]:
        raise ManifestError("SQLite staging database failed its integrity check")


def _replace_restore_targets(
    staged_database: Path,
    database: Path,
    staged_store: Path,
    storage_dir: Path,
) -> None:
    """Install staged targets and roll both back if a replacement fails."""

    old_database = (
        database.parent / f".{database.name}.before-restore-{uuid4().hex}"
        if database.exists()
        else None
    )
    old_store = (
        storage_dir.parent / f".{storage_dir.name}.before-restore-{uuid4().hex}"
        if storage_dir.exists()
        else None
    )
    database_installed = False
    store_installed = False
    try:
        if old_database is not None:
            os.replace(database, old_database)
        if old_store is not None:
            os.replace(storage_dir, old_store)
        os.replace(staged_database, database)
        database_installed = True
        os.replace(staged_store, storage_dir)
        store_installed = True
    except OSError:
        if database_installed:
            database.unlink(missing_ok=True)
        if store_installed and storage_dir.exists():
            shutil.rmtree(storage_dir)
        if old_database is not None and old_database.exists():
            os.replace(old_database, database)
        if old_store is not None and old_store.exists():
            os.replace(old_store, storage_dir)
        raise
    else:
        if old_database is not None:
            old_database.unlink(missing_ok=True)
        if old_store is not None:
            shutil.rmtree(old_store, ignore_errors=True)


def _safe_relative_path(value: str) -> bool:
    path = Path(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and value == path.as_posix()
    )


def _reject_symlink_input(path: Path, label: str) -> None:
    """Reject a supplied symlink before a later ``resolve`` could conceal it."""

    if path.is_symlink():
        raise ManifestError(f"{label} must not be a symbolic link: {path}")


def _reject_regular_file_tree(root: Path, label: str) -> None:
    """Reject links and non-regular filesystem nodes before copy or manifest hashing."""

    _reject_symlink_input(root, label)
    if not root.exists():
        return
    if not root.is_dir():
        raise ManifestError(f"{label} must be a directory: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ManifestError(f"{label} contains a symbolic link: {path}")
        if not path.is_dir() and not path.is_file():
            raise ManifestError(f"{label} contains an unsupported filesystem entry: {path}")


def _has_symlink_component(root: Path, relative: Path) -> bool:
    """Return whether a manifest path reaches a link under the verified root."""

    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    manifest = subcommands.add_parser("manifest")
    manifest.add_argument("root", type=Path)
    verify = subcommands.add_parser("verify")
    verify.add_argument("root", type=Path)
    snapshot = subcommands.add_parser("snapshot-sqlite")
    snapshot.add_argument("database", type=Path)
    snapshot.add_argument("storage_dir", type=Path)
    snapshot.add_argument("destination", type=Path)
    restore = subcommands.add_parser("restore-sqlite")
    restore.add_argument("source", type=Path)
    restore.add_argument("database", type=Path)
    restore.add_argument("storage_dir", type=Path)
    restore.add_argument(
        "--confirm",
        metavar="ERASE_LOCAL_DATA",
        help=(
            "required acknowledgement because restore replaces the target SQLite database and store"
        ),
    )
    preflight = subcommands.add_parser("maintenance-preflight")
    preflight.add_argument("--wait-seconds", type=float, default=0)
    inventory = subcommands.add_parser("database-inventory")
    inventory.add_argument("--verify", type=Path)
    inventory.add_argument(
        "--excluded-restore-entry",
        help=("exact operation-owned prior-store directory to omit after restore activation"),
    )
    arguments = parser.parse_args()

    try:
        if arguments.command == "manifest":
            print(write_manifest(arguments.root))
        elif arguments.command == "verify":
            verify_manifest(arguments.root)
            print("manifest verified")
        elif arguments.command == "snapshot-sqlite":
            print(snapshot_sqlite(arguments.database, arguments.storage_dir, arguments.destination))
        elif arguments.command == "restore-sqlite":
            if arguments.confirm != "ERASE_LOCAL_DATA":
                parser.error("restore-sqlite requires --confirm ERASE_LOCAL_DATA")
            restore_sqlite(arguments.source, arguments.database, arguments.storage_dir)
            print("SQLite snapshot restored")
        elif arguments.command == "maintenance-preflight":
            asyncio.run(maintenance_preflight(wait_seconds=arguments.wait_seconds))
            print("maintenance preflight passed")
        else:
            actual = asyncio.run(
                build_database_inventory(
                    excluded_restore_entry=arguments.excluded_restore_entry,
                )
            )
            if arguments.verify is not None:
                verify_database_inventory(arguments.verify, actual)
                print("database inventory verified")
            else:
                print(json.dumps(actual, indent=2, sort_keys=True))
    except (DatabaseInventoryError, MaintenancePreflightError, ManifestError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
