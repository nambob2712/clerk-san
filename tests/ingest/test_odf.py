from __future__ import annotations

import hashlib
import io
import tempfile
import zipfile

import pytest

from clerksan.ingest.adapters.odf import OdfAdapter
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded, UnsafeArchiveMemberError
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource

_MIMES = {
    "odt": "application/vnd.oasis.opendocument.text",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
}
_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"'
)


def _normalize(raw: bytes, detected_type: str, *, limits: IngestLimits | None = None):
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return OdfAdapter(limits=limits).normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename=f"document.{detected_type}",
            ),
            AdapterContext(
                adapter_key=f"universal.{detected_type}",
                metadata={"detected_type": detected_type},
            ),
        )


def _odf(
    detected_type: str,
    content_body: str,
    *,
    mimetype: str | None = None,
    manifest: str = '<manifest xmlns="urn:manifest"/>',
    extras: list[tuple[str, str | bytes]] | None = None,
) -> bytes:
    content = f"<office:document-content {_NS}>{content_body}</office:document-content>"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "mimetype",
            mimetype or _MIMES[detected_type],
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("content.xml", content)
        archive.writestr("META-INF/manifest.xml", manifest)
        for name, data in extras or []:
            archive.writestr(name, data)
    return output.getvalue()


def test_ods_preserves_formula_literals_repetitions_and_table_locator() -> None:
    body = """
    <office:body><office:spreadsheet><table:table table:name="Transactions">
      <table:table-header-rows><table:table-row>
        <table:table-cell><text:p>Name</text:p></table:table-cell>
        <table:table-cell><text:p>Formula</text:p></table:table-cell>
        <table:table-cell><text:p>Raw</text:p></table:table-cell>
      </table:table-row></table:table-header-rows>
      <table:table-row table:number-rows-repeated="2">
        <table:table-cell><text:p>A</text:p></table:table-cell>
        <table:table-cell table:formula="of:=1+1"><text:p>2</text:p></table:table-cell>
        <table:table-cell office:value="3.5"/>
      </table:table-row>
    </table:table></office:spreadsheet></office:body>
    """
    result = _normalize(_odf("ods", body), "ods")

    assert result.embeddable is False
    assert result.tables[0].header == ["Name", "Formula", "Raw"]
    assert result.tables[0].rows == [
        ["A", "of:=1+1", "3.5"],
        ["A", "of:=1+1", "3.5"],
    ]
    assert result.tables[0].source_location == "odf/table/1/Transactions"
    assert result.metadata.extra["formula_cell_count"] == 2
    assert result.metadata.extra["formula_evaluation"] == "disabled"


def test_odt_and_odp_keep_narrative_and_page_provenance_without_table_duplication() -> None:
    odt_body = """
    <office:body><office:text><text:h>Heading</text:h>
      <table:table table:name="T"><table:table-row>
        <table:table-cell><text:p>Cell</text:p></table:table-cell>
      </table:table-row></table:table><text:p>Tail</text:p>
    </office:text></office:body>
    """
    odt = _normalize(_odf("odt", odt_body), "odt")
    assert odt.markdown_body == "Heading\n\nTail"
    assert odt.tables[0].header == ["Cell"]

    odp_body = """
    <office:body><office:presentation>
      <draw:page draw:name="one"><text:p>First slide</text:p></draw:page>
      <draw:page draw:name="two"><text:p>Second slide</text:p></draw:page>
    </office:presentation></office:body>
    """
    odp = _normalize(_odf("odp", odp_body), "odp")
    assert "Slide 1\nFirst slide" in odp.markdown_body
    assert odp.metadata.page_provenance == ["odf/page/1", "odf/page/2"]


def test_odf_rejects_external_links_scripts_encryption_and_macro_members() -> None:
    external = (
        "<office:body><office:text><draw:image "
        'xlink:href="https://example.invalid/x"/></office:text></office:body>'
    )
    with pytest.raises(UnsafeArchiveMemberError, match="external"):
        _normalize(_odf("odt", external), "odt")

    script = "<office:body><office:text><office:scripts/></office:text></office:body>"
    with pytest.raises(UnsafeArchiveMemberError, match="active"):
        _normalize(_odf("odt", script), "odt")

    encrypted_manifest = '<manifest xmlns="urn:manifest"><encryption-data/></manifest>'
    with pytest.raises(UnsafeArchiveMemberError, match="encrypted"):
        _normalize(
            _odf("odt", "<office:body><office:text/></office:body>", manifest=encrypted_manifest),
            "odt",
        )

    with pytest.raises(UnsafeArchiveMemberError, match="active|embedded"):
        _normalize(
            _odf(
                "odt",
                "<office:body><office:text/></office:body>",
                extras=[("Basic/Standard/script.xml", "<x/>")],
            ),
            "odt",
        )


def test_odf_entities_mimetype_and_repeat_bounds_fail_closed() -> None:
    entity = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
    with pytest.raises(UnsafeArchiveMemberError, match="entities|doctypes"):
        _normalize(_odf("odt", entity), "odt")

    with pytest.raises(ValueError, match="mimetype"):
        _normalize(
            _odf("odt", "<office:body><office:text/></office:body>", mimetype=_MIMES["ods"]),
            "odt",
        )

    repeated = """
    <office:body><office:spreadsheet><table:table>
      <table:table-row><table:table-cell table:number-columns-repeated="2">
        <text:p>x</text:p></table:table-cell></table:table-row>
    </table:table></office:spreadsheet></office:body>
    """
    with pytest.raises(ResourceLimitExceeded, match="max_tabular_cells"):
        _normalize(
            _odf("ods", repeated),
            "ods",
            limits=IngestLimits(max_tabular_cells=1),
        )
