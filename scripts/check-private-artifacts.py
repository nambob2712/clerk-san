#!/usr/bin/env python3
"""Fail closed when Git artifacts could expose private document data.

Staged mode reads names and blobs from the Git index, never from the working tree. Tree
and range modes resolve immutable Git objects for release and CI checks. Optional private
terms and whole-file hashes belong in an ignored local file and are never echoed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath

GENERATED_ROOT = "tests/fixtures/generated"
GENERATED_MANIFEST = f"{GENERATED_ROOT}/manifest.json"
GENERATED_SCALE_FIXTURE = f"{GENERATED_ROOT}/universal-intake-1200x13.csv"
GENERATED_MANIFEST_FORMAT = 1
GENERATED_GENERATOR = "eval.universal_intake_scale"
GENERATED_SCALE_ROWS = 1_200
GENERATED_SCALE_COLUMNS = 13
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_PATTERN_FILE_BYTES = 1024 * 1024

# Formats that are overwhelmingly likely to be imported document/media bytes rather
# than source code. Paths under data-oriented directories fail closed regardless of
# suffix, and binary inspection catches extensionless or unfamiliar formats.
DATA_BEARING_SUFFIXES = frozenset(
    {
        ".7z",
        ".aac",
        ".avi",
        ".bmp",
        ".bz2",
        ".csv",
        ".dat",
        ".db",
        ".doc",
        ".docm",
        ".docx",
        ".eml",
        ".gif",
        ".gz",
        ".heic",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".msg",
        ".ods",
        ".ofx",
        ".ogg",
        ".parquet",
        ".pdf",
        ".png",
        ".ppt",
        ".pptm",
        ".pptx",
        ".rar",
        ".rtf",
        ".sqlite",
        ".tar",
        ".tgz",
        ".tif",
        ".tiff",
        ".tsv",
        ".wav",
        ".webp",
        ".webm",
        ".xls",
        ".xlsm",
        ".xlsx",
        ".xz",
        ".zip",
    }
)
DATA_ROOT_DIRECTORIES = frozenset(
    {
        "attachments",
        "backups",
        "data",
        "doc_store",
        "documents",
        "exports",
        "imports",
        "invoices",
        "receipts",
        "saved_receipts",
        "samples",
        "uploads",
    }
)
SOURCE_DIRECTORY_PREFIXES = (("web", "src", "features", "documents"),)
DATA_TREE_PREFIXES = (
    ("eval", "fixtures"),
    ("tests", "fixtures"),
)
DATA_METADATA_PATHS = frozenset(
    {GENERATED_MANIFEST, "eval/fixtures/README.md", "tests/fixtures/README.md"}
)

LOCAL_ABSOLUTE_PATHS = (
    re.compile(
        rb"(?i)(?<![a-z0-9])(?:file://)?/(?:users|home|volumes)/"
        rb"(?P<owner>[^/\x00\r\n\t \"'<>]+)[^\x00\r\n\t \"'<>]*"
    ),
    re.compile(
        rb"(?i)(?<![a-z0-9])[a-z]:[\\/]+users[\\/]+"
        rb"(?P<owner>[^\\/\x00\r\n\t \"'<>]+)[^\x00\r\n\t \"'<>]*"
    ),
    re.compile(
        rb"(?i)(?<![a-z0-9])(?:file://)?/(?:private/var/folders|var/folders)/"
        rb"(?P<owner>[^/\x00\r\n\t \"'<>]+)[^\x00\r\n\t \"'<>]*"
    ),
    re.compile(
        rb"(?i)(?<!\\)\\\\"
        rb"(?P<owner>[a-z0-9](?:[a-z0-9_.-]{0,253}[a-z0-9])?)"
        rb"\\(?!\\)[^\\/\x00\r\n\t \"'<>]+[^\x00\r\n\t \"'<>]*"
    ),
)
GENERIC_LOCAL_PATH_OWNERS = frozenset(
    {
        b"app",
        b"example",
        b"localhost",
        b"runner",
        b"server",
        b"user",
        b"username",
        b"your-name",
        b"yourname",
    }
)
HEX_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
HEX_OBJECT_ID = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
GIT_FILE_MODES = frozenset({"100644", "100755"})


class GateError(RuntimeError):
    """An operational/configuration error whose details must remain redacted."""


@dataclass(frozen=True, order=True)
class Violation:
    rule: str
    display_path: str


@dataclass(frozen=True)
class LocalPatterns:
    names: tuple[bytes, ...] = ()
    texts: tuple[bytes, ...] = ()
    hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RevisionEntry:
    mode: str
    object_type: str
    object_id: str
    size: int | None


BlobLoader = Callable[[str], bytes]


def _git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise GateError("git-command-failed") from exc
    if result.returncode != 0:
        raise GateError("git-command-failed")
    return result.stdout


def _repo_root(path_value: str) -> Path:
    try:
        requested_root = Path(path_value).expanduser().resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise GateError("invalid-git-worktree") from exc
    if not requested_root.is_dir():
        raise GateError("invalid-git-worktree")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=requested_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise GateError("git-command-failed") from exc
    if result.returncode != 0:
        raise GateError("not-a-git-worktree")
    try:
        return Path(os.fsdecode(result.stdout.rstrip(b"\n"))).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise GateError("invalid-git-worktree") from exc


def _normalize_repo_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise GateError("invalid-repository-path")
    return path.as_posix()


def _staged_paths(root: Path) -> list[str]:
    output = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z", "--")
    raw_paths = [item for item in output.split(b"\0") if item]
    return sorted({os.fsdecode(item) for item in raw_paths}, key=os.fsencode)


def _index_entries(root: Path) -> dict[str, RevisionEntry]:
    output = _git(root, "ls-files", "--stage", "-z", "--")
    entries: dict[str, RevisionEntry] = {}
    for raw_record in output.split(b"\0"):
        if not raw_record:
            continue
        metadata, separator, raw_path = raw_record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or not raw_path:
            raise GateError("unsupported-index-entry")
        raw_mode, raw_object_id, raw_stage = fields
        try:
            mode = raw_mode.decode("ascii")
            object_id = raw_object_id.decode("ascii")
            stage = int(raw_stage)
        except (UnicodeDecodeError, ValueError) as exc:
            raise GateError("unsupported-index-entry") from exc
        path = os.fsdecode(raw_path)
        normalized = _normalize_repo_path(path)
        if (
            normalized != path
            or normalized in entries
            or stage != 0
            or not HEX_OBJECT_ID.fullmatch(object_id)
        ):
            raise GateError("unsupported-index-entry")
        entries[normalized] = RevisionEntry(mode, "blob", object_id, None)
    return entries


def _index_blob_size(root: Path, object_id: str) -> int:
    raw = _git(root, "cat-file", "-s", object_id).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise GateError("unsupported-index-entry") from exc


def _index_blob(root: Path, entries: dict[str, RevisionEntry], path: str) -> bytes:
    entry = entries.get(path)
    if entry is None or entry.mode not in GIT_FILE_MODES:
        raise GateError("unsupported-index-entry")
    if _git(root, "cat-file", "-t", entry.object_id).strip() != b"blob":
        raise GateError("unsupported-index-entry")
    if _index_blob_size(root, entry.object_id) > MAX_SCAN_BYTES:
        raise GateError("index-blob-too-large")
    return _git(root, "cat-file", "blob", entry.object_id)


def _resolved_object(root: Path, value: str, kind: str) -> str:
    if not value or "\0" in value or "\r" in value or "\n" in value:
        raise GateError("invalid-revision")
    raw = _git(
        root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{value}^{{{kind}}}",
    ).strip()
    try:
        object_id = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GateError("invalid-revision") from exc
    if not HEX_OBJECT_ID.fullmatch(object_id):
        raise GateError("invalid-revision")
    return object_id


def _tree_entries(root: Path, tree_id: str) -> dict[str, RevisionEntry]:
    output = _git(root, "ls-tree", "-r", "-z", "--full-tree", "-l", tree_id)
    entries: dict[str, RevisionEntry] = {}
    for raw_record in output.split(b"\0"):
        if not raw_record:
            continue
        metadata, separator, raw_path = raw_record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 4 or not raw_path:
            raise GateError("invalid-revision-tree")
        raw_mode, raw_type, raw_object_id, raw_size = fields
        try:
            mode = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_object_id.decode("ascii")
            size = None if raw_size == b"-" else int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise GateError("invalid-revision-tree") from exc
        if not HEX_OBJECT_ID.fullmatch(object_id) or size is not None and size < 0:
            raise GateError("invalid-revision-tree")
        path = os.fsdecode(raw_path)
        normalized = _normalize_repo_path(path)
        if normalized != path or normalized in entries:
            raise GateError("invalid-revision-tree")
        entries[normalized] = RevisionEntry(mode, object_type, object_id, size)
    return entries


def _revision_blob(root: Path, entries: dict[str, RevisionEntry], path: str) -> bytes:
    entry = entries.get(path)
    if entry is None:
        raise GateError("revision-blob-unavailable")
    if entry.mode not in GIT_FILE_MODES or entry.object_type != "blob":
        raise GateError("unsupported-revision-entry")
    if entry.size is None:
        raise GateError("revision-blob-unavailable")
    if entry.size > MAX_SCAN_BYTES:
        raise GateError("revision-blob-too-large")
    return _git(root, "cat-file", "blob", entry.object_id)


def _parse_revision_range(value: str) -> tuple[str, str]:
    if "..." in value or value.count("..") != 1:
        raise GateError("invalid-revision-range")
    base, head = value.split("..", 1)
    if not base or not head:
        raise GateError("invalid-revision-range")
    return base, head


def _range_destination_paths(
    root: Path,
    base_id: str,
    head_id: str,
) -> tuple[list[str], set[str]]:
    output = _git(
        root,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--diff-filter=ACMRTD",
        base_id,
        head_id,
        "--",
    )
    fields = output.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    destinations: set[str] = set()
    touched: set[str] = set()
    index = 0
    while index < len(fields):
        raw_status = fields[index]
        index += 1
        try:
            status = raw_status.decode("ascii")
        except UnicodeDecodeError as exc:
            raise GateError("invalid-revision-range") from exc
        if not status or status[0] not in "ACMRTD":
            raise GateError("invalid-revision-range")
        path_count = 2 if status[0] in "CR" else 1
        if index + path_count > len(fields):
            raise GateError("invalid-revision-range")
        raw_paths = fields[index : index + path_count]
        index += path_count
        normalized_paths: list[str] = []
        for raw_path in raw_paths:
            path = os.fsdecode(raw_path)
            normalized = _normalize_repo_path(path)
            if normalized != path:
                raise GateError("invalid-revision-range")
            normalized_paths.append(normalized)
        destination = normalized_paths[-1]
        touched.update(normalized_paths)
        if status[0] != "D":
            destinations.add(destination)
    return sorted(destinations, key=os.fsencode), touched


def _is_data_bearing(path: str) -> bool:
    if path in DATA_METADATA_PATHS:
        return False
    parsed = PurePosixPath(path)
    suffix = parsed.suffix.lower()
    if suffix in DATA_BEARING_SUFFIXES:
        return True
    directory_parts = tuple(part.lower() for part in parsed.parts[:-1])
    if not directory_parts:
        return False
    root = directory_parts[0]
    if root.startswith(".clerksan-") or root in DATA_ROOT_DIRECTORIES:
        return True
    inspected_parts = directory_parts
    for prefix in SOURCE_DIRECTORY_PREFIXES:
        if inspected_parts[: len(prefix)] == prefix:
            inspected_parts = inspected_parts[len(prefix) :]
            break
    if any(
        part.startswith(".clerksan-") or part in DATA_ROOT_DIRECTORIES for part in inspected_parts
    ):
        return True
    return any(directory_parts[: len(prefix)] == prefix for prefix in DATA_TREE_PREFIXES)


def _looks_binary(blob: bytes) -> bool:
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return any(character < " " and character not in "\t\n\r\f" for character in text)


def _contains_private_local_path(blob: bytes) -> bool:
    for pattern in LOCAL_ABSOLUTE_PATHS:
        for match in pattern.finditer(blob):
            if match.group("owner").lower() not in GENERIC_LOCAL_PATH_OWNERS:
                return True
    return False


def _is_report(path: str) -> bool:
    parts = tuple(part.lower() for part in PurePosixPath(path).parts)
    return (
        len(parts) >= 3 and parts[0] == "plans" and "reports" in parts[1:-1]
    ) or path.lower().startswith("eval/results/")


def _path_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _load_local_patterns(root: Path, path_value: str | None) -> LocalPatterns:
    if not path_value:
        return LocalPatterns()
    path = Path(path_value).expanduser()
    if path.is_symlink():
        raise GateError("pattern-file-must-not-be-a-symlink")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise GateError("pattern-file-unavailable") from exc
    try:
        if not path.is_file() or path.stat().st_size > MAX_PATTERN_FILE_BYTES:
            raise GateError("invalid-pattern-file")
    except OSError as exc:
        raise GateError("invalid-pattern-file") from exc

    if _path_inside(path, root):
        relative = path.relative_to(root).as_posix()
        try:
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relative],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            ignored = subprocess.run(
                ["git", "check-ignore", "--quiet", "--", relative],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            raise GateError("git-command-failed") from exc
        if tracked.returncode == 0 or ignored.returncode != 0:
            raise GateError("pattern-file-must-be-local-and-ignored")

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise GateError("invalid-pattern-file") from exc

    names: set[bytes] = set()
    texts: set[bytes] = set()
    hashes: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        prefix, separator, value = line.partition(":")
        if not separator or not value:
            raise GateError("invalid-pattern-file-entry")
        if prefix == "name":
            names.add(value.encode("utf-8"))
        elif prefix == "text":
            texts.add(value.encode("utf-8"))
        elif prefix == "sha256" and HEX_SHA256.fullmatch(value.lower()):
            hashes.add(value.lower())
        else:
            raise GateError("invalid-pattern-file-entry")
    return LocalPatterns(tuple(sorted(names)), tuple(sorted(texts)), tuple(sorted(hashes)))


def _expected_generated_scale_fixture() -> tuple[bytes, dict[str, object]]:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [f"synthetic_field_{column:02d}" for column in range(1, GENERATED_SCALE_COLUMNS + 1)]
    )
    for row in range(1, GENERATED_SCALE_ROWS + 1):
        writer.writerow(
            [
                f"synthetic-r{row:04d}-c{column:02d}"
                for column in range(1, GENERATED_SCALE_COLUMNS + 1)
            ]
        )
    payload = output.getvalue().encode("utf-8")
    manifest: dict[str, object] = {
        "format": GENERATED_MANIFEST_FORMAT,
        "generated": True,
        "generator": GENERATED_GENERATOR,
        "files": [
            {
                "path": GENERATED_SCALE_FIXTURE,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "rows": GENERATED_SCALE_ROWS,
                "columns": GENERATED_SCALE_COLUMNS,
            }
        ],
    }
    return payload, manifest


def _generated_allowlist(load_blob: BlobLoader) -> tuple[dict[str, str], str | None]:
    try:
        raw = load_blob(GENERATED_MANIFEST)
        manifest = json.loads(raw)
    except (GateError, json.JSONDecodeError, UnicodeDecodeError):
        return {}, "generated-manifest-invalid"
    expected_payload, expected_manifest = _expected_generated_scale_fixture()
    if manifest != expected_manifest:
        return {}, "generated-manifest-invalid"
    return {GENERATED_SCALE_FIXTURE: hashlib.sha256(expected_payload).hexdigest()}, None


def _scan(
    paths: list[str],
    load_blob: BlobLoader,
    patterns: LocalPatterns,
    allowed_reports: set[str],
    *,
    policy_paths: set[str] | None = None,
) -> list[Violation]:
    violations: set[Violation] = set()
    inspected_policy_paths = set(paths) if policy_paths is None else policy_paths
    generated_candidates = {
        path
        for path in inspected_policy_paths
        if path.startswith(f"{GENERATED_ROOT}/") and _is_data_bearing(path)
    }
    generated_allowlist: dict[str, str] = {}
    manifest_error: str | None = None
    if generated_candidates or GENERATED_MANIFEST in inspected_policy_paths:
        generated_allowlist, manifest_error = _generated_allowlist(load_blob)
        if manifest_error:
            violations.add(Violation(manifest_error, GENERATED_MANIFEST))
        else:
            for generated_path, expected_digest in generated_allowlist.items():
                try:
                    generated_blob = load_blob(generated_path)
                except GateError:
                    violations.add(
                        Violation(
                            "generated-fixture-unavailable",
                            "<redacted-generated-path>",
                        )
                    )
                    continue
                if hashlib.sha256(generated_blob).hexdigest() != expected_digest:
                    violations.add(
                        Violation(
                            "generated-fixture-digest-mismatch",
                            "<redacted-generated-path>",
                        )
                    )

    for path in paths:
        normalized = _normalize_repo_path(path)
        data_bearing = _is_data_bearing(normalized)
        if data_bearing and normalized not in generated_allowlist:
            violations.add(
                Violation("data-file-outside-generated-allowlist", "<redacted-data-path>")
            )
        if _is_report(normalized) and normalized not in allowed_reports:
            violations.add(Violation("report-requires-explicit-approval", "<redacted-report-path>"))

        try:
            blob = load_blob(normalized)
        except GateError as exc:
            if str(exc) in {"index-blob-too-large", "revision-blob-too-large"}:
                rule = "blob-too-large-to-scan"
            elif str(exc) in {"unsupported-index-entry", "unsupported-revision-entry"}:
                rule = "unsupported-git-entry"
            else:
                rule = "blob-unavailable"
            violations.add(Violation(rule, "<redacted-path>"))
            continue

        if not data_bearing and normalized not in generated_allowlist and _looks_binary(blob):
            violations.add(
                Violation("data-file-outside-generated-allowlist", "<redacted-data-path>")
            )

        if normalized in generated_allowlist:
            if hashlib.sha256(blob).hexdigest() != generated_allowlist[normalized]:
                violations.add(
                    Violation("generated-fixture-digest-mismatch", "<redacted-generated-path>")
                )

        path_bytes = os.fsencode(normalized)
        if any(pattern in path_bytes for pattern in patterns.names):
            violations.add(Violation("local-pattern-name", "<redacted-path>"))
        if any(pattern in blob for pattern in patterns.texts):
            violations.add(Violation("local-pattern-content", "<redacted-path>"))
        if patterns.hashes and hashlib.sha256(blob).hexdigest() in patterns.hashes:
            violations.add(Violation("local-private-blob", "<redacted-path>"))
        if _contains_private_local_path(blob):
            violations.add(Violation("local-absolute-path", "<redacted-path>"))

    return sorted(violations)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Git artifacts for private data")
    parser.add_argument(
        "--repository",
        default=".",
        metavar="PATH",
        help="Git worktree to scan (defaults to the current directory)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--staged",
        action="store_true",
        help="scan names and blobs in the Git index",
    )
    mode.add_argument(
        "--tree",
        metavar="REF",
        help="scan every destination path and blob in one resolved Git tree",
    )
    mode.add_argument(
        "--range",
        dest="revision_range",
        metavar="BASE..HEAD",
        help="scan changed destination paths and blobs between two resolved commits",
    )
    parser.add_argument(
        "--local-pattern-file",
        default=os.environ.get("CLERKSAN_PRIVATE_PATTERN_FILE"),
        help="ignored local file containing name:, text:, and sha256: entries",
    )
    parser.add_argument(
        "--allow-report",
        action="append",
        default=[],
        metavar="REPO_PATH",
        help="explicitly approve one reviewed staged report path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.staged and args.tree is None and args.revision_range is None:
        print("privacy gate error: --staged is required", file=sys.stderr)
        return 2
    try:
        root = _repo_root(args.repository)
        allowed_reports = {_normalize_repo_path(path) for path in args.allow_report}
        patterns = _load_local_patterns(root, args.local_pattern_file)
        if args.staged:
            paths = _staged_paths(root)
            entries = _index_entries(root)
            if any(path not in entries for path in paths):
                raise GateError("unsupported-index-entry")
            load_blob = partial(_index_blob, root, entries)
            policy_paths = set(paths)
            success_label = "staged artifacts"
        elif args.tree is not None:
            tree_id = _resolved_object(root, args.tree, "tree")
            entries = _tree_entries(root, tree_id)
            paths = sorted(entries, key=os.fsencode)
            load_blob = partial(_revision_blob, root, entries)
            policy_paths = set(paths)
            success_label = "tree artifacts"
        else:
            base, head = _parse_revision_range(args.revision_range)
            base_id = _resolved_object(root, base, "commit")
            head_id = _resolved_object(root, head, "commit")
            head_tree_id = _resolved_object(root, head_id, "tree")
            entries = _tree_entries(root, head_tree_id)
            paths, policy_paths = _range_destination_paths(root, base_id, head_id)
            if any(path not in entries for path in paths):
                raise GateError("invalid-revision-range")
            load_blob = partial(_revision_blob, root, entries)
            success_label = "range artifacts"
        violations = _scan(
            paths,
            load_blob,
            patterns,
            allowed_reports,
            policy_paths=policy_paths,
        )
    except GateError as exc:
        print(f"privacy gate error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("privacy gate error: unexpected-failure", file=sys.stderr)
        return 2

    if violations:
        for violation in violations:
            print(
                f"privacy gate: {violation.rule}: {violation.display_path}",
                file=sys.stderr,
            )
        print(f"privacy gate blocked {len(violations)} rule/path pair(s)", file=sys.stderr)
        return 1
    print(f"privacy gate: {success_label} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
