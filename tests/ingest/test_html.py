from __future__ import annotations

import hashlib
import tempfile

import pytest

from clerksan.ingest.adapters.html import HtmlAdapter
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource


def _normalize(raw: bytes, *, limits: IngestLimits | None = None):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return HtmlAdapter(limits=limits).normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename="source.html",
            ),
            AdapterContext(
                adapter_key="universal.html",
                metadata={"detected_type": "html", "canonical_mime": "text/html"},
            ),
        )


def test_html_active_markup_and_remote_references_stay_inert() -> None:
    result = _normalize(
        b"""<html><body><iframe src="https://example.invalid/x">fallback</iframe>
        <p onclick="steal()">&lt;safe&gt;</p><script>do_bad()</script></body></html>"""
    )

    assert result.markdown_body == "fallback &lt;safe&gt;"
    assert "do_bad" not in result.markdown_body
    assert result.metadata.extra["active_element_count"] == 2
    assert result.metadata.extra["event_attribute_count"] == 1
    assert result.metadata.extra["external_reference_count"] == 1
    assert result.metadata.extra["remote_fetch"] == "disabled"


def test_html_tables_preserve_literal_cells_and_locators() -> None:
    result = _normalize(
        b"<table><tr><th>Name</th><th>Formula</th></tr>"
        b"<tr><td>A</td><td>=1+1</td></tr></table><p>Summary</p>"
    )

    assert result.embeddable is False
    assert result.markdown_body == "Summary"
    assert result.tables[0].header == ["Name", "Formula"]
    assert result.tables[0].rows == [["A", "=1+1"]]
    assert result.tables[0].source_location.startswith("html/table/1/")
    assert result.metadata.extra["text_locators"][0]["locator"] == "html/text/1"


def test_html_aggregate_bounds_fail_closed() -> None:
    with pytest.raises(ResourceLimitExceeded, match="max_recursion_depth"):
        _normalize(
            b"<div><span><b>deep</b></span></div>",
            limits=IngestLimits(max_recursion_depth=2),
        )
    with pytest.raises(ResourceLimitExceeded, match="max_structured_nodes"):
        _normalize(
            b"<p a='1' b='2'>text</p>",
            limits=IngestLimits(max_structured_nodes=2),
        )
    with pytest.raises(ResourceLimitExceeded, match="max_normalized_output_bytes"):
        _normalize(
            b"<table><tr><td>oversized</td></tr></table>",
            limits=IngestLimits(max_normalized_output_bytes=4),
        )
