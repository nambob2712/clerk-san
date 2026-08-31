from __future__ import annotations

from translations import (
    LOCALE_LABELS,
    SUPPORTED_LOCALES,
    TRANSLATIONS,
    locale_label,
    normalize_locale,
    translate,
    translation_keys,
)


def test_clerksan_ui_catalog_supports_exactly_english_vietnamese_and_japanese() -> None:
    assert SUPPORTED_LOCALES == ("en", "vi", "ja")
    assert set(TRANSLATIONS) == set(SUPPORTED_LOCALES)
    assert LOCALE_LABELS == {"en": "English", "vi": "Tiếng Việt", "ja": "日本語"}
    assert locale_label("vi") == "Tiếng Việt"
    assert locale_label("ja") == "日本語"


def test_every_locale_has_complete_current_clerksan_copy() -> None:
    expected = translation_keys()

    for locale in SUPPORTED_LOCALES:
        assert set(TRANSLATIONS[locale]) == expected
        assert "AI Receipt Professional" not in " ".join(TRANSLATIONS[locale].values())
        assert TRANSLATIONS[locale]["page.inbox.title"]
        assert TRANSLATIONS[locale]["review.approve"]
        assert TRANSLATIONS[locale]["bills.issuer_analysis"]


def test_each_locale_keeps_service_recovery_local() -> None:
    local_markers = {"en": "local", "vi": "cục bộ", "ja": "ローカル"}

    for locale in SUPPORTED_LOCALES:
        copy = TRANSLATIONS[locale]
        assert local_markers[locale].casefold() in copy["unavailable.title"].casefold()
        assert local_markers[locale].casefold() in copy["unavailable.caption"].casefold()
        assert copy["unavailable.start_local"]
        assert not any("gemini" in value.casefold() for value in copy.values())


def test_translation_lookup_normalizes_locale_and_falls_back_to_english() -> None:
    assert normalize_locale("vi") == "vi"
    assert normalize_locale("unsupported") == "en"
    assert translate("unsupported", "review.approve") == "Approve verified record"
    assert translate("ja", "scan.queued", document_id="doc-123") == "doc-123 をキューに追加しました"
    assert translate("vi", "missing.key") == "missing.key"


def test_financial_subtype_labels_are_complete_and_localized() -> None:
    subtypes = (
        "transaction",
        "receipt",
        "invoice",
        "bill",
        "recurring_bill",
        "quote",
        "other_financial",
    )

    for subtype in subtypes:
        key = f"financial_subtype.{subtype}"
        assert all(TRANSLATIONS[locale][key] for locale in SUPPORTED_LOCALES)
        assert TRANSLATIONS["vi"][key] != subtype
        assert TRANSLATIONS["ja"][key] != subtype
