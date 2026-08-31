from __future__ import annotations

import types
from pathlib import Path

import pytest

import app
from translations import translation_keys
from ui_api_client import ApiError


def test_streamlit_entrypoint_defaults_to_the_local_api() -> None:
    source = Path("app.py").read_text(encoding="utf-8")
    assert "ClerksanClient" in source
    assert "from translations import" in source
    assert '"clerk-locale"' in source
    assert "bill_reminders" in source
    assert "mark_bill_paid" in source
    assert "bill_analysis" in source
    assert "client.reprocess" in source
    assert "client.retry_derivatives" in source
    assert "receipt_ocr_system" not in source
    assert "from google" not in source
    assert "GEMINI" not in source


def test_streamlit_chooser_and_status_contract_stay_legacy() -> None:
    source = Path("app.py").read_text(encoding="utf-8")

    assert app.SUPPORTED_FORMATS == [
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
    assert "stored_unprocessed" not in source


def test_streamlit_preview_never_embeds_raw_pdf_or_untrusted_image_types() -> None:
    source = Path("app.py").read_text(encoding="utf-8")

    assert "components.iframe" not in source
    assert 'mime.startswith("image/")' not in source
    assert '{"image/jpeg", "image/png", "image/webp"}' in source


def test_locale_helpers_keep_canonical_route_ids_and_translate_only_display_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit = types.SimpleNamespace(session_state={"clerk-locale": "vi"})
    monkeypatch.setattr(app, "st", streamlit)

    assert app._active_locale() == "vi"
    assert app._NAVIGATION_ITEMS == ("Inbox", "Documents", "Bills", "Search")
    assert app._navigation_label("Inbox", "vi") == "Hộp chờ"
    assert app._page_label("Add documents", "ja") == "文書を追加"
    assert streamlit.session_state["clerk-locale"] == "vi"


def test_localized_dataframe_changes_headers_but_not_api_values() -> None:
    frame = app.pd.DataFrame(
        [
            {
                "issuer": "North Power",
                "issuer_kind": "electricity",
                "payment_status": "unpaid",
                "amount": 4100,
                "days_left": 5,
                "month_over_month": {"reference": "previous_month"},
                "metric": "amount",
            }
        ]
    )

    localized = app._localized_dataframe(frame, "ja")

    assert list(localized.columns) == [
        "発行元",
        "発行元種別",
        "支払状況",
        "金額",
        "期限までの日数",
        "前月比",
        "指標",
    ]
    assert localized.iloc[0].to_dict() == {
        "発行元": "North Power",
        "発行元種別": "electricity",
        "支払状況": "unpaid",
        "金額": 4100,
        "期限までの日数": 5,
        "前月比": {"reference": "previous_month"},
        "指標": "amount",
    }


def test_every_dataframe_header_has_a_translation_key() -> None:
    assert set(app._DATAFRAME_COLUMN_KEYS.values()) <= translation_keys()


def test_streamlit_entrypoint_uses_project_owned_svg_assets() -> None:
    app_text = Path("app.py").read_text(encoding="utf-8")
    assert "_SVG_ASSETS" in app_text
    assert "_PAGE_ICONS" in app_text
    assert "clerksan-mark.svg" in app_text
    assert "audit-timeline.svg" in app_text
    assert "duplicate-evidence.svg" in app_text
    assert "human-review.svg" in app_text
    assert "verified-record.svg" in app_text
    assert "recurring-bill.svg" in app_text
    assert "search-evidence.svg" in app_text
    assert "intake-document.svg" in app_text


def test_local_runtime_has_no_cloud_ocr_dependency() -> None:
    base = Path("requirements.txt").read_text(encoding="utf-8")

    assert "google-generativeai" not in base
    assert "google-genai" not in base


def test_unavailable_service_offers_only_local_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableStreamlit:
        def __init__(self) -> None:
            self.errors: list[str] = []
            self.codes: list[str] = []
            self.captions: list[str] = []
            self.infos: list[str] = []

        def error(self, message: str) -> None:
            self.errors.append(message)

        def code(self, command: str, **_: object) -> None:
            self.codes.append(command)

        def caption(self, message: str) -> None:
            self.captions.append(message)

        def info(self, message: str) -> None:
            self.infos.append(message)

    streamlit = UnavailableStreamlit()
    monkeypatch.setattr(app, "st", streamlit)

    app._local_unavailable(types.SimpleNamespace(base_url="http://127.0.0.1:8000"), "vi")

    assert streamlit.errors == ["Dịch vụ cục bộ chưa khả dụng"]
    assert streamlit.codes == ["docker compose --profile app up -d"]
    assert streamlit.captions == [
        "Giao diện đang kết nối tới dịch vụ cục bộ tại http://127.0.0.1:8000."
    ]
    assert streamlit.infos == ["Hãy khởi động các dịch vụ cục bộ ở trên để tiếp tục."]


@pytest.mark.parametrize("asset_name", sorted(app._SVG_ASSETS))
def test_svg_helper_parses_every_allowlisted_asset(asset_name: str) -> None:
    markup = app._inline_svg(asset_name, size=18, class_name="clerk-page-icon-svg")

    assert "<svg" in markup
    assert 'aria-hidden="true"' in markup
    assert "aria-labelledby" not in markup
    assert 'role="img"' not in markup
    assert "<title" not in markup
    assert "clerk-page-icon-svg" in markup
    if asset_name != "brand-mark":
        assert "currentColor" in markup


def test_svg_helper_is_allowlisted_and_stays_inside_the_brand_assets() -> None:
    with pytest.raises(ValueError):
        app._inline_svg("../verified-record", size=18, class_name="x")


def test_non_decorative_svg_preserves_its_accessible_title() -> None:
    markup = app._inline_svg(
        "verified-record",
        size=18,
        class_name="clerk-page-icon-svg",
        decorative=False,
    )

    assert 'role="img"' in markup
    assert 'aria-labelledby="verified-record-title"' in markup
    assert '<title id="verified-record-title"' in markup
    assert "aria-hidden" not in markup


def test_svg_asset_mapping_covers_route_and_contextual_icons() -> None:
    assert set(app._PAGE_ICONS.values()) <= set(app._SVG_ASSETS)
    assert {"audit-timeline", "duplicate-evidence"} <= set(app._SVG_ASSETS)


def test_contextual_icon_markup_uses_the_expected_assets() -> None:
    original_heading = app._original_document_heading_markup()
    duplicate_cue = app._duplicate_evidence_cue_markup()

    assert "audit-timeline-title" not in original_heading
    assert "Original document" in original_heading
    assert "duplicate-evidence-title" not in duplicate_cue
    assert "Duplicate evidence" in duplicate_cue


def test_processing_failure_actions_match_their_job_type() -> None:
    assert app._is_derivative_processing_error("index_document: local model unavailable")
    assert app._is_derivative_processing_error("process_embedded_media: unreadable image")
    assert not app._is_derivative_processing_error("RuntimeError: unreadable original")


def test_streamlit_review_mutation_is_singleton_financial_only() -> None:
    assert app._streamlit_can_mutate_review({"batch_id": None, "record_kind": None})
    assert app._streamlit_can_mutate_review(
        {"batch_id": "batch-1", "batch_candidate_count": 1, "record_kind": "financial"}
    )
    assert not app._streamlit_can_mutate_review(
        {"batch_id": "batch-1", "batch_candidate_count": 2, "record_kind": "financial"}
    )
    assert not app._streamlit_can_mutate_review(
        {
            "batch_id": "batch-1",
            "batch_candidate_count": 1,
            "record_kind": "generic_document",
        }
    )


def test_bill_helpers_keep_payment_and_issuer_selection_unambiguous() -> None:
    bill = {
        "id": "bill-1",
        "issuer_id": "issuer-1",
        "issuer": "North Power",
        "billing_period": "2026-07-01",
        "amount": 4100,
        "due_date": "2026-07-25",
        "payment_status": "unpaid",
    }

    assert app._issuers_from_bills([bill]) == {"issuer-1": "North Power"}
    assert app._bill_option_label(bill) == "North Power · 2026-07-01 · ¥4100 · due 2026-07-25"


def test_currency_summary_keeps_monetary_values_separate() -> None:
    frame = app.pd.DataFrame(
        [
            {"Amount": 1200, "Currency": "JPY"},
            {"Amount": 250000, "Currency": "VND"},
            {"Amount": 800, "Currency": "JPY"},
        ]
    )

    assert app._currency_summary(frame) == "JPY 2,000 | VND 250,000"
    assert app._currency_summary(frame, average=True) == "JPY 1,000 | VND 250,000"


def test_verified_history_uses_expense_type_and_compact_currency_metrics() -> None:
    rows = app._verified_rows(
        [
            {
                "id": "electricity-document",
                "verified": {
                    "transaction_date": "2026-07-12",
                    "counterparty": "North Power",
                    "total_amount": 4100,
                    "currency": "JPY",
                    "category": None,
                    "expense_kind": "electricity",
                },
            },
            {
                "id": "untyped-document",
                "verified": {
                    "transaction_date": "2026-07-13",
                    "counterparty": "Unknown issuer",
                    "total_amount": 100,
                    "currency": "JPY",
                    "category": None,
                    "expense_kind": None,
                },
            },
        ]
    )

    assert rows == [
        {
            "Date": "2026-07-12",
            "Counterparty": "North Power",
            "Amount": 4100,
            "Currency": "JPY",
            "Expense type": "electricity",
            "Document": "electricity-document",
        },
        {
            "Date": "2026-07-13",
            "Counterparty": "Unknown issuer",
            "Amount": 100,
            "Currency": "JPY",
            "Expense type": "Unspecified",
            "Document": "untyped-document",
        },
    ]
    assert "font-size: 1.1rem" in app.HISTORY_FINANCIAL_METRIC_STYLE
    assert ".st-key-history-spend-metric" in app.HISTORY_FINANCIAL_METRIC_STYLE
    assert ".st-key-history-average-metric" in app.HISTORY_FINANCIAL_METRIC_STYLE


class _NotReadyStreamlit:
    def __init__(self) -> None:
        self.session_state: dict[str, object] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.codes: list[str] = []
        self.captions: list[str] = []
        self.tabs_called = False

    def set_page_config(self, **_: object) -> None:
        return None

    def title(self, _: str) -> None:
        return None

    def caption(self, message: str) -> None:
        self.captions.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)

    def code(self, command: str, **_: object) -> None:
        self.codes.append(command)

    def tabs(self, _: list[str]) -> list[object]:
        self.tabs_called = True
        return []


class _NotReadyClient:
    base_url = "http://127.0.0.1:8000"

    def __init__(self, readiness_error: str) -> None:
        self._error = ApiError(
            503,
            "not_ready",
            readiness_error,
            {"errors": [readiness_error]},
        )

    def probe(self) -> bool:
        return True

    def readiness(self) -> dict[str, object]:
        raise self._error


class _DelayedProcessingClient:
    base_url = "http://127.0.0.1:8000"

    def probe(self) -> bool:
        return True

    def readiness(self) -> dict[str, object]:
        return {
            "status": "ready",
            "intake_ready": True,
            "review_ready": True,
            "processing_ready": False,
            "universal_processing_ready": False,
            "processing_reason_codes": ["model_unavailable"],
        }


def test_processing_component_delay_does_not_block_legacy_streamlit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit = _NotReadyStreamlit()
    rendered: list[tuple[str, str]] = []
    monkeypatch.setattr(app, "st", streamlit)
    monkeypatch.setattr(app, "apply_custom_css", lambda: None)
    monkeypatch.setattr(app, "_render_sidebar", lambda locale: ("Inbox", locale))
    monkeypatch.setattr(app, "_render_page_heading", lambda page, locale: None)
    monkeypatch.setattr(
        app,
        "_render_review",
        lambda client, locale: rendered.append(("Inbox", locale)),
    )
    monkeypatch.setattr(app, "_client", _DelayedProcessingClient)

    app.main()

    assert rendered == [("Inbox", "en")]
    assert streamlit.errors == []


@pytest.mark.parametrize(
    ("readiness_error", "expected_command"),
    [
        ("database unavailable: connection refused", "docker compose up -d db ollama"),
        (
            "missing model: ollama pull router-test:3b",
            "docker compose exec ollama ollama pull router-test:3b",
        ),
    ],
)
def test_not_ready_local_service_blocks_scan(
    monkeypatch: pytest.MonkeyPatch, readiness_error: str, expected_command: str
) -> None:
    streamlit = _NotReadyStreamlit()
    client = _NotReadyClient(readiness_error)
    monkeypatch.setattr(app, "st", streamlit)
    monkeypatch.setattr(app, "apply_custom_css", lambda: None)
    monkeypatch.setattr(app, "_render_sidebar", lambda locale: ("Inbox", locale))
    monkeypatch.setattr(app, "_client", lambda: client)
    app.main()

    assert streamlit.tabs_called is False
    assert readiness_error in streamlit.warnings
    assert any(expected_command in command for command in streamlit.codes)


def test_not_ready_recovery_copy_uses_the_selected_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit = _NotReadyStreamlit()
    error = ApiError(503, "not_ready", "database unavailable", {"errors": ["database unavailable"]})
    monkeypatch.setattr(app, "st", streamlit)

    app._local_not_ready(error, "ja")

    expected_error = "ローカルサービスには接続できますが、文書をスキャンする準備ができていません。"
    assert streamlit.errors == [expected_error]
    assert streamlit.captions == [
        "上記のローカルデータベースまたはモデルを修正してから、文書をアップロードする前に再読み込みしてください。"
    ]


def test_unavailable_recovery_copy_uses_the_selected_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit = _NotReadyStreamlit()
    client = types.SimpleNamespace(base_url="http://127.0.0.1:8000")
    monkeypatch.setattr(app, "st", streamlit)

    app._local_unavailable(client, "vi")

    assert streamlit.errors == ["Dịch vụ cục bộ chưa khả dụng"]
    assert streamlit.captions == [
        "Giao diện đang kết nối tới dịch vụ cục bộ tại http://127.0.0.1:8000."
    ]
    assert streamlit.infos == ["Hãy khởi động các dịch vụ cục bộ ở trên để tiếp tục."]
