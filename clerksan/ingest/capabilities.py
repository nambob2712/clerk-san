"""Canonical universal-intake capability evidence.

Phase 1 deliberately publishes an empty universal process set.  The existing
in-process adapters continue to run only through the legacy compatibility path;
they are not inputs to this registry factory and therefore cannot be mistaken for
sandbox-verified universal capabilities.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Protocol

from clerksan.config import (
    IntakeMode,
    SandboxUnavailable,
    Settings,
    ensure_intake_mode_available,
)

CAPABILITY_REGISTRY_SCHEMA: Final[str] = "clerksan.universal-intake-capabilities"
CAPABILITY_REGISTRY_VERSION: Final[int] = 1
LEGACY_COMPAT_EXECUTION_PROFILE: Final[str] = "legacy_compat"
UNIVERSAL_SANDBOXED_EXECUTION_PROFILE: Final[str] = "universal_sandboxed"

# Every setting that changes a Phase 1 intake/resource boundary participates in
# registry parity.  Keeping this explicit also makes default drift visible in tests.
CAPABILITY_LIMIT_FIELDS: Final[tuple[str, ...]] = (
    "max_request_bytes",
    "max_upload_bytes",
    "max_multipart_files",
    "max_multipart_fields",
    "max_json_bytes",
    "max_json_depth",
    "upload_concurrency",
    "idempotency_retention_hours",
    "recent_intakes_default_limit",
    "recent_intakes_max_limit",
    "worker_capability_heartbeat_seconds",
    "worker_capability_lease_seconds",
    "storage_reservation_grace_seconds",
    "max_pdf_pages",
    "max_image_frames",
    "max_image_pixels",
    "max_image_width",
    "max_image_height",
    "max_text_characters",
    "max_tabular_rows",
    "max_tabular_cells",
    "max_structured_nodes",
    "max_recursion_depth",
    "max_normalized_output_bytes",
    "max_archive_members",
    "max_archive_uncompressed_bytes",
    "max_archive_expansion_ratio",
)

_CAPABILITY_KEY = re.compile(r"[a-z0-9][a-z0-9._+-]*\Z")
_EVIDENCE_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SETTING_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")


class InvalidCapabilityRegistrationError(ValueError):
    """A capability record is incomplete, non-canonical, or unsafe to advertise."""


class DuplicateCapabilityRegistrationError(ValueError):
    """The same universal process capability was registered more than once."""

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"capability {capability!r} is registered more than once")


@dataclass(frozen=True, slots=True)
class SandboxEvidence:
    """Evidence required before any universal process capability may be advertised."""

    verified: bool
    backend: str | None = None
    evidence_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verified, bool):
            raise InvalidCapabilityRegistrationError("sandbox verified must be a boolean")
        if self.verified:
            if not self.backend or not _CAPABILITY_KEY.fullmatch(self.backend):
                raise InvalidCapabilityRegistrationError(
                    "verified sandbox evidence requires a canonical backend key"
                )
            if self.evidence_digest is None or not _EVIDENCE_DIGEST.fullmatch(self.evidence_digest):
                raise InvalidCapabilityRegistrationError(
                    "verified sandbox evidence requires a lowercase SHA-256 digest"
                )
        elif self.backend is not None or self.evidence_digest is not None:
            raise InvalidCapabilityRegistrationError(
                "unverified sandbox evidence cannot name a backend or evidence digest"
            )

    def canonical_payload(self) -> dict[str, bool | str | None]:
        """Return a fresh JSON-compatible evidence payload."""

        return {
            "backend": self.backend,
            "evidence_digest": self.evidence_digest,
            "verified": self.verified,
        }


PHASE_ONE_SANDBOX_EVIDENCE: Final[SandboxEvidence] = SandboxEvidence(verified=False)

LEGACY_REQUIRED_PROCESS_FORMATS: Final[frozenset[str]] = frozenset(
    {"md", "docx", "xlsx", "pdf", "jpeg", "png", "webp"}
)


class ActivationProbe(Protocol):
    verified: bool
    backend: str | None
    evidence_digest: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyCompatibilityEvidence:
    """Explicit evidence for current handlers, kept outside universal advertisement."""

    execution_profile: str = field(default=LEGACY_COMPAT_EXECUTION_PROFILE, init=False)
    sandbox_verified: bool = field(default=False, init=False)


LEGACY_COMPATIBILITY_EVIDENCE: Final[LegacyCompatibilityEvidence] = LegacyCompatibilityEvidence()


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    """Immutable, canonical universal capability registry.

    ``process`` contains format keys that may be scheduled through the universal
    parser boundary.  Construction rejects an advertised process set unless hard
    sandbox evidence is verified.  The Phase 1 factory below always supplies an
    empty tuple and unverified evidence.
    """

    process: tuple[str, ...]
    limits: Mapping[str, int | float]
    flags: Mapping[str, bool | str]
    sandbox: SandboxEvidence
    schema: str = CAPABILITY_REGISTRY_SCHEMA
    version: int = CAPABILITY_REGISTRY_VERSION
    _registry_digest: str = field(init=False, repr=False)
    _capabilities_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.schema != CAPABILITY_REGISTRY_SCHEMA or self.version != CAPABILITY_REGISTRY_VERSION:
            raise InvalidCapabilityRegistrationError("unsupported capability registry schema")

        process = tuple(self.process)
        for capability in process:
            if not isinstance(capability, str) or not _CAPABILITY_KEY.fullmatch(capability):
                raise InvalidCapabilityRegistrationError(
                    "process capabilities must use canonical lowercase keys"
                )
        duplicate = _first_duplicate(process)
        if duplicate is not None:
            raise DuplicateCapabilityRegistrationError(duplicate)
        process = tuple(sorted(process))
        if process and not self.sandbox.verified:
            raise InvalidCapabilityRegistrationError(
                "universal process capabilities require verified sandbox evidence"
            )

        limits = _canonical_limits(self.limits)
        flags = _canonical_flags(self.flags)
        object.__setattr__(self, "process", process)
        object.__setattr__(self, "limits", MappingProxyType(limits))
        object.__setattr__(self, "flags", MappingProxyType(flags))

        capabilities_digest = _payload_digest(self._capabilities_payload())
        registry_digest = _payload_digest(self._registry_payload())
        object.__setattr__(self, "_capabilities_digest", capabilities_digest)
        object.__setattr__(self, "_registry_digest", registry_digest)

    @property
    def sandbox_verified(self) -> bool:
        """Expose the sandbox result without requiring callers to unpack evidence."""

        return self.sandbox.verified

    @property
    def registry_digest(self) -> str:
        """SHA-256 of formats, limits, flags, and sandbox evidence."""

        return self._registry_digest

    @property
    def capabilities_digest(self) -> str:
        """SHA-256 of only the advertised process and sandbox capability payload."""

        return self._capabilities_digest

    @property
    def digest(self) -> str:
        """Compatibility alias for the full canonical registry digest."""

        return self.registry_digest

    def canonical_payload(self) -> dict[str, object]:
        """Return the complete canonical payload used for registry parity."""

        return self._registry_payload()

    def canonical_json(self) -> str:
        """Return the exact deterministic JSON bytes-as-text hashed by ``digest``."""

        return _canonical_json(self._registry_payload())

    def advertised_payload(self) -> dict[str, object]:
        """Return the dark Phase 1 universal capability response payload."""

        return {
            "capabilities_digest": self.capabilities_digest,
            "process": list(self.process),
            "registry_digest": self.registry_digest,
            "sandbox_verified": self.sandbox_verified,
            "schema": self.schema,
            "version": self.version,
        }

    def _capabilities_payload(self) -> dict[str, object]:
        return {
            "process": list(self.process),
            "sandbox": self.sandbox.canonical_payload(),
            "schema": self.schema,
            "version": self.version,
        }

    def _registry_payload(self) -> dict[str, object]:
        return {
            "flags": dict(self.flags),
            "limits": dict(self.limits),
            "process": list(self.process),
            "sandbox": self.sandbox.canonical_payload(),
            "schema": self.schema,
            "version": self.version,
        }


def build_activation_candidate_registry(
    settings: Settings, probe_evidence: ActivationProbe
) -> CapabilityRegistry:
    """Build offline candidate evidence without changing a legacy live registry."""

    _require_activation_probe(probe_evidence)
    process = tuple(sorted(set(probe_evidence.capabilities)))
    missing = sorted(LEGACY_REQUIRED_PROCESS_FORMATS - set(process))
    if missing:
        raise InvalidCapabilityRegistrationError(
            "activation candidate is missing legacy equivalents: " + ", ".join(missing)
        )
    return CapabilityRegistry(
        process=process,
        limits={name: getattr(settings, name) for name in CAPABILITY_LIMIT_FIELDS},
        flags={
            "intake_mode": IntakeMode.LEGACY.value,
            "safe_fallback_enabled": True,
            "universal_activation_enabled": False,
            "activation_candidate": True,
        },
        sandbox=SandboxEvidence(
            verified=True,
            backend=probe_evidence.backend,
            evidence_digest=probe_evidence.evidence_digest,
        ),
    )


def build_capability_registry(
    settings: Settings,
    probe_evidence: ActivationProbe | None = None,
) -> CapabilityRegistry:
    """Build the one live registry; legacy stays dark and universal fails closed."""

    mode = ensure_intake_mode_available(settings.intake_mode)
    limits = {name: getattr(settings, name) for name in CAPABILITY_LIMIT_FIELDS}
    if mode is IntakeMode.UNIVERSAL:
        if probe_evidence is None or not probe_evidence.verified:
            raise SandboxUnavailable
        candidate = build_activation_candidate_registry(settings, probe_evidence)
        return CapabilityRegistry(
            process=candidate.process,
            limits=limits,
            flags={
                "intake_mode": mode.value,
                "safe_fallback_enabled": True,
                "universal_activation_enabled": True,
            },
            sandbox=candidate.sandbox,
        )
    flags: dict[str, bool | str] = {
        "intake_mode": mode.value,
        "safe_fallback_enabled": False,
        "universal_activation_enabled": False,
    }
    return CapabilityRegistry(
        process=(),
        limits=limits,
        flags=flags,
        sandbox=PHASE_ONE_SANDBOX_EVIDENCE,
    )


def _require_activation_probe(probe_evidence: ActivationProbe) -> None:
    if not probe_evidence.verified:
        raise SandboxUnavailable
    if not probe_evidence.backend or not probe_evidence.evidence_digest:
        raise InvalidCapabilityRegistrationError("verified activation probe lacks sandbox evidence")
    if any(
        not isinstance(capability, str) or not _CAPABILITY_KEY.fullmatch(capability)
        for capability in probe_evidence.capabilities
    ):
        raise InvalidCapabilityRegistrationError(
            "activation probe capabilities must be canonical lowercase keys"
        )


def _canonical_limits(values: Mapping[str, int | float]) -> dict[str, int | float]:
    canonical: dict[str, int | float] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _SETTING_KEY.fullmatch(key):
            raise InvalidCapabilityRegistrationError("limit names must be canonical setting keys")
        if key in canonical:
            raise InvalidCapabilityRegistrationError(f"duplicate limit {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidCapabilityRegistrationError(f"limit {key!r} must be numeric")
        if value <= 0 or not math.isfinite(value):
            raise InvalidCapabilityRegistrationError(f"limit {key!r} must be finite and positive")
        canonical[key] = value
    return dict(sorted(canonical.items()))


def _canonical_flags(values: Mapping[str, bool | str]) -> dict[str, bool | str]:
    canonical: dict[str, bool | str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _SETTING_KEY.fullmatch(key):
            raise InvalidCapabilityRegistrationError("flag names must be canonical setting keys")
        if key in canonical:
            raise InvalidCapabilityRegistrationError(f"duplicate flag {key!r}")
        if not isinstance(value, (bool, str)):
            raise InvalidCapabilityRegistrationError(f"flag {key!r} must be a string or boolean")
        canonical[key] = value
    return dict(sorted(canonical.items()))


def _first_duplicate(values: tuple[str, ...]) -> str | None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def _payload_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
