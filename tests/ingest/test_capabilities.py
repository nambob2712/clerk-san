from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import pytest

from clerksan.config import SandboxUnavailable, Settings
from clerksan.ingest.adapters.base import (
    AdapterCapabilityMetadata,
    AdapterRegistry,
    CapabilityParityError,
    DuplicateAdapterKeyRegistrationError,
)
from clerksan.ingest.capabilities import (
    CAPABILITY_LIMIT_FIELDS,
    LEGACY_COMPAT_EXECUTION_PROFILE,
    LEGACY_COMPATIBILITY_EVIDENCE,
    UNIVERSAL_SANDBOXED_EXECUTION_PROFILE,
    CapabilityRegistry,
    DuplicateCapabilityRegistrationError,
    InvalidCapabilityRegistrationError,
    SandboxEvidence,
    build_activation_candidate_registry,
    build_capability_registry,
)
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument


class FakeAdapter:
    def __init__(self, *supported_types: FileType) -> None:
        self.supported_types = supported_types

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        return NormalizedDocument(markdown_body=raw.decode(), metadata=meta)


@dataclass(frozen=True)
class FakeProbe:
    verified: bool = True
    backend: str | None = "parser-sidecar"
    evidence_digest: str | None = "d" * 64
    capabilities: tuple[str, ...] = (
        "docx",
        "jpeg",
        "md",
        "pdf",
        "png",
        "webp",
        "xlsx",
        "csv",
    )


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        **overrides,
    )


def test_phase_one_registry_is_dark_canonical_and_settings_backed() -> None:
    registry = build_capability_registry(_settings())
    rebuilt = build_capability_registry(_settings())

    assert registry.process == ()
    assert registry.sandbox_verified is False
    assert registry.advertised_payload()["process"] == []
    assert registry.advertised_payload()["sandbox_verified"] is False
    assert registry.flags == {
        "intake_mode": "legacy",
        "safe_fallback_enabled": False,
        "universal_activation_enabled": False,
    }
    assert set(registry.limits) == set(CAPABILITY_LIMIT_FIELDS)
    assert registry.canonical_payload()["sandbox"] == {
        "backend": None,
        "evidence_digest": None,
        "verified": False,
    }

    assert registry.registry_digest == rebuilt.registry_digest == registry.digest
    assert registry.capabilities_digest == rebuilt.capabilities_digest
    assert re.fullmatch(r"[0-9a-f]{64}", registry.registry_digest)
    assert re.fullmatch(r"[0-9a-f]{64}", registry.capabilities_digest)
    assert (
        hashlib.sha256(registry.canonical_json().encode("utf-8")).hexdigest()
        == registry.registry_digest
    )

    changed_limit = build_capability_registry(_settings(max_pdf_pages=101))
    assert changed_limit.registry_digest != registry.registry_digest
    # The advertised process/sandbox capability did not change, only its settings envelope.
    assert changed_limit.capabilities_digest == registry.capabilities_digest


def test_activation_candidate_is_offline_and_universal_uses_exact_probe_set() -> None:
    legacy_settings = _settings()
    candidate = build_activation_candidate_registry(legacy_settings, FakeProbe())
    assert candidate.process == tuple(sorted(FakeProbe().capabilities))
    assert candidate.flags["intake_mode"] == "legacy"
    assert candidate.flags["universal_activation_enabled"] is False

    universal = build_capability_registry(_settings(intake_mode="universal"), FakeProbe())
    assert universal.process == candidate.process
    assert universal.sandbox_verified is True
    assert universal.flags == {
        "intake_mode": "universal",
        "safe_fallback_enabled": True,
        "universal_activation_enabled": True,
    }


def test_universal_registry_fails_closed_without_full_verified_probe() -> None:
    with pytest.raises(SandboxUnavailable, match="sandbox_unavailable"):
        build_capability_registry(_settings(intake_mode="universal"))
    with pytest.raises(InvalidCapabilityRegistrationError, match="missing legacy"):
        build_activation_candidate_registry(
            _settings(),
            FakeProbe(capabilities=("csv",)),
        )


def test_registry_digest_canonicalizes_order_and_includes_flags_and_sandbox() -> None:
    first = CapabilityRegistry(
        process=(),
        limits={"max_rows": 10, "max_bytes": 20},
        flags={"intake_mode": "legacy", "activation_enabled": False},
        sandbox=SandboxEvidence(verified=False),
    )
    reordered = CapabilityRegistry(
        process=(),
        limits={"max_bytes": 20, "max_rows": 10},
        flags={"activation_enabled": False, "intake_mode": "legacy"},
        sandbox=SandboxEvidence(verified=False),
    )
    changed_flag = CapabilityRegistry(
        process=(),
        limits={"max_rows": 10, "max_bytes": 20},
        flags={"intake_mode": "legacy", "activation_enabled": True},
        sandbox=SandboxEvidence(verified=False),
    )
    verified_sandbox = CapabilityRegistry(
        process=(),
        limits={"max_rows": 10, "max_bytes": 20},
        flags={"intake_mode": "legacy", "activation_enabled": False},
        sandbox=SandboxEvidence(
            verified=True,
            backend="parser-sidecar",
            evidence_digest="a" * 64,
        ),
    )

    assert first.registry_digest == reordered.registry_digest
    assert first.canonical_json() == reordered.canonical_json()
    assert changed_flag.registry_digest != first.registry_digest
    assert verified_sandbox.registry_digest != first.registry_digest
    assert verified_sandbox.capabilities_digest != first.capabilities_digest


def test_registry_rejects_duplicate_invalid_or_unsandboxed_process_capabilities() -> None:
    verified = SandboxEvidence(
        verified=True,
        backend="parser-sidecar",
        evidence_digest="b" * 64,
    )
    with pytest.raises(DuplicateCapabilityRegistrationError) as duplicate:
        CapabilityRegistry(
            process=("pdf", "pdf"),
            limits={"max_bytes": 1},
            flags={"activation_enabled": True},
            sandbox=verified,
        )
    assert duplicate.value.capability == "pdf"

    with pytest.raises(InvalidCapabilityRegistrationError, match="canonical lowercase"):
        CapabilityRegistry(
            process=("PDF",),
            limits={"max_bytes": 1},
            flags={"activation_enabled": True},
            sandbox=verified,
        )
    with pytest.raises(InvalidCapabilityRegistrationError, match="verified sandbox"):
        CapabilityRegistry(
            process=("pdf",),
            limits={"max_bytes": 1},
            flags={"activation_enabled": True},
            sandbox=SandboxEvidence(verified=False),
        )
    with pytest.raises(InvalidCapabilityRegistrationError, match="lowercase SHA-256"):
        SandboxEvidence(
            verified=True,
            backend="parser-sidecar",
            evidence_digest="not-a-digest",
        )


def test_legacy_adapter_evidence_stays_outside_universal_advertisement() -> None:
    adapters = AdapterRegistry()
    adapters.register(FakeAdapter(*tuple(FileType)))
    registry = build_capability_registry(_settings())

    assert adapters.registered_types == frozenset(FileType)
    assert adapters.legacy_compat_types == frozenset(FileType)
    assert len(adapters.registration_evidence) == 1
    evidence = adapters.registration_evidence[0]
    assert evidence.execution_profile == LEGACY_COMPAT_EXECUTION_PROFILE
    assert evidence.sandbox_verified is False
    assert LEGACY_COMPATIBILITY_EVIDENCE.execution_profile == "legacy_compat"
    assert set(registry.process).isdisjoint(file_type.value for file_type in FileType)
    assert "legacy_compat" not in registry.canonical_json()
    adapters.validate_capabilities(registry)

    advertised_pdf = CapabilityRegistry(
        process=("pdf",),
        limits={"max_bytes": 1},
        flags={"activation_enabled": True},
        sandbox=SandboxEvidence(
            verified=True,
            backend="parser-sidecar",
            evidence_digest="c" * 64,
        ),
    )
    with pytest.raises(CapabilityParityError, match="no sandboxed adapter"):
        adapters.validate_capabilities(advertised_pdf)


def test_adapter_capability_metadata_rejects_invalid_or_duplicate_evidence() -> None:
    with pytest.raises(ValueError, match="canonical lowercase"):
        AdapterCapabilityMetadata(adapter_key="PDF Adapter")
    with pytest.raises(ValueError, match="cannot claim sandbox"):
        AdapterCapabilityMetadata(adapter_key="legacy.pdf", sandbox_verified=True)
    with pytest.raises(ValueError, match="require sandbox"):
        AdapterCapabilityMetadata(
            adapter_key="universal.pdf",
            execution_profile=UNIVERSAL_SANDBOXED_EXECUTION_PROFILE,
            sandbox_verified=False,
        )

    adapters = AdapterRegistry()
    metadata = AdapterCapabilityMetadata(adapter_key="legacy.shared")
    adapters.register(FakeAdapter(FileType.PNG), capability_metadata=metadata)
    with pytest.raises(DuplicateAdapterKeyRegistrationError) as duplicate:
        adapters.register(FakeAdapter(FileType.PDF), capability_metadata=metadata)
    assert duplicate.value.adapter_key == "legacy.shared"
    assert adapters.registered_types == {FileType.PNG}
