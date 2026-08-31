from __future__ import annotations

import hashlib
import io
import tempfile
import zipfile

import pytest
from PIL import Image

from clerksan.ingest.adapters.pptx import PptxAdapter
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded, UnsafeArchiveMemberError
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _normalize(raw: bytes, *, limits: IngestLimits | None = None):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return PptxAdapter(limits=limits).normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename="slides.pptx",
            ),
            AdapterContext(
                adapter_key="universal.pptx",
                metadata={"detected_type": "pptx"},
            ),
        )


def _slide(text: str, *, table: list[list[str]] | None = None) -> str:
    table_xml = ""
    if table:
        rows = []
        for row in table:
            cells = "".join(
                f"<a:tc><a:txBody><a:p><a:r><a:t>{value}</a:t></a:r></a:p></a:txBody></a:tc>"
                for value in row
            )
            rows.append(f"<a:tr>{cells}</a:tr>")
        table_xml = f"<a:graphicFrame><a:tbl>{''.join(rows)}</a:tbl></a:graphicFrame>"
    return (
        '<p:sld xmlns:p="urn:p" xmlns:a="urn:a">'
        f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t>"
        f"</a:r></a:p></p:txBody></p:sp>{table_xml}</p:spTree></p:cSld></p:sld>"
    )


def _pptx(
    *,
    slides: dict[int, str],
    order: list[int] | None = None,
    extras: list[tuple[str, bytes | str]] | None = None,
    content_type: str = _CONTENT_TYPE,
) -> bytes:
    ordered = order or sorted(slides)
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/ppt/presentation.xml" ContentType="{content_type}"/>'
        "</Types>"
    )
    slide_ids = "".join(
        f'<p:sldId id="{255 + ordinal}" r:id="rId{number}"/>'
        for ordinal, number in enumerate(ordered, start=1)
    )
    presentation = (
        f'<p:presentation xmlns:p="urn:p" xmlns:r="{_DOC_REL_NS}">'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst></p:presentation>"
    )
    relationships = "".join(
        f'<Relationship Id="rId{number}" Type="{_DOC_REL_NS}/slide" '
        f'Target="slides/slide{number}.xml"/>'
        for number in ordered
    )
    rels = f'<Relationships xmlns="{_REL_NS}">{relationships}</Relationships>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", rels)
        for number, xml in slides.items():
            archive.writestr(f"ppt/slides/slide{number}.xml", xml)
        for name, data in extras or []:
            archive.writestr(name, data)
    return output.getvalue()


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 5), "blue").save(output, "PNG")
    return output.getvalue()


def test_pptx_preserves_relationship_order_tables_media_and_locators() -> None:
    raw = _pptx(
        slides={
            1: _slide("First"),
            2: _slide("Second", table=[["Name", "Formula"], ["A", "=1+1"]]),
        },
        order=[2, 1],
        extras=[("ppt/media/image1.png", _png())],
    )
    result = _normalize(raw)

    assert result.markdown_body.index("Second") < result.markdown_body.index("First")
    assert result.tables[0].header == ["Name", "Formula"]
    assert result.tables[0].rows == [["A", "=1+1"]]
    assert result.tables[0].source_location == "pptx/slide/1/table/1"
    assert result.metadata.page_provenance[0].endswith("slide2.xml")
    assert result.metadata.extra["media"][0]["width"] == 4
    assert result.metadata.extra["formula_evaluation"] == "disabled"


def test_pptx_rejects_macros_external_relationships_and_embedded_content() -> None:
    macro_type = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
    with pytest.raises(ValueError, match="macro|active"):
        _normalize(_pptx(slides={1: _slide("safe")}, content_type=macro_type))

    external = (
        f'<Relationships xmlns="{_REL_NS}"><Relationship Id="rId9" '
        f'Type="{_DOC_REL_NS}/hyperlink" Target="https://example.invalid" '
        'TargetMode="External"/></Relationships>'
    )
    with pytest.raises(UnsafeArchiveMemberError, match="external"):
        _normalize(
            _pptx(
                slides={1: _slide("safe")},
                extras=[("ppt/slides/_rels/slide1.xml.rels", external)],
            )
        )

    with pytest.raises((ValueError, UnsafeArchiveMemberError), match="active|embedded"):
        _normalize(
            _pptx(
                slides={1: _slide("safe")},
                extras=[("ppt/embeddings/oleObject1.bin", b"active")],
            )
        )


def test_pptx_xml_and_aggregate_cell_bounds_fail_closed() -> None:
    entity_slide = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
    with pytest.raises(UnsafeArchiveMemberError, match="entities|doctypes"):
        _normalize(_pptx(slides={1: entity_slide}))

    table = _slide("body", table=[["one", "two"]])
    with pytest.raises(ResourceLimitExceeded, match="max_tabular_cells"):
        _normalize(
            _pptx(slides={1: table}),
            limits=IngestLimits(max_tabular_cells=1),
        )
