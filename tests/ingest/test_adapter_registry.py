from __future__ import annotations

import pytest

from clerksan.ingest.adapters.base import (
    AdapterRegistry,
    DuplicateAdapterRegistrationError,
    NoAdapterError,
)
from clerksan.ingest.filetype import FileType
from clerksan.ingest.normalized import DocMetadata, NormalizedDocument


class FakeAdapter:
    def __init__(self, *supported_types: FileType) -> None:
        self.supported_types = supported_types

    async def adapt(self, raw: bytes, meta: DocMetadata) -> NormalizedDocument:
        return NormalizedDocument(markdown_body=raw.decode(), metadata=meta)


def test_registry_dispatches_by_detected_type() -> None:
    registry = AdapterRegistry()
    image = FakeAdapter(FileType.PNG, FileType.JPEG)
    document = FakeAdapter(FileType.PDF)

    registry.register(image)
    registry.register(document)

    assert registry.get(FileType.PNG) is image
    assert registry.get(FileType.PDF) is document
    assert registry.registered_types == {FileType.PNG, FileType.JPEG, FileType.PDF}


def test_registry_refuses_ambiguous_registrations() -> None:
    registry = AdapterRegistry()
    registry.register(FakeAdapter(FileType.PNG))

    with pytest.raises(DuplicateAdapterRegistrationError) as error:
        registry.register(FakeAdapter(FileType.PNG, FileType.JPEG))

    assert error.value.file_type is FileType.PNG
    assert registry.registered_types == {FileType.PNG}


def test_registry_rejects_invalid_declarations_and_unknown_types() -> None:
    registry = AdapterRegistry()
    with pytest.raises(ValueError, match="at least one"):
        registry.register(FakeAdapter())
    with pytest.raises(ValueError, match="more than once"):
        registry.register(FakeAdapter(FileType.PDF, FileType.PDF))

    invalid = FakeAdapter(FileType.PDF)
    invalid.supported_types = ("pdf",)  # type: ignore[assignment]
    with pytest.raises(TypeError, match="FileType"):
        registry.register(invalid)

    with pytest.raises(NoAdapterError) as error:
        registry.get(FileType.WEBP)
    assert error.value.file_type is FileType.WEBP
