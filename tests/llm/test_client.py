from __future__ import annotations

import json

import httpx
import pytest
import respx

from clerksan.config import Settings
from clerksan.llm.client import (
    ModelManager,
    ModelRole,
    OllamaClient,
    OllamaModelMissing,
    OllamaUnavailable,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        ollama_url="http://ollama.test",
        ocr_model="vision:7b",
        extract_model="extract:7b",
        router_model="router:3b",
        embed_model="embed:small",
        embed_model_digest="a" * 64,
        embed_dim=384,
    )


@pytest.mark.asyncio
@respx.mock
async def test_generate_encodes_images_and_requests_schema(settings: Settings) -> None:
    route = respx.post("http://ollama.test/api/generate").mock(
        return_value=httpx.Response(200, json={"response": '{"ok": true}'})
    )
    client = OllamaClient(settings)

    response = await client.generate(
        "vision:7b",
        "read this",
        images=[b"image-bytes"],
        json_schema={"type": "object"},
    )
    await client.aclose()

    assert response == '{"ok": true}'
    request_body = json.loads(route.calls[0].request.content)
    assert request_body["model"] == "vision:7b"
    assert request_body["stream"] is False
    assert request_body["images"] == ["aW1hZ2UtYnl0ZXM="]
    assert request_body["format"] == {"type": "object"}


@pytest.mark.asyncio
@respx.mock
async def test_generate_can_apply_a_benchmark_only_keep_alive(settings: Settings) -> None:
    route = respx.post("http://ollama.test/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )
    client = OllamaClient(settings, request_keep_alive="5m")

    assert await client.generate("vision:7b", "read this") == "ok"
    await client.aclose()

    assert json.loads(route.calls[0].request.content)["keep_alive"] == "5m"


@pytest.mark.asyncio
@respx.mock
async def test_embeddings_can_apply_a_benchmark_only_keep_alive(settings: Settings) -> None:
    route = respx.post("http://ollama.test/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0]]})
    )
    client = OllamaClient(settings, request_keep_alive="5m")

    assert await client.embeddings("embed:small", ["needle"]) == [[1.0]]
    await client.aclose()

    assert json.loads(route.calls[0].request.content)["keep_alive"] == "5m"


@pytest.mark.asyncio
@respx.mock
async def test_show_model_returns_the_local_modelfile(settings: Settings) -> None:
    respx.post("http://ollama.test/api/show").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelfile": "FROM sha256-deadbeef",
                "details": {"quantization_level": "Q4_K_M"},
            },
        )
    )
    client = OllamaClient(settings)

    assert await client.show_model("embed:small") == {
        "modelfile": "FROM sha256-deadbeef",
        "details": {"quantization_level": "Q4_K_M"},
    }
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_list_models_loaded_models_and_embeddings(settings: Settings) -> None:
    respx.get("http://ollama.test/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "extract:7b"}]})
    )
    respx.get("http://ollama.test/api/ps").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "extract:7b", "size": 3_000}]})
    )
    respx.post("http://ollama.test/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1, 2.5], [0.25, -1]]})
    )
    client = OllamaClient(settings)

    assert await client.list_models() == [{"name": "extract:7b"}]
    assert await client.loaded_models() == [{"name": "extract:7b", "size": 3_000}]
    assert await client.embeddings("embed:small", ["a", "b"]) == [[1.0, 2.5], [0.25, -1.0]]
    assert await client.embeddings("embed:small", []) == []
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_missing_model_names_exact_pull_command(settings: Settings) -> None:
    respx.post("http://ollama.test/api/generate").mock(
        return_value=httpx.Response(404, json={"error": "model not found"})
    )
    client = OllamaClient(settings)

    with pytest.raises(OllamaModelMissing, match=r"ollama pull unavailable:tag"):
        await client.generate("unavailable:tag", "hello")
    await client.aclose()


@pytest.mark.asyncio
async def test_connection_errors_become_local_service_errors(settings: Settings) -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    client = OllamaClient(settings, http_client=http_client)

    with pytest.raises(OllamaUnavailable, match="Could not contact local Ollama"):
        await client.list_models()
    await http_client.aclose()


class _FakeOllama:
    def __init__(self) -> None:
        self.unloaded: list[str] = []

    async def list_models(self) -> list[dict[str, object]]:
        return [{"name": "extract:7b"}, {"name": "vision:7b"}, {"name": "router:3b"}]

    async def loaded_models(self) -> list[dict[str, object]]:
        return [
            {"name": "vision:7b", "size": 3 * 1024 * 1024 * 1024},
            {"name": "old:7b", "size": 4 * 1024 * 1024 * 1024},
        ]

    async def unload(self, model: str) -> None:
        self.unloaded.append(model)


@pytest.mark.asyncio
async def test_model_manager_evicts_all_other_large_models(settings: Settings) -> None:
    fake = _FakeOllama()
    manager = ModelManager(fake, settings)  # type: ignore[arg-type]

    model = await manager.ensure_loaded(ModelRole.EXTRACT)

    assert model == "extract:7b"
    assert fake.unloaded == ["vision:7b", "old:7b"]


@pytest.mark.asyncio
async def test_model_manager_keeps_memory_intact_when_requested_tag_is_missing(
    settings: Settings,
) -> None:
    class MissingFake(_FakeOllama):
        async def list_models(self) -> list[dict[str, object]]:
            return []

    fake = MissingFake()
    manager = ModelManager(fake, settings)  # type: ignore[arg-type]

    with pytest.raises(OllamaModelMissing, match=r"ollama pull extract:7b"):
        await manager.ensure_loaded(ModelRole.EXTRACT)
    assert fake.unloaded == []
