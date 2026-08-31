from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify-candidate-manifest.py"
SOURCE_REVISION = "a" * 40


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _create(candidate: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        "create",
        "--candidate",
        str(candidate),
        "--manifest",
        str(manifest),
        "--source-revision",
        SOURCE_REVISION,
    )


def _verify(candidate: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        "verify",
        "--candidate",
        str(candidate),
        "--manifest",
        str(manifest),
    )


def _verify_tree(
    repository: Path,
    manifest: Path,
    tree: str = "HEAD",
) -> subprocess.CompletedProcess[str]:
    return _run(
        "verify-tree",
        "--repository",
        str(repository),
        "--manifest",
        str(manifest),
        "--tree",
        tree,
    )


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _initialize_and_commit(candidate: Path, *, force_add: bool = False) -> str:
    _git(candidate, "init", "--quiet")
    _git(candidate, "add", "-f" if force_add else "--all", ".")
    _git(
        candidate,
        "-c",
        "user.name=Clerk-san Test",
        "-c",
        "user.email=test.invalid@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "candidate root",
    )
    return _git(candidate, "rev-parse", "HEAD")


@pytest.fixture
def candidate_manifest(tmp_path: Path) -> tuple[Path, Path]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "bin").mkdir()
    script = candidate / "bin" / "review"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    (candidate / "notes.txt").write_bytes(b"synthetic only\n")
    manifest = tmp_path / "candidate-manifest.json"
    result = _create(candidate, manifest)
    assert result.returncode == 0, result.stderr
    return candidate, manifest


def test_create_writes_canonical_format_one_and_verify_accepts_it(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "z-last.txt").write_bytes(b"last\n")
    executable = candidate / "a-first.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (candidate / "日本語.txt").write_bytes("合成データのみ\n".encode())
    manifest = tmp_path / "candidate-manifest.json"

    created = _create(candidate, manifest)
    verified = _verify(candidate, manifest)

    assert created.returncode == 0, created.stderr
    assert verified.returncode == 0, verified.stderr
    raw = manifest.read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    document = json.loads(raw)
    assert set(document) == {"files", "format", "source_revision"}
    assert document["format"] == 1
    assert document["source_revision"] == SOURCE_REVISION
    paths = [entry["path"] for entry in document["files"]]
    assert paths == sorted(paths, key=lambda path: path.encode("utf-8"))
    entries = {entry["path"]: entry for entry in document["files"]}
    assert entries["a-first.sh"]["mode"] == "100755"
    assert entries["z-last.txt"]["mode"] == "100644"
    assert entries["z-last.txt"]["size"] == len(b"last\n")
    assert entries["z-last.txt"]["sha256"] == hashlib.sha256(b"last\n").hexdigest()
    expected = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    assert raw == expected


def test_create_accepts_exact_sha256_object_id_length(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "README.md").write_text("# Synthetic candidate\n", encoding="utf-8")
    manifest = tmp_path / "candidate-manifest.json"
    source_revision = "b" * 64

    result = _run(
        "create",
        "--candidate",
        str(candidate),
        "--manifest",
        str(manifest),
        "--source-revision",
        source_revision,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(manifest.read_text(encoding="utf-8"))["source_revision"] == source_revision


def test_verify_tree_binds_manifest_to_exact_committed_tree(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "README.md").write_text("# Synthetic candidate\n", encoding="utf-8")
    executable = candidate / "review.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    manifest = tmp_path / "candidate-manifest.json"
    created = _create(candidate, manifest)
    assert created.returncode == 0, created.stderr
    _initialize_and_commit(candidate)

    result = _verify_tree(candidate, manifest)

    assert result.returncode == 0, result.stderr
    assert "verified tree=" in result.stdout
    assert "files=2" in result.stdout


def test_verify_tree_rejects_file_omitted_by_git_ignore(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    marker = "synthetic-private-ignored-marker-4w"
    (candidate / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
    (candidate / ".DS_Store").write_text(marker, encoding="utf-8")
    (candidate / "README.md").write_text("# Synthetic candidate\n", encoding="utf-8")
    manifest = tmp_path / "candidate-manifest.json"
    created = _create(candidate, manifest)
    assert created.returncode == 0, created.stderr
    _initialize_and_commit(candidate)

    result = _verify_tree(candidate, manifest)
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert "missing-file" in result.stderr
    assert marker not in diagnostic
    assert os.fspath(candidate) not in diagnostic


@pytest.mark.parametrize(
    ("kind", "expected_rule"),
    (
        ("symlink", "symlink-not-allowed"),
        ("submodule", "submodule-not-allowed"),
    ),
)
def test_verify_tree_rejects_unsupported_git_entries_with_redaction(
    tmp_path: Path,
    kind: str,
    expected_rule: str,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "README.md").write_text("# Synthetic candidate\n", encoding="utf-8")
    manifest = tmp_path / "candidate-manifest.json"
    created = _create(candidate, manifest)
    assert created.returncode == 0, created.stderr
    head = _initialize_and_commit(candidate)
    marker = "synthetic-private-tree-entry-7p"
    if kind == "symlink":
        (candidate / marker).symlink_to("README.md")
        _git(candidate, "add", "--", marker)
    else:
        _git(candidate, "update-index", "--add", "--cacheinfo", f"160000,{head},{marker}")
    _git(
        candidate,
        "-c",
        "user.name=Clerk-san Test",
        "-c",
        "user.email=test.invalid@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "unsupported tree entry",
    )

    result = _verify_tree(candidate, manifest)
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert expected_rule in result.stderr
    assert marker not in diagnostic
    assert os.fspath(candidate) not in diagnostic


@pytest.mark.parametrize(
    ("tamper", "expected_rule"),
    (
        ("extra", "extra-file"),
        ("missing", "missing-file"),
        ("mode", "mode-mismatch"),
        ("size", "size-mismatch"),
        ("hash", "sha256-mismatch"),
    ),
)
def test_verify_rejects_membership_and_blob_tampering_with_redacted_diagnostics(
    candidate_manifest: tuple[Path, Path],
    tamper: str,
    expected_rule: str,
) -> None:
    candidate, manifest = candidate_manifest
    marker = "synthetic-private-candidate-marker-8q"
    notes = candidate / "notes.txt"
    if tamper == "extra":
        (candidate / f"{marker}.txt").write_text("private-marker-content", encoding="utf-8")
    elif tamper == "missing":
        notes.unlink()
    elif tamper == "mode":
        notes.chmod(0o755)
    elif tamper == "size":
        notes.write_bytes(b"synthetic content with a changed size\n")
    else:
        notes.write_bytes(b"synthetic else\n")

    result = _verify(candidate, manifest)
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert expected_rule in result.stderr
    assert marker not in diagnostic
    assert "private-marker-content" not in diagnostic
    assert os.fspath(candidate) not in diagnostic


def test_verify_rejects_noncanonical_or_invalid_manifest_without_echo(
    candidate_manifest: tuple[Path, Path],
) -> None:
    candidate, manifest = candidate_manifest
    document = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    noncanonical = _verify(candidate, manifest)

    assert noncanonical.returncode == 2
    assert "manifest-not-canonical" in noncanonical.stderr
    assert os.fspath(candidate) not in noncanonical.stderr

    invalid_marker = "synthetic-private-invalid-path-3m"
    document["files"][0]["path"] = f"../{invalid_marker}"
    manifest.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    invalid = _verify(candidate, manifest)

    assert invalid.returncode == 2
    assert "invalid-manifest" in invalid.stderr
    assert invalid_marker not in invalid.stderr


def test_verify_rejects_non_git_object_id_length_in_manifest(
    candidate_manifest: tuple[Path, Path],
) -> None:
    candidate, manifest = candidate_manifest
    document = json.loads(manifest.read_text(encoding="utf-8"))
    marker = "a" * 41
    document["source_revision"] = marker
    manifest.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    result = _verify(candidate, manifest)

    assert result.returncode == 2
    assert "invalid-manifest" in result.stderr
    assert marker not in result.stderr


@pytest.mark.parametrize(
    ("kind", "expected_rule"),
    (
        ("symlink", "symlink-not-allowed"),
        ("special", "special-file-not-allowed"),
        ("submodule", "submodule-not-allowed"),
    ),
)
def test_create_rejects_symlinks_special_files_and_submodules_with_redaction(
    tmp_path: Path,
    kind: str,
    expected_rule: str,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    marker = "synthetic-private-structure-marker-5n"
    if kind == "symlink":
        (candidate / f"{marker}-link").symlink_to(f"{marker}-target")
    elif kind == "special":
        os.mkfifo(candidate / f"{marker}-fifo")
    else:
        submodule = candidate / f"{marker}-dependency"
        submodule.mkdir()
        (submodule / ".git").write_text(f"gitdir: /private/{marker}\n", encoding="utf-8")
    manifest = tmp_path / "candidate-manifest.json"

    result = _create(candidate, manifest)
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 1
    assert expected_rule in result.stderr
    assert marker not in diagnostic
    assert os.fspath(candidate) not in diagnostic
    assert not manifest.exists()


def test_manifest_must_remain_outside_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "notes.txt").write_text("synthetic only\n", encoding="utf-8")
    internal_manifest = candidate / "candidate-manifest.json"

    result = _create(candidate, internal_manifest)

    assert result.returncode == 2
    assert "manifest-must-be-external" in result.stderr
    assert os.fspath(candidate) not in result.stderr
    assert not internal_manifest.exists()


@pytest.mark.parametrize(
    "source_revision",
    (
        "synthetic-private-source-revision-7r",
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
    ),
)
def test_invalid_source_revision_fails_closed_without_echo(
    tmp_path: Path,
    source_revision: str,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "notes.txt").write_text("synthetic only\n", encoding="utf-8")
    manifest = tmp_path / "candidate-manifest.json"
    result = _run(
        "create",
        "--candidate",
        str(candidate),
        "--manifest",
        str(manifest),
        "--source-revision",
        source_revision,
    )

    assert result.returncode == 2
    assert "invalid-source-revision" in result.stderr
    assert source_revision not in result.stderr
    assert not manifest.exists()
