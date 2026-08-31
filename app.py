"""Clerk-san's local-first Streamlit interface.

The browser UI talks only to the loopback FastAPI service, which keeps the review and
audit contract identical for the API and UI.
"""

from __future__ import annotations

import json
import os
from datetime import date
from functools import cache
from html import escape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import streamlit as st

from translations import SUPPORTED_LOCALES, locale_label, normalize_locale, translate
from ui_api_client import ApiConflict, ApiError, ClerksanClient, LocalServiceUnavailable
from ui_styles import apply_custom_css

SUPPORTED_FORMATS = [
    "png",
    "jpg",
    "jpeg",
    "webp",
    "pdf",
    "docx",
    "xlsx",
    "md",
    "markdown",
]
_DERIVATIVE_FAILURE_PREFIXES = ("index_document:", "process_embedded_media:")
_NAVIGATION_ITEMS = ("Inbox", "Documents", "Bills", "Search")
_PAGE_DETAILS = {
    "Inbox": (
        "page.inbox.title",
        "page.inbox.copy",
    ),
    "Add documents": (
        "page.add_documents.title",
        "page.add_documents.copy",
    ),
    "Documents": (
        "page.documents.title",
        "page.documents.copy",
    ),
    "Bills": (
        "page.bills.title",
        "page.bills.copy",
    ),
    "Search": (
        "page.search.title",
        "page.search.copy",
    ),
}
_NAVIGATION_LABEL_KEYS = {
    "Inbox": "nav.inbox",
    "Documents": "nav.documents",
    "Bills": "nav.bills",
    "Search": "nav.search",
}
_FIELD_LABEL_KEYS = {
    "transaction_date": "field.transaction_date",
    "total_amount": "field.total_amount",
    "counterparty": "field.counterparty",
    "currency": "field.currency",
    "expense_kind": "field.expense_kind",
    "category": "field.category",
    "tax_id": "field.tax_id",
    "due_date": "field.due_date",
    "billing_period": "field.billing_period",
    "issuer_kind": "field.issuer_kind",
    "consumption_value": "field.consumption_value",
    "consumption_unit": "field.consumption_unit",
    "line_items": "field.line_items",
}
_DATAFRAME_COLUMN_KEYS = {
    "id": "column.id",
    "issuer": "column.issuer",
    "issuer_id": "column.issuer_id",
    "issuer_kind": "column.issuer_kind",
    "billing_period": "column.billing_period",
    "due_date": "column.due_date",
    "amount": "column.amount",
    "currency": "column.currency",
    "payment_status": "column.payment_status",
    "paid_at": "column.paid_at",
    "consumption_value": "column.consumption_value",
    "consumption_unit": "column.consumption_unit",
    "reminder_date": "column.reminder_date",
    "days_until_due": "column.days_until_due",
    "days_left": "column.days_left",
    "status": "column.status",
    "consumption": "column.consumption",
    "unit_price": "column.unit_price",
    "month_over_month": "column.month_over_month",
    "year_over_year": "column.year_over_year",
    "reference": "column.reference",
    "reference_period": "column.reference_period",
    "missing_reference": "column.missing_reference",
    "amount_delta": "column.amount_delta",
    "consumption_delta": "column.consumption_delta",
    "unit_price_delta": "column.unit_price_delta",
    "metric": "column.metric",
    "value": "column.value",
    "median": "column.median",
    "mad": "column.mad",
    "robust_z": "column.robust_z",
    "explanation": "column.explanation",
    "current_amount": "column.current_amount",
    "baseline_amount": "column.baseline_amount",
    "delta": "column.delta",
    "percent_change": "column.percent_change",
    "severity": "column.severity",
    "reason": "column.reason",
}
HISTORY_FINANCIAL_METRIC_STYLE = """
<style>
.st-key-history-spend-metric [data-testid="stMetricValue"],
.st-key-history-average-metric [data-testid="stMetricValue"] {
    font-size: 1.1rem;
    letter-spacing: -0.02em;
}
</style>
"""
_ASSET_ROOT = Path(__file__).resolve().parent / "assets" / "brand"
_SVG_ASSETS = {
    "brand-mark": _ASSET_ROOT / "clerksan-mark.svg",
    "audit-timeline": _ASSET_ROOT / "icons" / "audit-timeline.svg",
    "duplicate-evidence": _ASSET_ROOT / "icons" / "duplicate-evidence.svg",
    "human-review": _ASSET_ROOT / "icons" / "human-review.svg",
    "verified-record": _ASSET_ROOT / "icons" / "verified-record.svg",
    "recurring-bill": _ASSET_ROOT / "icons" / "recurring-bill.svg",
    "search-evidence": _ASSET_ROOT / "icons" / "search-evidence.svg",
    "intake-document": _ASSET_ROOT / "icons" / "intake-document.svg",
}
_PAGE_ICONS = {
    "Inbox": "human-review",
    "Add documents": "intake-document",
    "Documents": "verified-record",
    "Bills": "recurring-bill",
    "Search": "search-evidence",
}
ET.register_namespace("", "http://www.w3.org/2000/svg")


def _client() -> ClerksanClient:
    return ClerksanClient(os.getenv("CLERKSAN_API_URL", "http://127.0.0.1:8000"))


def _t(locale: str, key: str, /, **values: object) -> str:
    return translate(locale, key, **values)


def _active_locale() -> str:
    """Keep a canonical locale code in session state across normal Streamlit reruns."""

    selected = normalize_locale(st.session_state.get("clerk-locale", "en"))
    st.session_state["clerk-locale"] = selected
    return selected


def _navigation_label(page: str, locale: str) -> str:
    return _t(locale, _NAVIGATION_LABEL_KEYS[page])


def _page_label(page: str, locale: str) -> str:
    title_key, _ = _PAGE_DETAILS[page]
    return _t(locale, title_key)


def _field_label(field: str, locale: str) -> str:
    key = _FIELD_LABEL_KEYS.get(field)
    return _t(locale, key) if key else field


def _localized_dataframe(frame: pd.DataFrame, locale: str) -> pd.DataFrame:
    """Translate table headers only; the API-provided cells remain exactly as received."""

    labels = {
        column: _t(locale, key)
        for column, key in _DATAFRAME_COLUMN_KEYS.items()
        if column in frame.columns
    }
    return frame.rename(columns=labels)


def _local_unavailable(client: ClerksanClient, locale: str = "en") -> None:
    st.error(_t(locale, "unavailable.title"))
    st.code("docker compose --profile app up -d", language="bash")
    st.caption(_t(locale, "unavailable.caption", base_url=client.base_url))
    st.info(_t(locale, "unavailable.start_local"))


def _local_not_ready(error: ApiError, locale: str = "en") -> None:
    """Explain why local scans are blocked without treating a live API as cloud fallback."""

    st.error(_t(locale, "not_ready.title"))
    errors = error.detail.get("errors") if error.detail else None
    readiness_errors = [str(item) for item in errors] if isinstance(errors, list) else [str(error)]
    for readiness_error in readiness_errors:
        st.warning(readiness_error)
    st.code(
        "docker compose up -d db ollama\ndocker compose --profile app up -d --build",
        language="bash",
    )
    model_commands = [
        readiness_error.removeprefix("missing model: ")
        for readiness_error in readiness_errors
        if readiness_error.startswith("missing model: ollama pull ")
    ]
    if model_commands:
        st.code(
            "\n".join(f"docker compose exec ollama {command}" for command in model_commands),
            language="bash",
        )
    st.caption(_t(locale, "not_ready.caption"))


def _is_derivative_processing_error(error: object) -> bool:
    """Select the narrow recovery action that matches an observable job failure."""

    return isinstance(error, str) and error.startswith(_DERIVATIVE_FAILURE_PREFIXES)


@cache
def _read_svg_asset(asset_name: str) -> str:
    try:
        asset_path = _SVG_ASSETS[asset_name]
    except KeyError as error:
        raise ValueError(f"Unknown SVG asset: {asset_name}") from error
    if not asset_path.is_file():
        raise FileNotFoundError(asset_path)
    return asset_path.read_text(encoding="utf-8")


def _inline_svg(
    asset_name: str,
    *,
    size: int,
    class_name: str,
    decorative: bool = True,
) -> str:
    """Return a known Clerk-san SVG asset as safe inline markup."""

    svg = ET.fromstring(_read_svg_asset(asset_name))
    svg.set("width", str(size))
    svg.set("height", str(size))
    svg.set("class", class_name)
    if decorative:
        svg.set("aria-hidden", "true")
        svg.set("focusable", "false")
        svg.attrib.pop("role", None)
        svg.attrib.pop("aria-labelledby", None)
        for child in list(svg):
            if child.tag.rsplit("}", 1)[-1] == "title":
                svg.remove(child)
    else:
        svg.set("role", "img")
    return ET.tostring(svg, encoding="unicode")


def _page_icon_markup(page: str) -> str:
    icon_name = _PAGE_ICONS.get(page)
    if icon_name is None:
        return ""
    return _inline_svg(
        icon_name,
        size=18,
        class_name="clerk-page-icon-svg",
    )


def _original_document_heading_markup(locale: str = "en") -> str:
    return f"""
    <div class="clerk-section-heading">
      <span class="clerk-section-icon" aria-hidden="true">
        {_inline_svg("audit-timeline", size=16, class_name="clerk-section-icon-svg")}
      </span>
      <p class="clerk-section-label">{escape(_t(locale, "original.heading"))}</p>
    </div>
    """


def _duplicate_evidence_cue_markup(locale: str = "en") -> str:
    return f"""
    <div class="clerk-evidence-cue">
      <span class="clerk-evidence-cue-icon" aria-hidden="true">
        {_inline_svg("duplicate-evidence", size=16, class_name="clerk-evidence-cue-icon-svg")}
      </span>
      <span>{escape(_t(locale, "duplicate.evidence"))}</span>
    </div>
    """


def _render_sidebar(locale: str) -> tuple[str, str]:
    """Render the Mercury-inspired workspace shell and return canonical page and locale IDs."""

    if "clerk-page" not in st.session_state:
        st.session_state["clerk-page"] = "Inbox"
    if "clerk-last-nav" not in st.session_state:
        st.session_state["clerk-last-nav"] = "Inbox"

    with st.sidebar:
        st.markdown(
            """
            <div class="clerk-brand">
              <div class="clerk-brand-mark">
                {brand_mark}
              </div>
              <div>
                <div class="clerk-brand-name">Clerk-san</div>
                <div class="clerk-brand-caption">{caption}</div>
              </div>
            </div>
            """.format(
                caption=escape(_t(locale, "brand.caption")),
                brand_mark=_inline_svg(
                    "brand-mark",
                    size=30,
                    class_name="clerk-brand-mark-svg",
                    decorative=True,
                )
            ),
            unsafe_allow_html=True,
        )
        upload_requested = st.button(
            _t(locale, "sidebar.add_documents"),
            type="primary",
            key="sidebar-add-documents",
        )
        selected_navigation = st.radio(
            _t(locale, "sidebar.workspace"),
            _NAVIGATION_ITEMS,
            format_func=lambda page: _navigation_label(page, locale),
            label_visibility="collapsed",
            key="clerk-navigation",
        )
        if upload_requested:
            st.session_state["clerk-page"] = "Add documents"
        elif selected_navigation != st.session_state["clerk-last-nav"]:
            st.session_state["clerk-page"] = selected_navigation
        st.session_state["clerk-last-nav"] = selected_navigation
        locale = normalize_locale(
            st.selectbox(
                _t(locale, "language.label"),
                SUPPORTED_LOCALES,
                format_func=locale_label,
                key="clerk-locale",
            )
        )
        st.markdown(
            """
            <div class="clerk-sidebar-note">
              {note}
            </div>
            """.format(note=escape(_t(locale, "sidebar.note"))),
            unsafe_allow_html=True,
        )
    return str(st.session_state["clerk-page"]), locale


def _render_page_heading(page: str, locale: str) -> None:
    title_key, copy_key = _PAGE_DETAILS[page]
    title = _t(locale, title_key)
    copy = _t(locale, copy_key)
    st.markdown(
        f"""
        <div class="clerk-page-head">
          <div class="clerk-page-icon" aria-hidden="true">{_page_icon_markup(page)}</div>
          <div>
            <div class="clerk-page-eyebrow">Clerk-san / {escape(_page_label(page, locale))}</div>
            <h1 class="clerk-page-title">{escape(title)}</h1>
            <p class="clerk-page-copy">{escape(copy)}</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _review_item_label(item: dict[str, Any], locale: str = "en") -> str:
    flagged = len(item.get("flagged_fields", []))
    document_id = str(item.get("document_id", ""))
    priority = _t(locale, "review.need_attention" if flagged else "review.ready_to_verify")
    return f"{priority} · {item.get('doc_class', 'document')} · {document_id[:8]}"


def _field_details(value: Any) -> tuple[Any, float | None, str | None]:
    """Unwrap a normalized extraction field without assuming a document class."""

    if not isinstance(value, dict) or "value" not in value:
        return value, None, None
    confidence = value.get("confidence")
    source_span = value.get("source_span")
    return (
        value.get("value"),
        float(confidence) if isinstance(confidence, (float, int)) else None,
        str(source_span) if source_span else None,
    )


def _display_field_value(value: Any, locale: str = "en") -> str:
    if value is None:
        return _t(locale, "field.not_extracted")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ": "))
    return str(value)


def _is_flagged_field(field: str, flagged_fields: set[str]) -> bool:
    return field in flagged_fields or any(name.startswith(f"{field}.") for name in flagged_fields)


def _render_extracted_fields(item: dict[str, Any], locale: str) -> None:
    """Present model output as reviewable fields rather than a raw JSON blob."""

    payload = item.get("suggested")
    if not isinstance(payload, dict) or not payload:
        st.info(_t(locale, "field.no_structured"))
        return
    flagged_fields = {str(field) for field in item.get("flagged_fields", [])}
    st.markdown(
        f'<p class="clerk-section-label">{escape(_t(locale, "field.details"))}</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<p class="clerk-section-copy">{escape(_t(locale, "field.details_copy"))}</p>',
        unsafe_allow_html=True,
    )
    for field, raw_value in payload.items():
        value, confidence, source_span = _field_details(raw_value)
        needs_review = _is_flagged_field(field, flagged_fields)
        if needs_review:
            tag_class, tag_text = "clerk-field-tag-check", _t(locale, "field.needs_review")
        elif confidence is None:
            tag_class, tag_text = "clerk-field-tag-unknown", _t(locale, "field.extracted")
        else:
            tag_class, tag_text = "clerk-field-tag-ok", _t(locale, "field.ready")
        label = _field_label(field, locale)
        source_markup = (
            '<div class="clerk-field-source">'
            f'{escape(_t(locale, "field.source", source=source_span))}'
            "</div>"
            if source_span
            else ""
        )
        st.markdown(
            f"""
            <div class="clerk-review-field">
              <div class="clerk-field-name">{escape(label)}</div>
              <div class="clerk-field-value">{escape(_display_field_value(value, locale))}</div>
              <span class="clerk-field-tag {tag_class}">{escape(tag_text)}</span>
              {source_markup}
            </div>
            """,
            unsafe_allow_html=True,
        )


def _latest_original_file(document: dict[str, Any]) -> dict[str, Any] | None:
    files = document.get("files")
    if not isinstance(files, list):
        return None
    originals = [
        file
        for file in files
        if isinstance(file, dict) and file.get("kind") == "original"
    ]
    if not originals:
        return None
    return max(
        originals,
        key=lambda item: (int(item.get("version", 0)), str(item.get("id", ""))),
    )


def _render_original_preview(client: ClerksanClient, item: dict[str, Any], locale: str) -> None:
    """Keep the document original alongside its extracted fields, matching the review task."""

    document_id = str(item["document_id"])
    filename = _t(locale, "original.fallback_filename")
    mime = ""
    try:
        document = client.status(document_id)
        original = _latest_original_file(document)
    except (LocalServiceUnavailable, ApiError) as error:
        original = None
        st.caption(_t(locale, "original.preview_unavailable", detail=str(error)))
    if original:
        filename = str(original.get("source_filename") or filename)
        mime = str(original.get("mime") or "")
        original_url = client.original_url(
            document_id,
            source_file_id=str(original["id"]),
            source_version=int(original["version"]),
            sha256=str(original["sha256"]),
        )
    else:
        original_url = client.original_url(document_id)

    st.markdown(_original_document_heading_markup(locale), unsafe_allow_html=True)
    st.markdown(
        f'<p class="clerk-section-copy">{escape(_t(locale, "original.copy"))}</p>',
        unsafe_allow_html=True,
    )
    if mime in {"image/jpeg", "image/png", "image/webp"}:
        st.image(original_url, caption=filename, use_container_width=True)
    else:
        st.markdown(
            """
            <div class="clerk-preview-shell">
              <div class="clerk-preview-empty">
                {copy}
              </div>
            </div>
            """.format(copy=escape(_t(locale, "original.native_viewer"))),
            unsafe_allow_html=True,
        )
    st.markdown(
        f'<div class="clerk-preview-meta"><code>{escape(filename)}</code>'
        f'<span>{escape(_t(locale, "original.immutable"))}</span></div>',
        unsafe_allow_html=True,
    )
    st.link_button(_t(locale, "original.open"), original_url, use_container_width=True)


def _render_scan(client: ClerksanClient, locale: str) -> None:
    if st.button(_t(locale, "scan.back"), key="scan-back-to-inbox"):
        st.session_state["clerk-page"] = "Inbox"
        st.rerun()
    uploaded = st.file_uploader(
        _t(locale, "scan.uploader"), type=SUPPORTED_FORMATS
    )
    if not uploaded:
        return
    st.caption(_t(locale, "scan.preserved"))
    if st.button(_t(locale, "scan.upload"), type="primary"):
        try:
            accepted = client.upload(uploaded.name, uploaded.getvalue())
        except (LocalServiceUnavailable, ApiError) as error:
            st.error(str(error))
            return
        st.success(_t(locale, "scan.queued", document_id=accepted["document_id"]))
        if accepted.get("duplicate_of"):
            st.markdown(_duplicate_evidence_cue_markup(locale), unsafe_allow_html=True)
            st.warning(_t(locale, "duplicate.queued", document_id=accepted["duplicate_of"]))
        st.session_state["last_document_id"] = accepted["document_id"]

    document_id = st.session_state.get("last_document_id")
    if document_id:
        try:
            document = client.status(str(document_id))
        except (LocalServiceUnavailable, ApiError) as error:
            st.error(str(error))
        else:
            st.info(_t(locale, "scan.current_status", status=document["status"]))
            if processing_error := document.get("processing_error"):
                st.warning(_t(locale, "scan.processing_attention", detail=processing_error))
                if _is_derivative_processing_error(processing_error):
                    if st.button(_t(locale, "scan.retry_background"), key="retry-last-document"):
                        try:
                            retried = client.retry_derivatives(str(document_id))
                        except (LocalServiceUnavailable, ApiError) as error:
                            st.error(str(error))
                        else:
                            if retried["status"] == "queued":
                                st.success(_t(locale, "scan.queued_recovery"))
                                st.rerun()
                            else:
                                st.info(_t(locale, "scan.no_retry"))
                elif document["status"] in {"failed", "needs_reprocess"}:
                    if st.button(_t(locale, "scan.reprocess"), key="reprocess-last-document"):
                        try:
                            queued = client.reprocess(str(document_id), actor="local-user")
                        except (LocalServiceUnavailable, ApiError) as error:
                            st.error(str(error))
                        else:
                            if queued["status"] == "queued":
                                st.success(_t(locale, "scan.queued_reprocess"))
                                st.rerun()
                            else:
                                st.info(_t(locale, "scan.already_queued"))
            if document.get("extracted"):
                st.json(document["extracted"])


def _parse_corrections(raw: str, locale: str = "en") -> dict[str, Any]:
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(_t(locale, "validation.corrections_json_object"))
    return parsed


def _streamlit_can_mutate_review(item: dict[str, Any]) -> bool:
    """Keep legacy mutations only for legacy or singleton financial review items."""

    record_kind = item.get("record_kind")
    if record_kind not in (None, "financial"):
        return False
    if item.get("batch_id") is None:
        return True
    return record_kind == "financial" and item.get("batch_candidate_count") == 1


def _render_review(client: ClerksanClient, locale: str) -> None:
    try:
        items = client.review_pending()
    except (LocalServiceUnavailable, ApiError) as error:
        st.error(str(error))
        return
    if not items:
        st.markdown(
            """
            <div class="clerk-empty">
              <strong>{title}</strong><br>
              {copy}
            </div>
            """.format(
                title=escape(_t(locale, "review.empty_title")),
                copy=escape(_t(locale, "review.empty_copy")),
            ),
            unsafe_allow_html=True,
        )
        return

    flagged_count = sum(bool(item.get("flagged_fields")) for item in items)
    duplicate_count = sum(bool(item.get("duplicate_candidates")) for item in items)
    queue_metric, attention_metric, duplicate_metric = st.columns(3)
    queue_metric.metric(_t(locale, "review.in_queue"), len(items))
    attention_metric.metric(_t(locale, "review.need_attention"), flagged_count)
    duplicate_metric.metric(_t(locale, "review.possible_duplicates"), duplicate_count)

    reviewer_col, document_col = st.columns((0.55, 1.45), gap="large")
    with reviewer_col:
        reviewer = st.text_input(_t(locale, "review.reviewer"), value="local-user", key="reviewer")
    with document_col:
        selected_index = st.selectbox(
            _t(locale, "review.document"),
            range(len(items)),
            format_func=lambda index: _review_item_label(items[index], locale),
            key="review-item-selection",
        )
    item = items[selected_index]
    attention_class = (
        "clerk-status-attention" if item.get("flagged_fields") else "clerk-status-success"
    )
    attention_text = _t(
        locale,
        "review.need_attention" if item.get("flagged_fields") else "review.ready_to_verify",
    )
    with st.container(key="review-workspace", border=True):
        st.markdown(
            f"""
            <div class="clerk-queue-bar">
              <span class="clerk-status {attention_class}">{attention_text}</span>
              <span class="clerk-queue-id">{escape(str(item['doc_class']))} ·
              {escape(str(item['document_id']))}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        detail_col, source_col = st.columns((1, 1), gap="large")
        with detail_col:
            if item.get("flagged_fields"):
                st.warning(
                    _t(locale, "review.fields_first", fields=", ".join(item["flagged_fields"]))
                )
            if item.get("duplicate_candidates"):
                st.warning(_t(locale, "review.duplicate_attached"))
                with st.expander(_t(locale, "review.show_duplicate")):
                    st.json(item["duplicate_candidates"])
            _render_extracted_fields(item, locale)
            if _streamlit_can_mutate_review(item):
                with st.expander(_t(locale, "review.correct_fields")):
                    st.caption(_t(locale, "review.corrections_help"))
                    corrections = st.text_area(
                        _t(locale, "review.corrections_json"),
                        value="",
                        placeholder=_t(locale, "review.corrections_placeholder"),
                        key=f"corrections-{item['extraction_id']}",
                        label_visibility="collapsed",
                    )
                if st.button(
                    _t(locale, "review.approve"),
                    key=f"approve-{item['extraction_id']}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        result = client.approve(
                            str(item["extraction_id"]),
                            int(item["version"]),
                            _parse_corrections(corrections, locale),
                            reviewer,
                        )
                    except ApiConflict:
                        st.warning(_t(locale, "review.changed_conflict"))
                    except (ValueError, LocalServiceUnavailable, ApiError) as error:
                        st.error(str(error))
                    else:
                        st.success(
                            _t(locale, "review.verified", verified_id=result["verified_id"])
                        )
                        st.rerun()
                with st.expander(_t(locale, "review.reject_reprocess")):
                    reason = st.text_input(
                        _t(locale, "review.rejection_reason"),
                        key=f"reason-{item['extraction_id']}",
                    )
                    confirm_rejection = st.checkbox(
                        _t(locale, "review.confirm_rejection"),
                        key=f"confirm-rejection-{item['extraction_id']}",
                    )
                    if st.button(
                        _t(locale, "review.reject"),
                        key=f"reject-{item['extraction_id']}",
                        disabled=not confirm_rejection,
                        use_container_width=True,
                    ):
                        if not reason.strip():
                            st.error(_t(locale, "review.rejection_reason_required"))
                        else:
                            try:
                                client.reject(str(item["extraction_id"]), reason, reviewer)
                                queued = client.reprocess(
                                    str(item["document_id"]), actor=reviewer
                                )
                            except (LocalServiceUnavailable, ApiError) as error:
                                st.error(str(error))
                            else:
                                if queued["status"] == "already_queued":
                                    st.info(_t(locale, "scan.already_queued"))
                                else:
                                    st.success(_t(locale, "review.rejected_queued"))
                                st.rerun()
            else:
                st.info(_t(locale, "review.batch_use_react"))
                st.link_button(
                    _t(locale, "intake.open_review"),
                    f"{client.base_url}/#review",
                    use_container_width=True,
                )
        with source_col:
            _render_original_preview(client, item, locale)


def _verified_rows(
    documents: list[dict[str, Any]], *, locale: str = "en"
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in documents:
        record = document.get("verified")
        if isinstance(record, dict):
            rows.append(
                {
                    "Date": record.get("transaction_date"),
                    "Counterparty": record.get("counterparty"),
                    "Amount": record.get("total_amount"),
                    "Currency": record.get("currency"),
                    "Expense type": record.get("expense_kind") or _t(locale, "value.unspecified"),
                    "Document": str(document.get("id")),
                }
            )
    return rows


def _currency_summary(frame: pd.DataFrame, *, average: bool = False, locale: str = "en") -> str:
    """Format totals per currency without implying a conversion rate."""

    values = pd.to_numeric(frame["Amount"], errors="coerce").fillna(0)
    unspecified = _t(locale, "value.unspecified")
    currencies = frame["Currency"].fillna(unspecified).astype(str).str.strip()
    currencies = currencies.mask(currencies == "", unspecified)
    grouped = pd.DataFrame({"Currency": currencies, "Amount": values}).groupby(
        "Currency", sort=True
    )["Amount"]
    summary = grouped.mean() if average else grouped.sum()
    return " | ".join(f"{currency} {amount:,.0f}" for currency, amount in summary.items())


def _render_history(client: ClerksanClient, locale: str) -> None:
    filters_col, amount_col, name_col = st.columns(3)
    with filters_col:
        start = st.date_input(_t(locale, "history.from"), value=None, key="history-from")
        end = st.date_input(_t(locale, "history.to"), value=None, key="history-to")
    with amount_col:
        amount_min = st.number_input(_t(locale, "history.minimum_amount"), min_value=0.0, value=0.0)
        amount_max = st.number_input(_t(locale, "history.maximum_amount"), min_value=0.0, value=0.0)
    with name_col:
        counterparty = st.text_input(_t(locale, "history.counterparty"))
    filters: dict[str, Any] = {
        "date_from": _iso_date(start),
        "date_to": _iso_date(end),
        "amount_min": amount_min or None,
        "amount_max": amount_max or None,
        "counterparty": counterparty or None,
        "limit": 500,
    }
    try:
        documents = client.list_documents(**filters)
    except (LocalServiceUnavailable, ApiError) as error:
        st.error(str(error))
        return
    rows = _verified_rows(documents, locale=locale)
    if not rows:
        st.info(_t(locale, "history.no_results"))
        return
    frame = pd.DataFrame(rows)
    numeric = pd.to_numeric(frame["Amount"], errors="coerce").fillna(0)
    unspecified = _t(locale, "value.unspecified")
    currencies = frame["Currency"].fillna(unspecified).astype(str).str.strip()
    display_frame = frame.assign(
        Amount=numeric,
        Currency=currencies.mask(currencies == "", unspecified),
    )
    first, second, third = st.columns(3)
    first.metric(_t(locale, "history.verified_records"), len(display_frame))
    st.markdown(HISTORY_FINANCIAL_METRIC_STYLE, unsafe_allow_html=True)
    with second:
        with st.container(key="history-spend-metric"):
            st.metric(
                _t(locale, "history.verified_spend"),
                _currency_summary(display_frame, locale=locale),
            )
    with third:
        with st.container(key="history-average-metric"):
            st.metric(
                _t(locale, "history.average"),
                _currency_summary(display_frame, average=True, locale=locale),
            )
    history_columns = {
        "Date": _t(locale, "history.date"),
        "Counterparty": _t(locale, "history.counterparty"),
        "Amount": _t(locale, "history.amount"),
        "Currency": _t(locale, "history.currency"),
        "Expense type": _t(locale, "history.expense_type"),
        "Document": _t(locale, "history.document"),
    }
    st.dataframe(display_frame.rename(columns=history_columns), width="stretch", hide_index=True)
    chart = display_frame.pivot_table(
        index="Expense type", columns="Currency", values="Amount", aggfunc="sum", fill_value=0
    )
    chart.index.name = _t(locale, "history.expense_type")
    chart.columns.name = _t(locale, "history.currency")
    st.bar_chart(chart)


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if isinstance(value, date) else None


def _render_query(client: ClerksanClient, locale: str) -> None:
    question = st.text_input(
        _t(locale, "search.question"),
        placeholder=_t(locale, "search.placeholder"),
    )
    if question and st.button(_t(locale, "search.ask")):
        try:
            answer = client.ask(question)
        except (LocalServiceUnavailable, ApiError) as error:
            st.error(str(error))
            return
        st.write(answer.get("text", ""))
        st.caption(_t(locale, "search.mode", mode=answer.get("mode", _t(locale, "value.unknown"))))
        if answer.get("sql_result"):
            st.json(answer["sql_result"])
        for citation in answer.get("citations", []):
            st.info(
                f"{citation['document_id']} · {citation['heading_path']}\n\n{citation['snippet']}"
            )


def _render_bills(client: ClerksanClient, locale: str) -> None:
    try:
        bills = client.list_bills()
    except (LocalServiceUnavailable, ApiError) as error:
        st.info(_t(locale, "bills.unavailable", detail=str(error)))
        return
    if not bills:
        st.info(_t(locale, "bills.empty"))
        return

    st.dataframe(
        _localized_dataframe(pd.DataFrame(bills), locale),
        width="stretch",
        hide_index=True,
    )
    _render_bill_reminders(client, locale)
    _render_bill_payment(client, bills, locale)
    _render_bill_analysis(client, bills, locale)


def _render_bill_reminders(client: ClerksanClient, locale: str) -> None:
    st.markdown(f"## {_t(locale, 'bills.due_reminders')}")
    days_ahead = int(
        st.number_input(
            _t(locale, "bills.days_ahead"),
            min_value=0,
            max_value=365,
            value=14,
            key="bill-reminder-days",
        )
    )
    try:
        reminders = client.bill_reminders(days_ahead=days_ahead)
    except (LocalServiceUnavailable, ApiError) as error:
        st.warning(_t(locale, "bills.reminders_unavailable", detail=str(error)))
        return

    overdue = reminders["overdue"]
    upcoming = reminders["upcoming"]
    if not overdue and not upcoming:
        st.success(_t(locale, "bills.none_due"))
        return
    if overdue:
        st.error(_t(locale, "bills.overdue_count", count=len(overdue)))
        st.dataframe(
            _localized_dataframe(pd.DataFrame(overdue), locale),
            width="stretch",
            hide_index=True,
        )
    if upcoming:
        st.info(_t(locale, "bills.due_within", count=len(upcoming), days=days_ahead))
        st.dataframe(
            _localized_dataframe(pd.DataFrame(upcoming), locale),
            width="stretch",
            hide_index=True,
        )


def _render_bill_payment(client: ClerksanClient, bills: list[dict[str, Any]], locale: str) -> None:
    unpaid_bills = [bill for bill in bills if bill.get("payment_status") != "paid"]
    if not unpaid_bills:
        st.caption(_t(locale, "bills.all_paid"))
        return

    st.markdown(f"## {_t(locale, 'bills.mark_paid')}")
    selected = st.selectbox(
        _t(locale, "bills.unpaid_bill"),
        unpaid_bills,
        format_func=lambda bill: _bill_option_label(bill, locale),
        key="bill-payment-selection",
    )
    actor = st.text_input(
        _t(locale, "bills.payment_actor"),
        value="local-user",
        key="bill-payment-actor",
    )
    if not st.button(_t(locale, "bills.mark_paid"), key="bill-mark-paid", type="primary"):
        return
    if not actor.strip():
        st.error(_t(locale, "bills.actor_required"))
        return
    try:
        result = client.mark_bill_paid(str(selected["id"]), actor=actor.strip())
    except (LocalServiceUnavailable, ApiError) as error:
        st.error(_t(locale, "bills.mark_failed", detail=str(error)))
        return
    st.success(_t(locale, "bills.marked_paid", bill_id=result.get("bill_id", selected["id"])))
    st.rerun()


def _render_bill_analysis(client: ClerksanClient, bills: list[dict[str, Any]], locale: str) -> None:
    issuers = _issuers_from_bills(bills)
    if not issuers:
        st.caption(_t(locale, "bills.analysis_waiting"))
        return

    st.markdown(f"## {_t(locale, 'bills.issuer_analysis')}")
    issuer_id = st.selectbox(
        _t(locale, "bills.issuer"),
        list(issuers),
        format_func=lambda selected_id: issuers[selected_id],
        key="bill-analysis-issuer",
    )
    months_col, window_col = st.columns(2)
    with months_col:
        months = int(
            st.number_input(
                _t(locale, "bills.periods"),
                min_value=1,
                max_value=120,
                value=13,
                key="bill-analysis-months",
            )
        )
    with window_col:
        anomaly_window = int(
            st.number_input(
                _t(locale, "bills.anomaly_window"),
                min_value=6,
                max_value=120,
                value=12,
                key="bill-analysis-window",
            )
        )
    if not st.button(_t(locale, "bills.load_analysis"), key="bill-load-analysis"):
        return
    try:
        analysis = client.bill_analysis(
            str(issuer_id), months=months, anomaly_window=anomaly_window
        )
    except (LocalServiceUnavailable, ApiError) as error:
        st.warning(_t(locale, "bills.analysis_unavailable", detail=str(error)))
        return
    comparisons = analysis.get("comparisons", [])
    anomalies = analysis.get("anomalies", [])
    if comparisons:
        st.dataframe(
            _localized_dataframe(pd.DataFrame(comparisons), locale),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info(_t(locale, "bills.not_enough_periods"))
    if anomalies:
        st.warning(_t(locale, "bills.anomaly_signals"))
        st.dataframe(
            _localized_dataframe(pd.DataFrame(anomalies), locale),
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption(_t(locale, "bills.no_anomaly"))


def _bill_option_label(bill: dict[str, Any], locale: str = "en") -> str:
    due_date = bill.get("due_date") or _t(locale, "value.no_due_date")
    return _t(
        locale,
        "bills.option",
        issuer=bill.get("issuer") or _t(locale, "value.unknown_issuer"),
        period=bill.get("billing_period") or _t(locale, "value.unknown_period"),
        amount=bill.get("amount", 0),
        due_date=due_date,
    )


def _issuers_from_bills(bills: list[dict[str, Any]]) -> dict[str, str]:
    issuers: dict[str, str] = {}
    for bill in bills:
        issuer_id = bill.get("issuer_id")
        if issuer_id:
            issuers[str(issuer_id)] = str(bill.get("issuer") or issuer_id)
    return issuers


def main() -> None:
    st.set_page_config(page_title="Clerk-san", layout="wide")
    apply_custom_css()
    locale = _active_locale()
    page, locale = _render_sidebar(locale)
    client = _client()
    if not client.probe():
        _local_unavailable(client, locale)
        return
    try:
        # Component processing readiness is advisory; a successful legacy top-level
        # result keeps durable intake available while local model work is delayed.
        client.readiness()
    except LocalServiceUnavailable:
        _local_unavailable(client, locale)
        return
    except ApiError as error:
        _local_not_ready(error, locale)
        return
    _render_page_heading(page, locale)
    if page == "Add documents":
        _render_scan(client, locale)
    elif page == "Inbox":
        _render_review(client, locale)
    elif page == "Documents":
        _render_history(client, locale)
    elif page == "Search":
        _render_query(client, locale)
    elif page == "Bills":
        _render_bills(client, locale)


if __name__ == "__main__":
    main()
