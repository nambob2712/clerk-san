from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "FINAL_SETUP_AND_RUN.md",
    ROOT / "RUN_AND_DEMO.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    ROOT / "docs" / "brand-spec.md",
    ROOT / "docs" / "codebase-summary.md",
    ROOT / "docs" / "compliance_denchoho.md",
    ROOT / "docs" / "developer-preview.md",
    ROOT / "docs" / "decisions" / "local-first-react-frontend.md",
    ROOT / "eval" / "fixtures" / "README.md",
    ROOT / "sbom" / "README.md",
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def test_public_documents_have_no_internal_plan_or_report_dependency() -> None:
    for document in PUBLIC_DOCUMENTS:
        source = document.read_text(encoding="utf-8")
        assert "plans/" not in source, document.relative_to(ROOT)
        assert "reports/" not in source, document.relative_to(ROOT)


def test_public_markdown_links_resolve_inside_the_candidate() -> None:
    for document in PUBLIC_DOCUMENTS:
        source = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(source):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative_path = unquote(target.split("#", 1)[0])
            assert relative_path, (document.relative_to(ROOT), raw_target)
            resolved = (document.parent / relative_path).resolve()
            assert resolved.is_relative_to(ROOT), (document.relative_to(ROOT), raw_target)
            assert resolved.exists(), (document.relative_to(ROOT), raw_target)


def test_developer_preview_states_the_supported_and_unsupported_boundaries() -> None:
    source = (ROOT / "docs" / "developer-preview.md").read_text(encoding="utf-8")

    for required in (
        "local-only developer preview",
        "host Ollama",
        "SQLite",
        "human",
        "demo mode disabled",
        "core readiness",
        "processing readiness",
        "never starts Ollama or downloads a model",
        "not production-ready",
        "not legal advice",
        "fresh Git root",
        "Public visibility is a separate maintainer decision",
    ):
        assert required in source
    assert "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f" in source
    assert "model weights" in source
    assert "public interface" in source
