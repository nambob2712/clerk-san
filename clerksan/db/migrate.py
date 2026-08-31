"""Small ordered SQL migration runner used by API, worker, and setup commands."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from clerksan.config import Settings, get_settings
from clerksan.db.engine import get_engine

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_MIGRATION_GLOB = "[0-9][0-9][0-9][0-9]_*.sql"
_MIGRATION_LOCK_NAMESPACE = 1_128_421_191
_MIGRATION_LOCK_ID = 1


def split_sql(script: str) -> list[str]:
    """Split SQL statements while preserving strings, comments, and dollar-quoted bodies."""
    statements: list[str] = []
    start = 0
    index = 0
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_comment = False

    while index < len(script):
        pair = script[index : index + 2]
        if line_comment:
            if script[index] == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if pair == "*/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if dollar_tag:
            if script.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
            else:
                index += 1
            continue
        if quote:
            if script[index] == quote:
                if quote == "'" and script[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if pair == "--":
            line_comment = True
            index += 2
            continue
        if pair == "/*":
            block_comment = True
            index += 2
            continue
        if script[index] in {"'", '"'}:
            quote = script[index]
            index += 1
            continue
        matched = _DOLLAR_QUOTE.match(script, index)
        if matched:
            dollar_tag = matched.group(0)
            index += len(dollar_tag)
            continue
        if script[index] == ";":
            statement = script[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1

    statement = script[start:].strip()
    if statement:
        statements.append(statement)
    return statements


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> tuple[Path, ...]:
    """Return regular migration files in their canonical filename order."""

    return tuple(
        sorted(
            (path for path in migrations_dir.glob(_MIGRATION_GLOB) if path.is_file()),
            key=lambda path: path.name,
        )
    )


async def _ensure_schema_table(connection: AsyncConnection, settings: Settings) -> None:
    applied_at = (
        "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        if settings.is_sqlite
        else "TIMESTAMPTZ NOT NULL DEFAULT now()"
    )
    await connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at "
            f"{applied_at})"
        )
    )


@asynccontextmanager
async def _migration_lock(
    engine: AsyncEngine, settings: Settings
) -> AsyncIterator[AsyncConnection]:
    """Yield one connection that serializes a complete PostgreSQL migration run.

    The lock holder also runs every transaction through this connection.  A waiting
    advisory-lock caller therefore cannot consume a pool slot needed by the holder.
    """
    lock_args = {"namespace": _MIGRATION_LOCK_NAMESPACE, "lock_id": _MIGRATION_LOCK_ID}
    async with engine.connect() as connection:
        if not settings.is_sqlite:
            await connection.execute(
                text("SELECT pg_advisory_lock(:namespace, :lock_id)"), lock_args
            )
            await connection.commit()
        try:
            yield connection
        finally:
            if not settings.is_sqlite:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:namespace, :lock_id)"), lock_args
                )
                await connection.commit()


async def run_migrations(
    migrations_dir: Path = MIGRATIONS_DIR, settings: Settings | None = None
) -> list[str]:
    """Apply pending ``NNNN_*.sql`` files, each in its own transaction.

    PostgreSQL callers acquire one session-level advisory lock for the complete ordered
    run. This prevents two concurrently starting services from both applying the same
    non-idempotent migration before either records its checksum.
    """
    active_settings = settings or get_settings()
    engine = get_engine(active_settings)
    applied: list[str] = []

    async with _migration_lock(engine, active_settings) as connection:
        async with connection.begin():
            await _ensure_schema_table(connection, active_settings)
        for migration_path in discover_migrations(migrations_dir):
            content = migration_path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            async with connection.begin():
                row = (
                    await connection.execute(
                        text("SELECT checksum FROM schema_migrations WHERE filename = :filename"),
                        {"filename": migration_path.name},
                    )
                ).scalar_one_or_none()
                if row is not None:
                    if row != checksum:
                        raise RuntimeError(
                            f"migration checksum changed after application: {migration_path.name}"
                        )
                    continue
                for statement in split_sql(content):
                    await connection.execute(text(statement))
                await connection.execute(
                    text(
                        "INSERT INTO schema_migrations (filename, checksum) "
                        "VALUES (:filename, :checksum)"
                    ),
                    {"filename": migration_path.name, "checksum": checksum},
                )
                applied.append(migration_path.name)
    return applied


def main() -> None:
    """Run migrations from the command line."""
    applied = asyncio.run(run_migrations())
    print("Applied: " + (", ".join(applied) if applied else "none"))


if __name__ == "__main__":
    main()
