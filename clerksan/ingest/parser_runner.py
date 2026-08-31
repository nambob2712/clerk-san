"""Fail-closed client boundary for universal parsers.

The legacy adapters deliberately do not use this module.  Universal parsing is
only possible when a release sidecar proves the same boundary at startup.
Sources are represented by an already-open descriptor; a host path is never
part of the protocol.
"""

from __future__ import annotations

import array
import asyncio
import fcntl
import hashlib
import json
import os
import secrets
import socket
import stat
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from clerksan.ingest.limits import IngestLimits
from clerksan.ingest.normalized import NormalizedDocument
from clerksan.ingest.parser_artifacts import (
    MAX_ARTIFACT_FDS,
    BackendRunResult,
    ParserArtifactError,
    ParserRunResult,
    validate_received_artifacts,
    validate_result_artifact_set,
)

PROTOCOL_SCHEMA = "clerksan.parser-sidecar"
PROTOCOL_VERSION = 1
MAX_PROTOCOL_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PROTOCOL_REQUEST_BYTES = 128 * 1024
MAX_NONCE_LENGTH = 128
_FD_BYTES = array.array("i").itemsize


class ParserSandboxError(RuntimeError):
    """Base error for a denied, unavailable, or malformed parser invocation."""


class SandboxUnavailableError(ParserSandboxError):
    """The required isolated parser backend is not available."""


class SandboxProtocolError(ParserSandboxError):
    """The sidecar response failed protocol validation."""


@dataclass(frozen=True, slots=True)
class ReadOnlySource:
    """A source supplied to a parser by descriptor, never by arbitrary path."""

    fd: int
    source_sha256: str
    source_id: str | None = None
    source_version: int | None = None
    filename: str = "source"
    mime_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fd, int) or self.fd < 0:
            raise ValueError("ReadOnlySource.fd must be a non-negative file descriptor")
        if len(self.source_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_sha256
        ):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            raise ValueError("filename must be a basename, not a host path")

    def digest(self, *, max_bytes: int | None = None) -> str:
        """Hash one stable regular file without changing its shared offset."""

        source_stat = os.fstat(self.fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise SandboxProtocolError("source descriptor must be a regular file")
        expected_size = source_stat.st_size
        if max_bytes is not None and expected_size > max_bytes:
            raise SandboxProtocolError("source descriptor exceeds configured limit")
        digest = hashlib.sha256()
        offset = 0
        while offset < expected_size:
            chunk = os.pread(self.fd, min(1024 * 1024, expected_size - offset), offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        final_stat = os.fstat(self.fd)
        if offset != expected_size or final_stat.st_size != expected_size:
            raise SandboxProtocolError("source changed while its digest was checked")
        return digest.hexdigest()

    def verify_digest(self, *, max_bytes: int | None = None) -> None:
        actual = self.digest(max_bytes=max_bytes)
        if actual != self.source_sha256:
            raise SandboxProtocolError("source digest does not match the accepted source")


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Versioned, non-secret context passed to a parser sidecar."""

    adapter_key: str
    adapter_version: str = "1"
    policy_version: str = "1"
    registry_digest: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.adapter_key or any(char.isspace() for char in self.adapter_key):
            raise ValueError("adapter_key must be a non-empty stable key")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class SandboxProbeResult:
    """Startup evidence.  Unverified probes advertise no processing formats."""

    verified: bool
    backend: str | None = None
    evidence_digest: str | None = None
    capabilities: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.verified and (self.backend or self.evidence_digest or self.capabilities):
            raise ValueError("an unverified sandbox cannot advertise backend or capabilities")
        if self.verified:
            if not self.backend or not self.evidence_digest or len(self.evidence_digest) != 64:
                raise ValueError("verified sandbox evidence requires backend and SHA-256 digest")
            if any(char not in "0123456789abcdef" for char in self.evidence_digest):
                raise ValueError("sandbox evidence digest must be lowercase hexadecimal")
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))


class SandboxBackend(Protocol):
    def startup_probe(self) -> SandboxProbeResult: ...

    def preflight(
        self, source: ReadOnlySource, detected: Mapping[str, Any], limits: IngestLimits
    ) -> Mapping[str, Any]: ...

    def run(
        self,
        adapter_key: str,
        source: ReadOnlySource,
        context: AdapterContext,
        limits: IngestLimits,
    ) -> Mapping[str, Any] | BackendRunResult: ...


class UnavailableSandboxBackend:
    """Required default-host backend: never executes an in-process parser."""

    def startup_probe(self) -> SandboxProbeResult:
        return SandboxProbeResult(verified=False, reason="sandbox_unavailable")

    def preflight(self, source: ReadOnlySource, detected: Mapping[str, Any], limits: IngestLimits):
        raise SandboxUnavailableError("sandbox_unavailable")

    def run(self, adapter_key, source, context, limits):
        raise SandboxUnavailableError("sandbox_unavailable")


class SidecarSandboxBackend:
    """Unix-socket/FD client for a permissioned parser sidecar.

    The socket is injected in tests; production callers provide a filesystem
    Unix socket.  Requests contain no host path and the source is sent exactly
    once as an ``SCM_RIGHTS`` descriptor.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        timeout_seconds: float = 20.0,
        socket_factory=None,
        verify_socket_permissions: bool | None = None,
    ):
        if not socket_path or not socket_path.startswith("/"):
            raise ValueError("sidecar socket must be an absolute Unix socket path")
        if timeout_seconds <= 0:
            raise ValueError("sidecar timeout must be greater than zero")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self._socket_factory = socket_factory or socket.socket
        self._verify_socket_permissions = (
            socket_factory is None
            if verify_socket_permissions is None
            else verify_socket_permissions
        )
        self._registry_digest: str | None = None

    @property
    def registry_digest(self) -> str | None:
        """Return the exact registry digest from the last successful startup probe."""

        return self._registry_digest

    def startup_probe(self) -> SandboxProbeResult:
        try:
            response = self._request({"operation": "probe"}, None, allow_artifacts=False)
            assert isinstance(response, Mapping)
            result = _probe_from_response(response)
            registry_digest = response.get("registry_digest")
            if result.verified and not _is_sha256(registry_digest):
                raise SandboxProtocolError("sidecar probe lacks registry digest")
            self._registry_digest = str(registry_digest) if result.verified else None
            return result
        except (OSError, ParserSandboxError, TypeError, ValueError):
            self._registry_digest = None
            return SandboxProbeResult(verified=False, reason="sandbox_probe_failed")

    def preflight(self, source, detected, limits):
        if self._registry_digest is None:
            raise SandboxUnavailableError("sandbox_probe_required")
        response = self._request(
            {
                "operation": "preflight",
                "detected": dict(detected),
                "policy_version": "1",
                "registry_digest": self._registry_digest,
                "limits": _limits(limits),
            },
            source,
            allow_artifacts=False,
        )
        assert isinstance(response, Mapping)
        return response

    def run(self, adapter_key, source, context, limits):
        if self._registry_digest is None:
            raise SandboxUnavailableError("sandbox_probe_required")
        if adapter_key != context.adapter_key:
            raise SandboxProtocolError("adapter key does not match the bound context")
        # ``context.registry_digest`` is the application capability-registry
        # evidence persisted on the job.  The sidecar protocol has its own closed
        # adapter-registry digest returned by the startup probe; never conflate the
        # two digests or a valid universal job becomes impossible to dispatch.
        return self._request(
            {
                "operation": "run",
                "adapter_key": adapter_key,
                "adapter_version": context.adapter_version,
                "policy_version": context.policy_version,
                "registry_digest": self._registry_digest,
                "metadata": dict(context.metadata),
                "limits": _limits(limits),
            },
            source,
            allow_artifacts=True,
        )

    def _request(
        self,
        payload: Mapping[str, Any],
        source: ReadOnlySource | None,
        *,
        allow_artifacts: bool,
    ) -> Mapping[str, Any] | BackendRunResult:
        nonce = secrets.token_hex(16)
        body = dict(payload)
        if source is not None:
            request_limits = payload.get("limits")
            max_bytes = (
                request_limits.get("max_upload_bytes")
                if isinstance(request_limits, Mapping)
                else None
            )
            source.verify_digest(max_bytes=max_bytes if isinstance(max_bytes, int) else None)
            body["source_sha256"] = source.source_sha256
            body["source"] = {
                "filename": source.filename,
                "mime_type": source.mime_type,
                "source_id": source.source_id,
                "source_version": source.source_version,
            }
        body.update({"schema": PROTOCOL_SCHEMA, "version": PROTOCOL_VERSION, "nonce": nonce})
        encoded = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_PROTOCOL_REQUEST_BYTES:
            raise SandboxProtocolError("sidecar request exceeds protocol limit")
        sock = self._socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        artifact_fds: tuple[int, ...] = ()
        try:
            sock.settimeout(self.timeout_seconds)
            if self._verify_socket_permissions:
                _validate_socket_path(self.socket_path)
            sock.connect(self.socket_path)
            if source is None:
                sock.sendall(encoded)
            else:
                sent = sock.sendmsg(
                    [encoded],
                    [
                        (
                            socket.SOL_SOCKET,
                            socket.SCM_RIGHTS,
                            struct.pack("i", source.fd),
                        )
                    ],
                )
                if sent < len(encoded):
                    sock.sendall(encoded[sent:])
            response, artifact_fds = _read_json_line_with_fds(sock)
        except (OSError, ValueError) as error:
            _close_descriptors(artifact_fds)
            raise SandboxProtocolError("sidecar request failed") from error
        finally:
            sock.close()
        try:
            if response.get("nonce") != nonce:
                raise SandboxProtocolError("sidecar nonce mismatch")
            if source is not None:
                if response.get("source_sha256") != source.source_sha256:
                    raise SandboxProtocolError("sidecar source digest mismatch")
            if (
                response.get("schema") != PROTOCOL_SCHEMA
                or response.get("version") != PROTOCOL_VERSION
            ):
                raise SandboxProtocolError("sidecar response schema mismatch")
            if response.get("ok") is not True:
                raise SandboxProtocolError(
                    str(response.get("reason") or "sidecar rejected request")
                )
            if not allow_artifacts:
                if artifact_fds or response.get("artifacts") not in (None, []):
                    raise SandboxProtocolError(
                        "sidecar returned artifacts for a non-artifact operation"
                    )
                return response
            return BackendRunResult(response, artifact_fds)
        except BaseException:
            _close_descriptors(artifact_fds)
            raise


class ParserRunner:
    """Validate the sidecar boundary and normalize its bounded response."""

    def __init__(self, backend: SandboxBackend | None = None):
        self.backend = backend or UnavailableSandboxBackend()

    def startup_probe(self) -> SandboxProbeResult:
        result = self.backend.startup_probe()
        if not result.verified:
            return SandboxProbeResult(verified=False, reason=result.reason or "sandbox_unavailable")
        return result

    def preflight(self, source, detected, limits=None) -> Mapping[str, Any]:
        active_limits = limits or IngestLimits()
        source.verify_digest(max_bytes=active_limits.max_upload_bytes)
        result = self.backend.preflight(source, detected, active_limits)
        return _validate_evidence(result, source)

    async def run(self, adapter_key, source, context, limits=None) -> NormalizedDocument:
        """Run a format that is contractually artifact-free.

        Image and PDF callers must use :meth:`run_with_artifacts` so generated
        bytes cannot be discarded accidentally.
        """

        result = await self.run_with_artifacts(adapter_key, source, context, limits)
        if result.artifacts:
            raise SandboxProtocolError(
                "parser returned artifacts; use run_with_artifacts to consume them"
            )
        return result.normalized

    async def run_with_artifacts(
        self, adapter_key, source, context, limits=None
    ) -> ParserRunResult:
        """Run a parser and consume an exact bounded artifact FD set."""

        active_limits = limits or IngestLimits()
        source.verify_digest(max_bytes=active_limits.max_upload_bytes)
        backend_result = await asyncio.to_thread(
            self.backend.run,
            adapter_key,
            source,
            context,
            active_limits,
        )
        if isinstance(backend_result, BackendRunResult):
            result = backend_result.payload
            artifact_fds = backend_result.artifact_fds
        else:
            result = backend_result
            artifact_fds = ()
        try:
            result = _validate_evidence(result, source)
            normalized = result.get("normalized")
            if not isinstance(normalized, Mapping):
                raise SandboxProtocolError("sidecar result lacks normalized document")
            try:
                normalized_bytes = len(
                    json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
                )
            except (TypeError, ValueError, UnicodeEncodeError) as error:
                raise SandboxProtocolError(
                    "normalized document is not JSON serializable"
                ) from error
            if normalized_bytes > active_limits.max_normalized_output_bytes:
                raise SandboxProtocolError("normalized output exceeds configured limit")
            try:
                normalized_document = NormalizedDocument.model_validate(normalized)
            except Exception as error:
                raise SandboxProtocolError(
                    "sidecar normalized result failed schema validation"
                ) from error
            artifacts = validate_received_artifacts(
                result.get("artifacts", []),
                artifact_fds,
                source_sha256=source.source_sha256,
                limits=active_limits,
            )
            aggregate_output = normalized_bytes + sum(
                artifact.descriptor.byte_size for artifact in artifacts
            )
            if aggregate_output > active_limits.max_normalized_output_bytes:
                raise SandboxProtocolError("aggregate parser output exceeds configured limit")
            validate_result_artifact_set(
                adapter_key,
                normalized_document,
                artifacts,
                source_sha256=source.source_sha256,
            )
            return ParserRunResult(normalized_document, artifacts)
        except (OSError, ParserArtifactError) as error:
            raise SandboxProtocolError(str(error)) from error
        finally:
            _close_descriptors(artifact_fds)


def _read_json_line(sock: socket.socket) -> dict[str, Any]:
    response, descriptors = _read_json_line_with_fds(sock)
    try:
        if descriptors:
            raise SandboxProtocolError("sidecar response contains unexpected descriptors")
        return response
    finally:
        _close_descriptors(descriptors)


def _read_json_line_with_fds(
    sock: socket.socket,
) -> tuple[dict[str, Any], tuple[int, ...]]:
    data = bytearray()
    descriptors: list[int] = []
    try:
        while len(data) < MAX_PROTOCOL_RESPONSE_BYTES:
            chunk, ancillary, flags, _ = sock.recvmsg(
                65536,
                socket.CMSG_SPACE(_FD_BYTES * MAX_ARTIFACT_FDS),
            )
            descriptors.extend(_descriptors_from_ancillary(ancillary))
            if flags & socket.MSG_CTRUNC:
                raise SandboxProtocolError("sidecar artifact control data was truncated")
            if len(descriptors) > MAX_ARTIFACT_FDS:
                raise SandboxProtocolError("too many sidecar artifact descriptors")
            if not chunk:
                break
            if len(data) + len(chunk) > MAX_PROTOCOL_RESPONSE_BYTES:
                raise SandboxProtocolError("sidecar response exceeds protocol limit")
            data.extend(chunk)
            if b"\n" in chunk:
                break
        if b"\n" not in data:
            raise SandboxProtocolError("sidecar response is not newline terminated")
        first_line, trailing = bytes(data).split(b"\n", 1)
        if trailing:
            raise SandboxProtocolError("sidecar response contains trailing data")
        if not first_line:
            raise SandboxProtocolError("sidecar response is empty")
        try:
            value = json.loads(first_line)
        except (ValueError, UnicodeDecodeError) as error:
            raise SandboxProtocolError("sidecar returned invalid JSON") from error
        if not isinstance(value, dict):
            raise SandboxProtocolError("sidecar response must be an object")
        for descriptor in descriptors:
            fcntl.fcntl(descriptor, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
        return value, tuple(descriptors)
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _descriptors_from_ancillary(
    ancillary: list[tuple[int, int, bytes]],
) -> list[int]:
    descriptors: list[int] = []
    try:
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                raise SandboxProtocolError("unsupported sidecar response ancillary data")
            usable = len(data) - (len(data) % _FD_BYTES)
            values = array.array("i")
            values.frombytes(data[:usable])
            descriptors.extend(values.tolist())
        return descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _close_descriptors(descriptors) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _validate_evidence(response: Mapping[str, Any], source: ReadOnlySource) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise SandboxProtocolError("sidecar response must be an object")
    digest = response.get("source_sha256")
    if digest != source.source_sha256:
        raise SandboxProtocolError("sidecar source digest mismatch")
    try:
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise SandboxProtocolError("sidecar response is not JSON serializable") from error
    if len(encoded) > MAX_PROTOCOL_RESPONSE_BYTES:
        raise SandboxProtocolError("sidecar response exceeds protocol limit")
    return dict(response)


def _probe_from_response(response: Mapping[str, Any]) -> SandboxProbeResult:
    if response.get("verified") is not True:
        return SandboxProbeResult(verified=False, reason="sandbox_probe_failed")
    capabilities = response.get("capabilities") or ()
    if not isinstance(capabilities, (list, tuple)) or not all(
        isinstance(capability, str) and capability and len(capability) <= 128
        for capability in capabilities
    ):
        raise SandboxProtocolError("invalid sandbox capabilities")
    backend = response.get("backend")
    evidence_digest = response.get("evidence_digest")
    if not isinstance(backend, str) or not backend or len(backend) > 128:
        raise SandboxProtocolError("invalid sandbox backend evidence")
    if not _is_sha256(evidence_digest):
        raise SandboxProtocolError("invalid sandbox evidence digest")
    return SandboxProbeResult(
        verified=True,
        backend=backend,
        evidence_digest=evidence_digest,
        capabilities=tuple(capabilities),
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _validate_socket_path(socket_path: str) -> None:
    """Require a real, non-world-accessible Unix socket owned by this runtime user."""

    try:
        socket_stat = os.lstat(socket_path)
    except OSError as error:
        raise SandboxUnavailableError("sandbox_unavailable") from error
    if not stat.S_ISSOCK(socket_stat.st_mode):
        raise SandboxProtocolError("sidecar endpoint is not a Unix socket")
    if socket_stat.st_uid != os.geteuid() or socket_stat.st_mode & 0o007:
        raise SandboxProtocolError("sidecar Unix socket permissions are not trusted")
    parent_stat = os.lstat(os.path.dirname(socket_path))
    if stat.S_ISLNK(parent_stat.st_mode) or parent_stat.st_mode & 0o002:
        raise SandboxProtocolError("sidecar socket directory permissions are not trusted")


def _limits(limits: IngestLimits) -> dict[str, int | float]:
    return {
        key: getattr(limits, key) for key in limits.__dataclass_fields__ if not key.startswith("_")
    }
