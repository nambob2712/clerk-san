from __future__ import annotations

import hashlib
import tempfile

import pytest

from clerksan.ingest.adapters.text import TextAdapter
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource


def _normalize(raw: bytes, detected_type: str = "txt"):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return TextAdapter().normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename=f"notes.{detected_type}",
                mime_type="text/plain",
            ),
            AdapterContext(
                adapter_key="universal.text",
                metadata={"detected_type": detected_type, "canonical_mime": "text/plain"},
            ),
        )


@pytest.mark.parametrize(
    ("raw", "charset"),
    [
        ("日本語".encode(), "utf-8"),
        ("日本語".encode("utf-16"), "utf-16"),
        ("日本語".encode("cp932"), "cp932"),
    ],
)
def test_text_adapter_decodes_supported_encodings_and_escapes_markup(
    raw: bytes, charset: str
) -> None:
    result = _normalize(raw)
    assert result.metadata.charset == charset
    assert "日本語" in result.markdown_body


def test_text_adapter_keeps_markup_inert() -> None:
    result = _normalize(b"<script>alert(1)</script> & text")
    assert result.markdown_body == "&lt;script&gt;alert(1)&lt;/script&gt; &amp; text"


def test_text_adapter_rejects_binary_and_character_overflow() -> None:
    with pytest.raises(ValueError, match="NUL"):
        _normalize(b"safe\x00unsafe")

    raw = b"abcd"
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        with pytest.raises(ResourceLimitExceeded, match="max_text_characters"):
            TextAdapter(limits=IngestLimits(max_text_characters=3)).normalize(
                ReadOnlySource(
                    handle.fileno(),
                    hashlib.sha256(raw).hexdigest(),
                    filename="notes.txt",
                ),
                AdapterContext(
                    adapter_key="universal.text",
                    metadata={"detected_type": "txt"},
                ),
            )
