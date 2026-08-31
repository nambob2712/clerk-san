from __future__ import annotations

import io
import json
import os
import sqlite3
from pathlib import Path

import pytest

from clerksan.config import Settings
from clerksan.tools import local_preview
from clerksan.tools.backup import ManifestError, verify_manifest
from clerksan.tools.local_preview import (
    LocalPreviewError,
    mark_sqlite_upgrade,
    ollama_models,
    prepare_sqlite_upgrade,
    readiness_message,
    required_models,
)


def readiness_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "ready",
        "demo_mode": False,
        "intake_ready": True,
        "review_ready": True,
        "processing_ready": True,
        "processing_reason_codes": [],
        "worker_registry_digest": "a" * 64,
        "worker_capabilities_digest": "b" * 64,
        "worker_capability_lease_age_seconds": 0.2,
    }
    payload.update(overrides)
    return payload


def test_readiness_requires_normal_mode_and_core_contract() -> None:
    assert readiness_message(readiness_payload(), "core") == (
        0,
        "Core: ready for local intake and review (demo mode: false)",
    )
    code, message = readiness_message(readiness_payload(demo_mode=True), "core")
    assert code == 2
    assert message == "Core: unavailable or unsafe (inspect /ready locally)"


def test_processing_distinguishes_ready_degraded_and_waiting() -> None:
    ready = readiness_message(readiness_payload(), "processing")
    degraded = readiness_message(
        readiness_payload(
            processing_ready=False,
            processing_reason_codes=["model_unavailable"],
        ),
        "processing",
    )
    waiting = readiness_message(
        readiness_payload(
            processing_ready=False,
            processing_reason_codes=["worker_capability_stale", "model_unavailable"],
            worker_registry_digest=None,
            worker_capabilities_digest=None,
            worker_capability_lease_age_seconds=None,
        ),
        "processing",
    )
    invalid_digest = readiness_message(
        readiness_payload(worker_registry_digest="not-a-digest"),
        "processing",
    )

    assert ready[0] == 0
    assert degraded == (3, "Processing: unavailable (model_unavailable)")
    assert waiting[0] == 4
    assert "worker/model evidence" in waiting[1]
    assert invalid_digest[0] == 4


def test_readiness_redacts_untrusted_reason_text() -> None:
    code, message = readiness_message(
        readiness_payload(
            processing_ready=False,
            processing_reason_codes=["token=private-value", "/Users/example"],
        ),
        "processing",
    )

    assert code == 3
    assert message == "Processing: unavailable (unreported)"
    assert "private-value" not in message
    assert "/Users" not in message


def test_readiness_preserves_allowlisted_core_reason_without_raw_detail() -> None:
    private_detail = "/" + "Users/operator/private.sqlite"
    code, message = readiness_message(
        {
            "code": "local_data_needs_upgrade",
            "message": private_detail,
            "detail": {
                "core_reason_codes": ["local_data_needs_upgrade", private_detail],
            },
        },
        "status",
    )

    assert (code, message) == (2, "Core: unavailable (local_data_needs_upgrade)")
    assert private_detail not in message


def test_readiness_distinguishes_model_subreasons() -> None:
    code, message = readiness_message(
        readiness_payload(
            processing_ready=False,
            processing_reason_codes=["model_unavailable"],
            model_reason_codes=["required_model_missing", "embedding_digest_mismatch"],
        ),
        "processing",
    )

    assert code == 3
    assert message == (
        "Processing: unavailable (embedding_digest_mismatch, required_model_missing)"
    )


def create_demo_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO evidence (value) VALUES ('synthetic')")


def test_prepare_sqlite_upgrade_creates_verified_rollback_once(tmp_path: Path) -> None:
    database = tmp_path / "clerksan.sqlite"
    storage = tmp_path / "doc_store"
    state = tmp_path / "runtime" / "sqlite-upgrade"
    storage.mkdir()
    (storage / "synthetic.txt").write_text("fixture", encoding="utf-8")
    create_demo_database(database)

    prepared = prepare_sqlite_upgrade(database, storage, state)
    backups = list((state / "pre-upgrade-backups").iterdir())

    assert prepared.startswith("SQLite rollback: verified (")
    assert str(tmp_path) not in prepared
    assert len(backups) == 1
    manifest = verify_manifest(backups[0])
    assert "database.sqlite" in {entry["path"] for entry in manifest["files"]}
    with sqlite3.connect(backups[0] / "database.sqlite") as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]

    mark_sqlite_upgrade(database, state)
    assert prepare_sqlite_upgrade(database, storage, state) == (
        "SQLite rollback: current schema state already verified"
    )
    assert len(list((state / "pre-upgrade-backups").iterdir())) == 1


def test_prepare_sqlite_upgrade_rebuilds_when_store_content_changes(tmp_path: Path) -> None:
    database = tmp_path / "clerksan.sqlite"
    storage = tmp_path / "doc_store"
    state = tmp_path / "runtime" / "sqlite-upgrade"
    storage.mkdir()
    stored = storage / "synthetic.txt"
    stored.write_text("first", encoding="utf-8")
    create_demo_database(database)

    prepare_sqlite_upgrade(database, storage, state)
    stored.write_text("second", encoding="utf-8")
    prepare_sqlite_upgrade(database, storage, state)

    backups = list((state / "pre-upgrade-backups").iterdir())
    assert len(backups) == 2
    assert sorted((backup / "doc_store" / stored.name).read_text() for backup in backups) == [
        "first",
        "second",
    ]


def test_prepare_sqlite_upgrade_preserves_unknown_collision_and_publishes_verified_copy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "clerksan.sqlite"
    storage = tmp_path / "doc_store"
    state = tmp_path / "runtime" / "sqlite-upgrade"
    storage.mkdir()
    create_demo_database(database)
    prepare_sqlite_upgrade(database, storage, state)
    original = next((state / "pre-upgrade-backups").iterdir())
    (original / "manifest.json").unlink()
    marker = original / "unknown-owner.txt"
    marker.write_text("preserve", encoding="utf-8")

    prepare_sqlite_upgrade(database, storage, state)

    assert marker.read_text(encoding="utf-8") == "preserve"
    backups = list((state / "pre-upgrade-backups").iterdir())
    assert len(backups) == 2
    verified = [backup for backup in backups if (backup / "manifest.json").is_file()]
    assert len(verified) == 1
    verify_manifest(verified[0])


def test_prepare_sqlite_upgrade_fails_closed_when_isolated_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "clerksan.sqlite"
    storage = tmp_path / "doc_store"
    state = tmp_path / "runtime" / "sqlite-upgrade"
    storage.mkdir()
    create_demo_database(database)

    def fail_restore(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ManifestError("synthetic restore failure")

    monkeypatch.setattr(local_preview, "restore_sqlite", fail_restore)
    with pytest.raises(LocalPreviewError, match="restoreability check failed"):
        prepare_sqlite_upgrade(database, storage, state)

    assert not list((state / "pre-upgrade-backups").iterdir())
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchall() == [("synthetic",)]


def test_changed_schema_requires_a_new_verified_rollback(tmp_path: Path) -> None:
    database = tmp_path / "clerksan.sqlite"
    storage = tmp_path / "doc_store"
    state = tmp_path / "runtime" / "sqlite-upgrade"
    storage.mkdir()
    create_demo_database(database)
    prepare_sqlite_upgrade(database, storage, state)
    mark_sqlite_upgrade(database, state)

    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE evidence ADD COLUMN note TEXT")

    prepare_sqlite_upgrade(database, storage, state)
    assert len(list((state / "pre-upgrade-backups").iterdir())) == 2


def test_prepare_sqlite_upgrade_rejects_symlinked_state(tmp_path: Path) -> None:
    database = tmp_path / "clerksan.sqlite"
    storage = tmp_path / "doc_store"
    storage.mkdir()
    create_demo_database(database)
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "state"
    state.symlink_to(outside, target_is_directory=True)

    with pytest.raises(LocalPreviewError, match="state directory is unsafe"):
        prepare_sqlite_upgrade(database, storage, state)


def test_schema_state_is_content_free(tmp_path: Path) -> None:
    database = tmp_path / "clerksan.sqlite"
    storage = tmp_path / "doc_store"
    state = tmp_path / "runtime" / "sqlite-upgrade"
    storage.mkdir()
    create_demo_database(database)
    prepare_sqlite_upgrade(database, storage, state)
    mark_sqlite_upgrade(database, state)

    record = next((state / "schema-state").iterdir())
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert set(payload) == {"contract", "schema"}
    assert str(tmp_path) not in record.read_text(encoding="utf-8")


def test_schema_state_temp_write_does_not_follow_predictable_symlink(tmp_path: Path) -> None:
    database = tmp_path / "clerksan.sqlite"
    storage = tmp_path / "doc_store"
    state = tmp_path / "runtime" / "sqlite-upgrade"
    storage.mkdir()
    create_demo_database(database)
    prepare_sqlite_upgrade(database, storage, state)
    state_dir = state / "schema-state"
    outside = tmp_path / "outside.txt"
    outside.write_text("preserved", encoding="utf-8")
    predictable = next(state_dir.glob("*.json"), None)
    assert predictable is None
    legacy_temp = state_dir / f".legacy.json.tmp-{os.getpid()}"
    legacy_temp.symlink_to(outside)

    mark_sqlite_upgrade(database, state)

    assert outside.read_text(encoding="utf-8") == "preserved"
    assert legacy_temp.is_symlink()
    assert len(list(state_dir.glob("*.json"))) == 1


def test_required_models_preserves_validated_settings_order() -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        storage_dir=Path(".tmp/test-required-models"),
        intake_mode="legacy",
        ocr_model="example/vision:4b",
        extract_model="example/extract:7b",
        router_model="example/extract:7b",
        embed_model="example/embed:v1.5",
        embed_model_digest="e" * 64,
        embed_dim=768,
    )

    assert required_models(settings) == (
        "example/vision:4b",
        "example/extract:7b",
        "example/embed:v1.5",
    )


def test_required_models_rejects_line_breaks_without_echoing_value() -> None:
    invalid = "safe:tag\n/private/path"
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        storage_dir=Path(".tmp/test-invalid-required-models"),
        intake_mode="legacy",
    ).model_copy(update={"extract_model": invalid})

    with pytest.raises(LocalPreviewError, match="model configuration is invalid") as captured:
        required_models(settings)

    assert invalid not in str(captured.value)
    assert "/private/path" not in str(captured.value)


def test_ollama_models_parses_exact_safe_names_and_rejects_untrusted_shape() -> None:
    assert ollama_models(
        {
            "models": [
                {"name": "gemma3:4b", "model": "gemma3:4b"},
                {"name": "library/nomic-embed-text:v1.5"},
            ]
        }
    ) == ("gemma3:4b", "library/nomic-embed-text:v1.5")

    private_value = "/" + "Users/operator/private\nmodel"
    with pytest.raises(LocalPreviewError, match="model response is invalid") as captured:
        ollama_models({"models": [{"name": private_value}]})
    assert private_value not in str(captured.value)
    assert "/Users" not in str(captured.value)


def test_model_cli_subcommands_emit_only_validated_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        storage_dir=Path(".tmp/test-model-cli"),
        intake_mode="legacy",
    )
    monkeypatch.setattr(local_preview, "Settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["local_preview.py", "required-models"])
    local_preview.main()
    assert capsys.readouterr().out.splitlines() == list(settings.required_models)

    monkeypatch.setattr("sys.argv", ["local_preview.py", "ollama-models"])
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"models":[{"name":"gemma3:4b"},{"name":"qwen2.5:7b"}]}'),
    )
    local_preview.main()
    assert capsys.readouterr().out == "gemma3:4b\nqwen2.5:7b\n"


def test_ollama_models_cli_redacts_invalid_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    private_value = "/" + "Users/operator/private"
    monkeypatch.setattr("sys.argv", ["local_preview.py", "ollama-models"])
    monkeypatch.setattr("sys.stdin", io.StringIO(f'{{"models":[{{"name":"{private_value}"}}]}}'))

    with pytest.raises(SystemExit, match="2"):
        local_preview.main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Ollama model response is invalid\n"
    assert private_value not in captured.err
