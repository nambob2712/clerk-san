from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "requirements.txt"
PYTHON_LOCK = ROOT / "requirements.lock"
PACKAGE_LOCK = ROOT / "web" / "package-lock.json"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
SBOM = ROOT / "sbom" / "source-dependencies.spdx.json"
SBOM_GENERATOR = ROOT / "scripts" / "generate-sbom.sh"
POLICY = ROOT / "sbom" / "license-policy.json"
POLICY_CHECKER = ROOT / "scripts" / "check-license-policy.py"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
NOTICES_GENERATOR = ROOT / "scripts" / "generate-third-party-notices.py"
SOURCE_LICENSE_REVIEW = ROOT / "sbom" / "source-license-review.md"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
REVIEW_BINDINGS_START = "<!-- clerksan-source-license-bindings-v1:start -->"
REVIEW_BINDINGS_END = "<!-- clerksan-source-license-bindings-v1:end -->"

LOCK_ENTRY = re.compile(
    r"(?m)^([A-Za-z0-9_.-]+)==([^ ;\\\r\n]+)"
    r"(?:[ \t]+;[^\\\r\n]+)?[ \t]+\\$"
)


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_requirements() -> set[str]:
    names: set[str] = set()
    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~; ]", line, maxsplit=1)[0].split("[", 1)[0]
        names.add(_canonical_name(name))
    return names


def _locked_python_packages(*, marker_only: bool = False) -> set[tuple[str, str]]:
    packages: set[tuple[str, str]] = set()
    for raw_line in PYTHON_LOCK.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.endswith("\\") or "==" not in line:
            continue
        requirement = line[:-1].strip()
        has_marker = ";" in requirement
        if marker_only and not has_marker:
            continue
        pinned = requirement.split(";", 1)[0].strip()
        name, separator, version = pinned.partition("==")
        assert separator and name and version, raw_line
        packages.add((name, version))
    return packages


def _python_packages() -> set[tuple[str, str]]:
    return {(_canonical_name(name), version) for name, version in _locked_python_packages()}


def _npm_packages() -> set[tuple[str, str]]:
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    packages: set[tuple[str, str]] = set()
    for package_path, metadata in lock["packages"].items():
        if not package_path or metadata.get("link"):
            continue
        name = metadata.get("name")
        if not name and "node_modules/" in package_path:
            name = package_path.rsplit("node_modules/", 1)[-1]
        packages.add((name, metadata["version"]))
    return packages


def _normalized_test_marker(marker: str | None) -> str:
    if marker is None or not marker.strip():
        return "unconditional"
    normalized = re.sub(r"'([^']*)'", r'"\1"', marker.strip())
    return " ".join(normalized.split())


def _locked_python_identities() -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for raw_line in PYTHON_LOCK.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.endswith("\\") or "==" not in line:
            continue
        requirement = line[:-1].strip()
        pinned, separator, marker = requirement.partition(";")
        name, equals, version = pinned.strip().partition("==")
        assert equals and name and version, raw_line
        identities.add(
            (
                f"pkg:pypi/{quote(name, safe='/')}@{version}",
                _normalized_test_marker(marker if separator else None),
            )
        )
    return identities


def _npm_lock_identities() -> set[tuple[str, str, str, str]]:
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    identities: set[tuple[str, str, str, str]] = set()
    for package_path, metadata in lock["packages"].items():
        if not package_path or metadata.get("link"):
            continue
        name = metadata.get("name")
        if not name and "node_modules/" in package_path:
            name = package_path.rsplit("node_modules/", 1)[-1]
        scope = (
            "development-build-test" if metadata.get("dev") is True else "production-lock-closure"
        )
        identities.add(
            (
                package_path,
                f"pkg:npm/{quote(name, safe='/')}@{metadata['version']}",
                metadata["license"],
                scope,
            )
        )
    return identities


def _npm_download_locations() -> dict[tuple[str, str], str]:
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    locations: dict[tuple[str, str], str] = {}
    for package_path, metadata in lock["packages"].items():
        if not package_path or metadata.get("link"):
            continue
        name = metadata.get("name")
        if not name and "node_modules/" in package_path:
            name = package_path.rsplit("node_modules/", 1)[-1]
        purl = f"pkg:npm/{quote(name, safe='/')}@{metadata['version']}"
        locations[(package_path, purl)] = metadata["resolved"]
    return locations


def _copy_policy_fixture(destination: Path) -> Path:
    root = destination / "repository"
    relative_files = (
        PYTHON_LOCK.relative_to(ROOT),
        PACKAGE_LOCK.relative_to(ROOT),
        POLICY.relative_to(ROOT),
        SBOM.relative_to(ROOT),
        NOTICES.relative_to(ROOT),
        SOURCE_LICENSE_REVIEW.relative_to(ROOT),
    )
    for relative in relative_files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return root


def _write_canonical_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_waiver_evidence(root: Path, waiver: dict[str, object]) -> None:
    evidence_value = {key: value for key, value in waiver.items() if key != "evidence"}
    evidence_path = root / "sbom" / "waivers" / f"{waiver['id']}.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    _write_canonical_json(evidence_path, evidence_value)
    waiver["evidence"] = {
        "locator": f"sbom/waivers/{waiver['id']}.json",
        "sha256": _sha256(evidence_path.read_bytes()),
    }


def _run_policy(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(POLICY_CHECKER), "--root", str(root)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_python_lock_is_transitive_exact_and_hash_enforced() -> None:
    lock = PYTHON_LOCK.read_text(encoding="utf-8")
    matches = list(LOCK_ENTRY.finditer(lock))

    assert "--generate-hashes" in lock.splitlines()[1]
    assert matches
    assert _direct_requirements() <= {name for name, _ in _python_packages()}
    assert "git+" not in lock
    assert "--editable" not in lock

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(lock)
        package_block = lock[match.start() : end]
        assert "--hash=sha256:" in package_block, match.group(1)


def test_hash_enforcement_rejects_a_local_artifact_with_the_wrong_digest(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "hash_probe-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hash_probe/__init__.py", "__version__ = '1.0.0'\n")
        archive.writestr(
            "hash_probe-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: hash-probe\nVersion: 1.0.0\n",
        )
        archive.writestr(
            "hash_probe-1.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("hash_probe-1.0.0.dist-info/RECORD", "")

    wrong_lock = tmp_path / "wrong-hash.lock"
    wrong_lock.write_text(
        f"hash-probe @ {wheel.as_uri()} \\\n    --hash=sha256:{'0' * 64}\n",
        encoding="utf-8",
    )
    environment = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(environment / "bin" / "python"),
            "--require-hashes",
            "--no-deps",
            "--requirement",
            str(wrong_lock),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "hash" in result.stderr.lower()


def test_docker_build_installs_only_the_hash_lock_and_pins_external_bases() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert (
        "FROM node:24.15.0-bookworm-slim@sha256:"
        "4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d AS web-build"
        in dockerfile
    )
    assert (
        "FROM python:3.11-slim-bookworm@sha256:"
        "2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91 "
        "AS python-runtime" in dockerfile
    )
    assert "COPY requirements.lock ./" in dockerfile
    assert "pip install --require-hashes --requirement requirements.lock" in dockerfile
    assert "--no-deps" in dockerfile
    assert "pip install --upgrade" not in dockerfile
    assert "npm ci" in dockerfile
    native_install = re.search(
        r"apt-get install --yes --no-install-recommends (?P<packages>[^\\\n]+)",
        dockerfile,
    )
    assert native_install is not None
    assert native_install.group("packages").split() == ["libglib2.0-0", "libgl1", "libmagic1"]


def test_compose_pins_external_services_and_preserves_local_build_tags() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert (
        "image: pgvector/pgvector:0.8.5-pg16-bookworm@sha256:"
        "1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb" in compose
    )
    assert (
        "image: ollama/ollama:0.31.1@sha256:"
        "f1a705f2bd113fb8d15f85f7c217f0dc5f6bebda6b0cc42b82c3ad165ffcb9dc" in compose
    )
    assert "image: clerksan-parser:v2" in compose
    assert "image: clerksan:v2" in compose


def test_npm_lock_is_exact_and_integrity_checked() -> None:
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))

    assert lock["lockfileVersion"] == 3
    for package_path, metadata in lock["packages"].items():
        if not package_path or metadata.get("link"):
            continue
        assert isinstance(metadata.get("version"), str), package_path
        assert metadata.get("integrity", "").startswith("sha512-"), package_path


def test_source_sbom_is_deterministic_and_covers_both_lockfiles(tmp_path: Path) -> None:
    regenerated = tmp_path / "source-dependencies.spdx.json"
    environment = os.environ | {"SOURCE_DATE_EPOCH": "0"}
    subprocess.run(
        [str(SBOM_GENERATOR), str(regenerated)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert regenerated.read_bytes() == SBOM.read_bytes()

    document = json.loads(SBOM.read_text(encoding="utf-8"))
    assert document["spdxVersion"] == "SPDX-2.3"
    assert "does not clear emitted web bundles" in document["comment"]
    assert "documentComment" not in document
    assert set(document) == {
        "SPDXID",
        "comment",
        "creationInfo",
        "dataLicense",
        "documentNamespace",
        "name",
        "packages",
        "relationships",
        "spdxVersion",
    }
    packages = document["packages"]
    assert len(packages) == 264
    assert len({package["SPDXID"] for package in packages}) == 264
    assert all(
        package[field] not in {"", "NONE", "NOASSERTION"}
        for package in packages
        for field in ("downloadLocation", "licenseDeclared", "licenseConcluded")
    )
    assert all(package["downloadLocation"].startswith("https://") for package in packages)

    purls = {package["externalRefs"][0]["referenceLocator"] for package in packages}
    expected_python = {
        f"pkg:pypi/{quote(name, safe='/')}@{version}" for name, version in _locked_python_packages()
    }
    expected_npm = {
        f"pkg:npm/{quote(name, safe='/')}@{version}" for name, version in _npm_packages()
    }
    actual_python = {purl for purl in purls if purl.startswith("pkg:pypi/")}
    actual_npm = {purl for purl in purls if purl.startswith("pkg:npm/")}
    marker_python = {
        f"pkg:pypi/{quote(name, safe='/')}@{version}"
        for name, version in _locked_python_packages(marker_only=True)
    }
    assert marker_python == {
        "pkg:pypi/colorama@0.4.6",
        "pkg:pypi/numpy@2.4.6",
        "pkg:pypi/numpy@2.5.2",
        "pkg:pypi/scipy@1.17.1",
        "pkg:pypi/scipy@1.18.1",
        "pkg:pypi/uvloop@0.22.1",
        "pkg:pypi/watchdog@6.0.0",
    }
    assert marker_python <= expected_python
    assert actual_python == expected_python
    assert actual_npm == expected_npm
    assert len(document["packages"]) == len(expected_python) + len(expected_npm)

    actual_python_identities: set[tuple[str, str]] = set()
    actual_npm_identities: set[tuple[str, str, str, str]] = set()
    expected_npm_downloads = _npm_download_locations()
    for package in packages:
        purl = package["externalRefs"][0]["referenceLocator"]
        package_file = package["packageFileName"]
        if purl.startswith("pkg:pypi/"):
            marker = unquote(package_file.split(";marker=", 1)[1])
            actual_python_identities.add((purl, marker))
            encoded_name, version = purl.removeprefix("pkg:pypi/").rsplit("@", 1)
            assert package["downloadLocation"] == (
                f"https://pypi.org/pypi/{encoded_name}/{version}/json"
            )
        else:
            package_path = package_file.removeprefix("web/package-lock.json#packages/")
            assert package["downloadLocation"] == expected_npm_downloads[(package_path, purl)]
            scope_match = re.search(r"package-lock scope=([a-z-]+)", package["comment"])
            assert scope_match is not None
            actual_npm_identities.add(
                (
                    package_path,
                    purl,
                    package["licenseConcluded"],
                    scope_match.group(1),
                )
            )
    assert actual_python_identities == _locked_python_identities()
    assert actual_npm_identities == _npm_lock_identities()

    by_purl = {package["externalRefs"][0]["referenceLocator"]: package for package in packages}
    assert by_purl["pkg:pypi/pypdf@6.16.2"]["licenseConcluded"] == "BSD-3-Clause"
    pypdfium2 = by_purl["pkg:pypi/pypdfium2@5.13.0"]
    assert pypdfium2["licenseConcluded"] == "Apache-2.0 OR BSD-3-Clause"
    assert "does not clear a selected wheel" in pypdfium2["comment"]
    assert not any(value in purl.lower() for purl in purls for value in ("pymupdf", "fitz"))


def test_source_license_policy_and_notices_are_canonical_and_deterministic(tmp_path: Path) -> None:
    result = _run_policy(ROOT)
    assert result.returncode == 0, result.stderr
    assert "81 Python + 183 npm = 264" in result.stdout

    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    assert POLICY.read_text(encoding="utf-8") == (
        json.dumps(policy, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert len(policy["pythonPackages"]) == 81
    assert policy["artifactPolicy"]["records"] == []
    assert policy["artifactPolicy"]["requireSeparateReview"] is True
    assert set(policy["evidence"][0]["bindings"]) == {
        "npmIdentitiesSha256",
        "npmLockSha256",
        "policyMachineSha256",
        "pythonIdentitiesSha256",
        "pythonLockSha256",
    }

    regenerated_notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    subprocess.run(
        [sys.executable, str(NOTICES_GENERATOR), str(regenerated_notices)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert regenerated_notices.read_bytes() == NOTICES.read_bytes()
    notices = NOTICES.read_text(encoding="utf-8")
    for boundary in (
        "informational source-lock index",
        "Binary wheels and sdists",
        "emitted `web/dist` bundle",
        "external service-image layers",
        "model weights/manifests",
        "No clearance for those scopes is inherited",
    ):
        assert boundary in notices
    assert "pypdfium2==5.13.0" in notices
    assert "platform-specific review" in notices

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/generate-sbom.sh /tmp/source-dependencies.spdx.json" in workflow
    assert "scripts/generate-third-party-notices.py /tmp/THIRD_PARTY_NOTICES.md" in workflow
    assert "cmp --silent sbom/source-dependencies.spdx.json" in workflow
    assert "cmp --silent THIRD_PARTY_NOTICES.md" in workflow
    assert ".venv/bin/python scripts/check-license-policy.py" in workflow


def test_source_policy_passes_in_an_isolated_public_candidate_without_plans(
    tmp_path: Path,
) -> None:
    root = _copy_policy_fixture(tmp_path)
    assert not (root / "plans").exists()
    assert (root / "sbom" / "source-license-review.md").is_file()

    result = _run_policy(root)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    [
        ("missing-python-row", "missing from policy"),
        ("extra-python-row", "extra policy identity"),
        ("marker-drift", "marker:"),
        ("direct-url-requirement", "unsupported installable requirement"),
        ("python-lock-digest", "requirements.lock: lock digest drift"),
        ("review-lock-binding", "reviewed lock, identity, or machine-policy binding drift"),
        ("review-machine-binding", "reviewed lock, identity, or machine-policy binding drift"),
        ("review-identity-binding", "reviewed lock, identity, or machine-policy binding drift"),
        ("review-document-binding", "machine review bindings do not match current inputs"),
        ("evidence-symlink", "symlinked evidence paths are not allowed"),
        ("evidence-escape", "locator must stay inside the repository"),
        ("npm-path-digest", "web/package-lock.json: lock digest drift"),
        ("missing-npm-license", "missing npm license"),
        ("missing-npm-resolved", "missing npm resolved URL"),
        ("unsafe-npm-resolved", "unsafe npm resolved URL"),
        ("npm-mpl-production", "MPL-2.0 npm row is outside development/build/test scope"),
        ("forbidden-license", "forbidden/incompatible family GPL"),
        ("expired-waiver", "waiver expired"),
        ("implicit-waiver", "implicit/stale waiver"),
        ("irrelevant-waiver-evidence", "dedicated evidence path required"),
        ("waiver-binding-mismatch", "evidence must bind the exact waiver"),
        ("sbom-policy-mismatch", "generated artifact does not match policy and exact locks"),
        ("noncanonical-policy", "noncanonical policy"),
    ],
)
def test_source_license_policy_fails_closed_on_tampering(
    tmp_path: Path,
    tamper: str,
    expected_error: str,
) -> None:
    root = _copy_policy_fixture(tmp_path)
    policy_path = root / POLICY.relative_to(ROOT)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))

    if tamper == "missing-python-row":
        policy["pythonPackages"].pop(0)
        _write_canonical_json(policy_path, policy)
    elif tamper == "extra-python-row":
        extra = dict(policy["pythonPackages"][0])
        extra["purl"] = "pkg:pypi/zz-policy-extra@1.0.0"
        policy["pythonPackages"].append(extra)
        policy["pythonPackages"].sort(
            key=lambda row: (row["purl"], row["normalizedMarkerIdentity"])
        )
        _write_canonical_json(policy_path, policy)
    elif tamper == "marker-drift":
        row = next(item for item in policy["pythonPackages"] if "colorama@" in item["purl"])
        row["normalizedMarkerIdentity"] = 'sys_platform != "win32"'
        _write_canonical_json(policy_path, policy)
    elif tamper == "direct-url-requirement":
        lock_path = root / PYTHON_LOCK.relative_to(ROOT)
        lock_path.write_text(
            lock_path.read_text(encoding="utf-8")
            + "adversarial @ https://example.invalid/adversarial.whl \\\n"
            + f"    --hash=sha256:{'0' * 64}\n",
            encoding="utf-8",
        )
        policy["locks"]["python"]["sha256"] = _sha256(lock_path.read_bytes())
        _write_canonical_json(policy_path, policy)
    elif tamper == "python-lock-digest":
        lock_path = root / PYTHON_LOCK.relative_to(ROOT)
        lock_path.write_text(lock_path.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    elif tamper == "review-lock-binding":
        lock_path = root / PYTHON_LOCK.relative_to(ROOT)
        lock_path.write_text(
            lock_path.read_text(encoding="utf-8") + "# reviewed drift\n", encoding="utf-8"
        )
        policy["locks"]["python"]["sha256"] = _sha256(lock_path.read_bytes())
        _write_canonical_json(policy_path, policy)
    elif tamper == "review-machine-binding":
        policy["artifactCaveats"][0]["statement"] += " Reviewed again."
        _write_canonical_json(policy_path, policy)
    elif tamper == "review-identity-binding":
        policy["evidence"][0]["bindings"]["pythonIdentitiesSha256"] = "0" * 64
        _write_canonical_json(policy_path, policy)
    elif tamper == "review-document-binding":
        review_path = root / SOURCE_LICENSE_REVIEW.relative_to(ROOT)
        review = review_path.read_text(encoding="utf-8")
        old_digest = policy["evidence"][0]["bindings"]["pythonIdentitiesSha256"]
        assert old_digest in review
        review_path.write_text(review.replace(old_digest, "0" * 64, 1), encoding="utf-8")
        policy["evidence"][0]["sha256"] = _sha256(review_path.read_bytes())
        _write_canonical_json(policy_path, policy)
    elif tamper == "evidence-symlink":
        review_path = root / SOURCE_LICENSE_REVIEW.relative_to(ROOT)
        outside_review = root.parent / "outside-review.md"
        shutil.copyfile(review_path, outside_review)
        review_path.unlink()
        review_path.symlink_to(outside_review)
    elif tamper == "evidence-escape":
        policy["evidence"][0]["locator"] = "../outside-review.md"
        _write_canonical_json(policy_path, policy)
    elif tamper == "npm-path-digest":
        lock_path = root / PACKAGE_LOCK.relative_to(ROOT)
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        metadata = lock["packages"].pop("node_modules/@adobe/css-tools")
        lock["packages"]["node_modules/path-drift/css-tools"] = metadata
        _write_canonical_json(lock_path, lock)
    elif tamper == "missing-npm-license":
        lock_path = root / PACKAGE_LOCK.relative_to(ROOT)
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["packages"]["node_modules/@adobe/css-tools"].pop("license")
        _write_canonical_json(lock_path, lock)
    elif tamper == "missing-npm-resolved":
        lock_path = root / PACKAGE_LOCK.relative_to(ROOT)
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["packages"]["node_modules/@adobe/css-tools"].pop("resolved")
        _write_canonical_json(lock_path, lock)
    elif tamper == "unsafe-npm-resolved":
        lock_path = root / PACKAGE_LOCK.relative_to(ROOT)
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["packages"]["node_modules/@adobe/css-tools"]["resolved"] = (
            "http://registry.npmjs.org/@adobe/css-tools/-/css-tools-4.5.0.tgz"
        )
        _write_canonical_json(lock_path, lock)
    elif tamper == "npm-mpl-production":
        lock_path = root / PACKAGE_LOCK.relative_to(ROOT)
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["packages"]["node_modules/@axe-core/playwright"]["dev"] = False
        _write_canonical_json(lock_path, lock)
    elif tamper in {
        "forbidden-license",
        "expired-waiver",
        "irrelevant-waiver-evidence",
        "waiver-binding-mismatch",
    }:
        row = policy["pythonPackages"][0]
        row["licenseDeclared"] = "GPL-3.0-only"
        row["licenseConcluded"] = "GPL-3.0-only"
        policy["licenseRules"]["allowedSpdxExpressions"].append("GPL-3.0-only")
        policy["licenseRules"]["allowedSpdxExpressions"].sort()
        if tamper == "expired-waiver":
            evidence = policy["evidence"][0]
            policy["waivers"].append(
                {
                    "evidence": {
                        "locator": evidence["locator"],
                        "sha256": evidence["sha256"],
                    },
                    "expiresOn": "2000-01-01",
                    "id": "expired-test-waiver",
                    "identity": f"pypi:{row['purl']}|marker:{row['normalizedMarkerIdentity']}",
                    "licenseExpression": "GPL-3.0-only",
                    "owner": "test-owner",
                    "reason": "tamper fixture",
                    "violationCode": "forbidden-license-family",
                }
            )
        elif tamper in {"irrelevant-waiver-evidence", "waiver-binding-mismatch"}:
            waiver: dict[str, object] = {
                "evidence": {},
                "expiresOn": "2999-01-01",
                "id": "adversarial-test-waiver",
                "identity": f"pypi:{row['purl']}|marker:{row['normalizedMarkerIdentity']}",
                "licenseExpression": "GPL-3.0-only",
                "owner": "test-owner",
                "reason": "tamper fixture",
                "violationCode": "forbidden-license-family",
            }
            if tamper == "irrelevant-waiver-evidence":
                lock_path = root / PYTHON_LOCK.relative_to(ROOT)
                waiver["evidence"] = {
                    "locator": "requirements.lock",
                    "sha256": _sha256(lock_path.read_bytes()),
                }
            else:
                _write_waiver_evidence(root, waiver)
                evidence_path = root / str(waiver["evidence"]["locator"])
                evidence_value = json.loads(evidence_path.read_text(encoding="utf-8"))
                evidence_value["owner"] = "different-owner"
                _write_canonical_json(evidence_path, evidence_value)
                waiver["evidence"]["sha256"] = _sha256(evidence_path.read_bytes())
            policy["waivers"].append(waiver)
        _write_canonical_json(policy_path, policy)
    elif tamper == "implicit-waiver":
        row = policy["pythonPackages"][0]
        waiver = {
            "evidence": {},
            "expiresOn": "2999-01-01",
            "id": "unused-test-waiver",
            "identity": f"pypi:{row['purl']}|marker:{row['normalizedMarkerIdentity']}",
            "licenseExpression": row["licenseConcluded"],
            "owner": "test-owner",
            "reason": "tamper fixture",
            "violationCode": "forbidden-license-family",
        }
        _write_waiver_evidence(root, waiver)
        policy["waivers"].append(waiver)
        _write_canonical_json(policy_path, policy)
    elif tamper == "sbom-policy-mismatch":
        sbom_path = root / SBOM.relative_to(ROOT)
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        sbom["packages"][0]["licenseConcluded"] = "NONE"
        _write_canonical_json(sbom_path, sbom)
    elif tamper == "noncanonical-policy":
        policy_path.write_text(json.dumps(policy, indent=4) + "\n", encoding="utf-8")
    else:
        raise AssertionError(tamper)

    result = _run_policy(root)
    assert result.returncode != 0
    assert expected_error in result.stderr
