"""Runtime configuration for the local Clerk-san services."""

from __future__ import annotations

import enum
import math
from functools import lru_cache
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

EMBED_MODEL_TAG = "nomic-embed-text:v1.5"
EMBED_MODEL_DIGEST = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"
EMBED_MODEL_DIMENSION = 768
DEFAULT_DATABASE_URL = "postgresql+asyncpg://clerksan@db:5432/clerksan"
OCR_ENGINES = frozenset(("vision_llm", "yomitoku", "paddleocr"))
DEFAULT_CONFIDENCE_THRESHOLD = 0.85
SANDBOX_UNAVAILABLE_REASON = "sandbox_unavailable"


class IntakeMode(enum.StrEnum):
    """Static process-wide intake modes.

    ``universal`` is part of the stable configuration vocabulary, but Phase 1 does
    not have a verified parser sandbox and therefore cannot activate it.
    """

    LEGACY = "legacy"
    UNIVERSAL = "universal"


class SandboxUnavailable(RuntimeError):
    """A universal intake configuration cannot prove the required hard sandbox."""

    reason_code: ClassVar[str] = SANDBOX_UNAVAILABLE_REASON
    retryable: ClassVar[bool] = True

    def __init__(self) -> None:
        # Keep the message machine-stable as well as exposing ``reason_code`` so
        # startup surfaces never need to parse a changing explanatory sentence.
        super().__init__(self.reason_code)


def ensure_intake_mode_available(mode: IntakeMode | str) -> IntakeMode:
    """Normalize the static mode vocabulary; runtime owns sandbox activation."""

    return mode if isinstance(mode, IntakeMode) else IntakeMode(mode)


def require_universal_sandbox(mode: IntakeMode | str, *, verified: bool) -> IntakeMode:
    """Fail closed when universal mode lacks current hard-sandbox evidence."""

    normalized = ensure_intake_mode_available(mode)
    if normalized is IntakeMode.UNIVERSAL and not verified:
        raise SandboxUnavailable
    return normalized


class Settings(BaseSettings):
    """Typed settings loaded from ``CLERKSAN_*`` environment variables.

    Production Compose supplies the PostgreSQL and in-network Ollama defaults. Tests and
    the documented demo override those values explicitly, so no caller reads an
    environment variable directly.
    """

    model_config = SettingsConfigDict(
        env_prefix="CLERKSAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = DEFAULT_DATABASE_URL
    database_password: SecretStr | None = None
    ollama_url: str = "http://ollama:11434"
    storage_dir: Path = Path("/data/doc_store")
    api_url: str = "http://127.0.0.1:8000"
    ui_static_dir: Path = Path("web/dist")
    browser_origins: str = "http://127.0.0.1:8000,http://127.0.0.1:5173"

    intake_mode: IntakeMode = IntakeMode.LEGACY
    parser_socket_path: Path = Path("/run/clerksan-parser/parser.sock")
    parser_request_timeout_seconds: float = 20.0

    ocr_engine: str = "vision_llm"
    ocr_model: str = "gemma3:4b"
    extract_model: str = "qwen2.5:7b"
    router_model: str = "qwen2.5:7b"
    embed_model: str | None = EMBED_MODEL_TAG
    embed_model_digest: str | None = EMBED_MODEL_DIGEST
    embed_dim: int | None = EMBED_MODEL_DIMENSION
    embedding_batch_size: int = 16

    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    worker_concurrency: int = 1
    job_lease_seconds: int = 120
    job_max_attempts: int = 4
    job_retry_base_seconds: int = 2

    max_upload_bytes: int = 25 * 1024 * 1024
    max_request_bytes: int = 32 * 1024 * 1024
    request_receive_timeout_seconds: float = 10.0
    max_multipart_files: int = 1
    max_multipart_fields: int = 8
    max_json_bytes: int = 1 * 1024 * 1024
    max_json_depth: int = 32
    upload_concurrency: int = 2
    idempotency_retention_hours: int = 168
    recent_intakes_default_limit: int = 50
    recent_intakes_max_limit: int = 100
    worker_capability_heartbeat_seconds: int = 10
    worker_capability_lease_seconds: int = 30
    storage_reservation_grace_seconds: int = 600
    max_pdf_pages: int = 100
    max_image_frames: int = 100
    max_image_pixels: int = 40_000_000
    max_image_width: int = 12_000
    max_image_height: int = 12_000
    max_text_characters: int = 2_000_000
    max_tabular_rows: int = 100_000
    max_tabular_cells: int = 1_000_000
    max_structured_nodes: int = 500_000
    max_recursion_depth: int = 4
    max_normalized_output_bytes: int = 50 * 1024 * 1024
    max_archive_members: int = 2_000
    max_archive_uncompressed_bytes: int = 200 * 1024 * 1024
    max_archive_expansion_ratio: float = 100.0
    pdf_min_chars_per_page: int = 32
    pdf_mojibake_ratio: float = 0.30

    reminder_days_ahead: int = 7
    anomaly_threshold: float = 0.5
    demo_mode: bool = False
    sql_echo: bool = False

    @field_validator("confidence_threshold", "pdf_mojibake_ratio", "anomaly_threshold")
    @classmethod
    def _fraction(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("must be between 0 and 1")
        return value

    @field_validator(
        "worker_concurrency",
        "job_lease_seconds",
        "job_max_attempts",
        "job_retry_base_seconds",
        "embedding_batch_size",
        "max_upload_bytes",
        "max_request_bytes",
        "max_multipart_files",
        "max_multipart_fields",
        "max_json_bytes",
        "max_json_depth",
        "upload_concurrency",
        "idempotency_retention_hours",
        "recent_intakes_default_limit",
        "recent_intakes_max_limit",
        "worker_capability_heartbeat_seconds",
        "worker_capability_lease_seconds",
        "storage_reservation_grace_seconds",
        "max_pdf_pages",
        "max_image_frames",
        "max_image_pixels",
        "max_image_width",
        "max_image_height",
        "max_text_characters",
        "max_tabular_rows",
        "max_tabular_cells",
        "max_structured_nodes",
        "max_recursion_depth",
        "max_normalized_output_bytes",
        "max_archive_members",
        "max_archive_uncompressed_bytes",
        "reminder_days_ahead",
    )
    @classmethod
    def _positive_integer(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("max_archive_expansion_ratio")
    @classmethod
    def _positive_ratio(cls, value: float) -> float:
        if value < 1:
            raise ValueError("must be at least one")
        return value

    @field_validator("parser_request_timeout_seconds", "request_receive_timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("parser_socket_path")
    @classmethod
    def _absolute_parser_socket(cls, value: Path) -> Path:
        if not value.is_absolute() or value.suffix != ".sock":
            raise ValueError("parser_socket_path must be an absolute .sock path")
        return value

    @field_validator("intake_mode")
    @classmethod
    def _static_intake_mode(cls, value: IntakeMode) -> IntakeMode:
        return ensure_intake_mode_available(value)

    @field_validator("ocr_engine")
    @classmethod
    def _known_ocr_engine(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in OCR_ENGINES:
            raise ValueError("must be one of: vision_llm, yomitoku, paddleocr")
        return normalized

    @model_validator(mode="after")
    def _bounded_intake_settings_are_coherent(self) -> Settings:
        if self.recent_intakes_default_limit > self.recent_intakes_max_limit:
            raise ValueError(
                "recent_intakes_default_limit must not exceed recent_intakes_max_limit"
            )
        if self.worker_capability_heartbeat_seconds >= self.worker_capability_lease_seconds:
            raise ValueError(
                "worker_capability_heartbeat_seconds must be less than "
                "worker_capability_lease_seconds"
            )
        return self

    @model_validator(mode="after")
    def _loopback_browser_origins_only(self) -> Settings:
        """Keep the browser surface local even when a dev proxy is enabled."""

        for origin in self.allowed_browser_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError("browser_origins and api_url must use a loopback http(s) origin")
        return self

    @model_validator(mode="after")
    def _embedding_pin_is_complete(self) -> Settings:
        pin_values = (self.embed_model, self.embed_model_digest, self.embed_dim)
        if any(value is not None for value in pin_values) and not all(
            value is not None for value in pin_values
        ):
            raise ValueError("embed_model, embed_model_digest, and embed_dim must be set together")
        if not self.is_sqlite and pin_values != (
            EMBED_MODEL_TAG,
            EMBED_MODEL_DIGEST,
            EMBED_MODEL_DIMENSION,
        ):
            raise ValueError(
                "the PostgreSQL runtime uses the pinned embedding model, digest, and dimension; "
                "changing them requires a schema migration and full re-embedding"
            )
        return self

    @model_validator(mode="after")
    def _normalize_database_credentials(self) -> Settings:
        """Build a SQLAlchemy-safe URL and reject implicit PostgreSQL credentials."""

        if self.is_sqlite:
            if self.database_password is None:
                return self
            raise ValueError("database_password is not supported for SQLite")
        url = make_url(self.database_url)
        if self.database_password is not None:
            if url.password is not None:
                raise ValueError("database_url must not include a password with database_password")
            self.database_url = url.set(
                password=self.database_password.get_secret_value()
            ).render_as_string(hide_password=False)
            return self
        if not url.password:
            raise ValueError(
                "database_url must include a password or database_password must be set "
                "for PostgreSQL"
            )
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite+")

    @property
    def allowed_browser_origins(self) -> frozenset[str]:
        """Return exact local origins accepted from browser mutation requests."""

        candidates = (self.api_url, *self.browser_origins.split(","))
        return frozenset(
            _canonical_origin(candidate) for candidate in candidates if candidate.strip()
        )

    @property
    def api_host(self) -> str:
        """Return the exact host header expected by the loopback API."""

        return urlsplit(_canonical_origin(self.api_url)).netloc

    @property
    def required_models(self) -> tuple[str, ...]:
        """Models required by the currently configured local runtime."""
        models = [self.extract_model, self.router_model]
        if self.ocr_engine.strip().lower() == "vision_llm":
            models.insert(0, self.ocr_model)
        if self.embed_model:
            models.append(self.embed_model)
        return tuple(dict.fromkeys(model for model in models if model))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object."""
    return Settings()


def reset_settings_cache() -> None:
    """Test helper for environment overrides."""
    get_settings.cache_clear()


def _canonical_origin(value: str) -> str:
    """Normalize a configured origin and reject paths, credentials, and fragments."""

    parsed = urlsplit(value.strip())
    if (
        not parsed.scheme
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("browser origins must be bare origins such as http://127.0.0.1:8000")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
