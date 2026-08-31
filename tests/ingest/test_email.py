from __future__ import annotations

import hashlib
import io
import tempfile
import zipfile
from email.message import EmailMessage

import pytest

from clerksan.ingest.adapters.email import EmailAdapter
from clerksan.ingest.limits import IngestLimits, ResourceLimitExceeded, UnsafeArchiveMemberError
from clerksan.ingest.parser_runner import AdapterContext, ReadOnlySource


def _normalize(message: EmailMessage, *, limits: IngestLimits | None = None):
    raw = message.as_bytes()
    with tempfile.TemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        return EmailAdapter(limits=limits).normalize(
            ReadOnlySource(
                handle.fileno(),
                hashlib.sha256(raw).hexdigest(),
                filename="message.eml",
            ),
            AdapterContext(
                adapter_key="universal.eml",
                metadata={"detected_type": "eml", "canonical_mime": "message/rfc822"},
            ),
        )


def _zip_text() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("inside.txt", "nested safe")
    return output.getvalue()


def test_multipart_alternative_prefers_plain_but_validates_html_as_inert() -> None:
    message = EmailMessage()
    message["Subject"] = "テスト"
    message["From"] = "sender@example.invalid"
    message.set_content("plain body")
    message.add_alternative('<p onclick="x()">html body</p><script>bad()</script>', subtype="html")

    result = _normalize(message)

    assert "plain body" in result.markdown_body
    assert "html body" not in result.markdown_body
    assert "bad()" not in result.markdown_body
    assert result.metadata.extra["headers"]["subject"] == "テスト"
    assert result.metadata.extra["html_active_element_count"] == 1
    assert result.metadata.extra["part_count"] == 3


def test_nested_archive_attachment_uses_shared_budget_and_member_locators() -> None:
    message = EmailMessage()
    message.set_content("cover")
    message.add_attachment(
        _zip_text(),
        maintype="application",
        subtype="zip",
        filename="bundle.zip",
    )

    result = _normalize(message)

    assert result.metadata.extra["attachment_count"] == 1
    assert result.metadata.extra["inspected_member_count"] == 2
    assert "nested safe" in result.markdown_body
    members = result.metadata.extra["attachments"]
    assert members[0]["name"] == "bundle.zip"
    assert members[1]["name"] == "inside.txt"
    assert all(member["locator"].startswith("email/") for member in members)


def test_html_email_tables_are_preserved_as_literal_structured_data() -> None:
    message = EmailMessage()
    message.set_content(
        "<table><tr><th>Name</th><th>Formula</th></tr><tr><td>A</td><td>=1+1</td></tr></table>",
        subtype="html",
    )

    result = _normalize(message)

    assert result.embeddable is False
    assert result.tables[0].header == ["Name", "Formula"]
    assert result.tables[0].rows == [["A", "=1+1"]]
    assert result.tables[0].source_location == "email/part/1/html_table/1"


def test_nested_message_attachment_is_recursively_inspected() -> None:
    nested = EmailMessage()
    nested["Subject"] = "nested"
    nested.set_content("nested body")
    outer = EmailMessage()
    outer.set_content("outer body")
    outer.add_attachment(nested)

    result = _normalize(outer)

    assert result.metadata.extra["attachment_count"] == 1
    assert "nested body" in result.markdown_body
    assert any(member["kind"] == "email" for member in result.metadata.extra["attachments"])


def test_duplicate_names_and_unsafe_attachment_poison_the_email() -> None:
    duplicate = EmailMessage()
    duplicate.set_content("body")
    duplicate.add_attachment(b"one", maintype="text", subtype="plain", filename="Report.txt")
    duplicate.add_attachment(b"two", maintype="text", subtype="plain", filename="report.TXT")
    with pytest.raises(ValueError, match="duplicate EML attachment"):
        _normalize(duplicate)

    unsafe = EmailMessage()
    unsafe.set_content("body")
    unsafe.add_attachment(
        b"MZactive", maintype="application", subtype="octet-stream", filename="run.exe"
    )
    with pytest.raises(UnsafeArchiveMemberError, match="active"):
        _normalize(unsafe)


def test_email_part_depth_header_and_body_limits_fail_closed() -> None:
    multipart = EmailMessage()
    multipart.set_content("body")
    multipart.add_attachment(b"safe", maintype="text", subtype="plain", filename="a.txt")
    with pytest.raises(ResourceLimitExceeded, match="max_archive_members"):
        _normalize(multipart, limits=IngestLimits(max_archive_members=1))
    with pytest.raises(ResourceLimitExceeded, match="max_recursion_depth"):
        _normalize(multipart, limits=IngestLimits(max_recursion_depth=1))

    headers = EmailMessage()
    headers["Subject"] = "one"
    headers["From"] = "two@example.invalid"
    headers.set_content("body")
    with pytest.raises(ResourceLimitExceeded, match="max_structured_nodes"):
        _normalize(headers, limits=IngestLimits(max_structured_nodes=1))

    body = EmailMessage()
    body.set_content("too long")
    with pytest.raises(ResourceLimitExceeded, match="max_text_characters"):
        _normalize(body, limits=IngestLimits(max_text_characters=4))
