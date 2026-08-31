from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from clerksan.config import (
    DEFAULT_DATABASE_URL,
    EMBED_MODEL_DIGEST,
    EMBED_MODEL_DIMENSION,
    EMBED_MODEL_TAG,
    SANDBOX_UNAVAILABLE_REASON,
    IntakeMode,
    SandboxUnavailable,
    Settings,
)
from clerksan.db.engine import dispose_engines, get_engine
from clerksan.db.migrate import discover_migrations, run_migrations, split_sql
from clerksan.ingest.capabilities import build_capability_registry


def test_settings_validate_embedding_pin_and_resource_limits() -> None:
    with pytest.raises(ValueError, match="set together"):
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            embed_model="demo",
            embed_model_digest=None,
            embed_dim=None,
        )
    with pytest.raises(ValueError, match="greater than zero"):
        Settings(database_url="sqlite+aiosqlite:///:memory:", max_pdf_pages=0)
    settings = Settings(
        database_url="sqlite+aiosqlite:///tmp/test.sqlite",
        embed_model=EMBED_MODEL_TAG,
        embed_model_digest=EMBED_MODEL_DIGEST,
        embed_dim=EMBED_MODEL_DIMENSION,
    )
    assert settings.is_sqlite
    assert settings.required_models == (
        settings.ocr_model,
        settings.extract_model,
        EMBED_MODEL_TAG,
    )

    distinct_router = settings.model_copy(update={"router_model": "router-test:3b"})
    assert distinct_router.required_models == (
        distinct_router.ocr_model,
        distinct_router.extract_model,
        "router-test:3b",
        EMBED_MODEL_TAG,
    )

    with pytest.raises(ValueError, match="schema migration and full re-embedding"):
        Settings(
            database_url="postgresql+asyncpg://clerksan:password@db:5432/clerksan",
            embed_model="different-embed:v1",
            embed_model_digest="sha256:different",
            embed_dim=384,
        )


def test_settings_require_explicit_postgres_password_and_skip_vision_model_for_paddle() -> None:
    with pytest.raises(ValueError, match="database_password must be set"):
        Settings()
    with pytest.raises(ValueError, match="database_password must be set"):
        Settings(database_url=DEFAULT_DATABASE_URL)
    with pytest.raises(ValueError, match="not supported for SQLite"):
        Settings(database_url="sqlite+aiosqlite:///:memory:", database_password="secret")

    postgres = Settings(database_url=DEFAULT_DATABASE_URL, database_password="secret")
    assert postgres.database_url == "postgresql+asyncpg://clerksan:secret@db:5432/clerksan"

    paddle = Settings(database_url="sqlite+aiosqlite:///:memory:", ocr_engine="paddleocr")
    assert paddle.required_models == (
        paddle.extract_model,
        EMBED_MODEL_TAG,
    )

    with pytest.raises(ValueError, match="vision_llm, yomitoku, paddleocr"):
        Settings(database_url="sqlite+aiosqlite:///:memory:", ocr_engine="cloud")


def test_phase_one_intake_settings_have_exact_defaults_and_fail_closed() -> None:
    expected_defaults = {
        "intake_mode": IntakeMode.LEGACY,
        "parser_socket_path": Path("/run/clerksan-parser/parser.sock"),
        "parser_request_timeout_seconds": 20.0,
        "max_request_bytes": 32 * 1024 * 1024,
        "max_multipart_files": 1,
        "max_multipart_fields": 8,
        "max_json_bytes": 1 * 1024 * 1024,
        "max_json_depth": 32,
        "upload_concurrency": 2,
        "idempotency_retention_hours": 168,
        "recent_intakes_default_limit": 50,
        "recent_intakes_max_limit": 100,
        "worker_capability_heartbeat_seconds": 10,
        "worker_capability_lease_seconds": 30,
        "storage_reservation_grace_seconds": 600,
        "max_image_frames": 100,
        "max_text_characters": 2_000_000,
        "max_tabular_rows": 100_000,
        "max_tabular_cells": 1_000_000,
        "max_structured_nodes": 500_000,
        "max_recursion_depth": 4,
        "max_normalized_output_bytes": 50 * 1024 * 1024,
    }
    assert {
        name: Settings.model_fields[name].default for name in expected_defaults
    } == expected_defaults

    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
    assert settings.intake_mode is IntakeMode.LEGACY

    universal_settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        intake_mode=IntakeMode.UNIVERSAL,
    )
    with pytest.raises(SandboxUnavailable) as error:
        build_capability_registry(universal_settings)
    assert error.value.reason_code == SANDBOX_UNAVAILABLE_REASON
    assert error.value.retryable is True
    assert str(error.value) == "sandbox_unavailable"

    with pytest.raises(ValueError, match="legacy.*universal"):
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            intake_mode="automatic",
        )

    with pytest.raises(ValueError, match="absolute .sock"):
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            parser_socket_path="relative/parser.sock",
        )
    with pytest.raises(ValueError, match="greater than zero"):
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            parser_request_timeout_seconds=0,
        )


def test_phase_one_intake_setting_relationships_are_bounded() -> None:
    with pytest.raises(ValueError, match="default_limit must not exceed"):
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            recent_intakes_default_limit=101,
            recent_intakes_max_limit=100,
        )
    with pytest.raises(ValueError, match="heartbeat_seconds must be less than"):
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            worker_capability_heartbeat_seconds=30,
            worker_capability_lease_seconds=30,
        )
    with pytest.raises(ValueError, match="greater than zero"):
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            max_normalized_output_bytes=0,
        )


def test_split_sql_keeps_semicolons_inside_strings_comments_and_function_bodies() -> None:
    script = """
    CREATE TABLE example (id integer, note text DEFAULT ';');
    -- a comment ; should not terminate anything
    CREATE FUNCTION sample() RETURNS void LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 'inside; function';
    END;
    $$;
    """
    statements = split_sql(script)
    assert len(statements) == 2
    assert "inside; function" in statements[1]


def test_discover_migrations_orders_valid_files_and_ignores_non_files(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for filename in (
        "0019_later.sql",
        "0002_second.sql",
        "0001_first.sql",
        "0018_reserved.txt",
        "invalid.sql",
    ):
        (migrations / filename).write_text("SELECT 1;", encoding="utf-8")
    (migrations / "0003_directory.sql").mkdir()

    assert [path.name for path in discover_migrations(migrations)] == [
        "0001_first.sql",
        "0002_second.sql",
        "0019_later.sql",
    ]


@pytest.mark.asyncio
async def test_migration_runner_is_idempotent_and_detects_changed_files(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    migration = migrations / "0001_example.sql"
    migration.write_text("CREATE TABLE example (id INTEGER PRIMARY KEY);", encoding="utf-8")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'migrations.sqlite'}")
    try:
        assert await run_migrations(migrations, settings) == ["0001_example.sql"]
        assert await run_migrations(migrations, settings) == []
        async with get_engine(settings).connect() as connection:
            migration_count = await connection.execute(
                text("SELECT count(*) FROM schema_migrations")
            )
            assert migration_count.scalar() == 1

        migration.write_text("CREATE TABLE changed (id INTEGER PRIMARY KEY);", encoding="utf-8")
        with pytest.raises(RuntimeError, match="checksum changed"):
            await run_migrations(migrations, settings)
    finally:
        await dispose_engines()
