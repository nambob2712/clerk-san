from __future__ import annotations

import array
import asyncio
import hashlib
import io
import json
import multiprocessing
import os
import socket
import time
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from clerksan.ingest.filetype import FileType
from clerksan.ingest.limits import IngestLimits
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.ingest.parser_artifacts import PNG_SIGNATURE, ArtifactRole
from clerksan.ingest.parser_runner import (
    AdapterContext,
    ParserRunner,
    ReadOnlySource,
    SandboxProtocolError,
    SidecarSandboxBackend,
)
from clerksan.ingest.parser_service import (
    ParserAdapterRegistry,
    ParserServiceConfig,
    ParserSidecarServer,
    SandboxRuntimeEvidence,
    _execute_child,
    build_default_registry,
    receive_request,
    response,
    validate_request,
)


def _request(**extra):
    value = {
        "schema": "clerksan.parser-sidecar",
        "version": 1,
        "nonce": "a" * 32,
        "operation": "run",
    }
    value.update(extra)
    return value


def _limits() -> dict[str, int | float]:
    limits = IngestLimits()
    return {
        name: getattr(limits, name)
        for name in limits.__dataclass_fields__
        if not name.startswith("_")
    }


def _run_request(registry_digest: str, raw: bytes, **extra):
    value = _request(
        source_sha256=hashlib.sha256(raw).hexdigest(),
        source={
            "filename": "transactions.csv",
            "mime_type": "text/csv",
            "source_id": "source-1",
            "source_version": 1,
        },
        adapter_key="delimited.csv",
        adapter_version="1",
        policy_version="1",
        registry_digest=registry_digest,
        metadata={"detected_type": "csv"},
        limits=_limits(),
    )
    value.update(extra)
    return value


def _all_sandbox_evidence() -> SandboxRuntimeEvidence:
    return SandboxRuntimeEvidence(
        network_isolated=True,
        secrets_absent=True,
        root_read_only=True,
        capabilities_dropped=True,
        no_new_privileges=True,
        tmpfs_hardened=True,
        cgroup_bounded=True,
        child_timeout_reaped=True,
    )


def _short_socket_path(tmp_path):
    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    directory = type(tmp_path)(f"/tmp/clerksan-{suffix}")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory / "parser.sock"


def _send_with_descriptors(
    connection: socket.socket, payload: dict, descriptors: list[int]
) -> None:
    rights = array.array("i", descriptors)
    connection.sendmsg(
        [(json.dumps(payload, sort_keys=True) + "\n").encode()],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())],
    )


def _serve_test_sidecar(config: ParserServiceConfig) -> None:
    ParserSidecarServer(config, runtime_evidence=_all_sandbox_evidence()).serve_forever()


def _direct_sidecar_scenario(
    scenario: str,
    directory: str,
    result_connection,
) -> None:
    """Exercise direct sidecar paths in a fresh process on fork-sensitive hosts."""

    base = Path(directory)
    config = ParserServiceConfig(socket_path=_short_socket_path(base))
    sidecar = None
    source_fd = None
    client = None
    server = None
    try:
        registry = build_default_registry()
        evidence = _all_sandbox_evidence()
        if scenario == "unverified":
            evidence = SandboxRuntimeEvidence(**{**evidence.as_dict(), "network_isolated": False})
        sidecar = ParserSidecarServer(config, registry, runtime_evidence=evidence)

        if scenario == "probe":
            request = validate_request(_request(operation="probe"), has_source_fd=False)
            before = sidecar._probe_response(request)
            sidecar._bind_listener()
            value = {"before": before, "after": sidecar._probe_response(request)}
        else:
            if scenario != "unverified":
                sidecar._bind_listener()
            raw = b"name,amount\ncoffee,450\n" if scenario == "csv" else b"a,b\n1,2\n"
            source_path = base / "source.csv"
            source_path.write_bytes(raw)
            flags = os.O_RDWR if scenario == "writable" else os.O_RDONLY
            source_fd = os.open(source_path, flags)
            client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            request = _run_request(registry.registry_digest, raw)
            _send_with_descriptors(client, request, [source_fd])
            sidecar.handle_connection(server)
            value = json.loads(client.recv(1024 * 1024).split(b"\n", 1)[0])

        result_connection.send({"ok": True, "value": value})
    except BaseException as error:
        result_connection.send({"ok": False, "error": repr(error)})
    finally:
        if client is not None:
            client.close()
        if server is not None:
            server.close()
        if source_fd is not None:
            os.close(source_fd)
        if sidecar is not None:
            sidecar.close()
        if config.socket_path.parent.exists():
            config.socket_path.parent.rmdir()
        result_connection.close()


def _run_direct_sidecar_scenario(scenario: str, tmp_path: Path) -> dict:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_direct_sidecar_scenario,
        args=(scenario, str(tmp_path), sender),
    )
    process.start()
    sender.close()
    try:
        assert receiver.poll(20), f"spawned {scenario} sidecar scenario timed out"
        envelope = receiver.recv()
    finally:
        receiver.close()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0
    assert envelope["ok"] is True, envelope.get("error")
    return envelope["value"]


def test_service_requires_descriptor_for_parser_operations():
    with pytest.raises(SandboxProtocolError, match="descriptor"):
        validate_request(_request(), has_source_fd=False)


def test_service_rejects_host_paths_and_secrets():
    with pytest.raises(SandboxProtocolError, match="paths and secrets"):
        validate_request(_request(path="/tmp/source"), has_source_fd=True)

    nested = _request(
        source_sha256="a" * 64,
        source={
            "filename": "sample.csv",
            "mime_type": None,
            "source_id": None,
            "source_version": None,
        },
        adapter_key="delimited.csv",
        adapter_version="1",
        policy_version="1",
        registry_digest="b" * 64,
        metadata={"database_password": "do-not-send"},
        limits=_limits(),
    )
    with pytest.raises(SandboxProtocolError, match="paths and secrets"):
        validate_request(nested, has_source_fd=True)


def test_service_accepts_probe_without_descriptor_and_builds_envelope():
    request = _request(operation="probe")
    parsed = validate_request(request, has_source_fd=False)
    assert parsed.operation == "probe"
    built = response(nonce=parsed.nonce, source_sha256=None, ok=True, verified=False)
    assert built["schema"] == "clerksan.parser-sidecar"
    assert built["nonce"] == parsed.nonce


def test_service_requires_exactly_one_descriptor_for_parser_request():
    request = _request(source_sha256="a" * 64)
    with pytest.raises(SandboxProtocolError, match="exactly one"):
        validate_request(request, fd_count=2)


def test_service_requires_no_descriptor_for_probe():
    with pytest.raises(SandboxProtocolError, match="no source descriptor"):
        validate_request(_request(operation="probe"), fd_count=1)


def test_service_rejects_unbounded_response_nonce():
    with pytest.raises(ValueError, match="bounded"):
        response(nonce="n" * 129, source_sha256=None, ok=True)


def test_response_bindings_cannot_be_overridden():
    with pytest.raises(ValueError, match="cannot override"):
        response(
            nonce="n" * 32,
            source_sha256=None,
            ok=True,
            schema="attacker-schema",
        )


def test_receive_request_accepts_exactly_one_scm_rights_descriptor(tmp_path):
    raw = b"name,amount\ncoffee,450\n"
    source_path = tmp_path / "transactions.csv"
    source_path.write_bytes(raw)
    source_fd = os.open(source_path, os.O_RDONLY)
    registry = build_default_registry()
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    received_fd = None
    try:
        _send_with_descriptors(client, _run_request(registry.registry_digest, raw), [source_fd])
        request, received_fd = receive_request(server)
        assert request.operation == "run"
        assert received_fd is not None
        assert os.pread(received_fd, len(raw), 0) == raw
    finally:
        if received_fd is not None:
            os.close(received_fd)
        client.close()
        server.close()
        os.close(source_fd)


def test_receive_request_rejects_multiple_descriptors(tmp_path):
    raw = b"a,b\n1,2\n"
    source_path = tmp_path / "source.csv"
    source_path.write_bytes(raw)
    first = os.open(source_path, os.O_RDONLY)
    second = os.open(source_path, os.O_RDONLY)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        registry = build_default_registry()
        _send_with_descriptors(client, _run_request(registry.registry_digest, raw), [first, second])
        with pytest.raises(SandboxProtocolError, match="exactly one"):
            receive_request(server)
    finally:
        client.close()
        server.close()
        os.close(first)
        os.close(second)


def test_registry_is_explicit_digest_bound_and_rejects_duplicates():
    registry = build_default_registry()
    assert registry.capabilities == tuple(sorted(item.value for item in FileType))
    assert len(registry.registry_digest) == 64
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(
            adapter_key="delimited.csv",
            format_key="another-csv",
            run=lambda source, context, limits: {},
            preflight=lambda source, detected, limits: {},
        )

    second = ParserAdapterRegistry()
    second.register(
        adapter_key="example.one",
        format_key="example",
        run=lambda source, context, limits: {},
        preflight=lambda source, detected, limits: {},
    )
    assert second.registry_digest != registry.registry_digest


def test_startup_self_test_does_not_advertise_a_registered_but_failing_handler(tmp_path):
    registry = ParserAdapterRegistry()

    def always_fails(source, context, limits):
        raise ValueError("injected handler failure")

    registry.register(
        adapter_key="txt",
        format_key="txt",
        run=always_fails,
        preflight=lambda source, detected, limits: {"safe": True},
        startup_fixture=lambda: b"safe bounded fixture",
    )
    config = ParserServiceConfig(socket_path=_short_socket_path(tmp_path))
    server = ParserSidecarServer(
        config,
        registry,
        runtime_evidence=_all_sandbox_evidence(),
    )
    request = validate_request(_request(operation="probe"), has_source_fd=False)
    try:
        server._bind_listener()
        result = server._probe_response(request)

        assert result["verified"] is True
        assert result["capabilities"] == []
        assert result["format_self_tests"] == {"txt": {"passed": False, "evidence_digest": None}}
        assert result["registry_digest"] != registry.registry_digest
    finally:
        server.close()
        config.socket_path.parent.rmdir()


def test_startup_self_test_runs_handler_in_scrubbed_bounded_child(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLERKSAN_TEST_PRIVATE_VALUE", "must-not-reach-parser")
    registry = ParserAdapterRegistry()

    def requires_scrubbed_environment(source, context, limits):
        if "CLERKSAN_TEST_PRIVATE_VALUE" in os.environ:
            raise ValueError("private environment reached startup parser")
        return NormalizedDocument(
            markdown_body="safe fixture",
            metadata=DocMetadata(
                filename=source.filename,
                detected_type=FileType.TXT,
                sha256=source.source_sha256,
            ),
        )

    registry.register(
        adapter_key="txt",
        format_key="txt",
        run=requires_scrubbed_environment,
        preflight=lambda source, detected, limits: {"safe": True},
        startup_fixture=lambda: b"safe bounded fixture",
    )
    config = ParserServiceConfig(socket_path=_short_socket_path(tmp_path))
    server = ParserSidecarServer(
        config,
        registry,
        runtime_evidence=_all_sandbox_evidence(),
    )
    try:
        assert server.enabled_formats == ("txt",)
        assert server.startup_self_tests[0].passed is True
        assert len(server.startup_self_tests[0].evidence_digest or "") == 64
    finally:
        server.close()
        config.socket_path.parent.rmdir()


def test_child_timeout_is_killed_reaped_and_environment_is_scrubbed(tmp_path):
    config = ParserServiceConfig(socket_path=tmp_path / "parser.sock")
    timeout = _execute_child(
        lambda: (time.sleep(1), {"late": True})[1],
        config,
        wall_timeout_seconds=0.03,
    )
    assert timeout.reason == "parser_timeout"
    assert timeout.reaped is True

    environment = _execute_child(
        lambda: {"environment": sorted(os.environ)},
        config,
    )
    assert environment.reason is None
    assert environment.payload is not None
    assert environment.payload["environment"] == sorted(
        ["HOME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"]
    )


def test_child_output_limit_is_enforced_and_reaped(tmp_path):
    config = ParserServiceConfig(socket_path=tmp_path / "parser.sock", max_child_output_bytes=128)
    outcome = _execute_child(lambda: {"content": "x" * 1000}, config)
    assert outcome.reason == "parser_output_limit"
    assert outcome.reaped is True


def test_server_runs_csv_only_through_bound_fd_and_registry(tmp_path):
    raw = b"name,amount\ncoffee,450\n"
    result = _run_direct_sidecar_scenario("csv", tmp_path)
    assert result["ok"] is True
    assert result["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["normalized"]["tables"][0]["rows"] == [["coffee", "450"]]
    assert result["normalized"]["metadata"]["filename"] == "transactions.csv"


def test_server_rejects_writable_source_descriptor(tmp_path):
    result = _run_direct_sidecar_scenario("writable", tmp_path)
    assert result["ok"] is False
    assert result["reason"] == "invalid_request"
    assert result["nonce"] == _request()["nonce"]


def test_unverified_runtime_refuses_parser_dispatch(tmp_path):
    result = _run_direct_sidecar_scenario("unverified", tmp_path)
    assert result["ok"] is False
    assert result["reason"] == "sandbox_unavailable"


def test_probe_is_verified_only_with_runtime_and_socket_evidence(tmp_path):
    scenario = _run_direct_sidecar_scenario("probe", tmp_path)
    assert scenario["before"]["verified"] is False
    result = scenario["after"]
    assert result["verified"] is True
    assert result["capabilities"] == sorted(item.value for item in FileType)
    assert len(result["registry_digest"]) == 64
    assert len(result["evidence_digest"]) == 64


def test_real_unix_socket_client_probes_and_runs_descriptor_only_csv(tmp_path):
    socket_path = _short_socket_path(tmp_path)
    config = ParserServiceConfig(socket_path=socket_path)
    # PDFium can load platform graphics runtimes that are not fork-safe on macOS.
    # The production parser is a fresh process, so spawn is the faithful test model.
    process = multiprocessing.get_context("spawn").Process(
        target=_serve_test_sidecar, args=(config,)
    )
    process.start()
    source_fd = None
    try:
        deadline = time.monotonic() + 3
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert socket_path.exists()

        backend = SidecarSandboxBackend(str(socket_path), timeout_seconds=3)
        probe = backend.startup_probe()
        assert probe.verified is True
        assert probe.capabilities == tuple(sorted(item.value for item in FileType))
        assert backend.registry_digest is not None

        raw = b"name,amount\ncoffee,450\n"
        source_path = tmp_path / "transactions.csv"
        source_path.write_bytes(raw)
        source_fd = os.open(source_path, os.O_RDONLY)
        source = ReadOnlySource(
            source_fd,
            hashlib.sha256(raw).hexdigest(),
            filename="transactions.csv",
            mime_type="text/csv",
        )
        preflight = ParserRunner(backend).preflight(source, {"format": "csv"})
        assert preflight["evidence"] == {
            "schema_version": 1,
            "safe": True,
            "detected_format": "csv",
            "policy": "bounded-normalizing-preflight-v1",
            "normalized_sha256": preflight["evidence"]["normalized_sha256"],
            "table_count": 1,
            "image_count": 0,
        }
        assert len(preflight["evidence"]["normalized_sha256"]) == 64
        normalized = asyncio.run(
            ParserRunner(backend).run(
                "delimited.csv",
                source,
                AdapterContext(
                    "delimited.csv",
                    registry_digest="c" * 64,
                    metadata={"detected_type": "csv"},
                ),
            )
        )
        assert normalized.tables[0].rows == [["coffee", "450"]]
    finally:
        if source_fd is not None:
            os.close(source_fd)
        process.terminate()
        process.join(timeout=3)
        if process.is_alive():
            process.kill()
            process.join(timeout=3)
        if socket_path.exists():
            socket_path.unlink()
        socket_path.parent.rmdir()


def test_real_unix_socket_transports_sanitized_image_and_complete_pdf_artifact_fds(
    tmp_path,
):
    socket_path = _short_socket_path(tmp_path)
    config = ParserServiceConfig(socket_path=socket_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_serve_test_sidecar, args=(config,)
    )
    process.start()
    source_fds: list[int] = []
    try:
        deadline = time.monotonic() + 3
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert socket_path.exists()
        backend = SidecarSandboxBackend(str(socket_path), timeout_seconds=5)
        assert backend.startup_probe().verified is True
        runner = ParserRunner(backend)

        image_output = io.BytesIO()
        image = Image.new("RGB", (4, 3), "white")
        image.save(image_output, "JPEG")
        image.close()
        image_raw = image_output.getvalue()
        image_path = tmp_path / "fixture.jpg"
        image_path.write_bytes(image_raw)
        image_fd = os.open(image_path, os.O_RDONLY)
        source_fds.append(image_fd)
        image_result = asyncio.run(
            runner.run_with_artifacts(
                "jpeg",
                ReadOnlySource(
                    image_fd,
                    hashlib.sha256(image_raw).hexdigest(),
                    filename="fixture.jpg",
                    mime_type="image/jpeg",
                ),
                AdapterContext("jpeg", metadata={"detected_type": "jpeg"}),
            )
        )
        assert len(image_result.artifacts) == 1
        assert image_result.artifacts[0].descriptor.role is ArtifactRole.SANITIZED_IMAGE
        assert image_result.artifacts[0].data.startswith(PNG_SIGNATURE)
        assert image_result.normalized.metadata.extra["ocr_required_pages"] == [1]

        writer = PdfWriter()
        first_page = writer.add_blank_page(width=320, height=72)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        first_page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 10 Tf 5 32 Td (safe bounded digital text for page one) Tj ET\n")
        first_page.replace_contents(content)
        writer.add_blank_page(width=72, height=72)
        pdf_output = io.BytesIO()
        writer.write(pdf_output)
        pdf_raw = pdf_output.getvalue()
        pdf_path = tmp_path / "fixture.pdf"
        pdf_path.write_bytes(pdf_raw)
        pdf_fd = os.open(pdf_path, os.O_RDONLY)
        source_fds.append(pdf_fd)
        pdf_result = asyncio.run(
            runner.run_with_artifacts(
                "pdf",
                ReadOnlySource(
                    pdf_fd,
                    hashlib.sha256(pdf_raw).hexdigest(),
                    filename="fixture.pdf",
                    mime_type="application/pdf",
                ),
                AdapterContext("pdf", metadata={"detected_type": "pdf"}),
            )
        )
        assert [artifact.descriptor.role for artifact in pdf_result.artifacts] == [
            ArtifactRole.PDF_PAGE,
            ArtifactRole.PDF_PAGE,
            ArtifactRole.PDF_PREVIEW_MANIFEST,
        ]
        assert [artifact.descriptor.page_number for artifact in pdf_result.artifacts[:-1]] == [1, 2]
        assert pdf_result.normalized.metadata.extra["preview_status"] == "ready"
    finally:
        for descriptor in source_fds:
            os.close(descriptor)
        process.terminate()
        process.join(timeout=3)
        if process.is_alive():
            process.kill()
            process.join(timeout=3)
        if socket_path.exists():
            socket_path.unlink()
        socket_path.parent.rmdir()
