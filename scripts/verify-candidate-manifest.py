#!/usr/bin/env python3
"""Create and verify Clerk-san format-1 source candidate manifests.

The manifest is deliberately stored outside the candidate directory. Diagnostics identify
rules but never print candidate paths, file bytes, or manifest values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FORMAT = 1
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
HEX_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
HEX_GIT_OBJECT_ID = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
GIT_FILE_MODES = frozenset({"100644", "100755"})


class ManifestError(RuntimeError):
    """A redacted operational or manifest-format error."""


@dataclass(frozen=True)
class CandidateEntry:
    path: str
    mode: str
    size: int
    sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True, order=True)
class Violation:
    rule: str
    path: str


def _normalized_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ManifestError("invalid-manifest")
    if value.startswith("/") or value.endswith("/"):
        raise ManifestError("invalid-manifest")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestError("invalid-manifest")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ManifestError("invalid-manifest") from exc
    return value


def _candidate_root(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise ManifestError("candidate-root-must-not-be-a-symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ManifestError("candidate-unavailable") from exc
    if not resolved.is_dir():
        raise ManifestError("candidate-unavailable")
    return resolved


def _path_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _manifest_path(candidate: Path, value: str, *, must_exist: bool) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise ManifestError("manifest-must-not-be-a-symlink")
    try:
        if must_exist:
            resolved = raw.resolve(strict=True)
        else:
            resolved = raw.parent.resolve(strict=True) / raw.name
    except OSError as exc:
        raise ManifestError("manifest-unavailable") from exc
    if _path_inside(resolved, candidate):
        raise ManifestError("manifest-must-be-external")
    if must_exist:
        try:
            if not resolved.is_file() or resolved.stat().st_size > MAX_MANIFEST_BYTES:
                raise ManifestError("invalid-manifest")
        except OSError as exc:
            raise ManifestError("invalid-manifest") from exc
    elif resolved.exists():
        raise ManifestError("manifest-already-exists")
    return resolved


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise ManifestError("git-command-failed") from exc
    if result.returncode != 0:
        raise ManifestError("git-command-failed")
    return result.stdout


def _repository_root(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise ManifestError("repository-root-must-not-be-a-symlink")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise ManifestError("repository-unavailable") from exc
    if not path.is_dir():
        raise ManifestError("repository-unavailable")
    raw_root = _git(path, "rev-parse", "--show-toplevel").rstrip(b"\n")
    try:
        root = Path(os.fsdecode(raw_root)).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ManifestError("repository-unavailable") from exc
    if not root.is_dir():
        raise ManifestError("repository-unavailable")
    return root


def _resolved_tree(repository: Path, value: str) -> str:
    if not value or "\0" in value or "\r" in value or "\n" in value:
        raise ManifestError("invalid-tree")
    raw = _git(
        repository,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{value}^{{tree}}",
    ).strip()
    try:
        tree_id = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ManifestError("invalid-tree") from exc
    if not HEX_GIT_OBJECT_ID.fullmatch(tree_id):
        raise ManifestError("invalid-tree")
    return tree_id


def _file_entry(path: Path, relative_path: str) -> CandidateEntry:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManifestError("candidate-unreadable") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ManifestError("candidate-changed-during-scan")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
            final = os.fstat(stream.fileno())
        if not stat.S_ISREG(final.st_mode) or size != final.st_size:
            raise ManifestError("candidate-changed-during-scan")
    except OSError as exc:
        raise ManifestError("candidate-unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    mode = "100755" if final.st_mode & stat.S_IXUSR else "100644"
    return CandidateEntry(relative_path, mode, size, digest.hexdigest())


def _snapshot(candidate: Path) -> tuple[list[CandidateEntry], list[Violation]]:
    entries: list[CandidateEntry] = []
    violations: set[Violation] = set()

    def visit(directory: Path, parent_parts: tuple[str, ...]) -> None:
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise ManifestError("candidate-unreadable") from exc
        normalized_children: list[tuple[bytes, str, os.DirEntry[str]]] = []
        for child in children:
            relative_path = "/".join((*parent_parts, child.name))
            try:
                normalized = _normalized_path(relative_path)
                encoded = normalized.encode("utf-8", errors="strict")
            except ManifestError as exc:
                raise ManifestError("candidate-path-is-not-normalized") from exc
            normalized_children.append((encoded, normalized, child))

        for _encoded, relative_path, child in sorted(normalized_children):
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ManifestError("candidate-unreadable") from exc
            file_type = metadata.st_mode
            if stat.S_ISLNK(file_type):
                violations.add(Violation("symlink-not-allowed", relative_path))
                continue
            if child.name == ".git":
                rule = "git-metadata-not-allowed" if not parent_parts else "submodule-not-allowed"
                violations.add(Violation(rule, relative_path))
                continue
            if stat.S_ISDIR(file_type):
                visit(Path(child.path), (*parent_parts, child.name))
            elif stat.S_ISREG(file_type):
                entries.append(_file_entry(Path(child.path), relative_path))
            else:
                violations.add(Violation("special-file-not-allowed", relative_path))

    visit(candidate, ())
    entries.sort(key=lambda entry: entry.path.encode("utf-8"))
    return entries, sorted(violations)


def _git_blob_entry(
    repository: Path,
    object_id: str,
    relative_path: str,
    mode: str,
    expected_size: int,
) -> CandidateEntry:
    try:
        process = subprocess.Popen(
            ["git", "cat-file", "blob", object_id],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ManifestError("git-command-failed") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        if process.stdout is None:
            raise ManifestError("git-command-failed")
        while chunk := process.stdout.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        process.stdout.close()
        returncode = process.wait()
    except (OSError, ManifestError) as exc:
        process.kill()
        process.wait()
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError("git-command-failed") from exc
    if returncode != 0 or size != expected_size:
        raise ManifestError("git-command-failed")
    return CandidateEntry(relative_path, mode, size, digest.hexdigest())


def _git_tree_snapshot(
    repository: Path,
    tree_id: str,
) -> tuple[list[CandidateEntry], list[Violation]]:
    output = _git(repository, "ls-tree", "-r", "-z", "--full-tree", "-l", tree_id)
    entries: list[CandidateEntry] = []
    violations: set[Violation] = set()
    seen_paths: set[str] = set()
    for raw_record in output.split(b"\0"):
        if not raw_record:
            continue
        metadata, separator, raw_path = raw_record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 4 or not raw_path:
            raise ManifestError("invalid-tree")
        raw_mode, raw_type, raw_object_id, raw_size = fields
        try:
            mode = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_object_id.decode("ascii")
            path = os.fsdecode(raw_path)
            normalized = _normalized_path(path)
        except (UnicodeDecodeError, ManifestError) as exc:
            raise ManifestError("invalid-tree") from exc
        if normalized != path or normalized in seen_paths:
            raise ManifestError("invalid-tree")
        seen_paths.add(normalized)
        if not HEX_GIT_OBJECT_ID.fullmatch(object_id):
            raise ManifestError("invalid-tree")
        if mode == "120000":
            violations.add(Violation("symlink-not-allowed", normalized))
            continue
        if mode == "160000" or object_type == "commit":
            violations.add(Violation("submodule-not-allowed", normalized))
            continue
        if mode not in GIT_FILE_MODES or object_type != "blob" or raw_size == b"-":
            violations.add(Violation("unsupported-git-entry", normalized))
            continue
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise ManifestError("invalid-tree") from exc
        if size < 0:
            raise ManifestError("invalid-tree")
        entries.append(_git_blob_entry(repository, object_id, normalized, mode, size))
    entries.sort(key=lambda entry: entry.path.encode("utf-8"))
    return entries, sorted(violations)


def _document(source_revision: str, entries: list[CandidateEntry]) -> dict[str, object]:
    if not HEX_GIT_OBJECT_ID.fullmatch(source_revision):
        raise ManifestError("invalid-source-revision")
    return {
        "files": [entry.as_json() for entry in entries],
        "format": MANIFEST_FORMAT,
        "source_revision": source_revision,
    }


def _canonical_bytes(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _validated_document(raw: bytes) -> tuple[dict[str, object], list[CandidateEntry]]:
    try:
        document = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("invalid-manifest") from exc
    if not isinstance(document, dict) or set(document) != {
        "files",
        "format",
        "source_revision",
    }:
        raise ManifestError("invalid-manifest")
    if type(document["format"]) is not int or document["format"] != MANIFEST_FORMAT:
        raise ManifestError("unsupported-manifest-format")
    source_revision = document["source_revision"]
    if not isinstance(source_revision, str) or not HEX_GIT_OBJECT_ID.fullmatch(source_revision):
        raise ManifestError("invalid-manifest")
    raw_files = document["files"]
    if not isinstance(raw_files, list):
        raise ManifestError("invalid-manifest")

    entries: list[CandidateEntry] = []
    previous_path: bytes | None = None
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "mode",
            "path",
            "sha256",
            "size",
        }:
            raise ManifestError("invalid-manifest")
        path = _normalized_path(raw_entry["path"])
        encoded_path = path.encode("utf-8")
        if previous_path is not None and encoded_path <= previous_path:
            raise ManifestError("invalid-manifest")
        previous_path = encoded_path
        mode = raw_entry["mode"]
        size = raw_entry["size"]
        sha256 = raw_entry["sha256"]
        if not isinstance(mode, str) or mode not in GIT_FILE_MODES:
            raise ManifestError("invalid-manifest")
        if type(size) is not int or size < 0:
            raise ManifestError("invalid-manifest")
        if not isinstance(sha256, str) or not HEX_SHA256.fullmatch(sha256):
            raise ManifestError("invalid-manifest")
        entries.append(CandidateEntry(path, mode, size, sha256))

    canonical_document = _document(source_revision, entries)
    canonical = _canonical_bytes(canonical_document)
    if raw != canonical:
        raise ManifestError("manifest-not-canonical")
    return canonical_document, entries


def _write_manifest(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o644)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ManifestError("manifest-write-failed") from exc


def _comparison_violations(
    expected: list[CandidateEntry],
    actual: list[CandidateEntry],
) -> list[Violation]:
    expected_by_path = {entry.path: entry for entry in expected}
    actual_by_path = {entry.path: entry for entry in actual}
    violations: set[Violation] = set()
    for path in expected_by_path.keys() - actual_by_path.keys():
        violations.add(Violation("missing-file", path))
    for path in actual_by_path.keys() - expected_by_path.keys():
        violations.add(Violation("extra-file", path))
    for path in expected_by_path.keys() & actual_by_path.keys():
        expected_entry = expected_by_path[path]
        actual_entry = actual_by_path[path]
        if expected_entry.mode != actual_entry.mode:
            violations.add(Violation("mode-mismatch", path))
        if expected_entry.size != actual_entry.size:
            violations.add(Violation("size-mismatch", path))
        if expected_entry.sha256 != actual_entry.sha256:
            violations.add(Violation("sha256-mismatch", path))
    return sorted(violations)


def _read_manifest(path: Path) -> tuple[bytes, dict[str, object], list[CandidateEntry]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError("manifest-unavailable") from exc
    document, entries = _validated_document(raw)
    return raw, document, entries


def _print_violations(violations: list[Violation]) -> None:
    for violation in violations:
        print(f"candidate manifest: {violation.rule}: <redacted-path>", file=sys.stderr)
    print(
        f"candidate manifest blocked {len(violations)} rule/path pair(s)",
        file=sys.stderr,
    )


def _create(args: argparse.Namespace) -> int:
    candidate = _candidate_root(args.candidate)
    manifest = _manifest_path(candidate, args.manifest, must_exist=False)
    entries, violations = _snapshot(candidate)
    if violations:
        _print_violations(violations)
        return 1
    document = _document(args.source_revision, entries)
    payload = _canonical_bytes(document)
    _write_manifest(manifest, payload)
    digest = hashlib.sha256(payload).hexdigest()
    print(
        f"candidate manifest: created format={MANIFEST_FORMAT} files={len(entries)} sha256={digest}"
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    candidate = _candidate_root(args.candidate)
    manifest = _manifest_path(candidate, args.manifest, must_exist=True)
    raw, document, expected = _read_manifest(manifest)
    actual, structural_violations = _snapshot(candidate)
    violations = sorted({*structural_violations, *_comparison_violations(expected, actual)})
    if violations:
        _print_violations(violations)
        return 1
    digest = hashlib.sha256(raw).hexdigest()
    print(
        f"candidate manifest: verified format={document['format']} "
        f"files={len(expected)} sha256={digest}"
    )
    return 0


def _verify_tree(args: argparse.Namespace) -> int:
    repository = _repository_root(args.repository)
    manifest = _manifest_path(repository, args.manifest, must_exist=True)
    raw, document, expected = _read_manifest(manifest)
    tree_id = _resolved_tree(repository, args.tree)
    actual, structural_violations = _git_tree_snapshot(repository, tree_id)
    violations = sorted({*structural_violations, *_comparison_violations(expected, actual)})
    if violations:
        _print_violations(violations)
        return 1
    digest = hashlib.sha256(raw).hexdigest()
    print(
        f"candidate manifest: verified tree={tree_id} format={document['format']} "
        f"files={len(expected)} sha256={digest}"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify an external canonical source candidate manifest"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a canonical manifest")
    create.add_argument("--candidate", required=True, help="candidate source directory")
    create.add_argument("--manifest", required=True, help="new external manifest path")
    create.add_argument(
        "--source-revision",
        required=True,
        help="full lowercase source Git object ID",
    )
    create.set_defaults(handler=_create)

    verify = commands.add_parser("verify", help="verify a candidate against a manifest")
    verify.add_argument("--candidate", required=True, help="candidate source directory")
    verify.add_argument("--manifest", required=True, help="external manifest path")
    verify.set_defaults(handler=_verify)

    verify_tree = commands.add_parser(
        "verify-tree",
        help="verify an immutable Git tree against a canonical external manifest",
    )
    verify_tree.add_argument(
        "--repository",
        required=True,
        help="candidate Git repository",
    )
    verify_tree.add_argument("--manifest", required=True, help="external manifest path")
    verify_tree.add_argument("--tree", required=True, help="candidate commit or tree reference")
    verify_tree.set_defaults(handler=_verify_tree)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except ManifestError as exc:
        print(f"candidate manifest error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("candidate manifest error: unexpected-failure", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
