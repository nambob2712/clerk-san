from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from clerksan.api.main import _create_config_error_app
from clerksan.api.request_boundaries import RequestBoundaryMiddleware
from clerksan.config import SandboxUnavailable, Settings


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'request-boundary.sqlite'}",
        "storage_dir": tmp_path / "storage",
        "api_url": "http://127.0.0.1:8000",
        "demo_mode": True,
    }
    values.update(overrides)
    return Settings(**values)


def _guarded_app(settings: Settings) -> tuple[FastAPI, dict[str, int]]:
    app = FastAPI()
    calls = {"count": 0}

    @app.api_route("/probe", methods=["GET", "POST"])
    async def probe() -> dict[str, str]:
        calls["count"] += 1
        return {"status": "reached"}

    app.add_middleware(RequestBoundaryMiddleware, settings=settings)
    return app, calls


def test_exact_configured_host_is_checked_before_get_and_post_routes(tmp_path: Path) -> None:
    app, calls = _guarded_app(_settings(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/probe").status_code == 200
        rejected_get = client.get("/probe", headers={"Host": "localhost:8000"})
        rejected_head = client.head("/probe", headers={"Host": "localhost:8000"})
        rejected_post = client.post(
            "/probe",
            headers={"Host": "attacker.test", "Content-Type": "application/json"},
            content=b"{}",
        )

    assert rejected_get.status_code == rejected_head.status_code == rejected_post.status_code == 400
    assert rejected_get.json()["code"] == "invalid_host"
    assert rejected_get.headers["cache-control"] == "no-store"
    assert rejected_get.headers["x-content-type-options"] == "nosniff"
    assert calls["count"] == 1


def test_config_error_fallback_enforces_host_and_redacts_validation_input(
    tmp_path: Path,
) -> None:
    private_input = "http://operator:private-value@127.0.0.1:8000"
    with pytest.raises(ValueError) as captured:
        _settings(tmp_path, api_url=private_input)

    app = _create_config_error_app(captured.value)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        ready = client.get("/ready")
        hostile_ready = client.get("/ready", headers={"Host": "attacker.invalid"})
        hostile_health = client.get("/health", headers={"Host": "localhost:8000"})

    assert ready.status_code == 503
    assert ready.json() == {
        "code": "not_ready",
        "message": "Runtime configuration is unavailable.",
        "detail": {
            "reason_code": "configuration_unavailable",
            "retryable": False,
        },
    }
    assert private_input not in ready.text
    assert str(tmp_path) not in ready.text
    assert ready.headers["cache-control"] == "no-store"
    assert hostile_ready.status_code == hostile_health.status_code == 400
    assert hostile_ready.json()["code"] == hostile_health.json()["code"] == "invalid_host"


def test_sandbox_error_fallback_preserves_stable_typed_reason() -> None:
    app = _create_config_error_app(SandboxUnavailable())

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "code": "sandbox_unavailable",
        "message": "sandbox_unavailable",
        "detail": {
            "reason_code": "sandbox_unavailable",
            "retryable": True,
        },
    }


@pytest.mark.parametrize("timeout", (0, -1, float("nan"), float("inf")))
def test_receive_timeout_must_be_finite_and_positive(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        _settings(tmp_path, request_receive_timeout_seconds=timeout)


def test_json_byte_and_depth_limits_run_before_the_route(tmp_path: Path) -> None:
    app, calls = _guarded_app(
        _settings(tmp_path, max_json_bytes=8, max_json_depth=1, max_request_bytes=64)
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        too_deep = client.post("/probe", json={"outer": {"inner": 1}})
        too_large = client.post(
            "/probe",
            headers={"Content-Type": "application/json; charset=utf-8"},
            content=b'{"value":123}',
        )

    assert too_deep.status_code == too_large.status_code == 413
    assert too_deep.json()["code"] == too_large.json()["code"] == "json_limit_exceeded"
    assert calls["count"] == 0


@pytest.mark.parametrize(
    ("content_type", "content", "json_overrides", "expected_message"),
    (
        (
            "application/problem+json; charset=utf-8",
            b'{"value":123}',
            {"max_json_bytes": 8, "max_json_depth": 32},
            "JSON body exceeds the configured byte limit.",
        ),
        (
            'application/vnd.api+json; profile="https://example.invalid/schema"',
            b'{"outer":{"inner":1}}',
            {"max_json_bytes": 128, "max_json_depth": 1},
            "JSON body exceeds the configured nesting limit.",
        ),
    ),
)
def test_structured_suffix_json_uses_byte_and_depth_limits(
    tmp_path: Path,
    content_type: str,
    content: bytes,
    json_overrides: dict[str, int],
    expected_message: str,
) -> None:
    app, calls = _guarded_app(_settings(tmp_path, max_request_bytes=256, **json_overrides))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/probe",
            headers={"Content-Type": content_type},
            content=content,
        )

    assert response.status_code == 413
    assert response.json()["code"] == "json_limit_exceeded"
    assert response.json()["message"] == expected_message
    assert calls["count"] == 0


def test_multipart_file_and_field_counts_run_before_the_route(tmp_path: Path) -> None:
    app, calls = _guarded_app(
        _settings(
            tmp_path,
            max_multipart_files=1,
            max_multipart_fields=1,
            max_request_bytes=4096,
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/probe",
            files=[
                ("first", ("first.txt", b"one", "text/plain")),
                ("second", ("second.txt", b"two", "text/plain")),
            ],
        )

    assert response.status_code == 413
    assert response.json()["code"] == "multipart_limit_exceeded"
    assert calls["count"] == 0


@pytest.mark.parametrize(
    ("content", "expected_code", "expected_status"),
    ((b"", "empty_file", 422), (b"too-large", "upload_too_large", 413)),
)
def test_multipart_file_size_runs_before_the_route(
    tmp_path: Path,
    content: bytes,
    expected_code: str,
    expected_status: int,
) -> None:
    app, calls = _guarded_app(_settings(tmp_path, max_upload_bytes=4, max_request_bytes=4096))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/probe",
            files={"file": ("sample.txt", content, "text/plain")},
        )

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_chunked_body_limit_rejects_before_the_asgi_app() -> None:
    settings = _settings(
        Path("/tmp"),
        max_request_bytes=5,
        max_json_bytes=5,
    )
    called = False
    sent: list[Message] = []
    incoming = iter(
        (
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        )
    )

    async def receive() -> Message:
        return next(incoming)

    async def send(message: Message) -> None:
        sent.append(message)

    async def downstream(scope: Scope, guarded_receive: Receive, downstream_send: Send) -> None:
        nonlocal called
        del scope, downstream_send
        called = True
        while (await guarded_receive()).get("more_body", False):
            pass

    middleware = RequestBoundaryMiddleware(downstream, settings)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/probe",
        "raw_path": b"/probe",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8000")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    await middleware(scope, receive, send)

    assert called is True
    response_start = next(message for message in sent if message["type"] == "http.response.start")
    assert response_start["status"] == 413


@pytest.mark.asyncio
async def test_upload_semaphore_rejects_excess_concurrency(tmp_path: Path) -> None:
    settings = _settings(tmp_path, upload_concurrency=1, max_request_bytes=4096)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    statuses: list[int] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        await receive()
        first_entered.set()
        await release_first.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestBoundaryMiddleware(downstream, settings)
    body, content_type = _multipart_body()

    async def invoke() -> None:
        delivered = False

        async def receive() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        await middleware(_upload_scope(content_type), receive, send)

    first = asyncio.create_task(invoke())
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    await invoke()
    release_first.set()
    await asyncio.wait_for(first, timeout=1)

    assert sorted(statuses) == [204, 429]


@pytest.mark.asyncio
async def test_receive_timeout_releases_upload_slot_without_reaching_the_app(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        upload_concurrency=1,
        max_request_bytes=4096,
        request_receive_timeout_seconds=0.01,
    )
    route_calls = 0
    statuses: list[int] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal route_calls
        del scope
        await receive()
        route_calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestBoundaryMiddleware(downstream, settings)
    body, content_type = _multipart_body()

    async def stalled_receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def complete_receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    await middleware(_upload_scope(content_type), stalled_receive, send)
    await middleware(_upload_scope(content_type), complete_receive, send)

    assert statuses == [408, 204]
    assert route_calls == 1


@pytest.mark.asyncio
async def test_receive_deadline_bounds_the_whole_body_not_each_chunk(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        max_request_bytes=4096,
        request_receive_timeout_seconds=0.03,
    )
    route_calls = 0
    statuses: list[int] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal route_calls
        del scope, receive, send
        route_calls += 1

    middleware = RequestBoundaryMiddleware(downstream, settings)
    body, content_type = _multipart_body()
    midpoint = len(body) // 2
    chunks = iter(((body[:midpoint], True), (body[midpoint:], False)))

    async def slow_receive() -> Message:
        await asyncio.sleep(0.02)
        chunk, more_body = next(chunks)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": more_body,
        }

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    await middleware(_upload_scope(content_type), slow_receive, send)

    assert statuses == [408]
    assert route_calls == 0


def _multipart_body() -> tuple[bytes, str]:
    boundary = "request-boundary-test"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="sample.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "sample\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _upload_scope(content_type: str) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/documents",
        "raw_path": b"/documents",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", content_type.encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
