from __future__ import annotations

import asyncio
import datetime as dt
import io
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from clerksan.api.main import create_app
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.models import (
    Document,
    DocumentClass,
    DocumentStatus,
    ExecutionProfile,
    ExtractedRecord,
    ExtractionBatch,
    FinancialSubtype,
    Job,
    RecordKind,
    SourceIntake,
    SourceIntakeState,
    SpreadsheetRow,
    VerifiedRecord,
)
from clerksan.db.repositories import WorkerCapabilityLeaseRepo
from clerksan.extract.classifier import ClassificationResult
from clerksan.ingest.adapters.delimited import DelimitedAdapter
from clerksan.ingest.capabilities import CapabilityRegistry
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument
from clerksan.ingest.parser_artifacts import ParserRunResult
from clerksan.ingest.parser_runner import ParserRunner, SandboxProbeResult
from clerksan.ingest.pipeline import build_default_dependencies, process_document


class _VerifiedTestRunner(ParserRunner):
    def startup_probe(self) -> SandboxProbeResult:
        return SandboxProbeResult(
            verified=True,
            backend="test-sidecar",
            evidence_digest="a" * 64,
            capabilities=tuple(file_type.value for file_type in FileType),
        )

    def preflight(self, source, detected, limits=None):
        source.verify_digest(max_bytes=limits.max_upload_bytes if limits else None)
        return {
            "source_sha256": source.source_sha256,
            "safe": True,
            "detected_format": detected["format"],
        }

    async def run(self, adapter_key, source, context, limits=None):
        assert adapter_key == "delimited.csv"
        return DelimitedAdapter(limits=limits).normalize(source, context)

    async def run_with_artifacts(self, adapter_key, source, context, limits=None):
        return ParserRunResult(await self.run(adapter_key, source, context, limits))


class _CapabilityTestRunner(_VerifiedTestRunner):
    def __init__(self, capabilities: tuple[str, ...]) -> None:
        self._capabilities = capabilities

    def startup_probe(self) -> SandboxProbeResult:
        return SandboxProbeResult(
            verified=True,
            backend="test-sidecar",
            evidence_digest="b" * 64,
            capabilities=self._capabilities,
        )


class _GenericTextRunner(_VerifiedTestRunner):
    async def run(self, adapter_key, source, context, limits=None):
        source.verify_digest(max_bytes=limits.max_upload_bytes if limits else None)
        assert adapter_key == "md"
        return NormalizedDocument(
            markdown_body="# Meeting note\n\nThis is not a financial record.",
            metadata=DocMetadata(
                filename=source.filename or "note.md",
                detected_type=FileType.MD,
                sha256=source.source_sha256,
                family="document",
                canonical_mime="text/markdown",
            ),
            embeddable=True,
        )


class _BillTextRunner(_VerifiedTestRunner):
    async def run(self, adapter_key, source, context, limits=None):
        source.verify_digest(max_bytes=limits.max_upload_bytes if limits else None)
        assert adapter_key == "png"
        return NormalizedDocument(
            markdown_body="領収書\n合計 1200円",
            metadata=DocMetadata(
                filename=source.filename or "receipt.png",
                detected_type=FileType.PNG,
                sha256=source.source_sha256,
                family="image",
                canonical_mime="image/png",
            ),
            embeddable=False,
        )


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(output, format="PNG")
    return output.getvalue()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'universal.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
        intake_mode="universal",
    )


async def _seed_matching_lease(settings: Settings, registry: CapabilityRegistry) -> None:
    now = dt.datetime.now(dt.UTC)
    async with get_session(settings) as session:
        await WorkerCapabilityLeaseRepo(session).refresh(
            worker_id="universal-test-worker",
            registry_digest=registry.registry_digest,
            capabilities_digest=registry.capabilities_digest,
            sandbox_verified=True,
            heartbeat_at=now,
            expires_at=now + dt.timedelta(seconds=settings.worker_capability_lease_seconds),
        )


def test_generic_csv_reaches_needs_mapping_without_model_extraction(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runner = _VerifiedTestRunner()
    with TestClient(
        create_app(settings, parser_runner=runner),
        base_url="http://127.0.0.1:8000",
    ) as client:
        registry = client.app.state.capability_registry
        asyncio.run(_seed_matching_lease(settings, registry))
        response = client.post(
            "/documents",
            data={"intake_intent": "generic_file"},
            files={
                "file": (
                    "transactions.csv",
                    b"date,amount\n2026-01-01,1200\n2026-01-02,900\n",
                    "text/csv",
                )
            },
        )

        assert response.status_code == 202
        accepted = response.json()
        assert accepted["reason_code"] == "mapping_required"
        assert accepted["job_id"] is not None

        async def process_and_read() -> tuple[SourceIntake, Job, int, int]:
            async with get_session(settings) as session:
                job = await session.get(Job, UUID(accepted["job_id"]))
                assert job is not None
                dependencies = build_default_dependencies(
                    settings,
                    parser_runner=runner,
                    capability_registry=registry,
                )
                await process_document(
                    session,
                    {
                        **job.payload,
                        "_job_id": str(job.id),
                        "_execution_profile": ExecutionProfile.UNIVERSAL_SANDBOXED.value,
                        "_sandbox_verified": True,
                        "_registry_digest": registry.registry_digest,
                        "_capabilities_digest": registry.capabilities_digest,
                        "_intake_intent": "generic_file",
                    },
                    dependencies=dependencies,
                )
                await session.commit()
                intake = await session.get(SourceIntake, UUID(accepted["source_intake_id"]))
                staged_count = await session.scalar(
                    select(func.count()).select_from(SpreadsheetRow)
                )
                extraction_count = await session.scalar(
                    select(func.count()).select_from(ExtractedRecord)
                )
                assert intake is not None
                assert staged_count is not None and extraction_count is not None
                return intake, job, staged_count, extraction_count

        intake, job, staged_count, extraction_count = asyncio.run(process_and_read())
        assert intake.state is SourceIntakeState.NEEDS_MAPPING
        assert intake.reason_code == "mapping_required"
        assert intake.execution_profile is ExecutionProfile.UNIVERSAL_SANDBOXED
        assert job.required_components == []
        assert job.payload["_pipeline"]["outcome"] == "needs_mapping"
        assert staged_count == 2
        assert extraction_count == 0


def test_bill_scan_csv_rejects_before_any_durable_write(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runner = _VerifiedTestRunner()
    with TestClient(
        create_app(settings, parser_runner=runner),
        base_url="http://127.0.0.1:8000",
    ) as client:
        registry = client.app.state.capability_registry
        asyncio.run(_seed_matching_lease(settings, registry))
        response = client.post(
            "/documents",
            data={"intake_intent": "bill_scan"},
            files={"file": ("transactions.csv", b"date,amount\n2026-01-01,1200\n", "text/csv")},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "intake_intent_mismatch"
        assert client.get("/documents").json()["items"] == []
        originals = settings.storage_dir / "originals"
        assert not originals.exists() or not any(originals.iterdir())


def test_generic_document_creates_a_nonfinancial_review_batch_without_models(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runner = _GenericTextRunner()
    with TestClient(
        create_app(settings, parser_runner=runner),
        base_url="http://127.0.0.1:8000",
    ) as client:
        registry = client.app.state.capability_registry
        asyncio.run(_seed_matching_lease(settings, registry))
        response = client.post(
            "/documents",
            data={"intake_intent": "generic_file"},
            files={"file": ("note.md", b"# inert input", "text/markdown")},
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["job_id"] is not None

        async def process_and_read() -> tuple[SourceIntake, Job, ExtractionBatch, ExtractedRecord]:
            async with get_session(settings) as session:
                job = await session.get(Job, UUID(accepted["job_id"]))
                assert job is not None
                dependencies = build_default_dependencies(
                    settings,
                    parser_runner=runner,
                    capability_registry=registry,
                )

                async def forbidden_classifier(*_args):
                    raise AssertionError("generic documents must not use financial models")

                dependencies.classifier = forbidden_classifier
                await process_document(
                    session,
                    {
                        **job.payload,
                        "_job_id": str(job.id),
                        "_execution_profile": ExecutionProfile.UNIVERSAL_SANDBOXED.value,
                        "_sandbox_verified": True,
                        "_registry_digest": registry.registry_digest,
                        "_capabilities_digest": registry.capabilities_digest,
                        "_intake_intent": "generic_file",
                    },
                    dependencies=dependencies,
                )
                await session.commit()
                intake = await session.get(SourceIntake, UUID(accepted["source_intake_id"]))
                batch = await session.scalar(select(ExtractionBatch))
                candidate = await session.scalar(select(ExtractedRecord))
                document = await session.get(Document, UUID(accepted["document_id"]))
                assert intake is not None and batch is not None and candidate is not None
                assert document is not None
                assert document.status is DocumentStatus.IN_REVIEW
                assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
                return intake, job, batch, candidate

        intake, job, batch, candidate = asyncio.run(process_and_read())
        assert intake.state is SourceIntakeState.PROCESSED
        assert batch.mapping_set_id is None
        assert batch.candidate_count == 1
        assert candidate.record_kind is RecordKind.GENERIC_DOCUMENT
        assert candidate.financial_subtype is None
        assert job.payload["_pipeline"]["batch_id"] == str(batch.id)


def test_universal_upload_fails_closed_before_a_matching_worker_lease(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(
        create_app(settings, parser_runner=_VerifiedTestRunner()),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = client.post(
            "/documents",
            data={"intake_intent": "generic_file"},
            files={"file": ("transactions.csv", b"date,amount\n", "text/csv")},
        )

        assert response.status_code == 503
        assert response.json()["code"] == "worker_capability_stale"
        assert client.get("/documents").json()["items"] == []


def test_bill_scan_creates_one_typed_financial_candidate_without_early_authority(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runner = _BillTextRunner()
    with TestClient(
        create_app(settings, parser_runner=runner),
        base_url="http://127.0.0.1:8000",
    ) as client:
        registry = client.app.state.capability_registry
        asyncio.run(_seed_matching_lease(settings, registry))
        response = client.post(
            "/documents",
            data={"intake_intent": "bill_scan"},
            files={"file": ("receipt.png", _png_bytes(), "image/png")},
        )
        assert response.status_code == 202, response.text
        accepted = response.json()

        async def process_and_read() -> tuple[ExtractionBatch, ExtractedRecord, Job]:
            async with get_session(settings) as session:
                job = await session.get(Job, UUID(accepted["job_id"]))
                assert job is not None
                dependencies = build_default_dependencies(
                    settings,
                    parser_runner=runner,
                    capability_registry=registry,
                )

                async def classify_receipt(*_args):
                    return ClassificationResult(
                        label=DocumentClass.RECEIPT,
                        confidence=1.0,
                        method="test",
                    )

                async def extract_receipt(*_args):
                    return {"merchant_name": {"value": "shop", "confidence": 0.9}}

                dependencies.classifier = classify_receipt
                dependencies.extractor = extract_receipt
                await process_document(
                    session,
                    {
                        **job.payload,
                        "_job_id": str(job.id),
                        "_execution_profile": ExecutionProfile.UNIVERSAL_SANDBOXED.value,
                        "_sandbox_verified": True,
                        "_registry_digest": registry.registry_digest,
                        "_capabilities_digest": registry.capabilities_digest,
                        "_intake_intent": "bill_scan",
                    },
                    dependencies=dependencies,
                )
                await session.commit()
                batch = await session.scalar(select(ExtractionBatch))
                candidate = await session.scalar(select(ExtractedRecord))
                document = await session.get(Document, UUID(accepted["document_id"]))
                assert batch is not None and candidate is not None and document is not None
                assert document.status is DocumentStatus.IN_REVIEW
                assert await session.scalar(select(func.count(VerifiedRecord.id))) == 0
                jobs = list((await session.scalars(select(Job))).all())
                assert len(jobs) == 2
                candidate_index = next(
                    item for item in jobs if item.job_type == "index_candidate_batch"
                )
                assert candidate_index.payload["batch_id"] == str(batch.id)
                return batch, candidate, job

        batch, candidate, job = asyncio.run(process_and_read())
        assert batch.candidate_count == 1
        assert candidate.record_kind is RecordKind.FINANCIAL
        assert candidate.financial_subtype is FinancialSubtype.RECEIPT
        assert candidate.payload["merchant_name"]["value"] == "shop"
        assert job.payload["_pipeline"]["batch_id"] == str(batch.id)


def test_new_capability_reprocesses_same_preserved_source_and_intake(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    initial_capabilities = tuple(
        file_type.value for file_type in FileType if file_type is not FileType.RTF
    )
    initial_runner = _CapabilityTestRunner(initial_capabilities)
    with TestClient(
        create_app(settings, parser_runner=initial_runner),
        base_url="http://127.0.0.1:8000",
    ) as client:
        registry = client.app.state.capability_registry
        asyncio.run(_seed_matching_lease(settings, registry))
        uploaded = client.post(
            "/documents",
            data={"intake_intent": "generic_file"},
            files={"file": ("notes.rtf", b"{\\rtf1\\ansi inert text}", "application/rtf")},
        )

        assert uploaded.status_code == 202
        accepted = uploaded.json()
        assert accepted["reason_code"] == "adapter_unavailable"
        assert accepted["job_id"] is None
        intake_id = UUID(accepted["source_intake_id"])
        source_file_id = UUID(accepted["source_file_id"])
        detail = client.get(f"/intakes/{intake_id}").json()
        assert detail["state"] == "stored_unprocessed"
        assert detail["detected_format"] == "rtf"

    promoted_runner = _CapabilityTestRunner(tuple(item.value for item in FileType))
    with TestClient(
        create_app(settings, parser_runner=promoted_runner),
        base_url="http://127.0.0.1:8000",
    ) as client:
        registry = client.app.state.capability_registry
        asyncio.run(_seed_matching_lease(settings, registry))
        response = client.post(
            f"/intakes/{intake_id}/reprocess",
            json={"expected_version": detail["version"], "actor": "local-user"},
        )

        assert response.status_code == 202
        assert response.json()["job_id"] is not None

    async def read_result() -> tuple[list[SourceIntake], list[Job]]:
        async with get_session(settings) as session:
            return (
                list(await session.scalars(select(SourceIntake))),
                list(await session.scalars(select(Job))),
            )

    intakes, jobs = asyncio.run(read_result())
    assert len(intakes) == 1
    assert intakes[0].id == intake_id
    assert intakes[0].source_file_id == source_file_id
    assert intakes[0].state is SourceIntakeState.QUEUED
    assert len(jobs) == 1
    assert jobs[0].execution_profile is ExecutionProfile.UNIVERSAL_SANDBOXED
    assert jobs[0].required_components == []
    assert jobs[0].payload["source_intake_id"] == str(intake_id)
    assert jobs[0].payload["source_file_id"] == str(source_file_id)
    assert jobs[0].payload["detected_format"] == "rtf"
    assert jobs[0].payload["adapter_key"] == "rtf"
