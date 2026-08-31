from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from ui_api_client import ApiConflict, ApiError, ClerksanClient, LocalServiceUnavailable


class _Response:
    def __init__(self, payload: object) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._data


def test_probe_and_document_list_use_loopback_http(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def fake_open(request: object, timeout: float) -> _Response:
        requests.append((request, timeout))
        url = request.full_url  # type: ignore[attr-defined]
        if url.endswith("/health"):
            return _Response({"status": "ok"})
        return _Response({"items": [{"id": "receipt-1"}]})

    monkeypatch.setattr("ui_api_client.urlopen", fake_open)
    client = ClerksanClient()

    assert client.probe() is True
    assert client.list_documents(counterparty="ACME") == [{"id": "receipt-1"}]
    assert [request.full_url for request, _ in requests] == [
        "http://127.0.0.1:8000/health",
        "http://127.0.0.1:8000/documents?counterparty=ACME",
    ]
    assert client.original_url("receipt-1") == "http://127.0.0.1:8000/documents/receipt-1/original"
    assert client.original_url(
        "receipt-1",
        source_file_id="source-1",
        source_version=2,
        sha256="a" * 64,
    ) == (
        "http://127.0.0.1:8000/documents/receipt-1/original"
        f"?source_file_id=source-1&version=2&sha256={'a' * 64}"
    )


def test_readiness_uses_the_local_readiness_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def fake_open(request: object, timeout: float) -> _Response:
        del timeout
        requests.append(request)
        return _Response({"status": "ready", "demo_mode": False})

    monkeypatch.setattr("ui_api_client.urlopen", fake_open)

    assert ClerksanClient().readiness() == {"status": "ready", "demo_mode": False}
    assert [request.full_url for request in requests] == ["http://127.0.0.1:8000/ready"]


def test_readiness_accepts_additive_delayed_processing_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "status": "ready",
        "demo_mode": False,
        "intake_ready": True,
        "review_ready": True,
        "processing_ready": False,
        "universal_processing_ready": False,
        "processing_reason_codes": ["model_unavailable"],
        "registry_digest": "api-registry",
        "capabilities_digest": "api-capabilities",
        "worker_registry_digest": None,
        "worker_capabilities_digest": None,
        "worker_capability_lease_age_seconds": None,
    }

    monkeypatch.setattr("ui_api_client.urlopen", lambda *_args, **_kwargs: _Response(payload))

    assert ClerksanClient().readiness() == payload


def test_readiness_surfaces_local_not_ready_details(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_ready(_: object, timeout: float) -> _Response:
        del timeout
        raise HTTPError(
            "http://127.0.0.1:8000/ready",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(
                b'{"code":"not_ready","message":"missing model",'
                b'"detail":{"errors":["missing model: ollama pull router-test:3b"]}}'
            ),
        )

    monkeypatch.setattr("ui_api_client.urlopen", not_ready)

    with pytest.raises(ApiError) as raised:
        ClerksanClient().readiness()

    assert raised.value.status_code == 503
    assert raised.value.code == "not_ready"
    assert raised.value.detail == {"errors": ["missing model: ollama pull router-test:3b"]}


def test_readiness_keeps_legacy_intake_available_during_model_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = {
        "errors": ["missing model: ollama pull router-test:3b"],
        "intake_ready": True,
        "review_ready": True,
        "processing_ready": False,
        "universal_processing_ready": False,
        "processing_reason_codes": ["model_unavailable"],
        "registry_digest": "api-registry",
        "capabilities_digest": "api-capabilities",
    }

    def not_ready(_: object, timeout: float) -> _Response:
        del timeout
        body = json.dumps(
            {"code": "not_ready", "message": "missing model", "detail": detail}
        ).encode()
        raise HTTPError(
            "http://127.0.0.1:8000/ready",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr("ui_api_client.urlopen", not_ready)

    assert ClerksanClient().readiness() == {"status": "not_ready", **detail}


def test_upload_is_multipart_and_never_has_a_cloud_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_open(request: object, timeout: float) -> _Response:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["data"] = request.data  # type: ignore[attr-defined]
        captured["content_type"] = request.get_header("Content-type")  # type: ignore[attr-defined]
        captured["idempotency_key"] = request.get_header("Idempotency-key")  # type: ignore[attr-defined]
        return _Response({"document_id": "d-1", "status": "uploaded"})

    monkeypatch.setattr("ui_api_client.urlopen", fake_open)
    result = ClerksanClient().upload('safe"name.png', b"png-bytes")

    assert result["document_id"] == "d-1"
    assert captured["url"] == "http://127.0.0.1:8000/documents"
    assert b"png-bytes" in captured["data"]  # type: ignore[operator]
    assert b'filename="safe\'name.png"' in captured["data"]  # type: ignore[operator]
    assert str(captured["content_type"]).startswith("multipart/form-data; boundary=")
    assert captured.get("idempotency_key") is None


def test_conflict_and_connection_failure_are_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    def conflict(_: object, timeout: float) -> _Response:
        raise HTTPError(
            "http://127.0.0.1:8000/review/approve",
            409,
            "Conflict",
            {},
            io.BytesIO(b'{"code":"stale_extraction","message":"Reload this item"}'),
        )

    monkeypatch.setattr("ui_api_client.urlopen", conflict)
    with pytest.raises(ApiConflict, match="Reload this item"):
        ClerksanClient().approve("e-1", 1, {}, "reviewer")

    def unavailable(_: object, timeout: float) -> _Response:
        raise URLError("connection refused")

    monkeypatch.setattr("ui_api_client.urlopen", unavailable)
    with pytest.raises(LocalServiceUnavailable, match="docker compose --profile app up -d"):
        ClerksanClient().status("d-1")


def test_reprocess_uses_the_document_lifecycle_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def fake_open(request: object, timeout: float) -> _Response:
        del timeout
        requests.append(request)
        return _Response(
            {
                "document_id": "document-1",
                "original_version": 1,
                "status": "queued",
                "job_id": "job-1",
            }
        )

    monkeypatch.setattr("ui_api_client.urlopen", fake_open)

    result = ClerksanClient().reprocess("document-1", actor="reviewer one")

    assert result["status"] == "queued"
    assert [(request.get_method(), request.full_url) for request in requests] == [
        ("POST", "http://127.0.0.1:8000/documents/document-1/reprocess?actor=reviewer+one")
    ]


def test_derivative_retry_uses_the_source_bound_recovery_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def fake_open(request: object, timeout: float) -> _Response:
        del timeout
        requests.append(request)
        return _Response(
            {
                "document_id": "document-1",
                "original_version": 1,
                "status": "queued",
                "job_ids": ["job-1"],
            }
        )

    monkeypatch.setattr("ui_api_client.urlopen", fake_open)

    result = ClerksanClient().retry_derivatives("document-1")

    assert result["job_ids"] == ["job-1"]
    assert [(request.get_method(), request.full_url) for request in requests] == [
        ("POST", "http://127.0.0.1:8000/documents/document-1/retry-derivatives")
    ]


def test_recurring_bill_client_uses_the_live_api_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []
    payloads = iter(
        [
            {"upcoming": [{"id": "bill-1"}], "overdue": []},
            {"issuer_id": "issuer-1", "comparisons": [], "anomalies": []},
            {"status": "paid", "bill_id": "bill-1", "paid_at": "2026-07-14T10:00:00Z"},
        ]
    )

    def fake_open(request: object, timeout: float) -> _Response:
        requests.append(request)
        return _Response(next(payloads))

    monkeypatch.setattr("ui_api_client.urlopen", fake_open)
    client = ClerksanClient()

    assert client.bill_reminders(days_ahead=21) == {"upcoming": [{"id": "bill-1"}], "overdue": []}
    assert client.bill_analysis("issuer-1", months=18, anomaly_window=12)["issuer_id"] == "issuer-1"
    assert client.mark_bill_paid("bill-1", actor="reviewer one")["status"] == "paid"
    assert [(request.get_method(), request.full_url) for request in requests] == [
        ("GET", "http://127.0.0.1:8000/bills/reminders?days_ahead=21"),
        (
            "GET",
            "http://127.0.0.1:8000/bills/issuer-1/analysis?months=18&anomaly_window=12",
        ),
        ("POST", "http://127.0.0.1:8000/bills/bill-1/mark-paid?actor=reviewer+one"),
    ]
