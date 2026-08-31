from __future__ import annotations

import asyncio
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from clerksan.config import Settings
from clerksan.db.engine import dispose_engines, get_engine
from clerksan.db.models import Base
from clerksan.ingest.storage_reconcile import reserve_quarantine
from clerksan.tools import backup
from clerksan.tools.backup import (
    DatabaseInventoryError,
    MaintenancePreflightError,
    ManifestError,
    restore_sqlite,
    snapshot_sqlite,
    verify_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = ROOT / "scripts" / "backup.sh"
RESTORE_SCRIPT = ROOT / "scripts" / "restore.sh"
_NEWLY_COVERED_INVENTORY_TABLES = (
    "upload_idempotency_reservations",
    "issuers",
    "duplicate_flags",
)


def test_sqlite_snapshot_restore_preserves_database_store_and_manifest(tmp_path: Path) -> None:
    database = tmp_path / "live" / "clerksan.sqlite"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value BLOB NOT NULL)")
        connection.execute("INSERT INTO state (value) VALUES (?)", (b"database-state",))
    storage = tmp_path / "live" / "doc_store"
    (storage / "originals").mkdir(parents=True)
    (storage / "originals" / "receipt.png").write_bytes(b"original-bytes")
    backup = tmp_path / "backup"

    snapshot_sqlite(database, storage, backup)
    assert verify_manifest(backup)["format"] == 1

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE state SET value = ?", (b"mutated",))
    (storage / "originals" / "receipt.png").write_bytes(b"changed")
    restore_sqlite(backup, database, storage)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM state").fetchall() == [(b"database-state",)]
    assert (storage / "originals" / "receipt.png").read_bytes() == b"original-bytes"


def test_sqlite_snapshot_excludes_quarantine_and_storage_lock(tmp_path: Path) -> None:
    database = tmp_path / "live" / "clerksan.sqlite"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
    storage = tmp_path / "live" / "doc_store"
    (storage / "originals").mkdir(parents=True)
    (storage / "originals" / "kept.bin").write_bytes(b"kept")
    reservation = reserve_quarantine(storage, reservation_id="temporary", now_ns=0)
    reservation.payload_path.write_bytes(b"temporary")
    (storage / ".storage.lock").write_bytes(b"")

    destination = tmp_path / "backup"
    snapshot_sqlite(database, storage, destination)

    assert (destination / "doc_store" / "originals" / "kept.bin").is_file()
    assert not (destination / "doc_store" / ".quarantine").exists()
    assert not (destination / "doc_store" / ".storage.lock").exists()
    verify_manifest(destination)


def test_sqlite_snapshot_includes_committed_wal_frames(tmp_path: Path) -> None:
    database = tmp_path / "live" / "clerksan.sqlite"
    database.parent.mkdir()
    storage = tmp_path / "live" / "doc_store"
    storage.mkdir()
    backup_dir = tmp_path / "backup"

    with sqlite3.connect(database) as live_connection:
        assert live_connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        live_connection.execute("PRAGMA wal_autocheckpoint = 0")
        live_connection.execute("CREATE TABLE receipts (id INTEGER PRIMARY KEY, amount INTEGER)")
        live_connection.execute("INSERT INTO receipts (amount) VALUES (2752)")
        live_connection.commit()
        assert database.with_name(f"{database.name}-wal").is_file()

        raw_copy = tmp_path / "raw-main-database.sqlite"
        raw_copy.write_bytes(database.read_bytes())
        with sqlite3.connect(raw_copy) as raw_connection:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                raw_connection.execute("SELECT amount FROM receipts").fetchall()

        snapshot_sqlite(database, storage, backup_dir)

    with sqlite3.connect(backup_dir / "database.sqlite") as snapshot_connection:
        assert snapshot_connection.execute("SELECT amount FROM receipts").fetchall() == [(2752,)]
    assert verify_manifest(backup_dir)["format"] == 1


def test_manifest_rejects_tampering(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state (value) VALUES ('state')")
    backup = tmp_path / "backup"
    snapshot_sqlite(database, tmp_path / "missing-store", backup)
    (backup / "database.sqlite").write_bytes(b"tampered")
    with pytest.raises(ManifestError, match="(size|checksum) mismatch"):
        verify_manifest(backup)


def test_manifest_rejects_a_non_object_document(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "manifest.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ManifestError, match="unsupported backup manifest format"):
        verify_manifest(backup_dir)


def test_manifest_preserves_a_nested_file_named_manifest_json(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backup"
    nested_manifest = backup_dir / "doc_store" / "originals" / "manifest.json"
    nested_manifest.parent.mkdir(parents=True)
    nested_manifest.write_bytes(b"preserved original named manifest")

    backup.write_manifest(backup_dir)
    manifest = verify_manifest(backup_dir)

    assert manifest["files"] == [
        {
            "path": "doc_store/originals/manifest.json",
            "sha256": backup.sha256_file(nested_manifest),
            "bytes": len(b"preserved original named manifest"),
        }
    ]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform does not support FIFOs")
def test_manifest_rejects_special_files_before_copy_or_hashing(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    fifo = backup_dir / "untrusted.fifo"
    os.mkfifo(fifo)

    with pytest.raises(ManifestError, match="unsupported filesystem entry"):
        backup.write_manifest(backup_dir)


def test_manifest_rejects_a_listed_symlink_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    external = tmp_path / "outside.sqlite"
    external.write_bytes(b"outside")
    (backup_dir / "database.sqlite").symlink_to(external)
    (backup_dir / "manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "files": [
                    {
                        "path": "database.sqlite",
                        "sha256": "0" * 64,
                        "bytes": len(b"outside"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_if_hashed(_: Path) -> str:
        pytest.fail("a manifest-listed symlink must be rejected before it is hashed")

    monkeypatch.setattr(backup, "sha256_file", fail_if_hashed)
    with pytest.raises(ManifestError, match="symbolic link"):
        verify_manifest(backup_dir)


def test_sqlite_snapshot_rejects_symlinked_database_and_storage_inputs(tmp_path: Path) -> None:
    database = tmp_path / "live" / "clerksan.sqlite"
    database.parent.mkdir()
    database.write_bytes(b"database-state")
    database_link = tmp_path / "database-link.sqlite"
    database_link.symlink_to(database)
    storage = tmp_path / "live" / "doc_store"
    storage.mkdir()
    storage_link = tmp_path / "storage-link"
    storage_link.symlink_to(storage, target_is_directory=True)

    with pytest.raises(ManifestError, match="SQLite database.*symbolic link"):
        snapshot_sqlite(database_link, storage, tmp_path / "backup-db-link")
    with pytest.raises(ManifestError, match="SQLite storage directory.*symbolic link"):
        snapshot_sqlite(database, storage_link, tmp_path / "backup-store-link")


def test_sqlite_restore_rebases_legacy_absolute_artifact_paths(tmp_path: Path) -> None:
    source_database = tmp_path / "source" / "clerksan.sqlite"
    source_database.parent.mkdir(parents=True)
    source_store = tmp_path / "source" / "doc_store"
    original = source_store / "originals" / "receipt.png"
    embedded = source_store / "embedded" / "sha256" / "image.png"
    original.parent.mkdir(parents=True)
    embedded.parent.mkdir(parents=True)
    original.write_bytes(b"original")
    embedded.write_bytes(b"embedded")
    with sqlite3.connect(source_database) as connection:
        connection.executescript(
            """
            CREATE TABLE document_files (content_path TEXT NOT NULL);
            CREATE TABLE embedded_media (content_path TEXT NOT NULL);
            CREATE TRIGGER document_files_append_only_sqlite_update
            BEFORE UPDATE ON document_files BEGIN
                SELECT RAISE(ABORT, 'document_files rows are append-only');
            END;
            """
        )
        connection.execute("INSERT INTO document_files VALUES (?)", (str(original),))
        connection.execute("INSERT INTO embedded_media VALUES (?)", (str(embedded),))

    backup_dir = tmp_path / "backup"
    snapshot_sqlite(source_database, source_store, backup_dir)
    target_database = tmp_path / "target" / "clerksan.sqlite"
    target_store = tmp_path / "target" / "doc_store"
    restore_sqlite(backup_dir, target_database, target_store)

    with sqlite3.connect(target_database) as connection:
        document_path = connection.execute("SELECT content_path FROM document_files").fetchone()[0]
        embedded_path = connection.execute("SELECT content_path FROM embedded_media").fetchone()[0]
    assert document_path == "originals/receipt.png"
    assert embedded_path == "embedded/sha256/image.png"
    assert (target_store / document_path).read_bytes() == b"original"
    assert (target_store / embedded_path).read_bytes() == b"embedded"
    with sqlite3.connect(target_database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE document_files SET content_path = ?",
                ("another/location.png",),
            )


def test_sqlite_restore_rejects_a_corrupt_staged_database_without_touching_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_dir = tmp_path / "backup"
    (backup_dir / "doc_store").mkdir(parents=True)
    (backup_dir / "database.sqlite").write_bytes(b"not a SQLite database")
    (backup_dir / "doc_store" / "replacement.bin").write_bytes(b"replacement")
    backup.write_manifest(backup_dir)

    target_database = tmp_path / "live" / "clerksan.sqlite"
    target_database.parent.mkdir()
    with sqlite3.connect(target_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state (value) VALUES ('preserved')")
    target_store = tmp_path / "live" / "doc_store"
    target_store.mkdir()
    (target_store / "preserved.bin").write_bytes(b"preserved")

    # Bypass artifact-path inspection so this regression specifically proves the
    # staged database is validated even when there are no path rewrites.
    monkeypatch.setattr(backup, "_sqlite_path_rewrites", lambda *args, **kwargs: [])

    with pytest.raises(ManifestError, match="SQLite staging database"):
        restore_sqlite(backup_dir, target_database, target_store)

    with sqlite3.connect(target_database) as connection:
        assert connection.execute("SELECT value FROM state").fetchall() == [("preserved",)]
    assert (target_store / "preserved.bin").read_bytes() == b"preserved"
    assert not (target_store / "replacement.bin").exists()


def test_sqlite_restore_treats_backup_database_inspection_errors_as_fatal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt.sqlite"
    database.write_bytes(b"not a SQLite database")

    with pytest.raises(ManifestError, match="inspect SQLite backup"):
        backup._sqlite_path_rewrites(
            database,
            source_root=None,
            destination_root=tmp_path / "doc_store",
        )


def test_sqlite_restore_cli_requires_an_explicit_destructive_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "backup.py",
            "restore-sqlite",
            str(tmp_path / "backup"),
            str(tmp_path / "database.sqlite"),
            str(tmp_path / "doc_store"),
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        backup.main()


def _maintenance_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'maintenance.sqlite'}",
        storage_dir=tmp_path / "doc_store",
        embed_model=None,
        embed_model_digest=None,
        embed_dim=None,
    )


def _table_inventory(inventory: dict[str, object], table_name: str) -> dict[str, object]:
    tables = inventory["tables"]
    assert isinstance(tables, list)
    for table in tables:
        assert isinstance(table, dict)
        if table.get("table") == table_name:
            return table
    raise AssertionError(f"missing inventory entry for {table_name}")


async def _create_inventory_fixture_tables(settings: Settings, *, rows: int = 1) -> None:
    async with get_engine(settings).begin() as connection:
        for table_name in _NEWLY_COVERED_INVENTORY_TABLES:
            identifier = backup._quote_identifier(table_name)
            await connection.execute(
                text(
                    f"CREATE TABLE {identifier} (id TEXT PRIMARY KEY, logical_value TEXT NOT NULL)"
                )
            )
            for row_number in range(rows):
                await connection.execute(
                    text(
                        f"INSERT INTO {identifier} (id, logical_value) VALUES (:id, :logical_value)"
                    ),
                    {
                        "id": f"private-id-{row_number}",
                        "logical_value": f"private-value-{row_number}",
                    },
                )


async def _create_maintenance_schema(settings: Settings) -> None:
    async with get_engine(settings).begin() as connection:
        await connection.execute(text("CREATE TABLE document_files (sha256 TEXT NOT NULL)"))
        await connection.execute(
            text("CREATE TABLE jobs (status TEXT NOT NULL, lease_expires_at DATETIME)")
        )
        await connection.execute(
            text("CREATE TABLE worker_capability_leases (expires_at DATETIME NOT NULL)")
        )


def test_maintenance_preflight_reconciles_zero_grace_under_exclusive_lock(
    tmp_path: Path,
) -> None:
    async def run() -> backup.MaintenancePreflightResult:
        settings = _maintenance_settings(tmp_path)
        await _create_maintenance_schema(settings)
        reservation = reserve_quarantine(
            settings.storage_dir,
            reservation_id="abandoned",
            now_ns=0,
        )
        reservation.payload_path.write_bytes(b"temporary")
        try:
            return await backup.maintenance_preflight(settings)
        finally:
            await dispose_engines()

    result = asyncio.run(run())

    assert result.references == 0
    assert result.reconciled == 1
    assert backup._quarantine_entry_count(tmp_path / "doc_store") == 0


def test_maintenance_preflight_supports_backup_before_universal_lease_migration(
    tmp_path: Path,
) -> None:
    async def run() -> backup.MaintenancePreflightResult:
        settings = _maintenance_settings(tmp_path)
        async with get_engine(settings).begin() as connection:
            await connection.execute(text("CREATE TABLE document_files (sha256 TEXT NOT NULL)"))
            await connection.execute(
                text("CREATE TABLE jobs (status TEXT NOT NULL, lease_expires_at DATETIME)")
            )
        try:
            return await backup.maintenance_preflight(settings)
        finally:
            await dispose_engines()

    result = asyncio.run(run())

    assert result.references == 0
    assert result.reconciled == 0


def test_maintenance_preflight_fails_closed_on_a_fresh_worker_lease(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = _maintenance_settings(tmp_path)
        await _create_maintenance_schema(settings)
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO worker_capability_leases (expires_at) "
                    "VALUES (datetime('now', '+1 hour'))"
                )
            )
        try:
            with pytest.raises(MaintenancePreflightError, match="active job or worker leases"):
                await backup.maintenance_preflight(settings)
        finally:
            await dispose_engines()

    asyncio.run(run())


def test_maintenance_preflight_fails_closed_on_an_active_running_job_lease(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = _maintenance_settings(tmp_path)
        await _create_maintenance_schema(settings)
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO jobs (status, lease_expires_at) "
                    "VALUES ('running', datetime('now', '+1 hour'))"
                )
            )
        try:
            with pytest.raises(MaintenancePreflightError, match="active job or worker leases"):
                await backup.maintenance_preflight(settings)
        finally:
            await dispose_engines()

    asyncio.run(run())


def test_database_inventory_classifies_every_application_table() -> None:
    inventoried = set(backup._INVENTORY_TABLES)
    excluded = set(backup._INVENTORY_EXCLUDED_TABLES)

    assert inventoried.isdisjoint(excluded)
    assert inventoried | excluded == set(Base.metadata.tables)
    introduced = set().union(*backup._TABLE_INTRODUCTIONS.values())
    assert introduced == inventoried | excluded
    assert all(reason.strip() for reason in backup._INVENTORY_EXCLUDED_TABLES.values())


def test_database_inventory_allows_sqlite_schema_without_a_migration_ledger(
    tmp_path: Path,
) -> None:
    async def run() -> dict[str, object]:
        settings = _maintenance_settings(tmp_path)
        async with get_engine(settings).begin() as connection:
            await connection.execute(text("CREATE TABLE documents (id TEXT PRIMARY KEY)"))
        try:
            return await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    inventory = asyncio.run(run())

    assert inventory["migrations"] == []


def test_database_inventory_supports_a_pre_universal_migration_prefix(
    tmp_path: Path,
) -> None:
    async def run() -> dict[str, object]:
        settings = _maintenance_settings(tmp_path)
        migrations = tuple(
            path for path in backup.discover_migrations() if path.name < "0015_universal_intake.sql"
        )
        introduced = set().union(
            *(backup._TABLE_INTRODUCTIONS.get(path.name, frozenset()) for path in migrations)
        )
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE schema_migrations ("
                    "filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
                    "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            )
            for migration in migrations:
                await connection.execute(
                    text(
                        "INSERT INTO schema_migrations (filename, checksum) "
                        "VALUES (:filename, :checksum)"
                    ),
                    {
                        "filename": migration.name,
                        "checksum": backup.sha256_file(migration),
                    },
                )
            for table_name in sorted(introduced):
                columns = (
                    "id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content_path TEXT NOT NULL"
                    if table_name in {"document_files", "embedded_media"}
                    else "id TEXT PRIMARY KEY"
                )
                await connection.execute(text(f"CREATE TABLE {table_name} ({columns})"))
        try:
            return await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    inventory = asyncio.run(run())

    assert [migration["filename"] for migration in inventory["migrations"]][-1].startswith("0014_")
    assert _table_inventory(inventory, "source_intakes")["present"] is False


def test_database_inventory_rejects_an_unclassified_physical_table(tmp_path: Path) -> None:
    async def run() -> None:
        settings = _maintenance_settings(tmp_path)
        async with get_engine(settings).begin() as connection:
            await connection.execute(text("CREATE TABLE documents (id TEXT PRIMARY KEY)"))
            await connection.execute(
                text("CREATE TABLE unexpected_durable_data (id TEXT PRIMARY KEY)")
            )
        try:
            with pytest.raises(DatabaseInventoryError, match="unclassified database table"):
                await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    asyncio.run(run())


def test_database_inventory_requires_postgresql_migration_ledger() -> None:
    with pytest.raises(DatabaseInventoryError, match="migration ledger is missing"):
        backup._validate_inventory_structure({}, dialect_name="postgresql")


def test_database_inventory_rejects_a_malformed_sqlite_migration_ledger(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = _maintenance_settings(tmp_path)
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text("CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, checksum TEXT)")
            )
        try:
            with pytest.raises(DatabaseInventoryError, match="migration ledger is malformed"):
                await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    asyncio.run(run())


def test_database_inventory_rejects_an_empty_present_sqlite_migration_ledger(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = _maintenance_settings(tmp_path)
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE schema_migrations ("
                    "filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
                    "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            )
        try:
            with pytest.raises(DatabaseInventoryError, match="migration ledger is malformed"):
                await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    asyncio.run(run())


def test_database_inventory_rejects_a_tampered_migration_checksum(tmp_path: Path) -> None:
    async def run() -> None:
        settings = _maintenance_settings(tmp_path)
        first_migration = backup.discover_migrations()[0]
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE schema_migrations ("
                    "filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
                    "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO schema_migrations (filename, checksum) "
                    "VALUES (:filename, :checksum)"
                ),
                {"filename": first_migration.name, "checksum": "0" * 64},
            )
        try:
            with pytest.raises(DatabaseInventoryError, match="migration ledger is malformed"):
                await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    asyncio.run(run())


def test_database_inventory_rejects_wrong_migration_ledger_types_and_default(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = _maintenance_settings(tmp_path)
        first_migration = backup.discover_migrations()[0]
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE schema_migrations ("
                    "filename BLOB PRIMARY KEY, checksum NUMERIC NOT NULL, "
                    "applied_at INTEGER NOT NULL DEFAULT 0)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO schema_migrations (filename, checksum) "
                    "VALUES (:filename, :checksum)"
                ),
                {
                    "filename": first_migration.name,
                    "checksum": backup.sha256_file(first_migration),
                },
            )
            for table_name in (
                "documents",
                "document_files",
                "extracted_records",
                "verified_records",
            ):
                columns = (
                    "id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content_path TEXT NOT NULL"
                    if table_name == "document_files"
                    else "id TEXT PRIMARY KEY"
                )
                await connection.execute(text(f"CREATE TABLE {table_name} ({columns})"))
        try:
            with pytest.raises(DatabaseInventoryError, match="migration ledger is malformed"):
                await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    asyncio.run(run())


def test_database_inventory_rejects_a_table_missing_from_the_applied_generation(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = _maintenance_settings(tmp_path)
        first_migration = backup.discover_migrations()[0]
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE schema_migrations ("
                    "filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
                    "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO schema_migrations (filename, checksum) "
                    "VALUES (:filename, :checksum)"
                ),
                {
                    "filename": first_migration.name,
                    "checksum": backup.sha256_file(first_migration),
                },
            )
            # 0001 owns four tables. Deliberately omit document_files while
            # retaining the other three to prove ledger-aware subset rejection.
            for table_name in ("documents", "extracted_records", "verified_records"):
                await connection.execute(text(f"CREATE TABLE {table_name} (id TEXT PRIMARY KEY)"))
        try:
            with pytest.raises(
                DatabaseInventoryError,
                match="tables do not match the migration ledger",
            ):
                await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    asyncio.run(run())


@pytest.mark.parametrize("missing_object", ("trigger", "index", "constraint"))
def test_sqlite_schema_inventory_detects_dropped_safety_objects(
    tmp_path: Path, missing_object: str
) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object]]:
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'schema.sqlite'}",
            storage_dir=tmp_path / "doc_store",
            embed_model=None,
            embed_model_digest=None,
            embed_dim=None,
        )
        try:
            async with get_engine(settings).begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE documents ("
                        "id TEXT PRIMARY KEY, amount INTEGER NOT NULL, "
                        "CONSTRAINT amount_nonnegative CHECK (amount >= 0))"
                    )
                )
                await connection.execute(
                    text("CREATE INDEX documents_amount_idx ON documents (amount)")
                )
                await connection.execute(
                    text(
                        "CREATE TRIGGER documents_private_guard BEFORE UPDATE ON documents "
                        "BEGIN SELECT RAISE(ABORT, 'private schema literal'); END"
                    )
                )
            complete = await backup.build_database_inventory(settings)
            async with get_engine(settings).begin() as connection:
                if missing_object == "trigger":
                    await connection.execute(text("DROP TRIGGER documents_private_guard"))
                elif missing_object == "index":
                    await connection.execute(text("DROP INDEX documents_amount_idx"))
                else:
                    await connection.execute(text("DROP TRIGGER documents_private_guard"))
                    await connection.execute(text("DROP INDEX documents_amount_idx"))
                    await connection.execute(
                        text("ALTER TABLE documents RENAME TO documents_with_constraint")
                    )
                    await connection.execute(
                        text(
                            "CREATE TABLE documents (id TEXT PRIMARY KEY, amount INTEGER NOT NULL)"
                        )
                    )
                    await connection.execute(text("DROP TABLE documents_with_constraint"))
                    await connection.execute(
                        text("CREATE INDEX documents_amount_idx ON documents (amount)")
                    )
                    await connection.execute(
                        text(
                            "CREATE TRIGGER documents_private_guard "
                            "BEFORE UPDATE ON documents "
                            "BEGIN SELECT RAISE(ABORT, 'private schema literal'); END"
                        )
                    )
            changed = await backup.build_database_inventory(settings)
            return complete, changed
        finally:
            await dispose_engines()

    complete, changed = asyncio.run(run())

    assert complete["tables"] == changed["tables"]
    assert complete["migrations"] == changed["migrations"] == []
    assert complete["schema_objects"] != changed["schema_objects"]
    encoded = json.dumps(complete, sort_keys=True)
    assert "documents_private_guard" not in encoded
    assert "private schema literal" not in encoded


class _FakeSchemaRows(list[tuple[str, ...]]):
    def all(self) -> list[tuple[str, ...]]:
        return list(self)


class _FakePostgresqlSchemaConnection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, rows: dict[str, list[tuple[str, ...]]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def execute(self, statement: object) -> _FakeSchemaRows:
        query = str(statement)
        self.queries.append(query)
        for marker, rows in self.rows.items():
            if f"/* {marker} */" in query:
                return _FakeSchemaRows(rows)
        raise AssertionError(f"unexpected PostgreSQL schema query: {query}")


@pytest.mark.parametrize(
    "removed_marker",
    (
        "inventory:postgresql-trigger",
        "inventory:postgresql-index",
        "inventory:postgresql-constraint",
        "inventory:postgresql-function",
    ),
)
def test_postgresql_schema_inventory_hashes_safety_object_definitions(
    removed_marker: str,
) -> None:
    rows = {
        "inventory:postgresql-trigger": [
            ("public", "documents", "private_guard", "CREATE TRIGGER private_guard")
        ],
        "inventory:postgresql-index": [
            ("public", "documents", "documents_lookup", "CREATE INDEX documents_lookup")
        ],
        "inventory:postgresql-constraint": [
            ("public", "documents", "amount_nonnegative", "CHECK ((amount >= 0))")
        ],
        "inventory:postgresql-function": [
            ("public", "write_audit_event", "", "CREATE FUNCTION write_audit_event()")
        ],
    }
    complete_connection = _FakePostgresqlSchemaConnection(rows)
    complete = backup._schema_object_inventory(complete_connection)
    changed_rows = {marker: list(values) for marker, values in rows.items()}
    changed_rows[removed_marker] = []
    changed = backup._schema_object_inventory(_FakePostgresqlSchemaConnection(changed_rows))

    assert complete != changed
    assert complete == {
        "count": 4,
        "sha256": complete["sha256"],
    }
    assert len(str(complete["sha256"])) == 64
    assert "private_guard" not in json.dumps(complete)
    assert any(
        "NOT trigger_metadata.tgisinternal" in query for query in complete_connection.queries
    )
    assert any("pg_depend" in query for query in complete_connection.queries)
    function_query = next(
        query
        for query in complete_connection.queries
        if "/* inventory:postgresql-function */" in query
    )
    assert "FROM pg_proc AS function_metadata" in function_query
    assert "JOIN pg_trigger" not in function_query


@pytest.mark.parametrize("table_name", _NEWLY_COVERED_INVENTORY_TABLES)
def test_database_inventory_detects_each_newly_covered_table_mutation(
    tmp_path: Path, table_name: str
) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object]]:
        settings = _maintenance_settings(tmp_path)
        try:
            await _create_inventory_fixture_tables(settings)
            before = await backup.build_database_inventory(settings)
            identifier = backup._quote_identifier(table_name)
            async with get_engine(settings).begin() as connection:
                await connection.execute(
                    text(f"UPDATE {identifier} SET logical_value = :logical_value WHERE id = :id"),
                    {"id": "private-id-0", "logical_value": "changed-private-value"},
                )
            after = await backup.build_database_inventory(settings)
            return before, after
        finally:
            await dispose_engines()

    before, after = asyncio.run(run())
    before_table = _table_inventory(before, table_name)
    after_table = _table_inventory(after, table_name)

    assert before_table["rows"] == after_table["rows"] == 1
    assert before_table["identity_sha256"] == after_table["identity_sha256"]
    assert before_table["content_sha256"] != after_table["content_sha256"]
    assert "private-id-0" not in json.dumps(before, sort_keys=True)
    assert "private-value-0" not in json.dumps(before, sort_keys=True)
    expected = tmp_path / "database-inventory.json"
    expected.write_text(json.dumps(before), encoding="utf-8")
    with pytest.raises(DatabaseInventoryError, match="does not match"):
        backup.verify_database_inventory(expected, after)


def test_database_inventory_detects_covered_table_row_count_change(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object]]:
        settings = _maintenance_settings(tmp_path)
        table_name = "upload_idempotency_reservations"
        try:
            await _create_inventory_fixture_tables(settings, rows=2)
            before = await backup.build_database_inventory(settings)
            identifier = backup._quote_identifier(table_name)
            async with get_engine(settings).begin() as connection:
                await connection.execute(
                    text(f"DELETE FROM {identifier} WHERE id = :id"),
                    {"id": "private-id-1"},
                )
            after = await backup.build_database_inventory(settings)
            return before, after
        finally:
            await dispose_engines()

    before, after = asyncio.run(run())
    before_table = _table_inventory(before, "upload_idempotency_reservations")
    after_table = _table_inventory(after, "upload_idempotency_reservations")

    assert before_table["rows"] == 2
    assert after_table["rows"] == 1
    assert before_table["identity_sha256"] != after_table["identity_sha256"]
    expected = tmp_path / "database-inventory.json"
    expected.write_text(json.dumps(before), encoding="utf-8")
    with pytest.raises(DatabaseInventoryError, match="does not match"):
        backup.verify_database_inventory(expected, after)


def test_database_inventory_detects_missing_covered_table(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object]]:
        settings = _maintenance_settings(tmp_path)
        table_name = "duplicate_flags"
        try:
            await _create_inventory_fixture_tables(settings, rows=0)
            before = await backup.build_database_inventory(settings)
            identifier = backup._quote_identifier(table_name)
            async with get_engine(settings).begin() as connection:
                await connection.execute(text(f"DROP TABLE {identifier}"))
            after = await backup.build_database_inventory(settings)
            return before, after
        finally:
            await dispose_engines()

    before, after = asyncio.run(run())
    before_table = _table_inventory(before, "duplicate_flags")
    after_table = _table_inventory(after, "duplicate_flags")

    assert before_table["present"] is True
    assert before_table["rows"] == 0
    assert before_table["identity_sha256"] is not None
    assert before_table["content_sha256"] is not None
    assert after_table == {
        "table": "duplicate_flags",
        "present": False,
        "rows": 0,
        "identity_sha256": None,
    }
    expected = tmp_path / "database-inventory.json"
    expected.write_text(json.dumps(before), encoding="utf-8")
    with pytest.raises(DatabaseInventoryError, match="does not match"):
        backup.verify_database_inventory(expected, after)


def test_database_inventory_is_value_free_and_detects_a_mismatch(tmp_path: Path) -> None:
    async def run() -> dict[str, object]:
        settings = _maintenance_settings(tmp_path)
        artifact = settings.storage_dir / "originals" / ("a" * 64)
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"synthetic artifact")
        digest = backup.sha256_file(artifact)
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text("CREATE TABLE documents (id TEXT PRIMARY KEY, label TEXT NOT NULL)")
            )
            await connection.execute(
                text(
                    "INSERT INTO documents (id, label) "
                    "VALUES ('document-id', 'synthetic logical value')"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE document_files ("
                    "id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content_path TEXT NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO document_files (id, sha256, content_path) "
                    "VALUES ('synthetic-id', :sha256, :content_path)"
                ),
                {
                    "sha256": digest,
                    "content_path": artifact.relative_to(settings.storage_dir).as_posix(),
                },
            )
        try:
            return await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    inventory = asyncio.run(run())
    encoded = json.dumps(inventory, sort_keys=True)
    assert "synthetic-id" not in encoded
    assert "synthetic logical value" not in encoded
    assert str(tmp_path) not in encoded
    expected = tmp_path / "database-inventory.json"
    expected.write_text(json.dumps(inventory), encoding="utf-8")
    backup.verify_database_inventory(expected, inventory)

    async def changed_inventory() -> dict[str, object]:
        settings = _maintenance_settings(tmp_path)
        async with get_engine(settings).begin() as connection:
            await connection.execute(
                text(
                    "UPDATE documents SET label = 'changed logical value' WHERE id = 'document-id'"
                )
            )
        try:
            return await backup.build_database_inventory(settings)
        finally:
            await dispose_engines()

    changed = asyncio.run(changed_inventory())
    expected_documents = next(
        table for table in inventory["tables"] if table["table"] == "documents"
    )
    changed_documents = next(table for table in changed["tables"] if table["table"] == "documents")
    assert expected_documents["identity_sha256"] == changed_documents["identity_sha256"]
    assert expected_documents["content_sha256"] != changed_documents["content_sha256"]
    with pytest.raises(DatabaseInventoryError, match="does not match"):
        backup.verify_database_inventory(expected, changed)


def test_storage_inventory_excludes_only_a_valid_operation_owned_restore_entry(
    tmp_path: Path,
) -> None:
    expected_root = tmp_path / "expected-store"
    actual_root = tmp_path / "actual-store"
    expected_root.mkdir()
    actual_root.mkdir()
    (expected_root / "replacement.bin").write_bytes(b"replacement")
    (actual_root / "replacement.bin").write_bytes(b"replacement")
    previous_name = ".restore-previous-4242"
    previous = actual_root / previous_name
    previous.mkdir()
    (previous / ".restore-state").write_bytes(b"replacement-active\n")
    (previous / "old-private.bin").write_bytes(b"old private data")

    expected = backup._storage_inventory(expected_root)
    actual = backup._storage_inventory(
        actual_root,
        excluded_restore_entry=previous_name,
    )

    assert actual == expected

    unrelated = actual_root / ".restore-previous-9999"
    unrelated.mkdir()
    (unrelated / "must-not-hide.bin").write_bytes(b"durable")
    assert (
        backup._storage_inventory(
            actual_root,
            excluded_restore_entry=previous_name,
        )
        != expected
    )


@pytest.mark.parametrize(
    ("entry_name", "state"),
    (
        ("../.restore-previous-4242", b"replacement-active\n"),
        (".restore-previous-4242", b"replacement-active\nextra\n"),
        (".restore-stage-4242", b"replacement-active\n"),
    ),
)
def test_storage_inventory_rejects_invalid_restore_exclusions(
    tmp_path: Path, entry_name: str, state: bytes
) -> None:
    root = tmp_path / "doc_store"
    root.mkdir()
    candidate = root / Path(entry_name).name
    candidate.mkdir()
    (candidate / ".restore-state").write_bytes(state)

    with pytest.raises(DatabaseInventoryError, match="restore exclusion is invalid"):
        backup._storage_inventory(root, excluded_restore_entry=entry_name)


def test_storage_inventory_rejects_a_symlinked_restore_state(tmp_path: Path) -> None:
    root = tmp_path / "doc_store"
    previous = root / ".restore-previous-4242"
    previous.mkdir(parents=True)
    external_state = tmp_path / "external-state"
    external_state.write_bytes(b"replacement-active\n")
    (previous / ".restore-state").symlink_to(external_state)

    with pytest.raises(DatabaseInventoryError, match="restore exclusion is invalid"):
        backup._storage_inventory(
            root,
            excluded_restore_entry=previous.name,
        )


def _compose_backup_environment(
    tmp_path: Path, *, running_services: tuple[str, ...] = ("api", "worker")
) -> tuple[Path, dict[str, str], Path, Path]:
    caller = tmp_path / "caller"
    caller.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_dir = tmp_path / "compose-state"
    state_dir.mkdir()
    log_path = tmp_path / "compose.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu

state_dir=${FAKE_DOCKER_STATE:?}
log_path=${FAKE_DOCKER_LOG:?}
printf '%s\\n' "$*" >> "$log_path"

command=
last=
for argument in "$@"; do
  last=$argument
  case "$argument" in
    ps|stop|start|up|exec|run) command=$argument ;;
  esac
done

case "$command" in
  ps)
    if [ -f "$state_dir/$last.running" ]; then
      printf '%s\\n' "$last-container"
    fi
    ;;
  stop)
    if [ "${FAKE_DOCKER_FAIL_STOP:-0}" = 1 ]; then
      echo 'simulated stop failure' >&2
      exit 71
    fi
    for argument in "$@"; do
      case "$argument" in
        api|worker)
          rm -f "$state_dir/$argument.running"
          if [ "${FAKE_DOCKER_DESTROY_ON_STOP:-0}" = 1 ]; then
            : > "$state_dir/$argument.removed"
          fi
          ;;
      esac
    done
    ;;
  start)
    for argument in "$@"; do
      case "$argument" in
        api|worker)
          if [ -f "$state_dir/$argument.removed" ]; then
            echo 'cannot start a removed container' >&2
            exit 73
          fi
          : > "$state_dir/$argument.running"
          ;;
      esac
    done
    ;;
  up)
    for argument in "$@"; do
      case "$argument" in
        api|worker)
          rm -f "$state_dir/$argument.removed"
          : > "$state_dir/$argument.running"
          ;;
      esac
    done
    ;;
  exec)
    if [ "${FAKE_DOCKER_FAIL_READY:-0}" = 1 ]; then
      for argument in "$@"; do
        if [ "$argument" = "SELECT 1" ]; then
          echo 'simulated PostgreSQL readiness failure' >&2
          exit 74
        fi
      done
    fi
    printf '%s\\n' '-- fake PostgreSQL dump'
    ;;
  run)
    backup_copy=false
    inventory=false
    preflight=false
    for argument in "$@"; do
      case "$argument" in
        *tarfile.open*) backup_copy=true ;;
        database-inventory) inventory=true ;;
        maintenance-preflight) preflight=true ;;
      esac
    done
    if [ "$preflight" = true ] && [ "${FAKE_DOCKER_FAIL_PREFLIGHT:-0}" = 1 ]; then
      echo 'simulated maintenance preflight failure' >&2
      exit 75
    fi
    if [ "$inventory" = true ] && [ "${FAKE_DOCKER_FAIL_INVENTORY:-0}" = 1 ]; then
      echo 'simulated inventory failure' >&2
      exit 76
    fi
    if [ "$inventory" = true ]; then
      printf '%s\n' '{"format":1}'
    fi
    if [ "$backup_copy" = true ]; then
      if [ "${FAKE_DOCKER_FAIL_BACKUP_COPY:-0}" = 1 ]; then
        echo 'simulated document-store copy failure' >&2
        exit 72
      fi
      python3 -c '
import sys
import tarfile

payload = b"original"
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    member = tarfile.TarInfo("receipt.txt")
    member.size = len(payload)
    archive.addfile(member, __import__("io").BytesIO(payload))
'
    fi
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_python = bin_dir / "python3"
    fake_python.write_text(
        f"""#!/bin/sh
exec {shlex.quote(sys.executable)} "$@"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    (state_dir / "db.running").touch()
    for service in running_services:
        (state_dir / f"{service}.running").touch()
    environment = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "POSTGRES_PASSWORD": "test-only-password",
        "FAKE_DOCKER_LOG": str(log_path),
        "FAKE_DOCKER_STATE": str(state_dir),
    }
    return caller, environment, state_dir, log_path


def test_compose_backup_and_restore_accept_relative_paths_from_another_directory(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)

    backup_run = subprocess.run(
        [str(BACKUP_SCRIPT), "relative-backup"],
        cwd=caller,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert backup_run.returncode == 0, backup_run.stderr
    backup_dir = caller / "relative-backup"
    assert (backup_dir / "manifest.json").is_file()
    assert not (ROOT / "relative-backup").exists()
    assert (state_dir / "api.running").is_file()
    assert (state_dir / "worker.running").is_file()
    log = log_path.read_text(encoding="utf-8")
    preflight = " maintenance-preflight --wait-seconds 300"
    inventory = " database-inventory"
    assert log.index(" stop --timeout 300 api worker") < log.index(preflight)
    assert log.index(preflight) < log.index(inventory)
    assert log.index(inventory) < log.index(" exec -T db pg_dump")
    assert log.index(" exec -T db pg_dump") < log.index(" run --rm --no-deps -T api python -c")
    assert log.index(" run --rm --no-deps -T api python -c") < log.rindex(" start api worker")
    assert " cp api:/data/doc_store" not in log

    restore_run = subprocess.run(
        [str(RESTORE_SCRIPT), "relative-backup"],
        cwd=caller,
        env=environment | {"CLERKSAN_RESTORE_CONFIRM": "ERASE_LOCAL_DATA"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert restore_run.returncode == 0, restore_run.stderr


def test_compose_backup_restores_previously_running_services_after_copy_failure(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)

    backup_run = subprocess.run(
        [str(BACKUP_SCRIPT), "failed-backup"],
        cwd=caller,
        env=environment | {"FAKE_DOCKER_FAIL_BACKUP_COPY": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert backup_run.returncode != 0
    assert (state_dir / "api.running").is_file()
    assert (state_dir / "worker.running").is_file()
    assert not (caller / "failed-backup" / "manifest.json").exists()
    log = log_path.read_text(encoding="utf-8")
    transient_copy = " run --rm --no-deps -T api python -c"
    assert log.index(" stop --timeout 300 api worker") < log.index(transient_copy)
    assert log.index(transient_copy) < log.rindex(" start api worker")


def test_compose_maintenance_backup_keeps_writers_stopped_after_success(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)

    result = subprocess.run(
        [str(BACKUP_SCRIPT), "maintenance-backup"],
        cwd=caller,
        env=environment | {"CLERKSAN_BACKUP_MODE": "maintenance"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "maintenance fence remains closed" in result.stdout
    assert not (state_dir / "api.running").exists()
    assert not (state_dir / "worker.running").exists()
    assert " start api worker" not in log_path.read_text(encoding="utf-8")


def test_compose_failed_maintenance_backup_restores_prior_running_state(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)

    result = subprocess.run(
        [str(BACKUP_SCRIPT), "failed-maintenance-backup"],
        cwd=caller,
        env=environment
        | {
            "CLERKSAN_BACKUP_MODE": "maintenance",
            "FAKE_DOCKER_FAIL_PREFLIGHT": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert (state_dir / "api.running").is_file()
    assert (state_dir / "worker.running").is_file()
    log = log_path.read_text(encoding="utf-8")
    assert log.index(" maintenance-preflight") < log.rindex(" start api worker")


def test_compose_backup_uses_a_transient_api_when_app_services_are_absent(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(
        tmp_path, running_services=()
    )

    backup_run = subprocess.run(
        [str(BACKUP_SCRIPT), "data-services-only-backup"],
        cwd=caller,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert backup_run.returncode == 0, backup_run.stderr
    backup_dir = caller / "data-services-only-backup"
    assert (backup_dir / "doc_store" / "receipt.txt").read_bytes() == b"original"
    assert (backup_dir / "manifest.json").is_file()
    assert not (state_dir / "api.running").exists()
    assert not (state_dir / "worker.running").exists()
    log = log_path.read_text(encoding="utf-8")
    assert " run --rm --no-deps -T api python -c" in log
    assert " cp api:/data/doc_store" not in log
    assert " start api worker" not in log


def test_compose_backup_restores_only_services_that_were_running_before_snapshot(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(
        tmp_path, running_services=("api",)
    )

    backup_run = subprocess.run(
        [str(BACKUP_SCRIPT), "api-only-backup"],
        cwd=caller,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert backup_run.returncode == 0, backup_run.stderr
    assert (state_dir / "api.running").is_file()
    assert not (state_dir / "worker.running").exists()
    assert " start api worker" not in log_path.read_text(encoding="utf-8")


def test_compose_backup_refuses_to_continue_when_it_cannot_quiesce_writers(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)

    backup_run = subprocess.run(
        [str(BACKUP_SCRIPT), "unquiesced-backup"],
        cwd=caller,
        env=environment | {"FAKE_DOCKER_FAIL_STOP": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert backup_run.returncode != 0
    assert "Could not quiesce the API and worker" in backup_run.stderr
    assert (state_dir / "api.running").is_file()
    assert (state_dir / "worker.running").is_file()
    assert not (caller / "unquiesced-backup" / "database.sql").exists()
    assert " exec -T db pg_dump" not in log_path.read_text(encoding="utf-8")


def test_compose_backup_rejects_a_nonempty_destination_before_pausing_services(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)
    destination = caller / "existing-backup"
    destination.mkdir()
    (destination / "old-receipt.txt").write_text("stale", encoding="utf-8")

    backup_run = subprocess.run(
        [str(BACKUP_SCRIPT), "existing-backup"],
        cwd=caller,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert backup_run.returncode != 0
    assert "Backup destination must be empty" in backup_run.stderr
    assert (state_dir / "api.running").is_file()
    assert (state_dir / "worker.running").is_file()
    assert not log_path.exists()


def _compose_restore_source(caller: Path) -> Path:
    source = caller / "restore-source"
    (source / "doc_store").mkdir(parents=True)
    (source / "database.sql").write_text("-- fake PostgreSQL dump\n", encoding="utf-8")
    (source / "doc_store" / "receipt.txt").write_text("original", encoding="utf-8")
    (source / "database-inventory.json").write_text('{"format":1}\n', encoding="utf-8")
    backup.write_manifest(source)
    return source


def test_compose_restore_recreates_previously_running_services_after_destroyed_drill(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)
    source = _compose_restore_source(caller)

    restore_run = subprocess.run(
        [str(RESTORE_SCRIPT), str(source)],
        cwd=caller,
        env=environment
        | {
            "CLERKSAN_RESTORE_CONFIRM": "ERASE_LOCAL_DATA",
            "FAKE_DOCKER_DESTROY_ON_STOP": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert restore_run.returncode == 0, restore_run.stderr
    assert (state_dir / "api.running").is_file()
    assert (state_dir / "worker.running").is_file()
    assert not (state_dir / "api.removed").exists()
    assert not (state_dir / "worker.removed").exists()
    log = log_path.read_text(encoding="utf-8")
    assert " up -d api worker" in log
    assert " start api worker" not in log


def test_compose_restore_leaves_intentionally_stopped_app_services_stopped(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(
        tmp_path, running_services=()
    )
    source = _compose_restore_source(caller)

    restore_run = subprocess.run(
        [str(RESTORE_SCRIPT), str(source)],
        cwd=caller,
        env=environment | {"CLERKSAN_RESTORE_CONFIRM": "ERASE_LOCAL_DATA"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert restore_run.returncode == 0, restore_run.stderr
    assert not (state_dir / "api.running").exists()
    assert not (state_dir / "worker.running").exists()
    assert " up -d" not in log_path.read_text(encoding="utf-8")


def test_compose_maintenance_restore_verifies_before_discard_and_keeps_writers_stopped(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)
    source = _compose_restore_source(caller)

    result = subprocess.run(
        [str(RESTORE_SCRIPT), str(source)],
        cwd=caller,
        env=environment
        | {
            "CLERKSAN_RESTORE_CONFIRM": "ERASE_LOCAL_DATA",
            "CLERKSAN_RESTORE_MODE": "maintenance",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "maintenance fence remains closed" in result.stdout
    assert not (state_dir / "api.running").exists()
    assert not (state_dir / "worker.running").exists()
    log = log_path.read_text(encoding="utf-8")
    database_restore = log.rindex(" exec -T db psql")
    verification = log.index(" database-inventory --verify /restore-inventory.json")
    discard = log.index('rm -rf "/data/doc_store/$PREVIOUS_TAG"')
    assert database_restore < verification < discard
    assert "--excluded-restore-entry .restore-previous-" in log
    assert " up -d api worker" not in log


def test_compose_restore_verification_failure_retains_prior_store_and_writers_stopped(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)
    source = _compose_restore_source(caller)

    result = subprocess.run(
        [str(RESTORE_SCRIPT), str(source)],
        cwd=caller,
        env=environment
        | {
            "CLERKSAN_RESTORE_CONFIRM": "ERASE_LOCAL_DATA",
            "CLERKSAN_RESTORE_MODE": "maintenance",
            "FAKE_DOCKER_FAIL_INVENTORY": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "prior store remains retained" in result.stderr
    assert not (state_dir / "api.running").exists()
    assert not (state_dir / "worker.running").exists()
    log = log_path.read_text(encoding="utf-8")
    assert 'rm -rf "/data/doc_store/$PREVIOUS_TAG"' not in log
    assert " up -d api worker" not in log


def test_compose_restore_refuses_to_change_storage_without_running_postgres(
    tmp_path: Path,
) -> None:
    caller, environment, state_dir, log_path = _compose_backup_environment(tmp_path)
    source = _compose_restore_source(caller)
    (state_dir / "db.running").unlink()

    restore_run = subprocess.run(
        [str(RESTORE_SCRIPT), str(source)],
        cwd=caller,
        env=environment | {"CLERKSAN_RESTORE_CONFIRM": "ERASE_LOCAL_DATA"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert restore_run.returncode != 0
    assert "PostgreSQL is not running" in restore_run.stderr
    assert " stop " not in log_path.read_text(encoding="utf-8")


def test_compose_restore_refuses_to_change_storage_when_postgres_is_not_ready(
    tmp_path: Path,
) -> None:
    caller, environment, _, log_path = _compose_backup_environment(tmp_path)
    source = _compose_restore_source(caller)

    restore_run = subprocess.run(
        [str(RESTORE_SCRIPT), str(source)],
        cwd=caller,
        env=environment
        | {
            "CLERKSAN_RESTORE_CONFIRM": "ERASE_LOCAL_DATA",
            "FAKE_DOCKER_FAIL_READY": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert restore_run.returncode != 0
    assert "PostgreSQL is not ready for the target database" in restore_run.stderr
    assert " stop " not in log_path.read_text(encoding="utf-8")
