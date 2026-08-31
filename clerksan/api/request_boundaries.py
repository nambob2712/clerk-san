"""ASGI request guards that run before FastAPI parses bodies or routes requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.message import Message as EmailMessage
from email.parser import BytesHeaderParser
from typing import Any

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from clerksan.config import Settings

_MAX_MULTIPART_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class RequestBoundaryError(ValueError):
    """One stable request rejection produced before application parsing."""

    status_code: int
    code: str
    message: str
    detail: dict[str, Any] | None = None


class ExactHostMiddleware:
    """Enforce one exact Host value without depending on runtime configuration."""

    def __init__(self, app: ASGIApp, expected_host: str) -> None:
        self.app = app
        self.expected_host = expected_host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            _validate_host(scope, self.expected_host)
        except RequestBoundaryError as error:
            await _send_boundary_error(error, scope, receive, send)
            return
        await self.app(scope, receive, send)


class RequestBoundaryMiddleware:
    """Validate the exact host and bounded body shape before framework allocation."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self._upload_slots = asyncio.Semaphore(settings.upload_concurrency)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            _validate_host(scope, self.settings.api_host)
            content_length = _content_length(scope)
            if content_length is not None and content_length > self.settings.max_request_bytes:
                raise RequestBoundaryError(
                    413,
                    "request_too_large",
                    "Request body exceeds the configured byte limit.",
                    {
                        "limit": self.settings.max_request_bytes,
                        "observed": content_length,
                    },
                )

            content_type = _header(scope, b"content-type") or ""
            media_type = content_type.split(";", 1)[0].strip().lower()
            guarded_upload = _is_upload_request(scope, media_type)
            acquired = False
            if guarded_upload:
                if self._upload_slots.locked():
                    raise RequestBoundaryError(
                        429,
                        "upload_capacity_exceeded",
                        "The local upload capacity is busy. Retry shortly.",
                        {"limit": self.settings.upload_concurrency},
                    )
                await self._upload_slots.acquire()
                acquired = True

            try:
                is_json = _is_json_media_type(media_type)
                if is_json or media_type == "multipart/form-data":
                    receive_deadline = (
                        asyncio.get_running_loop().time()
                        + self.settings.request_receive_timeout_seconds
                    )
                    body = await _read_body(
                        receive,
                        self.settings.max_request_bytes,
                        receive_deadline,
                        self.settings.request_receive_timeout_seconds,
                    )
                    if is_json:
                        _validate_json_body(body, self.settings)
                    else:
                        _validate_multipart_body(body, content_type, self.settings)
                    await self.app(scope, _replay_body(body), send)
                else:
                    await self.app(
                        scope,
                        _bounded_receive(
                            receive,
                            self.settings.max_request_bytes,
                            asyncio.get_running_loop().time()
                            + self.settings.request_receive_timeout_seconds,
                            self.settings.request_receive_timeout_seconds,
                        ),
                        send,
                    )
            finally:
                if acquired:
                    self._upload_slots.release()
        except RequestBoundaryError as error:
            await _send_boundary_error(error, scope, receive, send)


async def _send_boundary_error(
    error: RequestBoundaryError,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    response = JSONResponse(
        status_code=error.status_code,
        content={
            "code": error.code,
            "message": error.message,
            "detail": error.detail,
        },
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
    await response(scope, receive, send)


def _validate_host(scope: Scope, expected_host: str) -> None:
    host_values = [
        value.decode("latin-1").strip().lower()
        for name, value in scope.get("headers", [])
        if name.lower() == b"host"
    ]
    if len(host_values) != 1 or host_values[0] != expected_host.lower():
        raise RequestBoundaryError(
            400,
            "invalid_host",
            "Request Host does not match the configured local API.",
        )


def _content_length(scope: Scope) -> int | None:
    value = _header(scope, b"content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise RequestBoundaryError(
            422,
            "malformed_content",
            "Content-Length must be a non-negative integer.",
        ) from error
    if parsed < 0:
        raise RequestBoundaryError(
            422,
            "malformed_content",
            "Content-Length must be a non-negative integer.",
        )
    return parsed


def _header(scope: Scope, wanted: bytes) -> str | None:
    matches = [
        value.decode("latin-1")
        for name, value in scope.get("headers", [])
        if name.lower() == wanted
    ]
    if len(matches) > 1:
        raise RequestBoundaryError(
            422,
            "malformed_content",
            f"Duplicate {wanted.decode('ascii')} headers are not accepted.",
        )
    return matches[0] if matches else None


def _is_json_media_type(media_type: str) -> bool:
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def _is_upload_request(scope: Scope, media_type: str) -> bool:
    if scope.get("method") != "POST" or media_type != "multipart/form-data":
        return False
    path = str(scope.get("path", ""))
    if path == "/documents":
        return True
    parts = path.strip("/").split("/")
    return len(parts) == 3 and parts[0] == "documents" and parts[2] == "original"


async def _receive_before_deadline(
    receive: Receive,
    deadline: float,
    configured_timeout_seconds: float,
) -> Message:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise RequestBoundaryError(
            408,
            "request_receive_timeout",
            "Request body was not received within the configured time limit.",
            {"limit_seconds": configured_timeout_seconds},
        )
    try:
        return await asyncio.wait_for(receive(), timeout=remaining)
    except TimeoutError as error:
        raise RequestBoundaryError(
            408,
            "request_receive_timeout",
            "Request body was not received within the configured time limit.",
            {"limit_seconds": configured_timeout_seconds},
        ) from error


async def _read_body(
    receive: Receive,
    limit: int,
    deadline: float,
    configured_timeout_seconds: float,
) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    while True:
        message = await _receive_before_deadline(
            receive,
            deadline,
            configured_timeout_seconds,
        )
        if message["type"] == "http.disconnect":
            raise RequestBoundaryError(
                422,
                "malformed_content",
                "Request body disconnected before completion.",
            )
        if message["type"] != "http.request":
            continue
        chunk = message.get("body", b"")
        observed += len(chunk)
        if observed > limit:
            raise RequestBoundaryError(
                413,
                "request_too_large",
                "Request body exceeds the configured byte limit.",
                {"limit": limit, "observed": observed},
            )
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _bounded_receive(
    receive: Receive,
    limit: int,
    deadline: float,
    configured_timeout_seconds: float,
) -> Receive:
    observed = 0

    async def bounded() -> Message:
        nonlocal observed
        message = await _receive_before_deadline(
            receive,
            deadline,
            configured_timeout_seconds,
        )
        if message["type"] == "http.request":
            observed += len(message.get("body", b""))
            if observed > limit:
                raise RequestBoundaryError(
                    413,
                    "request_too_large",
                    "Request body exceeds the configured byte limit.",
                    {"limit": limit, "observed": observed},
                )
        return message

    return bounded


def _replay_body(body: bytes) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


def _validate_json_body(body: bytes, settings: Settings) -> None:
    if len(body) > settings.max_json_bytes:
        raise RequestBoundaryError(
            413,
            "json_limit_exceeded",
            "JSON body exceeds the configured byte limit.",
            {"limit": settings.max_json_bytes, "observed": len(body)},
        )
    depth = _json_depth(body)
    if depth > settings.max_json_depth:
        raise RequestBoundaryError(
            413,
            "json_limit_exceeded",
            "JSON body exceeds the configured nesting limit.",
            {"limit": settings.max_json_depth, "observed": depth},
        )


def _json_depth(body: bytes) -> int:
    depth = 0
    maximum = 0
    in_string = False
    escaped = False
    for value in body:
        if in_string:
            if escaped:
                escaped = False
            elif value == 0x5C:
                escaped = True
            elif value == 0x22:
                in_string = False
            continue
        if value == 0x22:
            in_string = True
        elif value in (0x7B, 0x5B):
            depth += 1
            maximum = max(maximum, depth)
        elif value in (0x7D, 0x5D):
            depth = max(0, depth - 1)
    return maximum


def _validate_multipart_body(body: bytes, content_type: str, settings: Settings) -> None:
    boundary = _multipart_boundary(content_type)
    delimiter = b"--" + boundary
    files = 0
    fields = 0
    cursor = 0
    saw_final = False

    while True:
        marker = body.find(delimiter, cursor)
        if marker < 0:
            break
        cursor = marker + len(delimiter)
        if body[cursor : cursor + 2] == b"--":
            saw_final = True
            break
        if body[cursor : cursor + 2] != b"\r\n":
            raise RequestBoundaryError(422, "malformed_content", "Malformed multipart boundary.")
        header_start = cursor + 2
        header_end = body.find(b"\r\n\r\n", header_start)
        if header_end < 0 or header_end - header_start > _MAX_MULTIPART_HEADER_BYTES:
            raise RequestBoundaryError(
                413,
                "multipart_limit_exceeded",
                "Multipart headers exceed the configured safety limit.",
            )
        headers = BytesHeaderParser().parsebytes(body[header_start:header_end] + b"\r\n\r\n")
        disposition = headers.get("Content-Disposition")
        if disposition is None:
            raise RequestBoundaryError(422, "malformed_content", "Multipart part has no name.")
        parsed = EmailMessage()
        parsed["Content-Disposition"] = disposition
        if parsed.get_param("name", header="Content-Disposition") is None:
            raise RequestBoundaryError(422, "malformed_content", "Multipart part has no name.")
        is_file = parsed.get_param("filename", header="Content-Disposition") is not None
        if not is_file:
            fields += 1
        else:
            files += 1
        if files > settings.max_multipart_files or fields > settings.max_multipart_fields:
            raise RequestBoundaryError(
                413,
                "multipart_limit_exceeded",
                "Multipart body contains too many files or fields.",
                {
                    "max_files": settings.max_multipart_files,
                    "max_fields": settings.max_multipart_fields,
                    "observed_files": files,
                    "observed_fields": fields,
                },
            )
        next_marker = body.find(b"\r\n" + delimiter, header_end + 4)
        if next_marker < 0:
            raise RequestBoundaryError(422, "malformed_content", "Multipart body is incomplete.")
        if is_file:
            file_bytes = next_marker - (header_end + 4)
            if file_bytes == 0:
                raise RequestBoundaryError(422, "empty_file", "Uploaded file is empty.")
            if file_bytes > settings.max_upload_bytes:
                raise RequestBoundaryError(
                    413,
                    "upload_too_large",
                    "Uploaded file exceeds the configured byte limit.",
                    {"limit": settings.max_upload_bytes, "observed": file_bytes},
                )
        cursor = header_end + 4

    if not saw_final:
        raise RequestBoundaryError(422, "malformed_content", "Multipart body is incomplete.")


def _multipart_boundary(content_type: str) -> bytes:
    message = EmailMessage()
    message["Content-Type"] = content_type
    boundary = message.get_param("boundary", header="Content-Type")
    if not isinstance(boundary, str) or not boundary or len(boundary) > 200:
        raise RequestBoundaryError(422, "malformed_content", "Multipart boundary is invalid.")
    try:
        rendered = boundary.encode("ascii")
    except UnicodeEncodeError as error:
        raise RequestBoundaryError(
            422, "malformed_content", "Multipart boundary is invalid."
        ) from error
    if b"\r" in rendered or b"\n" in rendered:
        raise RequestBoundaryError(422, "malformed_content", "Multipart boundary is invalid.")
    return rendered
