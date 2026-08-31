from __future__ import annotations

import hashlib
import tempfile

import pytest

from clerksan.ingest.adapters.structured import StructuredAdapter
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource


def _normalize(raw: bytes, detected_type: str, *, limits: IngestLimits | None = None):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return StructuredAdapter(limits=limits).normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename=f"source.{detected_type}",
            ),
            AdapterContext(
                adapter_key="universal.structured",
                metadata={"detected_type": detected_type},
            ),
        )


def test_json_array_becomes_literal_table_without_formula_execution() -> None:
    result = _normalize(
        b'[{"name":"one","formula":"=1+1"},{"name":"two","formula":"@cmd"}]',
        "json",
    )
    assert result.embeddable is False
    assert result.tables[0].header == ["name", "formula"]
    assert result.tables[0].rows == [["one", "=1+1"], ["two", "@cmd"]]
    assert result.tables[0].source_location == "array/root"


def test_jsonl_keeps_each_line_as_one_row() -> None:
    result = _normalize(b'{"a":1}\n{"a":2}\n', "jsonl")
    assert result.tables[0].rows == [["1"], ["2"]]


@pytest.mark.parametrize(
    "payload",
    [
        b"value: &shared [1, 2]\ncopy: *shared\n",
        b"value: !!python/object/apply:os.system ['id']\n",
    ],
)
def test_yaml_aliases_and_custom_constructors_are_rejected(payload: bytes) -> None:
    with pytest.raises(ValueError, match="forbidden|invalid yaml"):
        _normalize(payload, "yaml")


def test_xml_entities_are_rejected_and_svg_markup_is_escaped() -> None:
    with pytest.raises(ValueError, match="entities"):
        _normalize(b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>', "xml")

    result = _normalize(
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>&lt;safe&gt;</text><script>x</script></svg>',
        "svg",
    )
    assert "&lt;safe&gt;" in result.markdown_body
    assert "<script>" not in result.markdown_body


def test_structured_depth_and_node_limits_fail_closed() -> None:
    with pytest.raises(ResourceLimitExceeded, match="max_recursion_depth"):
        _normalize(
            b'{"a":{"b":{"c":1}}}',
            "json",
            limits=IngestLimits(max_recursion_depth=2),
        )
    with pytest.raises(ResourceLimitExceeded, match="max_structured_nodes"):
        _normalize(
            b"[1,2,3]",
            "json",
            limits=IngestLimits(max_structured_nodes=2),
        )
