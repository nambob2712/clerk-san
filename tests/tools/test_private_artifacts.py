from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-private-artifacts.py"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    return repository


def _stage(repository: Path, relative: str, content: bytes) -> Path:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    _git(repository, "add", "--", relative)
    return path


def _run(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _run_mode(repository, "--staged", *arguments)


def _run_mode(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


def _commit(repository: Path, message: str) -> str:
    _git(
        repository,
        "-c",
        "user.name=Clerk-san Test",
        "-c",
        "user.email=test.invalid@example.invalid",
        "commit",
        "--quiet",
        "-m",
        message,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _generated_manifest(content: bytes) -> bytes:
    return (
        json.dumps(
            {
                "format": 1,
                "generated": True,
                "generator": "eval.universal_intake_scale",
                "files": [
                    {
                        "path": "tests/fixtures/generated/universal-intake-1200x13.csv",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "rows": 1200,
                        "columns": 13,
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def test_generated_fixture_requires_exact_provenance_and_staged_digest(repository: Path) -> None:
    fixture = (ROOT / "tests/fixtures/generated/universal-intake-1200x13.csv").read_bytes()
    manifest = (ROOT / "tests/fixtures/generated/manifest.json").read_bytes()
    _stage(
        repository,
        "tests/fixtures/generated/universal-intake-1200x13.csv",
        fixture,
    )
    _stage(
        repository,
        "tests/fixtures/generated/manifest.json",
        manifest,
    )

    accepted = _run(repository)
    assert accepted.returncode == 0, accepted.stderr

    _stage(
        repository,
        "tests/fixtures/generated/universal-intake-1200x13.csv",
        fixture + b"synthetic_3,synthetic_4\n",
    )
    rejected = _run(repository)
    assert rejected.returncode == 1
    assert "generated-fixture-digest-mismatch" in rejected.stderr
    assert b"synthetic_3" not in rejected.stderr.encode()


def test_generated_manifest_cannot_authorize_arbitrary_or_unstaged_bytes(
    repository: Path,
) -> None:
    fixture_path = "tests/fixtures/generated/universal-intake-1200x13.csv"
    manifest_path = "tests/fixtures/generated/manifest.json"
    canonical_fixture = (ROOT / fixture_path).read_bytes()
    canonical_manifest = (ROOT / manifest_path).read_bytes()
    _stage(repository, fixture_path, canonical_fixture)
    _stage(repository, manifest_path, canonical_manifest)
    _git(
        repository,
        "-c",
        "user.name=Clerk-san Test",
        "-c",
        "user.email=test.invalid@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "synthetic baseline",
    )

    arbitrary = b"private-looking-but-synthetic-test-bytes\n"
    _stage(repository, fixture_path, arbitrary)
    _stage(repository, manifest_path, _generated_manifest(arbitrary))
    arbitrary_result = _run(repository)
    assert arbitrary_result.returncode == 1
    assert "generated-manifest-invalid" in arbitrary_result.stderr

    _stage(repository, fixture_path, canonical_fixture)
    _git(repository, "restore", "--staged", fixture_path)
    _stage(repository, manifest_path, _generated_manifest(b"different bytes\n"))
    manifest_only_result = _run(repository)
    assert manifest_only_result.returncode == 1
    assert "generated-manifest-invalid" in manifest_only_result.stderr


def test_data_file_outside_generated_allowlist_is_blocked_with_redacted_path(
    repository: Path,
) -> None:
    private_looking_name = "uploads/synthetic-person\ntransactions.csv"
    _stage(repository, private_looking_name, b"fake,only\n1,2\n")

    result = _run(repository)

    assert result.returncode == 1
    assert "data-file-outside-generated-allowlist" in result.stderr
    assert "synthetic-person" not in result.stderr
    assert "transactions.csv" not in result.stderr
    assert "fake,only" not in result.stderr


def test_structured_or_text_upload_is_data_bearing_but_source_json_is_not(
    repository: Path,
) -> None:
    _stage(repository, "uploads/synthetic-records.json", b'{"synthetic": true}\n')
    _stage(repository, "config/tooling.json", b'{"enabled": true}\n')

    result = _run(repository)

    assert result.returncode == 1
    assert result.stderr.count("data-file-outside-generated-allowlist") == 1
    assert "synthetic-records" not in result.stderr


@pytest.mark.parametrize(
    "relative",
    (
        "docs/uploads/records.json",
        "notes/data/extensionless",
        "notes/documents/extensionless",
        "notes/.clerksan-runtime/metadata.json",
        "web/samples/import.txt",
    ),
)
def test_nested_data_directories_fail_closed(repository: Path, relative: str) -> None:
    _stage(repository, relative, b"synthetic text payload\n")

    result = _run(repository)

    assert result.returncode == 1
    assert "data-file-outside-generated-allowlist" in result.stderr
    assert Path(relative).name not in result.stderr


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("uploads/extensionless", b"synthetic upload text\n"),
        ("data/records.unknown", b"synthetic data text\n"),
        (".clerksan-demo/metadata.json", b'{"synthetic": true}\n'),
        (".clerksan-runtime/api.log", b"synthetic runtime log\n"),
        (".clerksan-backup/export.sql", b"SELECT 'synthetic';\n"),
        ("doc_store/plaintext", b"synthetic stored document\n"),
        ("backups/export.sql", b"SELECT 'synthetic';\n"),
        ("assets/opaque.custom", b"\x00\x01\x02synthetic-binary"),
    ],
)
def test_extensionless_unknown_binary_and_upload_paths_fail_closed_but_source_is_allowed(
    repository: Path,
    relative: str,
    content: bytes,
) -> None:
    _stage(repository, relative, content)
    _stage(repository, "scripts/review-helper", b"#!/bin/sh\nexit 0\n")
    _stage(repository, "clerksan/review_helper.py", b"VALUE = 'synthetic'\n")

    result = _run(repository)

    assert result.returncode == 1
    assert result.stderr.count("data-file-outside-generated-allowlist") == 1
    assert Path(relative).name not in result.stderr
    assert "review-helper" not in result.stderr


def test_reviewed_source_code_and_extensionless_script_are_allowed(repository: Path) -> None:
    _stage(repository, "scripts/review-helper", b"#!/bin/sh\nexit 0\n")
    _stage(repository, "clerksan/review_helper.py", b"VALUE = 'synthetic'\n")
    _stage(repository, "docs/review-notes.md", b"# Synthetic review notes\n")
    _stage(
        repository,
        "web/src/features/documents/documents-view.tsx",
        b"export const synthetic = true;\n",
    )
    _stage(repository, "clerksan/reports/renderer.py", b"VALUE = 'synthetic'\n")

    result = _run(repository)

    assert result.returncode == 0, result.stderr


def test_local_pattern_file_detects_name_content_and_whole_blob_without_echo(
    repository: Path,
) -> None:
    synthetic_name_marker = "synthetic-sensitive-name-9q"
    synthetic_text_marker = "SYNTHETIC_PRIVATE_VALUE_4K8"
    blob = b"independently-generated-whole-blob"
    digest = hashlib.sha256(blob).hexdigest()
    _stage(repository, f"notes/{synthetic_name_marker}.txt", b"safe text")
    _stage(repository, "notes/value.txt", synthetic_text_marker.encode())
    _stage(repository, "notes/blob.txt", blob)
    _stage(repository, ".gitignore", b".private-patterns\n")
    pattern_file = repository / ".private-patterns"
    pattern_file.write_text(
        "\n".join(
            (
                f"name:{synthetic_name_marker}",
                f"text:{synthetic_text_marker}",
                f"sha256:{digest}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run(repository, "--local-pattern-file", str(pattern_file))
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert "local-pattern-name" in diagnostic
    assert "local-pattern-content" in diagnostic
    assert "local-private-blob" in diagnostic
    assert synthetic_name_marker not in diagnostic
    assert synthetic_text_marker not in diagnostic
    assert digest not in diagnostic


def test_pattern_file_inside_repository_must_be_ignored_and_untracked(repository: Path) -> None:
    pattern_file = repository / "patterns.local"
    pattern_file.write_text("text:SYNTHETIC_ONLY\n", encoding="utf-8")

    result = _run(repository, "--local-pattern-file", str(pattern_file))

    assert result.returncode == 2
    assert "pattern-file-must-be-local-and-ignored" in result.stderr
    assert str(pattern_file) not in result.stderr


def test_report_requires_exact_approval_and_approval_does_not_bypass_content_scan(
    repository: Path,
) -> None:
    report = "plans/example/reports/sanitized-check.md"
    _stage(repository, report, b"Synthetic verification only.\n")

    blocked = _run(repository)
    assert blocked.returncode == 1
    assert "report-requires-explicit-approval" in blocked.stderr
    assert "sanitized-check" not in blocked.stderr

    approved = _run(repository, "--allow-report", report)
    assert approved.returncode == 0, approved.stderr

    local_path = "/" + "Users" + "/synthetic-only/private.csv"
    _stage(repository, report, f"machine path: {local_path}\n".encode())
    still_blocked = _run(repository, "--allow-report", report)
    assert still_blocked.returncode == 1
    assert "local-absolute-path" in still_blocked.stderr
    assert local_path not in still_blocked.stderr


def test_staged_mode_reads_index_blob_instead_of_unstaged_worktree(repository: Path) -> None:
    path = _stage(repository, "notes/check.txt", b"safe staged content\n")
    local_path = "/" + "home" + "/synthetic-only/private.csv"
    path.write_text(local_path, encoding="utf-8")

    result = _run(repository)

    assert result.returncode == 0, result.stderr


def test_generic_documentation_paths_are_not_treated_as_private_local_paths(
    repository: Path,
) -> None:
    _stage(
        repository,
        "docs/container-layout.md",
        b"Use /home/app/cache or /Users/example/project in this generic example.\n",
    )

    result = _run(repository)

    assert result.returncode == 0, result.stderr


def test_staged_mode_detects_macos_private_temp_path_without_echo(repository: Path) -> None:
    private_root = "/" + "private/var/folders"
    marker = "synthetic-private-temp-owner-4m"
    local_path = f"{private_root}/{marker}/T/artifact.json"
    _stage(repository, "notes/check.txt", f"machine path: {local_path}\n".encode())

    result = _run(repository)
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert "local-absolute-path" in diagnostic
    assert marker not in diagnostic
    assert local_path not in diagnostic


def test_tree_mode_detects_unc_path_without_echo(repository: Path) -> None:
    marker = "synthetic-private-server-2k"
    local_path = "\\" * 2 + marker + "\\private-share\\artifact.json"
    _stage(repository, "notes/check.txt", f"machine path: {local_path}\n".encode())
    _commit(repository, "unsafe UNC tree")

    result = _run_mode(repository, "--tree", "HEAD")
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert "local-absolute-path" in diagnostic
    assert marker not in diagnostic
    assert local_path not in diagnostic


def test_staged_mode_allows_source_escaped_backslashes(repository: Path) -> None:
    _stage(
        repository,
        "src/escaped-examples.py",
        b'RTF = rb"{\\\\rtf1\\\\ansi}"\n'
        b'WINDOWS_PATH = "saved_receipts\\\\missing.png,legacy,[]\\n"\n',
    )

    result = _run(repository)

    assert result.returncode == 0, result.stderr


def test_range_mode_detects_macos_private_temp_path_without_echo(repository: Path) -> None:
    _stage(repository, "notes/check.txt", b"safe baseline\n")
    base = _commit(repository, "baseline")
    private_root = "/" + "private/var/folders"
    marker = "synthetic-private-range-temp-7x"
    local_path = f"{private_root}/{marker}/T/artifact.json"
    _stage(repository, "notes/check.txt", f"machine path: {local_path}\n".encode())
    head = _commit(repository, "unsafe private temp path")

    result = _run_mode(repository, "--range", f"{base}..{head}")
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert "local-absolute-path" in diagnostic
    assert marker not in diagnostic
    assert local_path not in diagnostic


def test_renamed_data_file_uses_the_destination_index_name(repository: Path) -> None:
    original = _stage(repository, "notes/source.txt", b"synthetic only\n")
    _git(
        repository,
        "-c",
        "user.name=Clerk-san Test",
        "-c",
        "user.email=test.invalid@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "synthetic baseline",
    )
    destination = repository / "uploads" / "renamed.csv"
    destination.parent.mkdir()
    original.rename(destination)
    _git(repository, "add", "--all")

    result = _run(repository)

    assert result.returncode == 1
    assert "data-file-outside-generated-allowlist" in result.stderr
    assert "renamed.csv" not in result.stderr


def test_staged_flag_is_mandatory(repository: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--staged is required" in result.stderr
    assert os.fspath(repository) not in result.stderr


def test_tree_mode_scans_the_complete_root_commit_and_redacts_content(
    repository: Path,
) -> None:
    private_owner = "synthetic-tree-owner-6q"
    private_path = f"/{'Users'}/{private_owner}/source.csv"
    _stage(repository, "notes/root-review.txt", f"machine path: {private_path}\n".encode())
    _commit(repository, "root tree")

    result = _run_mode(repository, "--tree", "HEAD")

    assert result.returncode == 1
    assert "local-absolute-path" in result.stderr
    assert private_owner not in result.stderr
    assert private_path not in result.stderr
    assert "root-review.txt" not in result.stderr


def test_tree_mode_reads_commit_blobs_instead_of_the_index(repository: Path) -> None:
    _stage(repository, "notes/check.txt", b"safe committed content\n")
    _commit(repository, "safe root")
    _stage(repository, "uploads/private.csv", b"synthetic,only\n1,2\n")

    result = _run_mode(repository, "--tree", "HEAD")

    assert result.returncode == 0, result.stderr
    assert "tree artifacts passed" in result.stdout


def test_staged_mode_rejects_symlink_entry_without_echo(repository: Path) -> None:
    marker = "synthetic-private-staged-link-5k"
    (repository / marker).symlink_to("safe-target.txt")
    _git(repository, "add", "--", marker)

    result = _run(repository)
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert "unsupported-git-entry" in result.stderr
    assert marker not in diagnostic


def test_tree_mode_rejects_symlink_entry_without_echo(repository: Path) -> None:
    marker = "synthetic-private-tree-link-2v"
    (repository / marker).symlink_to("safe-target.txt")
    _git(repository, "add", "--", marker)
    _commit(repository, "symlink root")

    result = _run_mode(repository, "--tree", "HEAD")
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert "unsupported-git-entry" in result.stderr
    assert marker not in diagnostic


def test_range_mode_rejects_symlink_entry_without_echo(repository: Path) -> None:
    _stage(repository, "notes/baseline.txt", b"safe baseline\n")
    base = _commit(repository, "baseline")
    marker = "synthetic-private-range-link-9d"
    (repository / marker).symlink_to("notes/baseline.txt")
    _git(repository, "add", "--", marker)
    head = _commit(repository, "symlink change")

    result = _run_mode(repository, "--range", f"{base}..{head}")
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert "unsupported-git-entry" in result.stderr
    assert marker not in diagnostic


def test_range_mode_scans_added_data_file_and_redacts_destination(repository: Path) -> None:
    _stage(repository, "notes/baseline.txt", b"safe baseline\n")
    base = _commit(repository, "baseline")
    marker = "synthetic-range-upload-8v"
    _stage(repository, f"uploads/{marker}.csv", b"fake,only\n1,2\n")
    head = _commit(repository, "add upload")

    result = _run_mode(repository, "--range", f"{base}..{head}")

    assert result.returncode == 1
    assert "data-file-outside-generated-allowlist" in result.stderr
    assert marker not in result.stderr
    assert "fake,only" not in result.stderr


def test_range_mode_scans_modified_revision_blob_not_worktree(repository: Path) -> None:
    path = _stage(repository, "notes/check.txt", b"safe baseline\n")
    base = _commit(repository, "baseline")
    private_owner = "synthetic-range-owner-2m"
    private_path = f"/{'home'}/{private_owner}/private.csv"
    _stage(repository, "notes/check.txt", f"machine path: {private_path}\n".encode())
    head = _commit(repository, "unsafe revision")
    path.write_text("safe worktree replacement\n", encoding="utf-8")

    result = _run_mode(repository, "--range", f"{base}..{head}")

    assert result.returncode == 1
    assert "local-absolute-path" in result.stderr
    assert private_owner not in result.stderr
    assert private_path not in result.stderr


def test_range_mode_uses_renamed_destination_path(repository: Path) -> None:
    source = _stage(repository, "notes/source.txt", b"synthetic only\n")
    base = _commit(repository, "baseline")
    marker = "synthetic-renamed-destination-3x"
    destination = repository / "uploads" / f"{marker}.csv"
    destination.parent.mkdir()
    source.rename(destination)
    _git(repository, "add", "--all")
    head = _commit(repository, "rename into data path")

    result = _run_mode(repository, "--range", f"{base}..{head}")

    assert result.returncode == 1
    assert "data-file-outside-generated-allowlist" in result.stderr
    assert marker not in result.stderr


def test_range_mode_blocks_binary_source_blob(repository: Path) -> None:
    _stage(repository, "scripts/review-helper", b"#!/bin/sh\nexit 0\n")
    base = _commit(repository, "baseline")
    _stage(repository, "scripts/review-helper", b"\x00\x01synthetic-binary")
    head = _commit(repository, "binary replacement")

    result = _run_mode(repository, "--range", f"{base}..{head}")

    assert result.returncode == 1
    assert "data-file-outside-generated-allowlist" in result.stderr
    assert "synthetic-binary" not in result.stderr
    assert "review-helper" not in result.stderr


def test_range_mode_preserves_report_approval_and_content_scan(repository: Path) -> None:
    _stage(repository, "notes/baseline.txt", b"safe baseline\n")
    base = _commit(repository, "baseline")
    report = "plans/example/reports/reviewed-result.md"
    _stage(repository, report, b"Synthetic verification only.\n")
    head = _commit(repository, "add report")

    blocked = _run_mode(repository, "--range", f"{base}..{head}")
    approved = _run_mode(
        repository,
        "--range",
        f"{base}..{head}",
        "--allow-report",
        report,
    )

    assert blocked.returncode == 1
    assert "report-requires-explicit-approval" in blocked.stderr
    assert "reviewed-result" not in blocked.stderr
    assert approved.returncode == 0, approved.stderr


def test_revision_modes_preserve_generated_fixture_exact_digest_policy(
    repository: Path,
) -> None:
    fixture_path = "tests/fixtures/generated/universal-intake-1200x13.csv"
    manifest_path = "tests/fixtures/generated/manifest.json"
    _stage(repository, "notes/baseline.txt", b"safe baseline\n")
    base = _commit(repository, "baseline")
    _stage(repository, fixture_path, (ROOT / fixture_path).read_bytes())
    _stage(repository, manifest_path, (ROOT / manifest_path).read_bytes())
    accepted_head = _commit(repository, "canonical generated fixture")

    range_result = _run_mode(repository, "--range", f"{base}..{accepted_head}")
    tree_result = _run_mode(repository, "--tree", accepted_head)

    assert range_result.returncode == 0, range_result.stderr
    assert tree_result.returncode == 0, tree_result.stderr

    _stage(
        repository,
        fixture_path,
        (ROOT / fixture_path).read_bytes() + b"synthetic-tamper\n",
    )
    tampered_head = _commit(repository, "tampered generated fixture")
    rejected = _run_mode(
        repository,
        "--range",
        f"{accepted_head}..{tampered_head}",
    )

    assert rejected.returncode == 1
    assert "generated-fixture-digest-mismatch" in rejected.stderr
    assert "synthetic-tamper" not in rejected.stderr


@pytest.mark.parametrize(
    "arguments",
    (
        ("--tree", "synthetic-private-invalid-ref-7j"),
        ("--range", "HEAD...synthetic-private-invalid-head-5p"),
        ("--range", "synthetic-private-invalid-base-4k..HEAD"),
        ("--range", "HEAD..synthetic-private-invalid-head-9w"),
    ),
)
def test_invalid_revision_inputs_fail_closed_without_echo(
    repository: Path,
    arguments: tuple[str, str],
) -> None:
    _stage(repository, "notes/baseline.txt", b"safe baseline\n")
    _commit(repository, "baseline")

    result = _run_mode(repository, *arguments)
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 2
    assert "privacy gate error" in result.stderr
    assert "synthetic-private" not in diagnostic
    assert os.fspath(repository) not in diagnostic


def test_scan_modes_are_mutually_exclusive(repository: Path) -> None:
    result = _run_mode(repository, "--staged", "--tree", "HEAD")

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr


def test_repository_argument_selects_tree_target_independent_of_cwd(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "--quiet")
    _stage(target, "notes/reviewed.txt", b"synthetic only\n")
    _commit(target, "reviewed target")

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "--quiet")
    _stage(decoy, "uploads/private.csv", b"synthetic,only\n1,2\n")
    _commit(decoy, "blocked decoy")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repository",
            str(target),
            "--tree",
            "HEAD",
        ],
        cwd=decoy,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "tree artifacts passed" in result.stdout
    assert os.fspath(target) not in result.stdout + result.stderr
    assert os.fspath(decoy) not in result.stdout + result.stderr
