"""Adapter protocol and unambiguous registry for normalized ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from clerksan.ingest.capabilities import (
    LEGACY_COMPAT_EXECUTION_PROFILE,
    UNIVERSAL_SANDBOXED_EXECUTION_PROFILE,
)
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

_ADAPTER_KEY = re.compile(r"[a-z0-9][a-z0-9._+-]*\Z")


class NoAdapterError(LookupError):
    """No adapter is registered for a detected file type."""

    def __init__(self, file_type: FileType) -> None:
        self.file_type = file_type
        super().__init__(f"no adapter registered for detected type {file_type.value!r}")


class DuplicateAdapterRegistrationError(ValueError):
    """Two adapters claim the same detected type."""

    def __init__(self, file_type: FileType) -> None:
        self.file_type = file_type
        super().__init__(f"an adapter is already registered for {file_type.value!r}")


class DuplicateAdapterKeyRegistrationError(ValueError):
    """Two registrations use the same stable adapter key."""

    def __init__(self, adapter_key: str) -> None:
        self.adapter_key = adapter_key
        super().__init__(f"adapter key {adapter_key!r} is already registered")


class CapabilityParityError(RuntimeError):
    """Advertised universal process formats do not have verified handlers."""


@dataclass(frozen=True, slots=True)
class AdapterCapabilityMetadata:
    """Execution evidence attached to one adapter registration.

    Merely registering metadata never advertises a capability.  Advertisement is
    owned by the separate canonical universal registry.
    """

    adapter_key: str
    execution_profile: str = LEGACY_COMPAT_EXECUTION_PROFILE
    sandbox_verified: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_key, str) or not _ADAPTER_KEY.fullmatch(self.adapter_key):
            raise ValueError("adapter_key must be a canonical lowercase key")
        if self.execution_profile not in {
            LEGACY_COMPAT_EXECUTION_PROFILE,
            UNIVERSAL_SANDBOXED_EXECUTION_PROFILE,
        }:
            raise ValueError("unknown adapter execution profile")
        if not isinstance(self.sandbox_verified, bool):
            raise TypeError("sandbox_verified must be a boolean")
        if self.execution_profile == LEGACY_COMPAT_EXECUTION_PROFILE and self.sandbox_verified:
            raise ValueError("legacy_compat adapters cannot claim sandbox verification")
        if (
            self.execution_profile == UNIVERSAL_SANDBOXED_EXECUTION_PROFILE
            and not self.sandbox_verified
        ):
            raise ValueError("universal_sandboxed adapters require sandbox verification")


@dataclass(frozen=True, slots=True)
class AdapterRegistrationEvidence:
    """Read-only startup evidence for one registered adapter."""

    adapter_key: str
    supported_types: tuple[FileType, ...]
    execution_profile: str
    sandbox_verified: bool


@runtime_checkable
class DocumentAdapter(Protocol):
    """Every input format converges to one ``NormalizedDocument`` through this seam."""

    supported_types: tuple[FileType, ...]

    def normalize(self, source: ReadOnlySource, context: AdapterContext) -> NormalizedDocument:
        """Normalize an already-open, digest-bound source without a host path."""

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        """Legacy compatibility wrapper; new callers must use ``normalize``."""


class AdvertisedCapabilityRegistry(Protocol):
    """Minimal registry surface needed for startup parity validation."""

    process: tuple[str, ...]
    sandbox_verified: bool


class AdapterRegistry:
    """Dispatch a detected type to exactly one adapter."""

    def __init__(self) -> None:
        self._adapters: dict[FileType, DocumentAdapter] = {}
        self._metadata: dict[FileType, AdapterCapabilityMetadata] = {}
        self._adapter_keys: set[str] = set()
        self._registration_evidence: list[AdapterRegistrationEvidence] = []

    def register(
        self,
        adapter: DocumentAdapter,
        *,
        capability_metadata: AdapterCapabilityMetadata | None = None,
    ) -> None:
        """Register ``adapter`` with explicit evidence or a legacy-safe default."""

        supported_types = tuple(adapter.supported_types)
        if not supported_types:
            raise ValueError("adapter must declare at least one supported type")
        if any(not isinstance(file_type, FileType) for file_type in supported_types):
            raise TypeError("adapter supported_types must contain FileType members")
        if len(set(supported_types)) != len(supported_types):
            raise ValueError("adapter declares a detected type more than once")

        conflicts = [file_type for file_type in supported_types if file_type in self._adapters]
        if conflicts:
            raise DuplicateAdapterRegistrationError(conflicts[0])

        metadata = capability_metadata or _legacy_capability_metadata(adapter, supported_types)
        if not isinstance(metadata, AdapterCapabilityMetadata):
            raise TypeError("capability_metadata must be AdapterCapabilityMetadata")
        if metadata.adapter_key in self._adapter_keys:
            raise DuplicateAdapterKeyRegistrationError(metadata.adapter_key)

        for file_type in supported_types:
            self._adapters[file_type] = adapter
            self._metadata[file_type] = metadata
        self._adapter_keys.add(metadata.adapter_key)
        self._registration_evidence.append(
            AdapterRegistrationEvidence(
                adapter_key=metadata.adapter_key,
                supported_types=supported_types,
                execution_profile=metadata.execution_profile,
                sandbox_verified=metadata.sandbox_verified,
            )
        )

    def get(self, file_type: FileType) -> DocumentAdapter:
        """Return the unique adapter for a detected type."""

        try:
            return self._adapters[file_type]
        except KeyError as error:
            raise NoAdapterError(file_type) from error

    def metadata_for(self, file_type: FileType) -> AdapterCapabilityMetadata:
        """Return execution evidence for a registered detected type."""

        try:
            return self._metadata[file_type]
        except KeyError as error:
            raise NoAdapterError(file_type) from error

    def validate_capabilities(self, registry: AdvertisedCapabilityRegistry) -> None:
        """Fail startup if advertised process formats lack sandboxed registrations."""

        process = tuple(registry.process)
        if process and not registry.sandbox_verified:
            raise CapabilityParityError(
                "universal process advertisement requires verified sandbox evidence"
            )
        available = {
            file_type.value
            for file_type, metadata in self._metadata.items()
            if metadata.execution_profile == UNIVERSAL_SANDBOXED_EXECUTION_PROFILE
            and metadata.sandbox_verified
        }
        missing = sorted(set(process) - available)
        if missing:
            raise CapabilityParityError(
                "advertised process formats have no sandboxed adapter: " + ", ".join(missing)
            )

    @property
    def registered_types(self) -> frozenset[FileType]:
        """Expose a read-only view for startup diagnostics and tests."""

        return frozenset(self._adapters)

    @property
    def registration_evidence(self) -> tuple[AdapterRegistrationEvidence, ...]:
        """Expose immutable evidence without treating handlers as advertised formats."""

        return tuple(self._registration_evidence)

    @property
    def legacy_compat_types(self) -> frozenset[FileType]:
        """Return current in-process formats explicitly marked as legacy-only."""

        return frozenset(
            file_type
            for file_type, metadata in self._metadata.items()
            if metadata.execution_profile == LEGACY_COMPAT_EXECUTION_PROFILE
            and not metadata.sandbox_verified
        )


def _legacy_capability_metadata(
    adapter: DocumentAdapter,
    supported_types: tuple[FileType, ...],
) -> AdapterCapabilityMetadata:
    class_key = re.sub(r"[^a-z0-9]+", "-", type(adapter).__name__.casefold()).strip("-")
    type_key = ".".join(sorted(file_type.value for file_type in supported_types))
    return AdapterCapabilityMetadata(
        adapter_key=f"legacy.{class_key or 'adapter'}.{type_key}",
        execution_profile=LEGACY_COMPAT_EXECUTION_PROFILE,
        sandbox_verified=False,
    )
