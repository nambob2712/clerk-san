"""Async, local-only client for the Ollama HTTP API.

The wrapper deliberately exposes only the operations used by Clerk-san.  Keeping
the protocol here makes it straightforward to test the application without a
model server and gives callers actionable errors when a local model is absent.
"""

from __future__ import annotations

import asyncio
import base64
import enum
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from clerksan.config import Settings


class OllamaError(RuntimeError):
    """Base error for a local Ollama request."""


class OllamaUnavailable(OllamaError):
    """Raised when the configured local Ollama service cannot be reached."""


class OllamaResponseError(OllamaError):
    """Raised when Ollama returns an invalid or unsuccessful response."""


class OllamaModelMissing(OllamaResponseError):
    """Raised with the precise command needed to make a configured model available."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"Local Ollama model '{model}' is unavailable. Run: ollama pull {model}")


class ModelConfigurationError(OllamaError):
    """Raised when a model role has no configured local tag."""


class ModelRole(enum.StrEnum):
    OCR = "ocr"
    EXTRACT = "extract"
    ROUTER = "router"
    EMBED = "embed"


_ResultT = TypeVar("_ResultT")


class OllamaClient:
    """Thin async wrapper over Ollama's documented local HTTP endpoints.

    The constructor accepts :class:`Settings` directly so every endpoint remains
    configuration-driven.  It owns a lazily-created ``httpx.AsyncClient`` unless
    an already-managed client is supplied by an application lifespan or a test.
    """

    _CONNECT_TIMEOUT_SECONDS = 8.0
    _READ_TIMEOUT_SECONDS = 180.0

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        request_keep_alive: str | int | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._request_keep_alive = request_keep_alive

        endpoint = settings.ollama_url.rstrip("/")
        self._api_url = endpoint if endpoint.endswith("/api") else f"{endpoint}/api"

    async def aclose(self) -> None:
        """Close the internally-owned HTTP client, if one was created."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        images: list[bytes] | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        """Generate one non-streaming response, optionally with images or a schema."""
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        }
        if images:
            payload["images"] = [base64.b64encode(image).decode("ascii") for image in images]
        if json_schema is not None:
            payload["format"] = json_schema
        if self._request_keep_alive is not None:
            payload["keep_alive"] = self._request_keep_alive

        body = await self._request("POST", "/generate", model=model, payload=payload)
        response = body.get("response")
        if not isinstance(response, str):
            raise OllamaResponseError("Ollama /api/generate returned no text response")
        return response

    async def embeddings(self, model: str, texts: list[str]) -> list[list[float]]:
        """Embed a non-empty text batch through ``POST /api/embed``."""
        if not texts:
            return []
        if any(not isinstance(text, str) for text in texts):
            raise TypeError("texts must contain only strings")

        body = await self._request(
            "POST",
            "/embed",
            model=model,
            payload={
                "model": model,
                "input": texts,
                "truncate": False,
                **(
                    {"keep_alive": self._request_keep_alive}
                    if self._request_keep_alive is not None
                    else {}
                ),
            },
        )
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise OllamaResponseError("Ollama /api/embed returned an unexpected embedding count")

        normalized: list[list[float]] = []
        for vector in embeddings:
            if not isinstance(vector, list) or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector
            ):
                raise OllamaResponseError("Ollama /api/embed returned a non-numeric vector")
            normalized.append([float(value) for value in vector])
        return normalized

    async def list_models(self) -> list[dict[str, Any]]:
        """Return locally installed model entries from ``GET /api/tags``."""
        body = await self._request("GET", "/tags")
        return self._model_list(body, "/api/tags")

    async def loaded_models(self) -> list[dict[str, Any]]:
        """Return resident model entries from ``GET /api/ps``."""
        body = await self._request("GET", "/ps")
        return self._model_list(body, "/api/ps")

    async def show_model(self, model: str) -> dict[str, Any]:
        """Return local model metadata, including the resolved Modelfile."""

        return await self._request("POST", "/show", model=model, payload={"name": model})

    async def unload(self, model: str) -> None:
        """Immediately release a loaded model using the documented ``keep_alive=0``."""
        await self._request(
            "POST",
            "/generate",
            model=model,
            payload={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        model: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._client()
        try:
            response = await client.request(method, f"{self._api_url}{path}", json=payload)
        except httpx.TimeoutException as error:
            raise OllamaUnavailable(
                f"Timed out contacting local Ollama at {self._api_url}: {error.__class__.__name__}"
            ) from error
        except httpx.HTTPError as error:
            raise OllamaUnavailable(
                f"Could not contact local Ollama at {self._api_url}: {error}"
            ) from error

        if response.status_code == 404 and model:
            raise OllamaModelMissing(model)
        if response.is_error:
            detail = _response_detail(response)
            if model and _looks_like_missing_model(detail):
                raise OllamaModelMissing(model)
            raise OllamaResponseError(
                f"Ollama {method} {path} failed with HTTP {response.status_code}: {detail}"
            )

        try:
            body = response.json()
        except ValueError as error:
            raise OllamaResponseError(f"Ollama {method} {path} returned invalid JSON") from error
        if not isinstance(body, dict):
            raise OllamaResponseError(f"Ollama {method} {path} returned a non-object JSON response")
        if isinstance(body.get("error"), str) and body["error"]:
            if model and _looks_like_missing_model(body["error"]):
                raise OllamaModelMissing(model)
            raise OllamaResponseError(f"Ollama {method} {path} reported: {body['error']}")
        return body

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self._CONNECT_TIMEOUT_SECONDS,
                    read=self._READ_TIMEOUT_SECONDS,
                    write=self._READ_TIMEOUT_SECONDS,
                    pool=self._CONNECT_TIMEOUT_SECONDS,
                )
            )
        return self._http_client

    @staticmethod
    def _model_list(body: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
        models = body.get("models")
        if not isinstance(models, list) or any(not isinstance(entry, dict) for entry in models):
            raise OllamaResponseError(f"Ollama {endpoint} returned an invalid models list")
        return models


class ModelManager:
    """Keep at most one large inference model resident on the target machine."""

    large_model_bytes = 2 * 1024 * 1024 * 1024

    def __init__(self, client: OllamaClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def ensure_loaded(self, role: ModelRole) -> str:
        """Return an installed role model after evicting competing large models.

        ``/api/generate`` or ``/api/embed`` performs the actual lazy load.  Doing the
        eviction first prevents a model swap from briefly retaining two multi-GB models.
        """
        model = self.model_for(role)

        async def verify_and_evict() -> str:
            installed = await self.client.list_models()
            if not _model_is_listed(model, installed):
                raise OllamaModelMissing(model)

            # Embedding models are deliberately not treated as one of the large stage
            # models. If a future embedding selection exceeds the threshold, revisit
            # this residency policy explicitly.
            if role is ModelRole.EMBED:
                return model

            loaded = await self.client.loaded_models()
            eviction_targets = [
                _model_name(entry)
                for entry in loaded
                if _is_large_model(entry, self.large_model_bytes)
                and not _same_model(_model_name(entry), model)
            ]
            for loaded_model in eviction_targets:
                if loaded_model:
                    await self.client.unload(loaded_model)
            return model

        return await _retry_once(verify_and_evict)

    def model_for(self, role: ModelRole) -> str:
        """Resolve a role to its configured local tag."""
        configured: str | None
        match role:
            case ModelRole.OCR:
                configured = self.settings.ocr_model
            case ModelRole.EXTRACT:
                configured = self.settings.extract_model
            case ModelRole.ROUTER:
                configured = self.settings.router_model
            case ModelRole.EMBED:
                configured = self.settings.embed_model
            case _:
                configured = None
        if not configured or not configured.strip():
            raise ModelConfigurationError(f"No local model is configured for role '{role.value}'")
        return configured.strip()


async def _retry_once(operation: Callable[[], Awaitable[_ResultT]]) -> _ResultT:
    """Retry a transient local-service failure once without masking model errors."""
    try:
        return await operation()
    except OllamaUnavailable:
        await asyncio.sleep(0.1)
        return await operation()


def _response_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or "no response body"
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return str(body)[:500]


def _looks_like_missing_model(detail: str) -> bool:
    lowered = detail.lower()
    return "model" in lowered and ("not found" in lowered or "not available" in lowered)


def _model_name(entry: dict[str, Any]) -> str:
    name = entry.get("name") or entry.get("model")
    return name if isinstance(name, str) else ""


def _same_model(left: str, right: str) -> bool:
    return _canonical_model_name(left) == _canonical_model_name(right)


def _canonical_model_name(model: str) -> str:
    model = model.strip()
    return model[:-7] if model.endswith(":latest") else model


def _model_is_listed(model: str, entries: list[dict[str, Any]]) -> bool:
    return any(_same_model(_model_name(entry), model) for entry in entries)


def _is_large_model(entry: dict[str, Any], threshold: int) -> bool:
    size = entry.get("size")
    if isinstance(size, int) and not isinstance(size, bool):
        return size > threshold
    if isinstance(size, float) and size.is_integer():
        return int(size) > threshold
    # `/api/ps` includes size. Treat malformed entries as large so bad server data cannot
    # accidentally permit two unknown-size models to remain resident.
    return True
