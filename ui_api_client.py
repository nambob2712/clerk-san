"""Synchronous, local-only HTTP boundary used by the Streamlit interface.

The UI deliberately depends on the standard library here.  Keeping its only network
boundary small makes it easy to prove that the normal path talks solely to the local
API and that an unavailable API cannot silently fall through to a cloud provider.
"""

from __future__ import annotations

import json
import mimetypes
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_API_URL = "http://127.0.0.1:8000"


class LocalServiceUnavailable(RuntimeError):
    """Raised when the loopback service cannot be reached."""

    startup_instructions = (
        "Local service unavailable. Start it with: docker compose --profile app up -d"
    )


@dataclass(slots=True)
class ApiError(RuntimeError):
    """A structured non-success response from the local API."""

    status_code: int
    code: str
    message: str
    detail: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


class ApiConflict(ApiError):
    """A review item was changed after the user loaded it."""


class ClerksanClient:
    """Small Streamlit-friendly client for the documented local API."""

    def __init__(self, base_url: str = DEFAULT_API_URL, *, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def probe(self) -> bool:
        """Return whether the local API responds to its liveness endpoint."""
        try:
            response = self._request("GET", "/health")
        except LocalServiceUnavailable:
            return False
        return response.get("status") == "ok"

    def readiness(self) -> dict[str, Any]:
        """Require legacy intake readiness without treating delayed processing as downtime."""

        try:
            response = self._request("GET", "/ready")
        except ApiError as error:
            if (
                error.code == "not_ready"
                and isinstance(error.detail, dict)
                and error.detail.get("intake_ready") is True
            ):
                return {"status": "not_ready", **error.detail}
            raise
        if not isinstance(response, dict) or (
            response.get("status") != "ready" and response.get("intake_ready") is not True
        ):
            raise ApiError(502, "invalid_response", "Local API returned invalid readiness data")
        return response

    def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        """Submit one raw file without sending it anywhere except the configured API."""
        boundary = f"----clerksan-{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        escaped_name = filename.replace('"', "'").replace("\r", "").replace("\n", "")
        body = b"".join(
            (
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{escaped_name}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                data,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            )
        )
        return self._request(
            "POST",
            "/documents",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def status(self, document_id: str) -> dict[str, Any]:
        return self._request("GET", f"/documents/{document_id}/status")

    def original_url(
        self,
        document_id: str,
        *,
        source_file_id: str | None = None,
        source_version: int | None = None,
        sha256: str | None = None,
    ) -> str:
        """Return an exact-source loopback URL for an immutable original."""

        query = {
            "source_file_id": source_file_id,
            "version": source_version,
            "sha256": sha256,
        }
        encoded = urlencode({key: value for key, value in query.items() if value is not None})
        suffix = f"?{encoded}" if encoded else ""
        return f"{self.base_url}/documents/{document_id}/original{suffix}"

    def list_documents(self, **filters: Any) -> list[dict[str, Any]]:
        query = {key: value for key, value in filters.items() if value not in (None, "")}
        suffix = f"?{urlencode(query)}" if query else ""
        response = self._request("GET", f"/documents{suffix}")
        items = response.get("items", [])
        if not isinstance(items, list):
            raise ApiError(502, "invalid_response", "Local API returned invalid document data")
        return items

    def review_pending(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/review")
        if not isinstance(response, list):
            raise ApiError(502, "invalid_response", "Local API returned invalid review data")
        return response

    def approve(
        self,
        extraction_id: str,
        expected_version: int,
        corrections: dict[str, Any],
        reviewer: str,
    ) -> dict[str, Any]:
        """Approve exactly one displayed extraction version."""
        return self._request(
            "POST",
            "/review/approve",
            json_body={
                "extraction_id": extraction_id,
                "expected_version": expected_version,
                "corrections": corrections,
                "reviewer": reviewer,
            },
        )

    def reject(self, extraction_id: str, reason: str, reviewer: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/review/reject",
            json_body={"extraction_id": extraction_id, "reason": reason, "reviewer": reviewer},
        )

    def reprocess(self, document_id: str, *, actor: str) -> dict[str, Any]:
        """Queue the current immutable source after a rejection or verified recheck."""

        response = self._request(
            "POST",
            f"/documents/{document_id}/reprocess?{urlencode({'actor': actor})}",
        )
        if not isinstance(response, dict):
            raise ApiError(502, "invalid_response", "Local API returned invalid reprocess data")
        return response

    def retry_derivatives(self, document_id: str) -> dict[str, Any]:
        """Retry terminal local indexing/OCR stages without changing the review item."""

        response = self._request("POST", f"/documents/{document_id}/retry-derivatives")
        if not isinstance(response, dict):
            raise ApiError(
                502, "invalid_response", "Local API returned invalid derivative retry data"
            )
        return response

    def ask(self, question: str) -> dict[str, Any]:
        return self._request("POST", "/query", json_body={"question": question})

    def list_bills(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/bills")
        if not isinstance(response, list):
            raise ApiError(502, "invalid_response", "Local API returned invalid bill data")
        return response

    def bill_reminders(self, *, days_ahead: int | None = None) -> dict[str, list[dict[str, Any]]]:
        """Return upcoming and overdue local bill reminders."""

        suffix = f"?{urlencode({'days_ahead': days_ahead})}" if days_ahead is not None else ""
        response = self._request("GET", f"/bills/reminders{suffix}")
        if not isinstance(response, dict):
            raise ApiError(502, "invalid_response", "Local API returned invalid bill reminders")

        reminders: dict[str, list[dict[str, Any]]] = {}
        for bucket in ("upcoming", "overdue"):
            items = response.get(bucket, [])
            if not isinstance(items, list):
                raise ApiError(502, "invalid_response", "Local API returned invalid bill reminders")
            reminders[bucket] = items
        return reminders

    def bill_analysis(
        self,
        issuer_id: str,
        *,
        months: int = 13,
        anomaly_window: int = 12,
    ) -> dict[str, Any]:
        """Return explainable comparisons and anomaly signals for one issuer."""

        query = urlencode({"months": months, "anomaly_window": anomaly_window})
        response = self._request("GET", f"/bills/{issuer_id}/analysis?{query}")
        if not isinstance(response, dict):
            raise ApiError(502, "invalid_response", "Local API returned invalid bill analysis")
        return response

    def mark_bill_paid(self, bill_id: str, *, actor: str) -> dict[str, Any]:
        """Record an auditable payment transition for one recurring bill."""

        response = self._request(
            "POST",
            f"/bills/{bill_id}/mark-paid?{urlencode({'actor': actor})}",
        )
        if not isinstance(response, dict):
            raise ApiError(502, "invalid_response", "Local API returned invalid payment data")
        return response

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=body, method=method, headers=request_headers
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                raw = response.read()
        except HTTPError as error:
            payload = _read_error_payload(error)
            exception_type = ApiConflict if error.code == 409 else ApiError
            raise exception_type(
                error.code,
                str(payload.get("code", "http_error")),
                str(payload.get("message", f"Local API returned HTTP {error.code}")),
                payload.get("detail") if isinstance(payload.get("detail"), dict) else None,
            ) from error
        except (TimeoutError, URLError, OSError) as error:
            raise LocalServiceUnavailable(LocalServiceUnavailable.startup_instructions) from error

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(502, "invalid_response", "Local API returned invalid JSON") from error


def _read_error_payload(error: HTTPError) -> dict[str, Any]:
    try:
        decoded = error.read().decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}
