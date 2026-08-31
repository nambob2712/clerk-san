from __future__ import annotations

import asyncio
import datetime as dt
import io
import os
import subprocess
import sys
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import clerksan.api.main as api_main
from clerksan.api.main import create_app
from clerksan.api.routes import ingest as ingest_routes
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.models import (
    DocumentStatus,
    ExtractedRecord,
    ExtractionStatus,
    IntakeIntent,
    Job,
    JobStatus,
    SourceIntake,
)
from clerksan.db.repositories import (
    DocumentRepo,
    ExtractionRepo,
    WorkerCapabilityLeaseRepo,
)
from clerksan.ingest.capabilities import build_capability_registry
from clerksan.ingest.jobs import enqueue
from clerksan.ingest.parser_runner import SandboxProbeResult
from clerksan.llm.client import OllamaClient


def _png_bytes(color: str = "white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(output, format="PNG")
    return output.getvalue()


def _payload(*, amount: int = 1200) -> dict:
    return {
        "transaction_date": {"value": "2026-07-13", "confidence": 0.99},
        "total_amount": {"value": amount, "confidence": 0.99},
        "counterparty": {"value": "サンプル商店", "confidence": 0.99},
        "currency": {"value": "JPY", "confidence": 0.99},
    }


def test_module_app_import_surfaces_invalid_default_config_without_crashing(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.pop("CLERKSAN_DATABASE_URL", None)
    environment.pop("CLERKSAN_DATABASE_PASSWORD", None)
    repo_root = Path(__file__).resolve().parents[2]
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(repo_root), environment.get("PYTHONPATH")) if value
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from clerksan.api.main import app\n"
                "print([route.path for route in app.routes if hasattr(route, 'path')])"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "/health" in result.stdout
    assert "/ready" in result.stdout
    assert "/documents" not in result.stdout


async def _seed_reviewable_document(settings: Settings) -> tuple[UUID, UUID]:
    async with get_session(settings) as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="review.png",
            content_path="originals/review.png",
            sha256="f" * 64,
            mime="image/png",
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload=_payload(),
            field_confidences={},
            model_name="test-model",
            prompt_version="test",
            actor="worker",
        )
    return document_id, extraction_id


def _ooxml_package_with_extra_member() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types><Override ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml" /></Types>',
        )
        archive.writestr("word/document.xml", "<document />")
        archive.writestr("word/custom/item.xml", "<item />")
    return output.getvalue()


@pytest.fixture
def demo_client(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        yield client


def test_health_and_demo_readiness(demo_client: TestClient) -> None:
    assert demo_client.get("/health").json() == {"status": "ok"}
    readiness = demo_client.get("/ready")
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ready",
        "demo_mode": True,
        "intake_ready": True,
        "review_ready": True,
        "processing_ready": False,
        "universal_processing_ready": False,
        "processing_reason_codes": ["worker_capability_stale"],
        "core_reason_codes": [],
        "model_reason_codes": [],
        "registry_digest": readiness.json()["registry_digest"],
        "capabilities_digest": readiness.json()["capabilities_digest"],
        "worker_registry_digest": None,
        "worker_capabilities_digest": None,
        "worker_capability_lease_age_seconds": None,
        "storage": {
            "ready": True,
            "reason_code": None,
            "path_state": "directory",
            "reference_snapshot_ready": True,
            "reconciliation": {"scanned": 0, "errors": 0},
        },
    }
    capabilities = demo_client.get("/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["process"] == []
    assert capabilities.json()["sandbox_verified"] is False
    assert capabilities.json()["registry_digest"] == readiness.json()["registry_digest"]


def test_readiness_accepts_fresh_matching_worker_evidence_without_enabling_universal(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'worker-ready.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    registry = build_capability_registry(settings)

    async def seed_lease() -> None:
        now = dt.datetime.now(dt.UTC)
        async with get_session(settings) as session:
            await WorkerCapabilityLeaseRepo(session).refresh(
                worker_id="ready-test-worker",
                registry_digest=registry.registry_digest,
                capabilities_digest=registry.capabilities_digest,
                sandbox_verified=False,
                heartbeat_at=now,
                expires_at=now + dt.timedelta(seconds=settings.worker_capability_lease_seconds),
            )

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        asyncio.run(seed_lease())
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["processing_ready"] is True
    assert response.json()["universal_processing_ready"] is False
    assert response.json()["processing_reason_codes"] == []
    assert response.json()["worker_registry_digest"] == registry.registry_digest
    assert response.json()["worker_capabilities_digest"] == registry.capabilities_digest


def test_exact_host_guard_wraps_the_real_api_before_routes(demo_client: TestClient) -> None:
    hostile = {"Host": "localhost:8000"}
    rejected_get = demo_client.get("/health", headers=hostile)
    rejected_head = demo_client.head("/documents", headers=hostile)
    rejected_original = demo_client.get(f"/documents/{uuid4()}/original", headers=hostile)
    rejected_original_head = demo_client.head(f"/documents/{uuid4()}/original", headers=hostile)
    rejected_export = demo_client.get(
        "/export?format=freee&date_from=2026-01-01&date_to=2026-01-31",
        headers=hostile,
    )
    rejected_post = demo_client.post(
        "/documents",
        headers={"Host": "attacker.invalid"},
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )

    assert {
        rejected_get.status_code,
        rejected_head.status_code,
        rejected_original.status_code,
        rejected_original_head.status_code,
        rejected_export.status_code,
        rejected_post.status_code,
    } == {400}
    assert rejected_get.json()["code"] == rejected_post.json()["code"] == "invalid_host"
    assert demo_client.get("/documents").json()["items"] == []


@pytest.mark.parametrize("intake_mode", ("legacy", "universal"))
def test_missing_models_keep_the_safe_core_ready_in_both_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, intake_mode: str
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router-readiness.sqlite'}",
        storage_dir=tmp_path / "storage",
        ocr_model="ocr-test:4b",
        extract_model="extract-test:7b",
        router_model="router-test:3b",
        embed_model="embed-test:v1",
        embed_model_digest="sha256:test",
        embed_dim=2,
        intake_mode=intake_mode,
    )

    class VerifiedParserRunner:
        def startup_probe(self) -> SandboxProbeResult:
            return SandboxProbeResult(
                verified=True,
                backend="test-sidecar",
                evidence_digest="a" * 64,
                capabilities=("csv", "docx", "jpeg", "md", "pdf", "png", "webp", "xlsx"),
            )

    async def fake_list_models(self: OllamaClient) -> list[dict[str, str]]:
        del self
        return [
            {"name": "ocr-test:4b"},
            {"name": "extract-test:7b"},
            {"name": "embed-test:v1", "digest": "sha256:test"},
        ]

    monkeypatch.setattr(OllamaClient, "list_models", fake_list_models)
    parser_runner = VerifiedParserRunner() if intake_mode == "universal" else None
    with TestClient(
        create_app(settings, parser_runner=parser_runner),
        base_url="http://127.0.0.1:8000",
    ) as client:
        if intake_mode == "universal":
            registry = client.app.state.capability_registry

            async def seed_matching_lease() -> None:
                now = dt.datetime.now(dt.UTC)
                async with get_session(settings) as session:
                    await WorkerCapabilityLeaseRepo(session).refresh(
                        worker_id="model-readiness-test-worker",
                        registry_digest=registry.registry_digest,
                        capabilities_digest=registry.capabilities_digest,
                        sandbox_verified=True,
                        heartbeat_at=now,
                        expires_at=now
                        + dt.timedelta(seconds=settings.worker_capability_lease_seconds),
                    )

            asyncio.run(seed_matching_lease())
        response = client.get("/ready")
        upload = (
            client.post(
                "/documents",
                files={"file": ("receipt.png", _png_bytes(), "image/png")},
            )
            if intake_mode == "legacy"
            else None
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["intake_ready"] is True
    assert response.json()["review_ready"] is True
    assert response.json()["processing_ready"] is False
    assert "model_unavailable" in response.json()["processing_reason_codes"]
    assert response.json()["model_reason_codes"] == ["required_model_missing"]
    if upload is not None:
        assert upload.status_code == 202, upload.text
        assert upload.json()["status"] == "uploaded"


def test_readiness_distinguishes_embedding_digest_mismatch_from_missing_tags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'digest-readiness.sqlite'}",
        storage_dir=tmp_path / "storage",
        ocr_model="ocr-test:4b",
        extract_model="extract-test:7b",
        router_model="router-test:3b",
        embed_model="embed-test:v1",
        embed_model_digest="expected-digest",
        embed_dim=2,
        intake_mode="legacy",
    )

    async def fake_list_models(self: OllamaClient) -> list[dict[str, str]]:
        del self
        return [
            {"name": "ocr-test:4b"},
            {"name": "extract-test:7b"},
            {"name": "router-test:3b"},
            {"name": "embed-test:v1", "digest": "different-digest"},
        ]

    monkeypatch.setattr(OllamaClient, "list_models", fake_list_models)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["processing_reason_codes"] == [
        "model_unavailable",
        "worker_capability_stale",
    ]
    assert response.json()["model_reason_codes"] == ["embedding_digest_mismatch"]


def test_readiness_treats_implicit_latest_as_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'implicit-latest.sqlite'}",
        storage_dir=tmp_path / "storage",
        intake_mode="legacy",
        ocr_model="ocr-test",
        extract_model="extract-test",
        router_model="extract-test",
        embed_model="embed-test:v1",
        embed_model_digest="expected-digest",
        embed_dim=2,
    )

    async def fake_list_models(self: OllamaClient) -> list[dict[str, str]]:
        del self
        return [
            {"name": "ocr-test:latest"},
            {"name": "extract-test:latest"},
            {"name": "embed-test:v1", "digest": "expected-digest"},
        ]

    monkeypatch.setattr(OllamaClient, "list_models", fake_list_models)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["model_reason_codes"] == []
    assert "model_unavailable" not in response.json()["processing_reason_codes"]


@pytest.mark.parametrize("unsafe_kind", ("symlink", "file"))
def test_unsafe_storage_is_a_structured_core_outage_without_touching_target(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    storage = tmp_path / "storage"
    target = tmp_path / "owned-target"
    target.mkdir()
    marker = target / "preserve.txt"
    marker.write_text("preserve", encoding="utf-8")
    if unsafe_kind == "symlink":
        storage.symlink_to(target, target_is_directory=True)
        expected_state = "symlink"
    else:
        storage.write_text("not a directory", encoding="utf-8")
        expected_state = "not_directory"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unsafe-storage.sqlite'}",
        storage_dir=storage,
        demo_mode=True,
    )

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        ready = client.get("/ready")
        documents = client.get("/documents")

    assert ready.status_code == 503
    assert ready.json()["code"] == "not_ready"
    evidence = ready.json()["detail"]["storage"]
    assert evidence["ready"] is False
    assert evidence["reason_code"] == "storage_path_unsafe"
    assert evidence["path_state"] == expected_state
    assert documents.status_code == 503
    assert documents.json()["code"] == "storage_unavailable"
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert sorted(path.name for path in target.iterdir()) == ["preserve.txt"]


def test_upload_is_bounded_persisted_and_enqueued(demo_client: TestClient) -> None:
    response = demo_client.post(
        "/documents",
        files={"file": ("receipt.png", _png_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "uploaded"
    document_id = payload["document_id"]

    status = demo_client.get(f"/documents/{document_id}/status")
    assert status.status_code == 200
    assert status.json()["files"][0]["kind"] == "original"

    original = demo_client.get(f"/documents/{document_id}/original")
    assert original.status_code == 200
    assert original.content == _png_bytes()
    assert original.headers["content-type"].startswith("image/png")

    duplicate = demo_client.post(
        "/documents",
        files={"file": ("renamed.pdf", _png_bytes(), "application/pdf")},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate_of"] == document_id


@pytest.mark.parametrize("append_version", [False, True], ids=["create", "append"])
def test_live_upload_storage_lease_spans_idempotency_publish_commit_and_finalization(
    demo_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    append_version: bool,
) -> None:
    if append_version:
        initial = demo_client.post(
            "/documents",
            files={"file": ("first.png", _png_bytes("white"), "image/png")},
        )
        assert initial.status_code == 202
        endpoint = f"/documents/{initial.json()['document_id']}/original"
    else:
        endpoint = "/documents"

    events: list[str] = []
    lock_state = {"held": False}

    @asynccontextmanager
    async def recording_storage_lock(
        storage_dir: Path,
        *,
        shared: bool,
        retry_seconds: float = 0.01,
    ):
        del storage_dir, retry_seconds
        assert shared is True
        assert lock_state["held"] is False
        lock_state["held"] = True
        events.append("lease_acquired")
        try:
            yield
        finally:
            events.append("lease_released")
            lock_state["held"] = False

    original_reserve = ingest_routes.SourceIntakeRepo.reserve_upload_idempotency

    async def recording_reserve(
        repository: ingest_routes.SourceIntakeRepo,
        *args: object,
        **kwargs: object,
    ):
        assert lock_state["held"] is True
        events.append("idempotency_reserved")
        return await original_reserve(repository, *args, **kwargs)

    original_publish = ingest_routes.publish_reserved_blob

    def recording_publish(*args: object, **kwargs: object):
        assert lock_state["held"] is True
        events.append("blob_published")
        return original_publish(*args, **kwargs)

    original_commit = AsyncSession.commit

    async def recording_commit(session: AsyncSession) -> None:
        if session.in_transaction():
            assert lock_state["held"] is True
            events.append("database_committed")
        await original_commit(session)

    original_finalize = ingest_routes._finalize_committed

    def recording_finalize(reservation: object) -> None:
        assert lock_state["held"] is True
        events.append("reservation_finalized")
        original_finalize(reservation)

    monkeypatch.setattr(ingest_routes, "async_storage_lock", recording_storage_lock)
    monkeypatch.setattr(
        ingest_routes.SourceIntakeRepo,
        "reserve_upload_idempotency",
        recording_reserve,
    )
    monkeypatch.setattr(ingest_routes, "publish_reserved_blob", recording_publish)
    monkeypatch.setattr(AsyncSession, "commit", recording_commit)
    monkeypatch.setattr(ingest_routes, "_finalize_committed", recording_finalize)

    response = demo_client.post(
        endpoint,
        headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("next.png", _png_bytes("black"), "image/png")},
    )

    assert response.status_code == 202
    assert events[0] == "lease_acquired"
    assert events[-1] == "lease_released"
    assert events.index("idempotency_reserved") < events.index("blob_published")
    assert events.index("blob_published") < events.index("database_committed")
    assert events.index("database_committed") < events.index("reservation_finalized")


def test_startup_reconciliation_holds_the_exclusive_lease_through_snapshot_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'startup.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    events: list[str] = []
    lock_state = {"held": False}

    @asynccontextmanager
    async def recording_storage_lock(
        storage_dir: Path,
        *,
        shared: bool,
        retry_seconds: float = 0.01,
    ):
        del storage_dir, retry_seconds
        assert shared is False
        lock_state["held"] = True
        events.append("lease_acquired")
        try:
            yield
        finally:
            events.append("lease_released")
            lock_state["held"] = False

    class FakeConnection:
        async def __aenter__(self) -> FakeConnection:
            assert lock_state["held"] is True
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def scalars(self, statement: object) -> list[str]:
            del statement
            assert lock_state["held"] is True
            events.append("references_snapshotted")
            return ["a" * 64]

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

    def recording_reconcile(
        storage_dir: Path,
        grace_period: float,
        reference_checker: object,
        *,
        lock_held: bool,
    ) -> None:
        del storage_dir, grace_period
        assert lock_state["held"] is True
        assert lock_held is True
        assert callable(reference_checker)
        assert reference_checker("a" * 64) is True
        events.append("cleanup_completed")

    monkeypatch.setattr(api_main, "async_storage_lock", recording_storage_lock)
    monkeypatch.setattr(api_main, "get_engine", lambda _settings: FakeEngine())
    monkeypatch.setattr(api_main, "reconcile_reservations", recording_reconcile)

    asyncio.run(api_main._reconcile_storage(settings))

    assert events == [
        "lease_acquired",
        "references_snapshotted",
        "cleanup_completed",
        "lease_released",
    ]


def test_upload_idempotency_replays_exact_result_and_conflicts_without_side_effects(
    demo_client: TestClient,
) -> None:
    key = str(uuid4())
    headers = {"Idempotency-Key": key}
    first = demo_client.post(
        "/documents",
        headers=headers,
        files={"file": ("receipt.png", _png_bytes("white"), "image/png")},
    )
    replay = demo_client.post(
        "/documents",
        headers=headers,
        files={"file": ("receipt.png", _png_bytes("white"), "image/png")},
    )

    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()
    assert first.json()["source_file_id"]
    assert first.json()["source_intake_id"]
    assert first.json()["job_id"]

    conflict = demo_client.post(
        "/documents",
        headers=headers,
        files={"file": ("receipt.png", _png_bytes("black"), "image/png")},
    )
    changed_intent = demo_client.post(
        "/documents",
        headers=headers,
        files={"file": ("renamed.png", _png_bytes("white"), "image/png")},
    )

    assert conflict.status_code == changed_intent.status_code == 409
    assert conflict.json()["code"] == changed_intent.json()["code"] == "idempotency_conflict"
    assert len(demo_client.get("/documents").json()["items"]) == 1
    originals = demo_client.app.state.settings.storage_dir / "originals"
    assert len(list(originals.iterdir())) == 1
    quarantine = demo_client.app.state.settings.storage_dir / ".quarantine"
    assert not any(path.is_file() for path in quarantine.rglob("*"))


def test_explicit_intake_intent_is_persisted_and_binds_upload_idempotency(
    demo_client: TestClient,
) -> None:
    key = str(uuid4())
    headers = {"Idempotency-Key": key}
    content = _png_bytes("white")
    first = demo_client.post(
        "/documents",
        headers=headers,
        data={"intake_intent": "generic_file"},
        files={"file": ("receipt.png", content, "image/png")},
    )
    replay = demo_client.post(
        "/documents",
        headers=headers,
        data={"intake_intent": "generic_file"},
        files={"file": ("receipt.png", content, "image/png")},
    )
    changed_intent = demo_client.post(
        "/documents",
        headers=headers,
        data={"intake_intent": "bill_scan"},
        files={"file": ("receipt.png", content, "image/png")},
    )

    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()
    assert changed_intent.status_code == 409
    assert changed_intent.json()["code"] == "idempotency_conflict"
    assert len(demo_client.get("/documents").json()["items"]) == 1
    assert len(list((demo_client.app.state.settings.storage_dir / "originals").iterdir())) == 1

    async def persisted_evidence() -> tuple[SourceIntake, Job, int, int]:
        async with get_session(demo_client.app.state.settings) as session:
            intake = await session.get(SourceIntake, UUID(first.json()["source_intake_id"]))
            job = await session.get(Job, UUID(first.json()["job_id"]))
            source_count = await session.scalar(select(func.count()).select_from(SourceIntake))
            job_count = await session.scalar(select(func.count()).select_from(Job))
            assert intake is not None
            assert job is not None
            assert source_count is not None
            assert job_count is not None
            return intake, job, source_count, job_count

    intake, job, source_count, job_count = asyncio.run(persisted_evidence())
    assert intake.intake_intent is IntakeIntent.GENERIC_FILE
    assert job.intake_intent is IntakeIntent.GENERIC_FILE
    assert job.payload["intake_intent"] == IntakeIntent.GENERIC_FILE.value
    assert (source_count, job_count) == (1, 1)


def test_omitted_upload_intake_intent_defaults_to_legacy_evidence(
    demo_client: TestClient,
) -> None:
    response = demo_client.post(
        "/documents",
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 202

    async def persisted_evidence() -> tuple[SourceIntake, Job]:
        async with get_session(demo_client.app.state.settings) as session:
            intake = await session.get(SourceIntake, UUID(response.json()["source_intake_id"]))
            job = await session.get(Job, UUID(response.json()["job_id"]))
            assert intake is not None
            assert job is not None
            return intake, job

    intake, job = asyncio.run(persisted_evidence())
    assert intake.intake_intent is IntakeIntent.LEGACY_UNSPECIFIED
    assert job.intake_intent is IntakeIntent.LEGACY_UNSPECIFIED
    assert job.payload["intake_intent"] == IntakeIntent.LEGACY_UNSPECIFIED.value


@pytest.mark.parametrize("value", ["legacy_unspecified", "unknown", " generic_file"])
def test_upload_rejects_non_explicit_intake_intent_values(
    demo_client: TestClient,
    value: str,
) -> None:
    response = demo_client.post(
        "/documents",
        data={"intake_intent": value},
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "malformed_content"
    assert demo_client.get("/documents").json()["items"] == []


def test_omitted_replacement_intent_inherits_predecessor_and_binds_idempotency(
    demo_client: TestClient,
) -> None:
    initial = demo_client.post(
        "/documents",
        data={"intake_intent": "generic_file"},
        files={"file": ("first.png", _png_bytes("white"), "image/png")},
    )
    assert initial.status_code == 202
    document_id = initial.json()["document_id"]
    key = str(uuid4())
    headers = {"Idempotency-Key": key}
    replacement_content = _png_bytes("black")
    replacement = demo_client.post(
        f"/documents/{document_id}/original",
        headers=headers,
        files={"file": ("replacement.png", replacement_content, "image/png")},
    )
    replay = demo_client.post(
        f"/documents/{document_id}/original",
        headers=headers,
        files={"file": ("replacement.png", replacement_content, "image/png")},
    )
    changed_intent = demo_client.post(
        f"/documents/{document_id}/original",
        headers=headers,
        data={"intake_intent": "bill_scan"},
        files={"file": ("replacement.png", replacement_content, "image/png")},
    )

    assert replacement.status_code == replay.status_code == 202
    assert replay.json() == replacement.json()
    assert replacement.json()["version"] == 2
    assert changed_intent.status_code == 409
    assert changed_intent.json()["code"] == "idempotency_conflict"

    async def persisted_evidence() -> tuple[SourceIntake, SourceIntake, Job, int, int]:
        async with get_session(demo_client.app.state.settings) as session:
            first_intake = await session.get(SourceIntake, UUID(initial.json()["source_intake_id"]))
            replacement_intake = await session.get(
                SourceIntake, UUID(replacement.json()["source_intake_id"])
            )
            job = await session.get(Job, UUID(replacement.json()["job_id"]))
            source_count = await session.scalar(select(func.count()).select_from(SourceIntake))
            job_count = await session.scalar(select(func.count()).select_from(Job))
            assert first_intake is not None
            assert replacement_intake is not None
            assert job is not None
            assert source_count is not None
            assert job_count is not None
            return first_intake, replacement_intake, job, source_count, job_count

    first_intake, replacement_intake, job, source_count, job_count = asyncio.run(
        persisted_evidence()
    )
    assert first_intake.intake_intent is IntakeIntent.GENERIC_FILE
    assert replacement_intake.intake_intent is IntakeIntent.GENERIC_FILE
    assert job.intake_intent is IntakeIntent.GENERIC_FILE
    assert job.payload["intake_intent"] == IntakeIntent.GENERIC_FILE.value
    assert (source_count, job_count) == (2, 2)


def test_invalid_upload_idempotency_key_is_typed_and_persists_nothing(
    demo_client: TestClient,
) -> None:
    response = demo_client.post(
        "/documents",
        headers={"Idempotency-Key": "not-a-uuid"},
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "malformed_content"
    assert demo_client.get("/documents").json()["items"] == []


def test_original_download_rejects_a_tampered_artifact(demo_client: TestClient) -> None:
    upload = demo_client.post(
        "/documents",
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 202
    document_id = upload.json()["document_id"]
    status = demo_client.get(f"/documents/{document_id}/status")
    stored_path = Path(status.json()["files"][0]["content_path"])
    assert not stored_path.is_absolute()
    artifact = demo_client.app.state.settings.storage_dir / stored_path
    artifact.write_bytes(b"tampered")

    original = demo_client.get(f"/documents/{document_id}/original")

    assert original.status_code == 409
    assert original.json()["detail"] == "Original artifact checksum mismatch"


def test_content_addressed_upload_does_not_reuse_a_tampered_artifact(
    demo_client: TestClient,
) -> None:
    content = _png_bytes()
    upload = demo_client.post(
        "/documents",
        files={"file": ("receipt.png", content, "image/png")},
    )
    assert upload.status_code == 202
    document_id = upload.json()["document_id"]
    status = demo_client.get(f"/documents/{document_id}/status")
    stored_path = Path(status.json()["files"][0]["content_path"])
    artifact = demo_client.app.state.settings.storage_dir / stored_path
    artifact.write_bytes(b"tampered")

    duplicate = demo_client.post(
        "/documents",
        files={"file": ("renamed.png", content, "image/png")},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "artifact_checksum_mismatch"
    assert artifact.read_bytes() == b"tampered"
    assert len(demo_client.get("/documents").json()["items"]) == 1


def test_replacing_an_original_appends_an_immutable_source_version(
    demo_client: TestClient,
) -> None:
    first = _png_bytes("white")
    replacement_bytes = _png_bytes("black")
    upload = demo_client.post(
        "/documents",
        files={"file": ("first.png", first, "image/png")},
    )
    assert upload.status_code == 202
    document_id = upload.json()["document_id"]

    replacement = demo_client.post(
        f"/documents/{document_id}/original?actor=reviewer",
        files={"file": ("replacement.png", replacement_bytes, "image/png")},
    )

    assert replacement.status_code == 202
    result = replacement.json()
    assert result["version"] == 2
    assert result["status"] == "reprocess_queued"
    assert result["job_id"]

    detail = demo_client.get(f"/documents/{document_id}")
    assert detail.status_code == 200
    originals = [file for file in detail.json()["files"] if file["kind"] == "original"]
    assert [(file["version"], file["source_filename"]) for file in originals] == [
        (1, "first.png"),
        (2, "replacement.png"),
    ]
    assert originals[0]["sha256"] != originals[1]["sha256"]

    current = demo_client.get(f"/documents/{document_id}/original")
    assert current.status_code == 200
    assert current.content == replacement_bytes
    assert "replacement.png" in current.headers["content-disposition"]

    duplicate = demo_client.post(
        f"/documents/{document_id}/original?actor=reviewer",
        files={"file": ("same.png", replacement_bytes, "image/png")},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "raw_source_version_conflict"


def test_original_inspection_can_bind_an_exact_retained_source_version(
    demo_client: TestClient,
) -> None:
    first = _png_bytes("white")
    replacement_bytes = _png_bytes("black")
    upload = demo_client.post(
        "/documents",
        files={"file": ("first.png", first, "image/png")},
    )
    document_id = upload.json()["document_id"]
    replacement = demo_client.post(
        f"/documents/{document_id}/original?actor=reviewer",
        files={"file": ("replacement.png", replacement_bytes, "image/png")},
    )
    assert replacement.status_code == 202

    originals = [
        item
        for item in demo_client.get(f"/documents/{document_id}").json()["files"]
        if item["kind"] == "original"
    ]
    first_original, second_original = originals
    exact = demo_client.get(
        f"/documents/{document_id}/original?version={first_original['version']}"
        f"&source_file_id={first_original['id']}"
    )

    assert exact.status_code == 200
    assert exact.content == first
    assert exact.headers["content-disposition"].startswith("inline")
    assert exact.headers["cache-control"] == "private, no-store"
    assert exact.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'self'; sandbox"
    )
    assert exact.headers["x-content-type-options"] == "nosniff"

    mismatched_identity = demo_client.get(
        f"/documents/{document_id}/original?version={first_original['version']}"
        f"&source_file_id={second_original['id']}"
    )
    assert mismatched_identity.status_code == 404


def test_browser_mutations_require_an_exact_loopback_origin(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'origin.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        rejected = client.post(
            "/documents",
            headers={"Origin": "https://example.test", "Sec-Fetch-Site": "cross-site"},
            files={"file": ("receipt.png", _png_bytes(), "image/png")},
        )
        missing_origin = client.post(
            "/documents",
            headers={"Sec-Fetch-Site": "same-origin"},
            files={"file": ("receipt.png", _png_bytes(), "image/png")},
        )
        accepted = client.post(
            "/documents",
            headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
            files={"file": ("receipt.png", _png_bytes(), "image/png")},
        )

    assert rejected.status_code == missing_origin.status_code == 403
    assert rejected.json()["code"] == "unsafe_browser_origin"
    assert accepted.status_code == 202


def test_static_ui_fallback_never_turns_invalid_api_routes_into_html(tmp_path: Path) -> None:
    static_dir = tmp_path / "web-dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<main>Clerk-san</main>", encoding="utf-8")
    assets = static_dir / "assets"
    assets.mkdir()
    (assets / "app-abc123.js").write_text("console.log('local')", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'static.sqlite'}",
        storage_dir=tmp_path / "storage",
        ui_static_dir=static_dir,
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        app_route = client.get("/review-workspace")
        missing_api_route = client.get("/review/not-a-valid-endpoint")
        unknown_api_prefix = client.get("/api/not-a-valid-endpoint")
        asset = client.get("/assets/app-abc123.js")

    assert app_route.status_code == 200
    assert app_route.text == "<main>Clerk-san</main>"
    assert app_route.headers["content-security-policy"].startswith("default-src 'self'")
    assert missing_api_route.status_code == 404
    assert unknown_api_prefix.status_code == 404
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_rejected_document_reprocesses_once_and_keeps_old_approval_stale(
    demo_client: TestClient,
) -> None:
    settings = demo_client.app.state.settings
    document_id, extraction_id = asyncio.run(_seed_reviewable_document(settings))
    rejected = demo_client.post(
        "/review/reject",
        json={
            "extraction_id": str(extraction_id),
            "reason": "source needs another pass",
            "reviewer": "reviewer",
        },
    )
    assert rejected.status_code == 200

    reprocess = demo_client.post(f"/documents/{document_id}/reprocess?actor=reviewer")
    assert reprocess.status_code == 202
    accepted = reprocess.json()
    assert accepted["original_version"] == 1
    assert accepted["status"] == "queued"
    assert accepted["job_id"]

    repeated = demo_client.post(f"/documents/{document_id}/reprocess?actor=reviewer")
    assert repeated.status_code == 202
    assert repeated.json()["status"] == "already_queued"
    assert repeated.json()["job_id"] is None

    async def complete_reprocess() -> UUID:
        async with get_session(settings) as session:
            job = await session.get(Job, UUID(accepted["job_id"]))
            assert job is not None
            assert job.payload["source_version"] == 1
            assert job.idempotency_key == f"reprocess:1:{extraction_id}:2"
            replacement_id = await ExtractionRepo(session).add(
                document_id,
                payload=_payload(amount=1300),
                field_confidences={},
                model_name="replacement-model",
                prompt_version="replacement",
                actor="worker",
            )
            replacement = await session.get(ExtractedRecord, replacement_id)
            assert replacement is not None
            assert replacement.status is ExtractionStatus.PENDING_REVIEW
            assert replacement.source_version == 1
            return replacement_id

    replacement_id = asyncio.run(complete_reprocess())
    review = demo_client.get("/review")
    assert review.status_code == 200
    assert str(replacement_id) in {item["extraction_id"] for item in review.json()}

    stale = demo_client.post(
        "/review/approve",
        json={
            "extraction_id": str(extraction_id),
            "expected_version": 1,
            "corrections": {},
            "reviewer": "reviewer",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_extraction"


def test_verified_document_reprocess_is_idempotent_before_the_worker_runs(
    demo_client: TestClient,
) -> None:
    settings = demo_client.app.state.settings
    document_id, extraction_id = asyncio.run(_seed_reviewable_document(settings))
    approved = demo_client.post(
        "/review/approve",
        json={
            "extraction_id": str(extraction_id),
            "expected_version": 1,
            "corrections": {},
            "reviewer": "reviewer",
        },
    )
    assert approved.status_code == 200

    first = demo_client.post(f"/documents/{document_id}/reprocess?actor=reviewer")
    second = demo_client.post(f"/documents/{document_id}/reprocess?actor=reviewer")

    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == "queued"
    assert first.json()["job_id"]
    assert second.json()["status"] == "already_queued"
    assert second.json()["job_id"] is None


def test_retry_derivatives_requeues_only_current_terminal_stages(demo_client: TestClient) -> None:
    settings = demo_client.app.state.settings
    normalized_sha256 = "a" * 64

    async def seed_terminal_index() -> tuple[UUID, UUID]:
        async with get_session(settings) as session:
            documents = DocumentRepo(session)
            document_id = await documents.create_with_raw(
                filename="review.png",
                content_path="originals/review.png",
                sha256="b" * 64,
                mime="image/png",
            )
            await documents.set_status(document_id, DocumentStatus.IN_REVIEW, source_version=1)
            job_id = await enqueue(
                session,
                job_type="index_document",
                payload={
                    "document_id": str(document_id),
                    "source_version": 1,
                    "normalized_sha256": normalized_sha256,
                },
                idempotency_key=f"index:1:{normalized_sha256}",
            )
            assert job_id is not None
            job = await session.get(Job, job_id)
            assert job is not None
            job.status = JobStatus.DEAD
            job.attempts = 3
            job.last_error = "IndexingError: local embedder unavailable"
            await session.commit()
            return document_id, job_id

    document_id, failed_job_id = asyncio.run(seed_terminal_index())

    first = demo_client.post(f"/documents/{document_id}/retry-derivatives")
    second = demo_client.post(f"/documents/{document_id}/retry-derivatives")

    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == "queued"
    assert len(first.json()["job_ids"]) == 1
    assert second.json() == {
        "document_id": str(document_id),
        "original_version": 1,
        "status": "nothing_to_retry",
        "job_ids": [],
    }

    async def assert_recovery() -> None:
        async with get_session(settings) as session:
            failed = await session.get(Job, failed_job_id)
            assert failed is not None
            successor = await session.get(Job, UUID(first.json()["job_ids"][0]))
            assert successor is not None
            assert failed.status is JobStatus.DEAD
            assert failed.last_error == "IndexingError: local embedder unavailable"
            assert successor.status is JobStatus.QUEUED
            assert successor.payload == failed.payload
            assert successor.idempotency_key == f"recovery:{failed_job_id}"
            detail = await DocumentRepo(session).get(document_id)
            assert detail["status"] == DocumentStatus.IN_REVIEW.value
            assert detail["processing_error"] is None

    asyncio.run(assert_recovery())


def test_replacing_a_source_supersedes_an_open_review(demo_client: TestClient) -> None:
    settings = demo_client.app.state.settings
    document_id, extraction_id = asyncio.run(_seed_reviewable_document(settings))

    replacement = demo_client.post(
        f"/documents/{document_id}/original?actor=reviewer",
        files={"file": ("replacement.png", _png_bytes("black"), "image/png")},
    )
    assert replacement.status_code == 202

    stale = demo_client.post(
        "/review/approve",
        json={
            "extraction_id": str(extraction_id),
            "expected_version": 1,
            "corrections": {},
            "reviewer": "reviewer",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_extraction"


def test_upload_maps_content_and_size_failures_to_typed_errors(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'limits.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
        max_upload_bytes=8,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        oversize = client.post(
            "/documents",
            files={"file": ("receipt.png", _png_bytes(), "image/png")},
        )
        assert oversize.status_code == 413
        assert oversize.json()["code"] == "upload_too_large"
        assert oversize.json()["detail"]["limit"] == 8

    default_settings = settings.model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'unsupported.sqlite'}",
            "max_upload_bytes": 1024,
        }
    )
    with TestClient(create_app(default_settings), base_url="http://127.0.0.1:8000") as client:
        unsupported = client.post(
            "/documents",
            files={
                "file": ("receipt.bin", b"not a supported document", "application/octet-stream")
            },
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["code"] == "inspection_ambiguous"


def test_upload_rejects_excessive_ooxml_metadata_before_persisting_a_document(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ooxml-limits.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
        max_archive_members=2,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/documents",
            files={
                "file": (
                    "receipt.docx",
                    _ooxml_package_with_extra_member(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )

        assert response.status_code == 413
        assert response.json()["code"] == "resource_limit_exceeded"
        assert response.json()["detail"]["limit_name"] == "max_archive_members"
        assert client.get("/documents").json()["items"] == []

    originals = settings.storage_dir / "originals"
    assert not originals.exists()
    quarantine = settings.storage_dir / ".quarantine"
    assert not any(path.is_file() for path in quarantine.rglob("*"))
