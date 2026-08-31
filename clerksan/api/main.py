"""FastAPI application factory for the loopback-only Clerk-san service."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import select, text

from clerksan.api.request_boundaries import ExactHostMiddleware, RequestBoundaryMiddleware
from clerksan.api.schemas import CapabilityOut, ComponentReadinessOut, ErrorOut
from clerksan.bills.service import BillConflictError, BillValidationError
from clerksan.config import IntakeMode, SandboxUnavailable, Settings, get_settings
from clerksan.db.engine import dispose_engines, get_engine, get_session
from clerksan.db.models import Base, WorkerCapabilityLease
from clerksan.db.repositories import (
    DocumentNotFoundError,
    MappingConflictError,
    MappingNotFoundError,
    RawSourceVersionError,
    ReprocessStateError,
    ReviewValidationError,
    StaleExtractionError,
    UploadIdempotencyConflictError,
)
from clerksan.db.sqlite_schema import upgrade_sqlite_demo_schema
from clerksan.ingest.activation import evaluate_universal_activation
from clerksan.ingest.capabilities import CapabilityRegistry, build_capability_registry
from clerksan.ingest.filetype import UnsupportedFileError
from clerksan.ingest.limits import ResourceLimitExceeded, UnsafeArchiveMemberError
from clerksan.ingest.mapping import MappingValidationError
from clerksan.ingest.parser_runner import (
    ParserRunner,
    SidecarSandboxBackend,
    UnavailableSandboxBackend,
)
from clerksan.ingest.policy import PublicIntakeError, PublicReasonCode
from clerksan.ingest.storage_reconcile import (
    ReconcileReport,
    async_storage_lock,
    reconcile_reservations,
)
from clerksan.storage import ArtifactIntegrityError

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_API_PATH_PREFIXES = frozenset(
    {
        "api",
        "bills",
        "capabilities",
        "documents",
        "export",
        "health",
        "intakes",
        "openapi.json",
        "query",
        "ready",
        "review",
    }
)
_APP_CSP = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "object-src 'none'; img-src 'self' blob:; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; worker-src 'self' blob:"
)
_LOCAL_SCHEMA_UPGRADE_MESSAGE = (
    "Local SQLite data needs an upgrade. Stop the local app, create a backup, and restart it."
)
_CONFIG_UNAVAILABLE_MESSAGE = "Runtime configuration is unavailable."
_CONFIG_ERROR_API_HOST = "127.0.0.1:8000"
_MODEL_REASON_MISSING = "required_model_missing"
_MODEL_REASON_DIGEST = "embedding_digest_mismatch"
_MODEL_REASON_OLLAMA = "ollama_unavailable"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StorageReadinessEvidence:
    """Sanitized store evidence suitable for readiness responses and tests."""

    ready: bool
    reason_code: str | None
    path_state: str
    reference_snapshot_ready: bool = False
    reconciliation_scanned: int = 0
    reconciliation_errors: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "reason_code": self.reason_code,
            "path_state": self.path_state,
            "reference_snapshot_ready": self.reference_snapshot_ready,
            "reconciliation": {
                "scanned": self.reconciliation_scanned,
                "errors": self.reconciliation_errors,
            },
        }


def _error(status_code: int, code: str, message: str, detail: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorOut(code=code, message=message, detail=detail).model_dump(mode="json"),
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _unsafe_browser_request(request: Request, settings: Settings) -> str | None:
    """Return a rejection reason for an unsafe cross-origin browser mutation.

    Browser fetches and form submissions carry an Origin or Sec-Fetch-Site header.  A missing
    pair is reserved for local CLI/Streamlit compatibility and test automation, all of which
    already require access to the loopback host.  Browser-originated mutations fail closed.
    """

    if request.method not in _UNSAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if origin is None:
        return "missing Origin header" if fetch_site is not None else None

    normalized_origin = origin.rstrip("/").lower()
    if normalized_origin not in settings.allowed_browser_origins:
        return "unexpected Origin header"
    if request.headers.get("host", "").lower() != settings.api_host:
        return "unexpected Host header"
    return None


def _install_static_ui(app: FastAPI, settings: Settings) -> None:
    """Serve a built Vite UI after API routes without swallowing invalid API/original URLs."""

    static_dir = Path(settings.ui_static_dir)
    index = static_dir / "index.html"
    if not index.is_file():
        return
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    @app.get("/{ui_path:path}", include_in_schema=False)
    async def single_page_application(ui_path: str) -> FileResponse:
        first_path_segment = ui_path.split("/", 1)[0]
        if first_path_segment in _API_PATH_PREFIXES:
            raise HTTPException(status_code=404, detail="API route not found")
        return FileResponse(
            index,
            media_type="text/html",
            headers={"Cache-Control": "no-cache", "Content-Security-Policy": _APP_CSP},
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.sqlite_schema_error = False
    app.state.storage_readiness = _prepare_storage_root(settings.storage_dir)
    if settings.is_sqlite:
        try:
            async with get_engine(settings).begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await upgrade_sqlite_demo_schema(connection)
        except Exception:
            app.state.sqlite_schema_error = True
    if not app.state.sqlite_schema_error and app.state.storage_readiness.ready:
        try:
            app.state.storage_readiness = await _reconcile_storage(settings)
        except Exception:  # noqa: BLE001 - readiness must report a safe core outage
            app.state.storage_readiness = StorageReadinessEvidence(
                ready=False,
                reason_code="storage_reconcile_failed",
                path_state="reconcile_failed",
            )
    try:
        yield
    finally:
        await dispose_engines()


def _prepare_storage_root(storage_dir: Path) -> StorageReadinessEvidence:
    """Create only a trustworthy configured root; never traverse a root symlink."""

    if not storage_dir.is_absolute():
        return StorageReadinessEvidence(False, "storage_path_unsafe", "relative")
    if storage_dir.is_symlink():
        return StorageReadinessEvidence(False, "storage_path_unsafe", "symlink")
    if storage_dir.exists() and not storage_dir.is_dir():
        return StorageReadinessEvidence(False, "storage_path_unsafe", "not_directory")
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return StorageReadinessEvidence(False, "storage_unavailable", "unavailable")
    if storage_dir.is_symlink() or not storage_dir.is_dir():
        return StorageReadinessEvidence(False, "storage_path_unsafe", "unsafe")
    return StorageReadinessEvidence(True, None, "directory")


async def _reconcile_storage(settings: Settings) -> StorageReadinessEvidence:
    """Clean stale reservations before serving requests, using a DB reference snapshot."""

    async with async_storage_lock(settings.storage_dir, shared=False):
        referenced: set[str] | None = None
        reference_snapshot_ready = True
        try:
            async with get_engine(settings).connect() as connection:
                referenced = set(
                    await connection.scalars(text("SELECT sha256 FROM document_files"))
                )
        except Exception:  # noqa: BLE001 - conservative cleanup retains published blobs
            logger.warning("storage reconciliation could not load source references")
            reference_snapshot_ready = False
        report = reconcile_reservations(
            settings.storage_dir,
            settings.storage_reservation_grace_seconds,
            None if referenced is None else referenced.__contains__,
            lock_held=True,
        )
    if not isinstance(report, ReconcileReport):
        # Test doubles and older integrations may not yet return the additive report.
        report = ReconcileReport()
    errors = len(report.errors)
    return StorageReadinessEvidence(
        ready=errors == 0,
        reason_code="storage_reconcile_failed" if errors else None,
        path_state="reconcile_failed" if errors else "directory",
        reference_snapshot_ready=reference_snapshot_ready,
        reconciliation_scanned=report.scanned,
        reconciliation_errors=errors,
    )


async def _worker_capability_evidence(
    settings: Settings,
    *,
    registry_digest: str,
    capabilities_digest: str,
    sandbox_verified: bool,
) -> tuple[dict[str, object], PublicReasonCode | None, bool]:
    """Return the freshest lease without letting it alter legacy top-level readiness."""

    async with get_session(settings) as session:
        lease = await session.scalar(
            select(WorkerCapabilityLease)
            .order_by(
                WorkerCapabilityLease.heartbeat_at.desc(),
                WorkerCapabilityLease.worker_id.asc(),
            )
            .limit(1)
        )
    if lease is None:
        return (
            {
                "worker_registry_digest": None,
                "worker_capabilities_digest": None,
                "worker_capability_lease_age_seconds": None,
            },
            PublicReasonCode.WORKER_CAPABILITY_STALE,
            False,
        )

    now = dt.datetime.now(dt.UTC)
    heartbeat = _aware_datetime(lease.heartbeat_at)
    expires = _aware_datetime(lease.expires_at)
    age_seconds = max(0.0, (now - heartbeat).total_seconds())
    evidence = {
        "worker_registry_digest": lease.registry_digest,
        "worker_capabilities_digest": lease.capabilities_digest,
        "worker_capability_lease_age_seconds": age_seconds,
    }
    if expires <= now:
        return evidence, PublicReasonCode.WORKER_CAPABILITY_STALE, False
    if (
        lease.registry_digest != registry_digest
        or lease.capabilities_digest != capabilities_digest
        or lease.sandbox_verified is not sandbox_verified
    ):
        return evidence, PublicReasonCode.REGISTRY_MISMATCH, False
    return evidence, None, True


def _aware_datetime(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _parser_runner_for_settings(settings: Settings) -> ParserRunner:
    if settings.intake_mode is IntakeMode.LEGACY:
        return ParserRunner(UnavailableSandboxBackend())
    return ParserRunner(
        SidecarSandboxBackend(
            str(settings.parser_socket_path),
            timeout_seconds=settings.parser_request_timeout_seconds,
        )
    )


def _withheld_capability_registry(registry: CapabilityRegistry) -> CapabilityRegistry:
    return CapabilityRegistry(
        process=(),
        limits=registry.limits,
        flags={
            **dict(registry.flags),
            "universal_activation_enabled": False,
            "activation_withheld": True,
        },
        sandbox=registry.sandbox,
    )


def create_app(
    settings: Settings | None = None,
    *,
    parser_runner: ParserRunner | None = None,
) -> FastAPI:
    """Build an API process that never starts or claims background jobs."""

    active_settings = settings or get_settings()
    active_parser_runner = parser_runner or _parser_runner_for_settings(active_settings)
    probe = (
        active_parser_runner.startup_probe()
        if active_settings.intake_mode is IntakeMode.UNIVERSAL
        else None
    )
    capability_registry = build_capability_registry(active_settings, probe)
    withheld_registry = _withheld_capability_registry(capability_registry)
    app = FastAPI(
        title="Clerk-san local document service",
        version="2.0.0",
        lifespan=_lifespan,
    )
    app.state.settings = active_settings
    app.state.capability_registry = capability_registry
    app.state.withheld_capability_registry = withheld_registry
    app.state.parser_runner = active_parser_runner
    app.state.sqlite_schema_error = False
    app.state.storage_readiness = StorageReadinessEvidence(
        ready=False,
        reason_code="storage_not_checked",
        path_state="not_checked",
    )

    @app.middleware("http")
    async def local_browser_boundary(request: Request, call_next):  # type: ignore[no-untyped-def]
        reason = _unsafe_browser_request(request, active_settings)
        if reason is not None:
            return _error(403, "unsafe_browser_origin", reason)
        first_path_segment = request.url.path.lstrip("/").split("/", 1)[0]
        if (
            app.state.sqlite_schema_error
            and first_path_segment in _API_PATH_PREFIXES
            and request.url.path not in {"/health", "/ready"}
        ):
            return _error(503, "local_data_needs_upgrade", _LOCAL_SCHEMA_UPGRADE_MESSAGE)
        if (
            not app.state.storage_readiness.ready
            and first_path_segment in _API_PATH_PREFIXES
            and request.url.path not in {"/health", "/ready"}
        ):
            return _error(
                503,
                "storage_unavailable",
                "The configured document store is not safe to use.",
                {"storage": app.state.storage_readiness.as_dict()},
            )
        response = await call_next(request)
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith("/assets/") and response.status_code == 200:
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        return response

    # Installed after the function middleware so this pure ASGI guard is outermost
    # and executes before Starlette/FastAPI parses JSON or multipart bodies.
    app.add_middleware(RequestBoundaryMiddleware, settings=active_settings)

    @app.exception_handler(DocumentNotFoundError)
    async def document_not_found(_: Request, error: DocumentNotFoundError) -> JSONResponse:
        return _error(404, "document_not_found", "Document not found", {"id": str(error)})

    @app.exception_handler(StaleExtractionError)
    async def stale_extraction(_: Request, error: StaleExtractionError) -> JSONResponse:
        return _error(409, "stale_extraction", str(error))

    @app.exception_handler(ReviewValidationError)
    async def invalid_review(_: Request, error: ReviewValidationError) -> JSONResponse:
        return _error(422, "invalid_review", str(error))

    @app.exception_handler(RawSourceVersionError)
    async def raw_source_version_conflict(_: Request, error: RawSourceVersionError) -> JSONResponse:
        return _error(409, "raw_source_version_conflict", str(error))

    @app.exception_handler(ReprocessStateError)
    async def reprocess_not_available(_: Request, error: ReprocessStateError) -> JSONResponse:
        return _error(409, "reprocess_not_available", str(error))

    @app.exception_handler(UploadIdempotencyConflictError)
    async def upload_idempotency_conflict(
        _: Request, error: UploadIdempotencyConflictError
    ) -> JSONResponse:
        del error
        return _error(
            409,
            PublicReasonCode.IDEMPOTENCY_CONFLICT.value,
            "The idempotency key is already bound to a different upload intent.",
            {"retryable": False},
        )

    @app.exception_handler(MappingNotFoundError)
    async def mapping_not_found(_: Request, error: MappingNotFoundError) -> JSONResponse:
        return _error(404, "mapping_not_found", "Mapping contract not found", {"id": str(error)})

    @app.exception_handler(MappingConflictError)
    async def mapping_conflict(_: Request, error: MappingConflictError) -> JSONResponse:
        return _error(409, error.code, str(error), error.detail)

    @app.exception_handler(MappingValidationError)
    async def invalid_mapping(_: Request, error: MappingValidationError) -> JSONResponse:
        return _error(422, "invalid_mapping", str(error))

    @app.exception_handler(BillConflictError)
    async def recurring_bill_conflict(_: Request, error: BillConflictError) -> JSONResponse:
        return _error(409, "recurring_bill_conflict", str(error))

    @app.exception_handler(BillValidationError)
    async def recurring_bill_invalid(_: Request, error: BillValidationError) -> JSONResponse:
        return _error(422, "recurring_bill_invalid", str(error))

    @app.exception_handler(ResourceLimitExceeded)
    async def resource_limit(_: Request, error: ResourceLimitExceeded) -> JSONResponse:
        return _error(
            413,
            "resource_limit_exceeded",
            str(error),
            {"limit_name": error.limit_name, "limit": error.limit, "observed": error.observed},
        )

    @app.exception_handler(UnsafeArchiveMemberError)
    async def unsafe_archive(_: Request, error: UnsafeArchiveMemberError) -> JSONResponse:
        del error
        return _error(
            422,
            PublicReasonCode.INSPECTION_AMBIGUOUS.value,
            "The uploaded container cannot be inspected safely.",
        )

    @app.exception_handler(UnsupportedFileError)
    async def unsupported_file(_: Request, error: UnsupportedFileError) -> JSONResponse:
        del error
        return _error(
            422,
            PublicReasonCode.INSPECTION_AMBIGUOUS.value,
            "The uploaded content is not supported by the legacy intake policy.",
        )

    @app.exception_handler(PublicIntakeError)
    async def public_intake_rejection(_: Request, error: PublicIntakeError) -> JSONResponse:
        status_code = {
            PublicReasonCode.PROHIBITED_AUDIO: 415,
            PublicReasonCode.PROHIBITED_VIDEO: 415,
            PublicReasonCode.PROHIBITED_EXECUTABLE: 415,
            PublicReasonCode.IDEMPOTENCY_CONFLICT: 409,
            PublicReasonCode.SANDBOX_UNAVAILABLE: 503,
            PublicReasonCode.WORKER_CAPABILITY_STALE: 503,
            PublicReasonCode.REGISTRY_MISMATCH: 503,
            PublicReasonCode.INTERNAL_ERROR: 500,
        }.get(error.reason_code, 422)
        return _error(
            status_code,
            error.reason_code.value,
            "The upload was rejected by the local intake policy.",
            {"retryable": error.retryable},
        )

    @app.exception_handler(ArtifactIntegrityError)
    async def artifact_checksum_mismatch(_: Request, error: ArtifactIntegrityError) -> JSONResponse:
        return _error(409, "artifact_checksum_mismatch", str(error))

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/capabilities", tags=["health"], response_model=CapabilityOut)
    async def capabilities() -> CapabilityOut:
        advertised = capability_registry
        if active_settings.intake_mode is IntakeMode.UNIVERSAL:
            try:
                async with get_session(active_settings) as session:
                    activation = await evaluate_universal_activation(
                        session, active_settings, capability_registry
                    )
                if not activation.ready:
                    advertised = withheld_registry
            except Exception:  # noqa: BLE001 - fail closed to an empty process set
                advertised = withheld_registry
        return CapabilityOut(**advertised.advertised_payload())

    @app.get("/ready", tags=["health"])
    async def ready() -> dict[str, object]:
        if app.state.sqlite_schema_error:
            return _error(
                503,
                "local_data_needs_upgrade",
                _LOCAL_SCHEMA_UPGRADE_MESSAGE,
                {
                    "core_reason_codes": ["local_data_needs_upgrade"],
                    "storage": app.state.storage_readiness.as_dict(),
                },
            )
        errors: list[str] = []
        core_reason_codes: list[str] = []
        processing_errors: list[str] = []
        model_reason_codes: list[str] = []
        storage_ready = app.state.storage_readiness.ready
        if not storage_ready:
            errors.append("storage unavailable")
            core_reason_codes.append("storage_unavailable")
        database_ready = True
        try:
            async with get_engine(active_settings).connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:  # pragma: no cover - database-specific failures vary
            database_ready = False
            errors.append("database unavailable")
            core_reason_codes.append("database_unavailable")

        if database_ready and storage_ready and not active_settings.demo_mode:
            try:
                from clerksan.llm.client import OllamaClient

                client = OllamaClient(active_settings)
                try:
                    installed_models = await client.list_models()
                finally:
                    await client.aclose()
                installed_by_name = {
                    _canonical_model_name(name): model
                    for model in installed_models
                    if isinstance(name := (model.get("name") or model.get("model")), str)
                }
                missing = [
                    model
                    for model in active_settings.required_models
                    if _canonical_model_name(model) not in installed_by_name
                ]
                if missing:
                    processing_errors.extend(
                        f"missing model: ollama pull {model}" for model in missing
                    )
                    model_reason_codes.append(_MODEL_REASON_MISSING)
                if active_settings.embed_model and active_settings.embed_model_digest:
                    installed = installed_by_name.get(
                        _canonical_model_name(active_settings.embed_model)
                    )
                    actual_digest = installed.get("digest") if installed is not None else None
                    if (
                        installed is not None
                        and actual_digest != active_settings.embed_model_digest
                    ):
                        processing_errors.append(
                            "embedding model digest does not match the configured pin; "
                            "pull the selected tag or perform a new migration and full re-embedding"
                        )
                        model_reason_codes.append(_MODEL_REASON_DIGEST)
            except Exception:  # pragma: no cover - network failure details vary
                processing_errors.append("ollama unavailable")
                model_reason_codes.append(_MODEL_REASON_OLLAMA)

        worker_evidence: dict[str, object] = {
            "worker_registry_digest": None,
            "worker_capabilities_digest": None,
            "worker_capability_lease_age_seconds": None,
        }
        worker_reason: PublicReasonCode | None = None
        worker_ready = False
        universal_ready = False
        core_ready = database_ready and storage_ready
        if core_ready:
            try:
                if active_settings.intake_mode is IntakeMode.UNIVERSAL:
                    async with get_session(active_settings) as session:
                        activation = await evaluate_universal_activation(
                            session, active_settings, capability_registry
                        )
                    lease = activation.lease
                    if lease is not None:
                        age_seconds = max(
                            0.0,
                            (
                                dt.datetime.now(dt.UTC) - _aware_datetime(lease.heartbeat_at)
                            ).total_seconds(),
                        )
                        worker_evidence = {
                            "worker_registry_digest": lease.registry_digest,
                            "worker_capabilities_digest": lease.capabilities_digest,
                            "worker_capability_lease_age_seconds": age_seconds,
                        }
                    worker_reason = activation.reason_code
                    worker_ready = activation.ready
                    universal_ready = activation.ready
                else:
                    (
                        worker_evidence,
                        worker_reason,
                        worker_ready,
                    ) = await _worker_capability_evidence(
                        active_settings,
                        registry_digest=capability_registry.registry_digest,
                        capabilities_digest=capability_registry.capabilities_digest,
                        sandbox_verified=False,
                    )
            except Exception:  # noqa: BLE001 - lease evidence stays additive in legacy mode
                worker_reason = PublicReasonCode.WORKER_CAPABILITY_STALE
        processing_reason_codes: list[PublicReasonCode] = []
        if processing_errors:
            processing_reason_codes.append(PublicReasonCode.MODEL_UNAVAILABLE)
        if worker_reason is not None:
            processing_reason_codes.append(worker_reason)
        components = ComponentReadinessOut(
            intake_ready=core_ready,
            review_ready=core_ready,
            processing_ready=core_ready and not processing_errors and worker_ready,
            universal_processing_ready=universal_ready,
            processing_reason_codes=processing_reason_codes,
            registry_digest=capability_registry.registry_digest,
            capabilities_digest=capability_registry.capabilities_digest,
            **worker_evidence,
        ).model_dump(mode="json")
        components["core_reason_codes"] = core_reason_codes
        components["model_reason_codes"] = list(dict.fromkeys(model_reason_codes))
        components["storage"] = app.state.storage_readiness.as_dict()

        if errors:
            return JSONResponse(
                status_code=503,
                content=ErrorOut(
                    code="not_ready",
                    message="; ".join(errors),
                    detail={"errors": errors, **components},
                ).model_dump(mode="json"),
            )
        return {"status": "ready", "demo_mode": active_settings.demo_mode, **components}

    from clerksan.api.routes import (
        bills,
        documents,
        export,
        ingest,
        intakes,
        mappings,
        query,
        review,
    )

    app.include_router(ingest.router)
    app.include_router(intakes.router)
    app.include_router(mappings.router)
    app.include_router(documents.router)
    app.include_router(review.router)
    app.include_router(query.router)
    app.include_router(bills.router)
    app.include_router(export.router)
    _install_static_ui(app, active_settings)
    return app


def _create_config_error_app(error: Exception) -> FastAPI:
    """Keep module import safe without trusting or disclosing invalid configuration."""

    sandbox_unavailable = isinstance(error, SandboxUnavailable)
    app = FastAPI(
        title="Clerk-san local document service",
        version="2.0.0",
    )
    # Invalid settings cannot be reused safely. Keep this diagnostic-only app on the
    # documented default host and reject every other Host before routing.
    app.add_middleware(ExactHostMiddleware, expected_host=_CONFIG_ERROR_API_HOST)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["health"])
    async def ready() -> JSONResponse:
        code = "sandbox_unavailable" if sandbox_unavailable else "not_ready"
        reason_code = "sandbox_unavailable" if sandbox_unavailable else "configuration_unavailable"
        message = "sandbox_unavailable" if sandbox_unavailable else _CONFIG_UNAVAILABLE_MESSAGE
        return _error(
            503,
            code,
            message,
            {
                "reason_code": reason_code,
                "retryable": sandbox_unavailable,
            },
        )

    return app


try:
    app = create_app()
except (SandboxUnavailable, ValueError, ValidationError) as error:
    app = _create_config_error_app(error)


def _canonical_model_name(model: str) -> str:
    return model.strip().removesuffix(":latest")
