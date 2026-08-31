from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_public_ci_is_read_only_sha_pinned_and_untrusted_pr_safe() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request_target" not in source
    assert "secrets." not in source
    assert "permissions:\n  contents: read" in source
    assert "persist-credentials: false" in source
    assert "fetch-depth: 0" in source
    assert "contents: write" not in source
    assert "actions: write" not in source
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", source, flags=re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", value) for value in uses)


def test_public_ci_checks_exact_revisions_locks_and_public_supply_chain() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        'python-version: "3.11"',
        'version: "0.12.7"',
        'node-version: "24.15.0"',
        'test "$(npm --version)" = "11.12.1"',
        "--require-hashes --no-deps",
        "check-private-artifacts.py --range",
        "check-private-artifacts.py --tree HEAD",
        "scripts/run-gitleaks.sh self-test",
        "scripts/run-gitleaks.sh git-range",
        "scripts/run-osv-scanner.sh sbom/source-dependencies.spdx.json",
        "scripts/check-license-policy.py",
        "SOURCE_DATE_EPOCH=0",
        "npm ci --ignore-scripts",
        "npm audit --audit-level=high",
        ".venv/bin/python -m pytest -q",
        "npm test",
        "npm run typecheck",
        "npm run build",
    ):
        assert required in source
    assert "CLERKSAN_PRIVATE_PATTERN_FILE" not in source
    assert "ollama pull" not in source
    assert "docker compose" not in source


def test_scanner_downloads_are_versioned_and_checksum_pinned() -> None:
    gitleaks = (ROOT / "scripts" / "run-gitleaks.sh").read_text(encoding="utf-8")
    osv = (ROOT / "scripts" / "run-osv-scanner.sh").read_text(encoding="utf-8")
    config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")

    assert 'readonly version="8.30.1"' in gitleaks
    assert 'readonly version="2.5.1"' in osv
    assert len(set(re.findall(r'expected="([0-9a-f]{64})"', gitleaks))) == 4
    assert len(set(re.findall(r'expected="([0-9a-f]{64})"', osv))) == 4
    assert "shasum -a 256 -c -" in gitleaks
    assert "shasum -a 256 -c -" in osv
    assert "--redact=100" in gitleaks
    assert "useDefault = true" in config


def test_public_contribution_surfaces_enforce_synthetic_evidence() -> None:
    codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    dependabot = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    issue_config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")
    pull_request = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")

    assert codeowners == "* @nambob2712\n"
    assert set(re.findall(r"package-ecosystem:\s*([\w-]+)", dependabot)) == {
        "github-actions",
        "npm",
        "pip",
    }
    assert "blank_issues_enabled: false" in issue_config
    assert "/security/advisories/new" in issue_config
    assert "no real receipts" in pull_request.lower()
    assert "private vulnerability reporting" in pull_request.lower()
