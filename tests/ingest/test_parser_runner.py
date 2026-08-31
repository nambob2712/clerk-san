from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import socket
import struct
import threading

import pytest
from PIL import Image

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits
from clerksan.ingest.normalized import DocMetadata, ExtractedImage, NormalizedDocument
from clerksan.ingest.parser_artifacts import (
    PNG_MIME,
    ArtifactRole,
    BackendRunResult,
    GeneratedArtifact,
    create_sealed_artifact_fd,
    descriptor_for_generated,
)
from clerksan.ingest.parser_runner import (
    AdapterContext,
    ParserRunner,
    ReadOnlySource,
    SandboxProbeResult,
    SandboxProtocolError,
    SidecarSandboxBackend,
    UnavailableSandboxBackend,
)


def _source(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"hello")
    fd = os.open(path, os.O_RDONLY)
    return fd, ReadOnlySource(fd, hashlib.sha256(b"hello").hexdigest(), filename="sample.txt")


def _png() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (3, 2), "white")
    image.save(output, "PNG")
    image.close()
    return output.getvalue()


def _artifact_backend_result(source: ReadOnlySource, **descriptor_updates):
    data = _png()
    generated = GeneratedArtifact(
        ArtifactRole.SANITIZED_IMAGE,
        PNG_MIME,
        data,
        page_number=1,
        width=3,
        height=2,
        source_location="image:1",
        ocr_required=True,
    )
    artifact_fd, seal_supported, sealed = create_sealed_artifact_fd(generated)
    descriptor = descriptor_for_generated(
        generated,
        ordinal=1,
        source_sha256=source.source_sha256,
        seal_supported=seal_supported,
        sealed=sealed,
    ).as_dict()
    descriptor.update(descriptor_updates)
    normalized = NormalizedDocument(
        markdown_body="",
        metadata=DocMetadata(
            filename="sample.jpg",
            detected_type=FileType.JPEG,
            sha256=source.source_sha256,
            family="image",
            page_provenance=["ocr_required"],
            extra={"ocr_required": True, "ocr_required_pages": [1]},
        ),
        images=[
            ExtractedImage(
                sha256=hashlib.sha256(data).hexdigest(),
                content_path="artifact:sanitized-image:1",
                width=3,
                height=2,
                source_location="image:1",
            )
        ],
        embeddable=False,
    )
    return (
        BackendRunResult(
            {
                "source_sha256": source.source_sha256,
                "normalized": normalized.model_dump(mode="json"),
                "artifacts": [descriptor],
            },
            (artifact_fd,),
        ),
        artifact_fd,
    )


def _serve_one_response(listener: socket.socket, response_update: dict[str, object]) -> None:
    connection, _ = listener.accept()
    received_fds: list[int] = []
    try:
        request_bytes, ancillary, _flags, _address = connection.recvmsg(
            65536,
            socket.CMSG_SPACE(struct.calcsize("i")),
        )
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                received_fds.append(struct.unpack("i", data[: struct.calcsize("i")])[0])
        request = json.loads(request_bytes.split(b"\n", 1)[0])
        response = {
            "schema": "clerksan.parser-sidecar",
            "version": 1,
            "nonce": request["nonce"],
            "source_sha256": request["source_sha256"],
            "ok": True,
            "artifacts": [],
        }
        response.update(response_update)
        connection.sendall(
            (json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
    finally:
        for descriptor in received_fds:
            os.close(descriptor)
        connection.close()
        listener.close()


class _ArtifactBackend:
    def __init__(self, result):
        self.result = result

    def startup_probe(self):
        return SandboxProbeResult(False, reason="test")

    def preflight(self, source, detected, limits):
        return {"source_sha256": source.source_sha256, "evidence": {"safe": True}}

    def run(self, adapter_key, source, context, limits):
        return self.result


def test_unavailable_backend_never_advertises_capabilities(tmp_path):
    fd, source = _source(tmp_path)
    try:
        result = ParserRunner(UnavailableSandboxBackend()).startup_probe()
        assert result == SandboxProbeResult(verified=False, reason="sandbox_unavailable")
        with pytest.raises(Exception, match="sandbox_unavailable"):
            asyncio.run(ParserRunner().run("text", source, AdapterContext("text")))
    finally:
        os.close(fd)


def test_source_rejects_digest_mismatch(tmp_path):
    fd, source = _source(tmp_path)
    try:
        bad = ReadOnlySource(fd, "0" * 64, filename="sample.txt")
        with pytest.raises(SandboxProtocolError, match="digest"):
            bad.verify_digest()
    finally:
        os.close(fd)


def test_source_digest_is_bounded_and_requires_a_regular_file(tmp_path):
    fd, source = _source(tmp_path)
    try:
        with pytest.raises(SandboxProtocolError, match="configured limit"):
            source.verify_digest(max_bytes=4)
    finally:
        os.close(fd)

    read_fd, write_fd = os.pipe()
    try:
        pipe_source = ReadOnlySource(
            read_fd,
            hashlib.sha256(b"").hexdigest(),
            filename="pipe.bin",
        )
        with pytest.raises(SandboxProtocolError, match="regular file"):
            pipe_source.verify_digest()
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    ("response_update", "message"),
    [
        ({"nonce": "f" * 32}, "nonce"),
        ({"source_sha256": "0" * 64}, "source digest"),
    ],
)
def test_sidecar_client_rejects_nonce_and_source_binding_mismatch(
    tmp_path,
    response_update,
    message,
):
    fd, source = _source(tmp_path)
    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    socket_path = f"/tmp/clerksan-parser-{suffix}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(socket_path)
    listener.listen(1)
    thread = threading.Thread(
        target=_serve_one_response,
        args=(listener, response_update),
    )
    thread.start()
    try:
        backend = SidecarSandboxBackend(
            socket_path,
            timeout_seconds=2,
            verify_socket_permissions=False,
        )
        with pytest.raises(SandboxProtocolError, match=message):
            backend._request(
                {"operation": "run"},
                source,
                allow_artifacts=True,
            )
    finally:
        thread.join(timeout=2)
        os.close(fd)
        if os.path.exists(socket_path):
            os.unlink(socket_path)


def test_runner_validates_normalized_schema_and_digest(tmp_path):
    fd, source = _source(tmp_path)

    class Backend:
        def startup_probe(self):
            return SandboxProbeResult(False, reason="test")

        def preflight(self, source, detected, limits):
            return {"source_sha256": source.source_sha256, "evidence": {"safe": True}}

        def run(self, adapter_key, source, context, limits):
            return {"source_sha256": source.source_sha256, "normalized": {"bad": True}}

    try:
        with pytest.raises(SandboxProtocolError, match="normalized"):
            asyncio.run(ParserRunner(Backend()).run("text", source, AdapterContext("text")))
    finally:
        os.close(fd)


def test_runner_rejects_normalized_output_over_limit(tmp_path):
    fd, source = _source(tmp_path)

    class Backend:
        def startup_probe(self):
            return SandboxProbeResult(False, reason="test")

        def preflight(self, source, detected, limits):
            return {"source_sha256": source.source_sha256, "evidence": {"safe": True}}

        def run(self, adapter_key, source, context, limits):
            return {
                "source_sha256": source.source_sha256,
                "normalized": {"text": "x" * 100},
            }

    try:
        with pytest.raises(SandboxProtocolError, match="output exceeds"):
            asyncio.run(
                ParserRunner(Backend()).run(
                    "text",
                    source,
                    AdapterContext("text"),
                    IngestLimits(max_normalized_output_bytes=32),
                )
            )
    finally:
        os.close(fd)


def test_runner_consumes_valid_artifact_and_closes_received_descriptor(tmp_path):
    fd, source = _source(tmp_path)
    backend_result, artifact_fd = _artifact_backend_result(source)
    try:
        result = asyncio.run(
            ParserRunner(_ArtifactBackend(backend_result)).run_with_artifacts(
                "jpeg",
                source,
                AdapterContext("jpeg"),
            )
        )

        assert result.artifacts[0].data == _png()
        assert result.artifacts[0].descriptor.role is ArtifactRole.SANITIZED_IMAGE
        with pytest.raises(OSError):
            os.fstat(artifact_fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "descriptor_updates",
    [
        {"sha256": "0" * 64},
        {"byte_size": 1},
        {"ordinal": 2},
        {"media_type": "application/pdf"},
    ],
)
def test_runner_rejects_artifact_digest_size_ordinal_and_media_regressions_and_closes_fd(
    tmp_path,
    descriptor_updates,
):
    fd, source = _source(tmp_path)
    backend_result, artifact_fd = _artifact_backend_result(source, **descriptor_updates)
    try:
        with pytest.raises(SandboxProtocolError):
            asyncio.run(
                ParserRunner(_ArtifactBackend(backend_result)).run_with_artifacts(
                    "jpeg",
                    source,
                    AdapterContext("jpeg"),
                )
            )
        with pytest.raises(OSError):
            os.fstat(artifact_fd)
    finally:
        os.close(fd)


def test_runner_rejects_artifact_metadata_fd_cardinality_and_closes_fd(tmp_path):
    fd, source = _source(tmp_path)
    backend_result, artifact_fd = _artifact_backend_result(source)
    malformed = BackendRunResult(
        {**dict(backend_result.payload), "artifacts": []},
        backend_result.artifact_fds,
    )
    try:
        with pytest.raises(SandboxProtocolError, match="cardinality"):
            asyncio.run(
                ParserRunner(_ArtifactBackend(malformed)).run_with_artifacts(
                    "jpeg", source, AdapterContext("jpeg")
                )
            )
        with pytest.raises(OSError):
            os.fstat(artifact_fd)
    finally:
        os.close(fd)


def test_runner_rejects_seal_evidence_mismatch_and_closes_fd(tmp_path):
    fd, source = _source(tmp_path)
    backend_result, artifact_fd = _artifact_backend_result(source)
    descriptor = dict(backend_result.payload["artifacts"][0])
    if descriptor["seal_supported"]:
        descriptor["sealed"] = False
    else:
        descriptor["seal_supported"] = True
        descriptor["sealed"] = True
    malformed = BackendRunResult(
        {**dict(backend_result.payload), "artifacts": [descriptor]},
        backend_result.artifact_fds,
    )
    try:
        with pytest.raises(SandboxProtocolError, match="seal"):
            asyncio.run(
                ParserRunner(_ArtifactBackend(malformed)).run_with_artifacts(
                    "jpeg", source, AdapterContext("jpeg")
                )
            )
        with pytest.raises(OSError):
            os.fstat(artifact_fd)
    finally:
        os.close(fd)


def test_runner_closes_artifact_fd_when_normalized_schema_is_invalid(tmp_path):
    fd, source = _source(tmp_path)
    backend_result, artifact_fd = _artifact_backend_result(source)
    malformed = BackendRunResult(
        {**dict(backend_result.payload), "normalized": {"bad": True}},
        backend_result.artifact_fds,
    )
    try:
        with pytest.raises(SandboxProtocolError, match="normalized"):
            asyncio.run(
                ParserRunner(_ArtifactBackend(malformed)).run_with_artifacts(
                    "jpeg", source, AdapterContext("jpeg")
                )
            )
        with pytest.raises(OSError):
            os.fstat(artifact_fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    ("normalized_update", "message"),
    [
        ({"metadata.sha256": "0" * 64}, "source digest"),
        ({"images.0.content_path": "/host/leak.png"}, "metadata"),
    ],
)
def test_runner_rejects_unbound_normalized_artifact_links_and_closes_fd(
    tmp_path,
    normalized_update,
    message,
):
    fd, source = _source(tmp_path)
    backend_result, artifact_fd = _artifact_backend_result(source)
    normalized = dict(backend_result.payload["normalized"])
    if "metadata.sha256" in normalized_update:
        normalized["metadata"] = {
            **normalized["metadata"],
            "sha256": normalized_update["metadata.sha256"],
        }
    else:
        normalized["images"] = [
            {
                **normalized["images"][0],
                "content_path": normalized_update["images.0.content_path"],
            }
        ]
    malformed = BackendRunResult(
        {**dict(backend_result.payload), "normalized": normalized},
        backend_result.artifact_fds,
    )
    try:
        with pytest.raises(SandboxProtocolError, match=message):
            asyncio.run(
                ParserRunner(_ArtifactBackend(malformed)).run_with_artifacts(
                    "jpeg", source, AdapterContext("jpeg")
                )
            )
        with pytest.raises(OSError):
            os.fstat(artifact_fd)
    finally:
        os.close(fd)


def test_legacy_run_refuses_to_discard_artifacts(tmp_path):
    fd, source = _source(tmp_path)
    backend_result, artifact_fd = _artifact_backend_result(source)
    try:
        with pytest.raises(SandboxProtocolError, match="run_with_artifacts"):
            asyncio.run(
                ParserRunner(_ArtifactBackend(backend_result)).run(
                    "jpeg", source, AdapterContext("jpeg")
                )
            )
        with pytest.raises(OSError):
            os.fstat(artifact_fd)
    finally:
        os.close(fd)
