from __future__ import annotations

import hashlib
import tempfile

import pytest

from clerksan.ingest.adapters.rtf import RtfAdapter
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource


def _normalize(raw: bytes, *, limits: IngestLimits | None = None):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return RtfAdapter(limits=limits).normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename="document.rtf",
            ),
            AdapterContext(
                adapter_key="universal.rtf",
                metadata={"detected_type": "rtf"},
            ),
        )


def test_rtf_extracts_unicode_cp932_and_escaped_literals_as_inert_text() -> None:
    raw = (
        rb"{\rtf1\ansi\ansicpg932{\fonttbl{\f0 Arial;}}"
        rb"Hello \u26085? \'93\'fa\'96\'7b \{tag\}\par next}"
    )
    result = _normalize(raw)

    assert result.markdown_body == "Hello 日 日本 {tag}\nnext"
    assert result.metadata.extra["paragraph_locators"] == [
        "rtf/paragraph/1",
        "rtf/paragraph/2",
    ]
    assert result.metadata.extra["active_content"] == "rejected"


def test_rtf_markup_is_escaped_and_format_destinations_are_not_emitted() -> None:
    result = _normalize(rb"{\rtf1\ansi{\info hidden}<safe>\tab value}")

    assert result.markdown_body == "&lt;safe&gt;\tvalue"
    assert "hidden" not in result.markdown_body


@pytest.mark.parametrize(
    "control",
    ["field", "fldinst", "object", "objdata", "bin4", "htmltag", "include"],
)
def test_rtf_active_controls_are_rejected(control: str) -> None:
    with pytest.raises(ValueError, match="active control"):
        _normalize(f"{{\\rtf1\\ansi \\{control} unsafe}}".encode())


@pytest.mark.parametrize(
    "raw",
    [
        b"not rtf",
        rb"{\rtf1\ansi missing close",
        rb"{\rtf1\ansi extra}}",
        rb"{\rtf1\ansi \u-10179?}",
    ],
)
def test_malformed_rtf_fails_closed(raw: bytes) -> None:
    with pytest.raises(ValueError):
        _normalize(raw)


def test_rtf_depth_node_character_and_output_limits_fail_closed() -> None:
    with pytest.raises(ResourceLimitExceeded, match="max_recursion_depth"):
        _normalize(
            rb"{\rtf1\ansi{{deep}}}",
            limits=IngestLimits(max_recursion_depth=2),
        )
    with pytest.raises(ResourceLimitExceeded, match="max_structured_nodes"):
        _normalize(
            rb"{\rtf1\ansi text}",
            limits=IngestLimits(max_structured_nodes=2),
        )
    with pytest.raises(ResourceLimitExceeded, match="max_text_characters"):
        _normalize(
            rb"{\rtf1\ansi 12345}",
            limits=IngestLimits(max_text_characters=4),
        )
    with pytest.raises(ResourceLimitExceeded, match="max_normalized_output_bytes"):
        _normalize(
            rb"{\rtf1\ansi&lt;}",
            limits=IngestLimits(max_normalized_output_bytes=3),
        )
