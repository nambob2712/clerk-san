#!/usr/bin/env python3
"""Validate and render Clerk-san's exact source-lock license policy.

The policy covers references in requirements.lock and web/package-lock.json only. It deliberately
does not approve redistributed wheels, emitted bundles, images, OS packages, services, or models.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = Path("sbom/license-policy.json")
SBOM_PATH = Path("sbom/source-dependencies.spdx.json")
NOTICES_PATH = Path("THIRD_PARTY_NOTICES.md")

EXPECTED_TOP_LEVEL_KEYS = {
    "artifactCaveats",
    "artifactPolicy",
    "evidence",
    "licenseRules",
    "locks",
    "npmDerivation",
    "pythonPackages",
    "schemaVersion",
    "scope",
    "waivers",
}
EXPECTED_EXCLUDED_SCOPES = [
    "application-and-parser-images",
    "base-and-operating-system-packages",
    "emitted-web-bundles",
    "external-service-images",
    "model-weights-and-manifests",
    "python-wheels-and-sdists",
]
EXPECTED_FORBIDDEN_FAMILIES = [
    "AGPL",
    "BUSL",
    "EULA",
    "GPL",
    "LGPL",
    "SSPL",
    "UNLICENSED",
]
EXPECTED_NO_ASSERTION_VALUES = ["", "NONE", "NOASSERTION"]
EXPECTED_LOCKS = {
    "npm": {
        "expectedRows": 183,
        "identityFields": ["packageLockPath", "purl"],
        "path": "web/package-lock.json",
    },
    "python": {
        "expectedRows": 81,
        "identityFields": ["purl", "normalizedMarkerIdentity"],
        "path": "requirements.lock",
    },
}
EXPECTED_REVIEW_PATH = "sbom/source-license-review.md"
EXPECTED_REVIEW_BINDING_KEYS = {
    "npmIdentitiesSha256",
    "npmLockSha256",
    "policyMachineSha256",
    "pythonIdentitiesSha256",
    "pythonLockSha256",
}
REVIEW_BINDINGS_START = "<!-- clerksan-source-license-bindings-v1:start -->"
REVIEW_BINDINGS_END = "<!-- clerksan-source-license-bindings-v1:end -->"
EXPECTED_NPM_DERIVATION = {
    "licenseField": "packages[*].license",
    "scopeRule": (
        "packages[*].dev == true => development-build-test; otherwise production-lock-closure"
    ),
}
EXPECTED_POLICY_PACKAGE_KEYS = {
    "artifactReviewRequired",
    "licenseConcluded",
    "licenseDeclared",
    "normalizedMarkerIdentity",
    "purl",
    "sourceScope",
}
EXPECTED_WAIVER_KEYS = {
    "evidence",
    "expiresOn",
    "id",
    "identity",
    "licenseExpression",
    "owner",
    "reason",
    "violationCode",
}
WAIVABLE_VIOLATIONS = {
    "forbidden-license-family",
    "npm-mpl-production-scope",
    "unreviewed-license-expression",
}
FORBIDDEN_PACKAGE_NAMES = {"fitz", "pymupdf"}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PYTHON_REQUIREMENT_HEADER = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^ ;\\\r\n]+)"
    r"(?:[ \t]+;([^\\\r\n]+))?[ \t]+\\$"
)
PYTHON_HASH_LINE = re.compile(r"^--hash=sha256:[0-9a-f]{64}(?:[ \t]+\\)?$")
WAIVER_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
MARKER_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\")|"
    r"(?P<operator>===|==|!=|<=|>=|~=|<|>)|(?P<paren>[()])|"
    r"(?P<word>[A-Za-z_][A-Za-z0-9_.-]*))"
)


@dataclass(frozen=True)
class SourcePackage:
    ecosystem: str
    purl: str
    download_location: str
    license_declared: str
    license_concluded: str
    source_scope: str
    normalized_marker: str = "unconditional"
    package_lock_path: str | None = None
    artifact_review_required: bool = False

    @property
    def identity(self) -> str:
        if self.ecosystem == "pypi":
            return f"pypi:{self.purl}|marker:{self.normalized_marker}"
        return f"npm:{self.package_lock_path}|purl:{self.purl}"


@dataclass
class PolicyContext:
    root: Path
    policy: dict[str, Any]
    policy_bytes: bytes
    python_lock_bytes: bytes
    npm_lock_bytes: bytes
    packages: list[SourcePackage]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _expect_keys(
    value: object,
    expected: set[str],
    label: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{label}: expected an object")
        return None
    actual = set(value)
    if actual != expected:
        errors.append(
            f"{label}: keys differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}"
        )
    return value


def _relative_file(root: Path, locator: object, label: str, errors: list[str]) -> Path | None:
    if not isinstance(locator, str) or not locator:
        errors.append(f"{label}: locator must be a non-empty relative path")
        return None
    relative = Path(locator)
    if relative.is_absolute() or ".." in relative.parts:
        errors.append(f"{label}: locator must stay inside the repository")
        return None
    root = root.resolve()
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            errors.append(f"{label}: symlinked evidence paths are not allowed")
            return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        errors.append(f"{label}: evidence must be an existing regular file")
        return None
    if not resolved.is_relative_to(root):
        errors.append(f"{label}: locator must stay inside the repository")
        return None
    if not resolved.is_file():
        errors.append(f"{label}: evidence must be an existing regular file")
        return None
    return resolved


def normalize_marker(raw_marker: str | None) -> str:
    """Return a stable marker identity without evaluating it for the current host."""

    if raw_marker is None or not raw_marker.strip():
        return "unconditional"
    raw = raw_marker.strip()
    tokens: list[str] = []
    cursor = 0
    while cursor < len(raw):
        match = MARKER_TOKEN.match(raw, cursor)
        if match is None:
            raise ValueError(f"unsupported marker syntax near {raw[cursor:]!r}")
        cursor = match.end()
        if match.group("string") is not None:
            parsed = ast.literal_eval(match.group("string"))
            if not isinstance(parsed, str):
                raise ValueError("marker literal is not a string")
            tokens.append(json.dumps(parsed, ensure_ascii=False))
        elif match.group("word") is not None:
            word = match.group("word")
            tokens.append(word.lower() if word.lower() in {"and", "in", "not", "or"} else word)
        else:
            tokens.append(match.group("operator") or match.group("paren"))
    return " ".join(tokens).replace("( ", "(").replace(" )", ")")


def _parse_python_lock(lock_bytes: bytes, errors: list[str]) -> list[tuple[str, str]]:
    try:
        source = lock_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"requirements.lock: not valid UTF-8: {exc}")
        return []

    blocks: list[tuple[int, list[str]]] = []
    lines = source.splitlines()
    cursor = 0
    while cursor < len(lines):
        raw_line = lines[cursor]
        stripped = raw_line.strip()
        line_number = cursor + 1
        cursor += 1
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            errors.append(
                f"requirements.lock:{line_number}: stray requirement continuation or option"
            )
            continue
        block = [stripped]
        while block[-1].endswith("\\"):
            if cursor >= len(lines):
                errors.append(f"requirements.lock:{line_number}: unterminated requirement block")
                break
            continuation = lines[cursor].strip()
            cursor += 1
            if not continuation or continuation.startswith("#"):
                errors.append(f"requirements.lock:{line_number}: invalid requirement continuation")
                break
            block.append(continuation)
        blocks.append((line_number, block))

    identities: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line_number, block in blocks:
        match = PYTHON_REQUIREMENT_HEADER.fullmatch(block[0])
        if match is None:
            errors.append(
                f"requirements.lock:{line_number}: unsupported installable requirement; "
                "an exact name==version pin is required"
            )
            continue
        if len(block) < 2 or any(PYTHON_HASH_LINE.fullmatch(line) is None for line in block[1:]):
            errors.append(
                f"requirements.lock:{line_number}: requirement must contain only SHA-256 hashes"
            )
            continue
        name, version, raw_marker = match.groups()
        try:
            marker = normalize_marker(raw_marker)
        except (SyntaxError, ValueError) as exc:
            errors.append(f"requirements.lock:{name}=={version}: invalid marker: {exc}")
            continue
        purl = f"pkg:pypi/{quote(name, safe='/')}@{version}"
        identity = (purl, marker)
        if identity in seen:
            errors.append(f"pypi:{purl}|marker:{marker}: duplicate lock identity")
        seen.add(identity)
        identities.append(identity)
    return identities


def _identity_digest(identities: list[str]) -> str:
    return _sha256(_canonical_json(sorted(identities)))


def _review_bindings(
    policy: dict[str, Any],
    python_lock_bytes: bytes,
    npm_lock_bytes: bytes,
    python_identities: list[tuple[str, str]],
    npm_rows: list[SourcePackage],
) -> dict[str, str]:
    machine_policy = {key: value for key, value in policy.items() if key != "evidence"}
    return {
        "npmIdentitiesSha256": _identity_digest([row.identity for row in npm_rows]),
        "npmLockSha256": _sha256(npm_lock_bytes),
        "policyMachineSha256": _sha256(_canonical_json(machine_policy)),
        "pythonIdentitiesSha256": _identity_digest(
            [f"pypi:{purl}|marker:{marker}" for purl, marker in python_identities]
        ),
        "pythonLockSha256": _sha256(python_lock_bytes),
    }


def _review_document_bindings(review_bytes: bytes, errors: list[str]) -> dict[str, Any] | None:
    try:
        source = review_bytes.decode("utf-8")
    except UnicodeDecodeError:
        errors.append("policy.evidence[0]: review evidence is not valid UTF-8")
        return None
    if source.count(REVIEW_BINDINGS_START) != 1 or source.count(REVIEW_BINDINGS_END) != 1:
        errors.append("policy.evidence[0]: exactly one machine review binding block is required")
        return None
    payload = source.split(REVIEW_BINDINGS_START, 1)[1].split(REVIEW_BINDINGS_END, 1)[0].strip()
    if not payload.startswith("```json\n") or not payload.endswith("\n```"):
        errors.append("policy.evidence[0]: malformed machine review binding block")
        return None
    encoded = payload.removeprefix("```json\n").removesuffix("\n```")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        errors.append("policy.evidence[0]: invalid machine review binding JSON")
        return None
    if encoded + "\n" != _canonical_json(value).decode():
        errors.append("policy.evidence[0]: machine review binding JSON must be canonical")
    return _expect_keys(
        value,
        EXPECTED_REVIEW_BINDING_KEYS,
        "policy.evidence[0].reviewBindings",
        errors,
    )


def _parse_npm_lock(lock_bytes: bytes, errors: list[str]) -> list[SourcePackage]:
    try:
        document = json.loads(lock_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"web/package-lock.json: invalid JSON: {exc}")
        return []
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, dict):
        errors.append("web/package-lock.json: packages must be an object")
        return []

    rows: list[SourcePackage] = []
    seen_paths: set[str] = set()
    for package_path, metadata in packages.items():
        if package_path == "":
            continue
        if not isinstance(package_path, str) or not isinstance(metadata, dict):
            errors.append(f"web/package-lock.json: invalid package row at {package_path!r}")
            continue
        if metadata.get("link"):
            continue
        name = metadata.get("name")
        if not isinstance(name, str) and "node_modules/" in package_path:
            name = package_path.rsplit("node_modules/", 1)[-1]
        version = metadata.get("version")
        provisional = f"npm:{package_path}"
        if not isinstance(name, str) or not name:
            errors.append(f"{provisional}: missing package name")
            continue
        if not isinstance(version, str) or not version:
            errors.append(f"{provisional}: missing package version")
            continue
        purl = f"pkg:npm/{quote(name, safe='/')}@{version}"
        identity = f"npm:{package_path}|purl:{purl}"
        license_expression = metadata.get("license")
        if not isinstance(license_expression, str) or not license_expression.strip():
            errors.append(f"{identity}: missing npm license")
            license_expression = ""
        resolved = metadata.get("resolved")
        if not isinstance(resolved, str) or not resolved:
            errors.append(f"{identity}: missing npm resolved URL")
            resolved = ""
        else:
            parsed_resolved = urlsplit(resolved)
            if (
                parsed_resolved.scheme != "https"
                or parsed_resolved.netloc != "registry.npmjs.org"
                or not parsed_resolved.path.startswith("/")
                or not parsed_resolved.path.endswith(".tgz")
                or parsed_resolved.query
                or parsed_resolved.fragment
                or any(character.isspace() for character in resolved)
            ):
                errors.append(f"{identity}: unsafe npm resolved URL {resolved!r}")
        if package_path in seen_paths:
            errors.append(f"{identity}: duplicate package-lock path")
        seen_paths.add(package_path)
        scope = (
            "development-build-test" if metadata.get("dev") is True else "production-lock-closure"
        )
        rows.append(
            SourcePackage(
                ecosystem="npm",
                package_lock_path=package_path,
                purl=purl,
                download_location=resolved,
                license_declared=license_expression,
                license_concluded=license_expression,
                source_scope=scope,
            )
        )
    return rows


def _validate_policy_shape(
    root: Path,
    policy: dict[str, Any],
    errors: list[str],
) -> None:
    _expect_keys(policy, EXPECTED_TOP_LEVEL_KEYS, "policy", errors)
    if policy.get("schemaVersion") != 1:
        errors.append("policy.schemaVersion: expected 1")

    scope = _expect_keys(
        policy.get("scope"),
        {"excludedDistributionScopes", "kind", "statement"},
        "policy.scope",
        errors,
    )
    if scope is not None:
        if scope.get("kind") != "source-lock-references":
            errors.append("policy.scope.kind: must be source-lock-references")
        if scope.get("excludedDistributionScopes") != EXPECTED_EXCLUDED_SCOPES:
            errors.append(
                "policy.scope.excludedDistributionScopes: exact artifact exclusions required"
            )
        statement = scope.get("statement")
        if (
            not isinstance(statement, str)
            or "not artifact redistribution clearance" not in statement
        ):
            errors.append("policy.scope.statement: source-only non-clearance statement required")

    artifact_policy = _expect_keys(
        policy.get("artifactPolicy"),
        {"records", "requireSeparateReview", "statement"},
        "policy.artifactPolicy",
        errors,
    )
    if artifact_policy is not None:
        if artifact_policy.get("records") != []:
            errors.append("policy.artifactPolicy.records: source policy must not approve artifacts")
        if artifact_policy.get("requireSeparateReview") is not True:
            errors.append("policy.artifactPolicy.requireSeparateReview: must be true")
        statement = artifact_policy.get("statement")
        required_words = ("wheel", "web bundle", "container", "operating-system", "model-weight")
        if not isinstance(statement, str) or any(word not in statement for word in required_words):
            errors.append("policy.artifactPolicy.statement: incomplete artifact boundary")

    caveats = policy.get("artifactCaveats")
    if not isinstance(caveats, list) or len(caveats) != 1:
        errors.append("policy.artifactCaveats: exactly one pypdfium2 caveat is required")
    else:
        caveat = _expect_keys(
            caveats[0], {"purl", "sourceClassification", "statement"}, "artifactCaveats[0]", errors
        )
        if caveat is not None:
            if caveat.get("purl") != "pkg:pypi/pypdfium2@5.13.0":
                errors.append("artifactCaveats[0]: must identify pypdfium2 5.13.0")
            if caveat.get("sourceClassification") != "Apache-2.0 OR BSD-3-Clause":
                errors.append("artifactCaveats[0]: wrong pypdfium2 source classification")
            statement = caveat.get("statement")
            required_words = ("source-project", "wheel", "platform-specific", "separate artifact")
            if not isinstance(statement, str) or any(
                word not in statement for word in required_words
            ):
                errors.append("artifactCaveats[0]: explicit binary artifact-review caveat required")

    npm_derivation = policy.get("npmDerivation")
    if npm_derivation != EXPECTED_NPM_DERIVATION:
        errors.append("policy.npmDerivation: exact lock-derived license and scope rules required")

    locks = _expect_keys(policy.get("locks"), {"npm", "python"}, "policy.locks", errors)
    if locks is not None:
        for ecosystem, expected in EXPECTED_LOCKS.items():
            lock = _expect_keys(
                locks.get(ecosystem),
                {"expectedRows", "identityFields", "path", "sha256"},
                f"policy.locks.{ecosystem}",
                errors,
            )
            if lock is None:
                continue
            for field, value in expected.items():
                if lock.get(field) != value:
                    errors.append(f"policy.locks.{ecosystem}.{field}: expected {value!r}")
            if not isinstance(lock.get("sha256"), str) or not SHA256_PATTERN.fullmatch(
                lock["sha256"]
            ):
                errors.append(f"policy.locks.{ecosystem}.sha256: invalid SHA-256")

    evidence = policy.get("evidence")
    if not isinstance(evidence, list) or len(evidence) != 1:
        errors.append("policy.evidence: exactly one reviewed audit record is required")
    else:
        record = _expect_keys(
            evidence[0],
            {"bindings", "kind", "locator", "sha256"},
            "policy.evidence[0]",
            errors,
        )
        if record is not None:
            if record.get("kind") != "reviewed-source-license-evidence":
                errors.append("policy.evidence[0].kind: wrong evidence kind")
            if record.get("locator") != EXPECTED_REVIEW_PATH:
                errors.append("policy.evidence[0].locator: wrong public review path")
            bindings = _expect_keys(
                record.get("bindings"),
                EXPECTED_REVIEW_BINDING_KEYS,
                "policy.evidence[0].bindings",
                errors,
            )
            if bindings is not None:
                for name, digest in bindings.items():
                    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
                        errors.append(f"policy.evidence[0].bindings.{name}: invalid SHA-256")
            expected_digest = record.get("sha256")
            if not isinstance(expected_digest, str) or not SHA256_PATTERN.fullmatch(
                expected_digest
            ):
                errors.append("policy.evidence[0].sha256: invalid SHA-256")

    rules = _expect_keys(
        policy.get("licenseRules"),
        {
            "allowedSpdxExpressions",
            "forbiddenFamilies",
            "noAssertionValues",
            "npmMplAllowedScope",
        },
        "policy.licenseRules",
        errors,
    )
    if rules is not None:
        allowed = rules.get("allowedSpdxExpressions")
        if (
            not isinstance(allowed, list)
            or not all(isinstance(item, str) and item for item in allowed)
            or allowed != sorted(set(allowed))
        ):
            errors.append(
                "policy.licenseRules.allowedSpdxExpressions: sorted unique strings required"
            )
        if rules.get("forbiddenFamilies") != EXPECTED_FORBIDDEN_FAMILIES:
            errors.append(
                "policy.licenseRules.forbiddenFamilies: exact fail-closed families required"
            )
        if rules.get("noAssertionValues") != EXPECTED_NO_ASSERTION_VALUES:
            errors.append("policy.licenseRules.noAssertionValues: exact unresolved values required")
        if rules.get("npmMplAllowedScope") != "development-build-test":
            errors.append("policy.licenseRules.npmMplAllowedScope: wrong conditional scope")


def _validate_review_evidence(
    root: Path,
    policy: dict[str, Any],
    python_lock_bytes: bytes,
    npm_lock_bytes: bytes,
    python_identities: list[tuple[str, str]],
    npm_rows: list[SourcePackage],
    errors: list[str],
) -> None:
    raw_evidence = policy.get("evidence")
    if not isinstance(raw_evidence, list) or len(raw_evidence) != 1:
        return
    record = raw_evidence[0]
    if not isinstance(record, dict):
        return
    evidence_path = _relative_file(root, record.get("locator"), "policy.evidence[0]", errors)
    if evidence_path is None:
        return
    try:
        review_bytes = evidence_path.read_bytes()
    except OSError:
        errors.append("policy.evidence[0]: cannot read review evidence")
        return
    expected_digest = record.get("sha256")
    if isinstance(expected_digest, str) and SHA256_PATTERN.fullmatch(expected_digest):
        actual_digest = _sha256(review_bytes)
        if actual_digest != expected_digest:
            errors.append("policy.evidence[0]: review evidence digest drift")

    expected_bindings = _review_bindings(
        policy,
        python_lock_bytes,
        npm_lock_bytes,
        python_identities,
        npm_rows,
    )
    policy_bindings = record.get("bindings")
    if policy_bindings != expected_bindings:
        errors.append(
            "policy.evidence[0]: reviewed lock, identity, or machine-policy binding drift"
        )
    review_bindings = _review_document_bindings(review_bytes, errors)
    if review_bindings != expected_bindings:
        errors.append("policy.evidence[0]: machine review bindings do not match current inputs")

    try:
        review_text = review_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return
    expected_rows = (
        (
            "requirements.lock",
            expected_bindings["pythonLockSha256"],
            len(python_identities),
        ),
        (
            "web/package-lock.json",
            expected_bindings["npmLockSha256"],
            len(npm_rows),
        ),
    )
    for locator, digest, rows in expected_rows:
        table_row = f"| `{locator}` | `{digest}` | {rows} |"
        if table_row not in review_text:
            errors.append(f"policy.evidence[0]: reviewed {locator} table binding is stale")


def _policy_python_packages(policy: dict[str, Any], errors: list[str]) -> list[SourcePackage]:
    raw_packages = policy.get("pythonPackages")
    if not isinstance(raw_packages, list):
        errors.append("policy.pythonPackages: expected a list")
        return []

    rows: list[SourcePackage] = []
    seen: set[tuple[str, str]] = set()
    sort_keys: list[tuple[str, str]] = []
    for index, raw in enumerate(raw_packages):
        label = f"policy.pythonPackages[{index}]"
        package = _expect_keys(raw, EXPECTED_POLICY_PACKAGE_KEYS, label, errors)
        if package is None:
            continue
        purl = package.get("purl")
        marker = package.get("normalizedMarkerIdentity")
        declared = package.get("licenseDeclared")
        concluded = package.get("licenseConcluded")
        source_scope = package.get("sourceScope")
        artifact_review = package.get("artifactReviewRequired")
        if not isinstance(purl, str) or not purl.startswith("pkg:pypi/") or "@" not in purl:
            errors.append(f"{label}: invalid PyPI purl")
            continue
        if not isinstance(marker, str) or not marker:
            errors.append(f"{label}: missing normalized marker identity")
            continue
        try:
            canonical_marker = normalize_marker(None if marker == "unconditional" else marker)
        except (SyntaxError, ValueError) as exc:
            errors.append(f"{label}: invalid normalized marker: {exc}")
            continue
        if canonical_marker != marker:
            errors.append(f"{label}: marker is not canonical; expected {canonical_marker!r}")
        if not isinstance(declared, str) or not isinstance(concluded, str):
            errors.append(f"{label}: license fields must be strings")
            continue
        if source_scope != "source-lock-reference":
            errors.append(f"{label}: sourceScope must be source-lock-reference")
        if not isinstance(artifact_review, bool):
            errors.append(f"{label}: artifactReviewRequired must be boolean")
            continue
        identity = (purl, marker)
        if identity in seen:
            errors.append(f"pypi:{purl}|marker:{marker}: duplicate policy identity")
        seen.add(identity)
        sort_keys.append(identity)
        rows.append(
            SourcePackage(
                ecosystem="pypi",
                purl=purl,
                download_location=_pypi_version_url(purl),
                normalized_marker=marker,
                license_declared=declared,
                license_concluded=concluded,
                source_scope=source_scope if isinstance(source_scope, str) else "",
                artifact_review_required=artifact_review,
            )
        )
    if sort_keys != sorted(sort_keys):
        errors.append("policy.pythonPackages: entries must be sorted by purl and marker")
    return rows


def _pypi_version_url(purl: str) -> str:
    name_and_version = purl.removeprefix("pkg:pypi/")
    encoded_name, separator, version = name_and_version.rpartition("@")
    if not separator or not encoded_name or not version:
        return ""
    name = quote(unquote(encoded_name), safe="")
    return f"https://pypi.org/pypi/{name}/{quote(version, safe='')}/json"


def _compare_python_identities(
    lock_identities: list[tuple[str, str]],
    policy_rows: list[SourcePackage],
    errors: list[str],
) -> None:
    lock_set = set(lock_identities)
    policy_set = {(row.purl, row.normalized_marker) for row in policy_rows}
    for purl, marker in sorted(lock_set - policy_set):
        errors.append(f"pypi:{purl}|marker:{marker}: missing from policy")
    for purl, marker in sorted(policy_set - lock_set):
        errors.append(f"pypi:{purl}|marker:{marker}: extra policy identity")


def _validate_targeted_python_conclusions(
    policy_rows: list[SourcePackage], errors: list[str]
) -> None:
    by_purl = {row.purl: row for row in policy_rows}
    pypdf = by_purl.get("pkg:pypi/pypdf@6.16.2")
    if pypdf is None or {
        pypdf.license_declared,
        pypdf.license_concluded,
    } != {"BSD-3-Clause"}:
        errors.append("pypi:pkg:pypi/pypdf@6.16.2|marker:unconditional: BSD-3-Clause required")
    pypdfium2 = by_purl.get("pkg:pypi/pypdfium2@5.13.0")
    if (
        pypdfium2 is None
        or pypdfium2.license_declared != "Apache-2.0 OR BSD-3-Clause"
        or pypdfium2.license_concluded != "Apache-2.0 OR BSD-3-Clause"
        or not pypdfium2.artifact_review_required
    ):
        errors.append(
            "pypi:pkg:pypi/pypdfium2@5.13.0|marker:unconditional: source classification "
            "and artifact-review caveat required"
        )
    flagged = {row.purl for row in policy_rows if row.artifact_review_required}
    if flagged != {"pkg:pypi/pypdfium2@5.13.0"}:
        errors.append("policy.pythonPackages: only pypdfium2 has an entry-specific artifact caveat")


def _read_and_bind_locks(
    root: Path,
    policy: dict[str, Any],
    errors: list[str],
) -> tuple[bytes, bytes]:
    result: dict[str, bytes] = {}
    locks = policy.get("locks") if isinstance(policy.get("locks"), dict) else {}
    for ecosystem in ("python", "npm"):
        expected = EXPECTED_LOCKS[ecosystem]
        path = root / expected["path"]
        try:
            data = path.read_bytes()
        except OSError as exc:
            errors.append(f"{expected['path']}: cannot read lock: {exc}")
            data = b""
        result[ecosystem] = data
        lock_policy = locks.get(ecosystem) if isinstance(locks, dict) else None
        expected_digest = lock_policy.get("sha256") if isinstance(lock_policy, dict) else None
        actual_digest = _sha256(data)
        if expected_digest != actual_digest:
            errors.append(
                f"{expected['path']}: lock digest drift; expected {expected_digest!r}, "
                f"found {actual_digest}"
            )
    return result["python"], result["npm"]


def _validate_counts(
    policy: dict[str, Any],
    python_identities: list[tuple[str, str]],
    npm_rows: list[SourcePackage],
    errors: list[str],
) -> None:
    locks = policy.get("locks") if isinstance(policy.get("locks"), dict) else {}
    counts = {"python": len(python_identities), "npm": len(npm_rows)}
    for ecosystem, actual in counts.items():
        lock_policy = locks.get(ecosystem) if isinstance(locks, dict) else None
        expected = lock_policy.get("expectedRows") if isinstance(lock_policy, dict) else None
        if actual != expected:
            errors.append(f"{ecosystem} lock: expected {expected!r} rows, found {actual}")
    if counts != {"python": 81, "npm": 183}:
        errors.append(
            f"source inventory: expected 81 Python + 183 npm = 264; "
            f"found {counts['python']} + {counts['npm']} = {sum(counts.values())}"
        )


def _row_lookup(packages: list[SourcePackage]) -> dict[str, SourcePackage]:
    return {row.identity: row for row in packages}


def _validate_waiver_evidence(
    root: Path,
    waiver: dict[str, Any],
    label: str,
    errors: list[str],
) -> bool:
    waiver_id = waiver["id"]
    evidence = _expect_keys(
        waiver.get("evidence"), {"locator", "sha256"}, f"{label}.evidence", errors
    )
    if evidence is None:
        return False
    required_locator = f"sbom/waivers/{waiver_id}.json"
    if evidence.get("locator") != required_locator:
        errors.append(f"{label}.evidence: dedicated evidence path required for this waiver id")
        return False
    evidence_digest = evidence.get("sha256")
    if not isinstance(evidence_digest, str) or not SHA256_PATTERN.fullmatch(evidence_digest):
        errors.append(f"{label}.evidence: valid SHA-256 required")
        return False
    evidence_path = _relative_file(root, evidence.get("locator"), f"{label}.evidence", errors)
    if evidence_path is None:
        return False
    try:
        evidence_bytes = evidence_path.read_bytes()
        evidence_value = json.loads(evidence_bytes)
    except OSError:
        errors.append(f"{label}.evidence: cannot read dedicated waiver evidence")
        return False
    except (UnicodeDecodeError, json.JSONDecodeError):
        errors.append(f"{label}.evidence: dedicated waiver evidence must be valid JSON")
        return False
    expected_value = {key: waiver[key] for key in sorted(EXPECTED_WAIVER_KEYS - {"evidence"})}
    if evidence_bytes != _canonical_json(evidence_value):
        errors.append(f"{label}.evidence: dedicated waiver evidence must be canonical JSON")
        return False
    if evidence_value != expected_value:
        errors.append(
            f"{label}.evidence: evidence must bind the exact waiver id, identity, violation, "
            "expression, owner, reason, and expiry"
        )
        return False
    if _sha256(evidence_bytes) != evidence_digest:
        errors.append(f"{label}.evidence: digest drift")
        return False
    return True


def _validate_waivers(
    root: Path,
    policy: dict[str, Any],
    packages: list[SourcePackage],
    errors: list[str],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    raw_waivers = policy.get("waivers")
    if not isinstance(raw_waivers, list):
        errors.append("policy.waivers: expected a list")
        return {}
    rows = _row_lookup(packages)
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    seen_ids: set[str] = set()
    today = dt.date.today()
    for position, raw in enumerate(raw_waivers):
        label = f"policy.waivers[{position}]"
        waiver = _expect_keys(raw, EXPECTED_WAIVER_KEYS, label, errors)
        if waiver is None:
            continue
        waiver_id = waiver.get("id")
        identity = waiver.get("identity")
        violation = waiver.get("violationCode")
        expression = waiver.get("licenseExpression")
        owner = waiver.get("owner")
        reason = waiver.get("reason")
        if not isinstance(waiver_id, str) or WAIVER_ID_PATTERN.fullmatch(waiver_id) is None:
            errors.append(f"{label}: lowercase kebab-case id required")
            continue
        if waiver_id in seen_ids:
            errors.append(f"{label}: duplicate waiver id {waiver_id!r}")
        seen_ids.add(waiver_id)
        if not isinstance(identity, str) or identity not in rows:
            errors.append(f"{label}: exact existing composite identity required")
            continue
        if violation not in WAIVABLE_VIOLATIONS:
            errors.append(f"{label}: unsupported violationCode")
            continue
        if not isinstance(expression, str) or expression not in {
            rows[identity].license_declared,
            rows[identity].license_concluded,
        }:
            errors.append(f"{label}: licenseExpression must match the exact identity")
            continue
        if not isinstance(owner, str) or not owner.strip():
            errors.append(f"{label}: explicit owner required")
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{label}: explicit reason required")
            continue
        expires_on = waiver.get("expiresOn")
        try:
            expiry = dt.date.fromisoformat(expires_on) if isinstance(expires_on, str) else None
        except ValueError:
            expiry = None
        if expiry is None:
            errors.append(f"{label}: expiresOn must be an ISO date")
            continue
        if expiry <= today:
            errors.append(f"{label}: waiver expired on {expiry.isoformat()}")
            continue
        if not _validate_waiver_evidence(root, waiver, label, errors):
            continue
        key = (identity, violation, expression)
        if key in index:
            errors.append(f"{label}: duplicate waiver for {identity} and {violation}")
            continue
        index[key] = waiver
    return index


def _validate_licenses(
    policy: dict[str, Any],
    packages: list[SourcePackage],
    waiver_index: dict[tuple[str, str, str], dict[str, Any]],
    errors: list[str],
) -> None:
    rules = policy.get("licenseRules") if isinstance(policy.get("licenseRules"), dict) else {}
    allowed_values = rules.get("allowedSpdxExpressions")
    allowed = set(allowed_values) if isinstance(allowed_values, list) else set()
    no_assertion = {item.upper() for item in EXPECTED_NO_ASSERTION_VALUES}
    used_waivers: set[str] = set()

    def violation(row: SourcePackage, code: str, expression: str, message: str) -> None:
        waiver = waiver_index.get((row.identity, code, expression))
        if waiver is None:
            errors.append(f"{row.identity}: {message}; no explicit valid waiver")
        else:
            used_waivers.add(waiver["id"])

    for row in packages:
        for field, expression in (
            ("licenseDeclared", row.license_declared),
            ("licenseConcluded", row.license_concluded),
        ):
            if not expression or expression.upper() in no_assertion:
                errors.append(f"{row.identity}: {field} is unresolved ({expression!r})")
                continue
            upper = expression.upper()
            family = next(
                (item for item in EXPECTED_FORBIDDEN_FAMILIES if item in upper),
                None,
            )
            if family is not None:
                violation(
                    row,
                    "forbidden-license-family",
                    expression,
                    f"{field} contains forbidden/incompatible family {family}",
                )
            if expression not in allowed:
                violation(
                    row,
                    "unreviewed-license-expression",
                    expression,
                    f"{field} is not a reviewed SPDX expression",
                )
        package_name = row.purl.split("/", 1)[-1].rsplit("@", 1)[0].lower()
        if package_name in FORBIDDEN_PACKAGE_NAMES:
            errors.append(f"{row.identity}: forbidden former PDF dependency")
        if (
            row.ecosystem == "npm"
            and row.license_concluded == "MPL-2.0"
            and row.source_scope != "development-build-test"
        ):
            violation(
                row,
                "npm-mpl-production-scope",
                row.license_concluded,
                "MPL-2.0 npm row is outside development/build/test scope",
            )

    expected_allowed = sorted(
        {
            expression
            for row in packages
            for expression in (row.license_declared, row.license_concluded)
            if expression and expression.upper() not in no_assertion
        }
    )
    if allowed_values != expected_allowed:
        errors.append(
            "policy.licenseRules.allowedSpdxExpressions: must equal the exact current reviewed "
            "source expressions"
        )
    mpl_npm = [
        row for row in packages if row.ecosystem == "npm" and row.license_concluded == "MPL-2.0"
    ]
    if len(mpl_npm) != 14:
        errors.append(f"npm MPL inventory: expected 14 rows, found {len(mpl_npm)}")

    for waiver in waiver_index.values():
        if waiver["id"] not in used_waivers:
            errors.append(
                f"policy waiver {waiver['id']!r}: implicit/stale waiver does not match "
                "an active violation"
            )


def build_context(root: Path) -> tuple[PolicyContext | None, list[str]]:
    errors: list[str] = []
    policy_file = root / POLICY_PATH
    try:
        policy_bytes = policy_file.read_bytes()
    except OSError as exc:
        return None, [f"{POLICY_PATH}: cannot read policy: {exc}"]
    try:
        policy_value = json.loads(policy_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{POLICY_PATH}: invalid JSON: {exc}"]
    if not isinstance(policy_value, dict):
        return None, [f"{POLICY_PATH}: policy root must be an object"]
    policy = policy_value
    if policy_bytes != _canonical_json(policy):
        errors.append(
            f"{POLICY_PATH}: noncanonical policy; use sorted keys, two-space JSON, and one newline"
        )

    _validate_policy_shape(root, policy, errors)
    python_lock_bytes, npm_lock_bytes = _read_and_bind_locks(root, policy, errors)
    python_identities = _parse_python_lock(python_lock_bytes, errors)
    npm_rows = _parse_npm_lock(npm_lock_bytes, errors)
    python_rows = _policy_python_packages(policy, errors)
    _compare_python_identities(python_identities, python_rows, errors)
    _validate_counts(policy, python_identities, npm_rows, errors)
    _validate_targeted_python_conclusions(python_rows, errors)
    _validate_review_evidence(
        root,
        policy,
        python_lock_bytes,
        npm_lock_bytes,
        python_identities,
        npm_rows,
        errors,
    )
    packages = sorted(python_rows + npm_rows, key=lambda row: (row.ecosystem, row.identity))
    waiver_index = _validate_waivers(root, policy, packages, errors)
    _validate_licenses(policy, packages, waiver_index, errors)
    return (
        PolicyContext(
            root=root,
            policy=policy,
            policy_bytes=policy_bytes,
            python_lock_bytes=python_lock_bytes,
            npm_lock_bytes=npm_lock_bytes,
            packages=packages,
        ),
        errors,
    )


def render_spdx(context: PolicyContext) -> bytes:
    epoch_text = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        epoch = int(epoch_text)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer") from exc
    if epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must not be negative")
    created = dt.datetime.fromtimestamp(epoch, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    packages: list[dict[str, Any]] = []
    relationships: list[dict[str, str]] = []
    for row in context.packages:
        spdx_id = "SPDXRef-Package-" + hashlib.sha256(row.identity.encode()).hexdigest()[:24]
        if row.ecosystem == "pypi":
            package_file_name = (
                f"requirements.lock#purl={row.purl};marker={quote(row.normalized_marker, safe='')}"
            )
            comment = (
                "Source-lock reference only; exact identity uses the normalized marker "
                f"{row.normalized_marker!r}."
            )
            if row.artifact_review_required:
                comment += (
                    " The source classification does not clear a selected wheel; review its "
                    "platform-specific bundled dependency licenses and notices separately."
                )
        else:
            package_file_name = f"web/package-lock.json#packages/{row.package_lock_path}"
            comment = (
                f"Source-lock reference only; package-lock scope={row.source_scope}. "
                "This scope is not emitted-bundle evidence."
            )
        item = {
            "SPDXID": spdx_id,
            "comment": comment,
            "downloadLocation": row.download_location,
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceLocator": row.purl,
                    "referenceType": "purl",
                }
            ],
            "filesAnalyzed": False,
            "licenseConcluded": row.license_concluded,
            "licenseDeclared": row.license_declared,
            "name": unquote(row.purl.split("/", 1)[-1].rsplit("@", 1)[0]),
            "packageFileName": package_file_name,
            "versionInfo": row.purl.rsplit("@", 1)[-1],
        }
        packages.append(item)
        relationships.append(
            {
                "relatedSpdxElement": spdx_id,
                "relationshipType": "DESCRIBES",
                "spdxElementId": "SPDXRef-DOCUMENT",
            }
        )

    input_digest = hashlib.sha256(
        context.python_lock_bytes + b"\0" + context.npm_lock_bytes + b"\0" + context.policy_bytes
    ).hexdigest()
    python_digest = _sha256(context.python_lock_bytes)
    npm_digest = _sha256(context.npm_lock_bytes)
    policy_digest = _sha256(context.policy_bytes)
    document = {
        "SPDXID": "SPDXRef-DOCUMENT",
        "creationInfo": {
            "created": created,
            "creators": ["Tool: clerksan-source-license-policy/1"],
        },
        "dataLicense": "CC0-1.0",
        "comment": (
            "Source-lock reference inventory only. "
            f"requirements.lock sha256:{python_digest}; "
            f"web/package-lock.json sha256:{npm_digest}; "
            f"sbom/license-policy.json sha256:{policy_digest}. "
            "This does not clear emitted web bundles, Python artifacts, application/parser/base/OS "
            "images, external service images, or model weights/manifests."
        ),
        "documentNamespace": f"https://clerksan.local/spdx/source-dependencies/{input_digest}",
        "name": "clerksan-source-dependencies",
        "packages": packages,
        "relationships": relationships,
        "spdxVersion": "SPDX-2.3",
    }
    return _canonical_json(document)


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_notices(context: PolicyContext) -> bytes:
    python_rows = [row for row in context.packages if row.ecosystem == "pypi"]
    npm_rows = [row for row in context.packages if row.ecosystem == "npm"]
    caveat = context.policy["artifactCaveats"][0]["statement"]
    lines = [
        "# Third-party source-lock reference index",
        "",
        (
            "This deterministic file is an informational source-lock index for the exact "
            "dependency references in `requirements.lock` and `web/package-lock.json`. It records "
            "reviewed source license classifications; it is not legal advice and does not say that "
            "dependency bytes are included in the source snapshot."
        ),
        "",
        "## Scope boundary",
        "",
        (
            "Binary wheels and sdists, the emitted `web/dist` bundle, application/parser/base/OS "
            "images, "
            "external service-image layers, and model weights/manifests require separate exact "
            "artifact inventories, review, and notices. No clearance for those scopes is inherited "
            "from this file."
        ),
        "The npm `production-lock-closure` label is a lock-graph scope only, not a bundle scan.",
        "",
        (
            "`pypdfium2==5.13.0` is classified here as `Apache-2.0 OR BSD-3-Clause` for its source "
            f"project. {_escape_markdown(caveat)}"
        ),
        "",
        f"## Python lock references ({len(python_rows)})",
        "",
        "| Package URL | Normalized marker identity | Declared license | Concluded license |",
        "| --- | --- | --- | --- |",
    ]
    for row in python_rows:
        lines.append(
            "| "
            + " | ".join(
                _escape_markdown(value)
                for value in (
                    f"`{row.purl}`",
                    f"`{row.normalized_marker}`",
                    f"`{row.license_declared}`",
                    f"`{row.license_concluded}`",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            f"## npm lock references ({len(npm_rows)})",
            "",
            "| package-lock path | Package URL | Declared/concluded license | Lock scope |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in npm_rows:
        lines.append(
            "| "
            + " | ".join(
                _escape_markdown(value)
                for value in (
                    f"`{row.package_lock_path}`",
                    f"`{row.purl}`",
                    f"`{row.license_concluded}`",
                    f"`{row.source_scope}`",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Exact input bindings",
            "",
            f"- `requirements.lock`: `sha256:{_sha256(context.python_lock_bytes)}`",
            f"- `web/package-lock.json`: `sha256:{_sha256(context.npm_lock_bytes)}`",
            f"- `sbom/license-policy.json`: `sha256:{_sha256(context.policy_bytes)}`",
            "",
        ]
    )
    return "\n".join(lines).encode()


def _compare_generated(root: Path, relative: Path, expected: bytes, errors: list[str]) -> None:
    path = root / relative
    try:
        actual = path.read_bytes()
    except OSError as exc:
        errors.append(f"{relative}: cannot read generated artifact: {exc}")
        return
    if actual != expected:
        errors.append(f"{relative}: generated artifact does not match policy and exact locks")


def _spdx_identity(package: object) -> str:
    if not isinstance(package, dict):
        return "spdx:malformed-package"
    refs = package.get("externalRefs")
    purl = None
    if isinstance(refs, list):
        for ref in refs:
            if (
                isinstance(ref, dict)
                and ref.get("referenceType") == "purl"
                and isinstance(ref.get("referenceLocator"), str)
            ):
                purl = ref["referenceLocator"]
                break
    package_file = package.get("packageFileName")
    if (
        isinstance(purl, str)
        and isinstance(package_file, str)
        and package_file.startswith("requirements.lock#")
        and ";marker=" in package_file
    ):
        marker = unquote(package_file.split(";marker=", 1)[1])
        return f"pypi:{purl}|marker:{marker}"
    npm_prefix = "web/package-lock.json#packages/"
    if (
        isinstance(purl, str)
        and isinstance(package_file, str)
        and package_file.startswith(npm_prefix)
    ):
        return f"npm:{package_file.removeprefix(npm_prefix)}|purl:{purl}"
    return f"spdx:{package_file!r}|purl:{purl!r}"


def _compare_spdx(root: Path, expected: bytes, errors: list[str]) -> None:
    path = root / SBOM_PATH
    try:
        actual = path.read_bytes()
    except OSError as exc:
        errors.append(f"{SBOM_PATH}: cannot read generated artifact: {exc}")
        return
    try:
        actual_document = json.loads(actual)
        expected_document = json.loads(expected)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{SBOM_PATH}: invalid SPDX JSON: {exc}")
        return
    actual_packages = actual_document.get("packages") if isinstance(actual_document, dict) else None
    expected_packages = (
        expected_document.get("packages") if isinstance(expected_document, dict) else None
    )
    if isinstance(actual_packages, list) and isinstance(expected_packages, list):
        actual_by_identity: dict[str, object] = {}
        for package in actual_packages:
            identity = _spdx_identity(package)
            if identity in actual_by_identity:
                errors.append(f"{identity}: duplicate SPDX identity")
            actual_by_identity[identity] = package
        expected_by_identity = {_spdx_identity(package): package for package in expected_packages}
        for identity in sorted(expected_by_identity.keys() - actual_by_identity.keys()):
            errors.append(f"{identity}: missing from checked-in SPDX")
        for identity in sorted(actual_by_identity.keys() - expected_by_identity.keys()):
            errors.append(f"{identity}: extra checked-in SPDX identity")
        for identity in sorted(expected_by_identity.keys() & actual_by_identity.keys()):
            actual_package = actual_by_identity[identity]
            expected_package = expected_by_identity[identity]
            if actual_package != expected_package:
                errors.append(f"{identity}: SPDX package does not match source policy")
            if isinstance(actual_package, dict):
                for field in ("licenseDeclared", "licenseConcluded"):
                    value = actual_package.get(field)
                    if not isinstance(value, str) or value.upper() in {
                        "",
                        "NONE",
                        "NOASSERTION",
                    }:
                        errors.append(f"{identity}: checked-in SPDX {field} is unresolved")
    else:
        errors.append(f"{SBOM_PATH}: packages must be a list")
    if actual != expected:
        errors.append(f"{SBOM_PATH}: generated artifact does not match policy and exact locks")


def _write_output(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SCRIPT_ROOT, help=argparse.SUPPRESS)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write-sbom", type=Path, metavar="PATH")
    group.add_argument("--write-notices", type=Path, metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    context, errors = build_context(root)
    if context is not None and not errors:
        try:
            sbom = render_spdx(context)
            notices = render_notices(context)
        except (OSError, OverflowError, ValueError) as exc:
            errors.append(f"generation failed: {exc}")
        else:
            if args.write_sbom is not None:
                _write_output(args.write_sbom, sbom)
            elif args.write_notices is not None:
                _write_output(args.write_notices, notices)
            else:
                _compare_spdx(root, sbom, errors)
                _compare_generated(root, NOTICES_PATH, notices, errors)
    if errors:
        print("source license policy check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    if args.write_sbom is None and args.write_notices is None:
        print("source license policy OK: 81 Python + 183 npm = 264 exact source-lock references")
        print(
            "artifact clearance remains separate for bundles, binaries, images, OS, services, "
            "and models"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
