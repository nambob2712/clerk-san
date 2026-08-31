"""Presentation tokens for Clerk-san's document-review workspace."""

from __future__ import annotations

import streamlit as st


def apply_custom_css() -> None:
    """Apply a quiet, document-first visual system without external assets or fonts."""

    st.markdown(
        """
        <style>
        :root {
            --clerk-canvas: #f7f8fa;
            --clerk-surface: #ffffff;
            --clerk-surface-muted: #f2f4f7;
            --clerk-ink: #20242c;
            --clerk-muted: #667085;
            --clerk-faint: #98a2b3;
            --clerk-line: #e3e6eb;
            --clerk-indigo: #4e5bd5;
            --clerk-indigo-strong: #404bb8;
            --clerk-indigo-soft: #eef0ff;
            --clerk-green: #067647;
            --clerk-green-soft: #ecfdf3;
            --clerk-amber: #9a6700;
            --clerk-amber-soft: #fff7e8;
            --clerk-red: #b42318;
            --clerk-red-soft: #fff1f0;
            --clerk-shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 8px 20px rgba(16, 24, 40, 0.035);
            --clerk-font: "Avenir Next", "Hiragino Sans", "Noto Sans JP", "Yu Gothic", sans-serif;
        }

        .stApp {
            background: var(--clerk-canvas);
            color: var(--clerk-ink);
            font-family: var(--clerk-font);
        }

        header[data-testid="stHeader"] {
            background: transparent;
        }

        [data-testid="stToolbar"] {
            right: 1rem;
        }

        .main .block-container {
            max-width: 1440px;
            padding: 1.7rem 2.4rem 4.75rem;
        }

        [data-testid="stSidebar"] {
            background: var(--clerk-surface);
            border-right: 1px solid var(--clerk-line);
            min-width: 244px;
        }

        [data-testid="stSidebar"] > div:first-child {
            padding: 1.35rem 0.85rem 1.25rem;
        }

        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            font-family: var(--clerk-font);
        }

        .clerk-brand {
            display: flex;
            align-items: center;
            gap: 0.7rem;
            margin: 0.15rem 0.55rem 1.65rem;
        }

        .clerk-brand-mark {
            align-items: center;
            border-radius: 10px;
            display: flex;
            height: 32px;
            justify-content: center;
            overflow: hidden;
            width: 32px;
        }

        .clerk-brand-mark svg,
        .clerk-page-icon-svg,
        .clerk-section-icon-svg,
        .clerk-evidence-cue-icon-svg {
            display: block;
            height: 100%;
            width: 100%;
        }

        .clerk-brand-name {
            color: var(--clerk-ink);
            font-size: 0.92rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            line-height: 1.1;
        }

        .clerk-brand-caption {
            color: var(--clerk-faint);
            font-size: 0.69rem;
            letter-spacing: 0.01em;
            margin-top: 0.16rem;
        }

        [data-testid="stSidebar"] .stButton > button {
            justify-content: center;
            margin-bottom: 1.15rem;
            width: 100%;
        }

        [data-testid="stSidebar"] [role="radiogroup"] {
            gap: 0.18rem;
        }

        [data-testid="stSidebar"] [role="radiogroup"] label {
            align-items: center;
            border-left: 2px solid transparent;
            border-radius: 7px;
            color: #475467;
            cursor: pointer;
            font-size: 0.88rem;
            font-weight: 560;
            min-height: 40px;
            padding: 0.48rem 0.68rem;
            transition: background 160ms ease, color 160ms ease, border-color 160ms ease;
            width: 100%;
        }

        [data-testid="stSidebar"] [role="radiogroup"] label [data-testid="stMarkdownContainer"] p {
            color: #475467 !important;
        }

        [data-testid="stSidebar"] [role="radiogroup"] label:hover {
            background: #f8f9fc;
            color: var(--clerk-ink);
        }

        [data-testid="stSidebar"] [role="radiogroup"] label:hover
        [data-testid="stMarkdownContainer"] p {
            color: var(--clerk-ink) !important;
        }

        [data-testid="stSidebar"] [role="radiogroup"] label:has(input:focus-visible) {
            box-shadow: 0 0 0 3px rgba(78, 91, 213, 0.2);
        }

        [data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {
            background: var(--clerk-indigo-soft);
            border-left-color: var(--clerk-indigo);
            color: #3845ad;
            font-weight: 650;
        }

        [data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked)
        [data-testid="stMarkdownContainer"] p {
            color: #3845ad !important;
        }

        .clerk-sidebar-note {
            border-top: 1px solid var(--clerk-line);
            color: var(--clerk-muted);
            font-size: 0.72rem;
            line-height: 1.48;
            margin: 2.1rem 0.55rem 0;
            padding-top: 1rem;
        }

        .clerk-page-head {
            align-items: flex-start;
            display: flex;
            gap: 0.68rem;
            justify-content: flex-start;
            margin: 0.2rem 0 1.8rem;
        }

        .clerk-page-icon {
            color: var(--clerk-indigo);
            flex: 0 0 auto;
            height: 1.35rem;
            margin-top: 0.18rem;
            width: 1.35rem;
        }

        .clerk-page-eyebrow {
            color: var(--clerk-muted);
            font-size: 0.72rem;
            font-weight: 650;
            letter-spacing: 0.015em;
            margin-bottom: 0.45rem;
        }

        .clerk-page-title {
            color: var(--clerk-ink);
            font-size: clamp(1.55rem, 1.9vw, 2.05rem);
            font-weight: 650;
            letter-spacing: -0.045em;
            line-height: 1.1;
            margin: 0;
            text-wrap: balance;
        }

        .clerk-page-copy {
            color: var(--clerk-muted);
            font-size: 0.91rem;
            line-height: 1.52;
            margin: 0.58rem 0 0;
            max-width: 67ch;
        }

        .clerk-status {
            align-items: center;
            border-radius: 999px;
            display: inline-flex;
            font-size: 0.74rem;
            font-weight: 650;
            gap: 0.36rem;
            line-height: 1;
            padding: 0.4rem 0.64rem;
            white-space: nowrap;
        }

        .clerk-status::before {
            background: currentColor;
            border-radius: 50%;
            content: "";
            height: 0.38rem;
            opacity: 0.8;
            width: 0.38rem;
        }

        .clerk-status-neutral { background: #f2f4f7; color: #475467; }
        .clerk-status-success { background: var(--clerk-green-soft); color: var(--clerk-green); }
        .clerk-status-attention { background: var(--clerk-amber-soft); color: var(--clerk-amber); }
        .clerk-status-danger { background: var(--clerk-red-soft); color: var(--clerk-red); }

        [data-testid="stMetric"] {
            background: var(--clerk-surface);
            border: 1px solid var(--clerk-line);
            border-radius: 10px;
            box-shadow: var(--clerk-shadow);
            padding: 1.05rem 1.1rem;
        }

        [data-testid="stMetricLabel"] {
            color: var(--clerk-muted);
            font-size: 0.77rem;
            font-weight: 580;
        }

        [data-testid="stMetricValue"] {
            color: var(--clerk-ink);
            font-family: var(--clerk-font);
            font-size: 1.55rem;
            font-variant-numeric: tabular-nums;
            font-weight: 650;
            letter-spacing: -0.04em;
        }

        .stButton > button,
        .stDownloadButton > button {
            background: #ffffff;
            border: 1px solid #d7dbe2;
            border-radius: 7px;
            box-shadow: none;
            color: #344054;
            font-family: var(--clerk-font);
            font-size: 0.85rem;
            font-weight: 620;
            min-height: 2.55rem;
            padding: 0.48rem 0.84rem;
            transition: background 150ms ease, border-color 150ms ease,
                color 150ms ease, transform 150ms ease;
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover {
            background: #f8f9fc;
            border-color: #b9c0ce;
            color: #1d2939;
        }

        .stButton > button:active,
        .stDownloadButton > button:active {
            transform: translateY(1px);
        }

        .stButton > button:focus-visible,
        .stDownloadButton > button:focus-visible {
            box-shadow: 0 0 0 3px rgba(78, 91, 213, 0.28);
            outline: 2px solid transparent;
        }

        .stButton > button[kind="primary"] {
            background: var(--clerk-indigo);
            border-color: var(--clerk-indigo);
            color: #ffffff;
        }

        .stButton > button[kind="primary"]:hover {
            background: var(--clerk-indigo-strong);
            border-color: var(--clerk-indigo-strong);
            color: #ffffff;
        }

        .stTextInput input,
        .stTextArea textarea,
        .stNumberInput input,
        .stDateInput input,
        [data-baseweb="select"] > div {
            background: #ffffff;
            border-color: #d7dbe2;
            border-radius: 7px;
            color: var(--clerk-ink);
            font-family: var(--clerk-font);
            font-size: 0.91rem;
            min-height: 2.75rem;
        }

        .stTextInput input:focus-visible,
        .stTextArea textarea:focus-visible,
        .stNumberInput input:focus-visible,
        .stDateInput input:focus-visible,
        [data-baseweb="select"] > div:focus-within {
            border-color: var(--clerk-indigo);
            box-shadow: 0 0 0 3px rgba(78, 91, 213, 0.13);
        }

        [data-testid="stFileUploader"] {
            background: var(--clerk-surface);
            border: 1px dashed #b8c0cc;
            border-radius: 11px;
            padding: 0.5rem;
        }

        [data-testid="stFileUploader"] section {
            background: #fbfcfe;
            border: 0;
        }

        [data-testid="stDataFrame"],
        [data-testid="stTable"] {
            border: 1px solid var(--clerk-line);
            border-radius: 10px;
            overflow: hidden;
        }

        [data-testid="stExpander"] {
            border: 1px solid var(--clerk-line);
            border-radius: 9px;
            box-shadow: none;
        }

        [data-testid="stExpander"] summary {
            color: #344054;
            font-size: 0.86rem;
            font-weight: 600;
        }

        .clerk-workspace,
        .st-key-review-workspace {
            background: var(--clerk-surface);
            border: 1px solid var(--clerk-line);
            border-radius: 12px;
            box-shadow: var(--clerk-shadow);
            margin-top: 0.9rem;
            padding: 1.35rem;
        }

        .clerk-section-label {
            color: #344054;
            font-size: 0.92rem;
            font-weight: 650;
            letter-spacing: -0.015em;
            margin: 0;
        }

        .clerk-section-heading {
            align-items: center;
            display: flex;
            gap: 0.42rem;
        }

        .clerk-section-icon {
            color: var(--clerk-indigo);
            display: inline-flex;
            flex: 0 0 auto;
            height: 1rem;
            width: 1rem;
        }

        .clerk-evidence-cue {
            align-items: center;
            color: var(--clerk-amber);
            display: flex;
            font-size: 0.76rem;
            font-weight: 650;
            gap: 0.38rem;
            margin: 0.4rem 0;
        }

        .clerk-evidence-cue-icon {
            display: inline-flex;
            flex: 0 0 auto;
            height: 1rem;
            width: 1rem;
        }

        .clerk-section-copy {
            color: var(--clerk-muted);
            font-size: 0.78rem;
            line-height: 1.5;
            margin: 0.28rem 0 1rem;
        }

        .clerk-review-field {
            align-items: center;
            border-top: 1px solid #edf0f4;
            display: grid;
            gap: 0.9rem;
            grid-template-columns: minmax(105px, 0.7fr) minmax(0, 1.6fr) auto;
            padding: 0.72rem 0;
        }

        .clerk-review-field:first-child {
            border-top: 0;
            padding-top: 0;
        }

        .clerk-field-name {
            color: var(--clerk-muted);
            font-size: 0.72rem;
            font-weight: 600;
            line-height: 1.35;
        }

        .clerk-field-value {
            color: #1d2939;
            font-size: 0.89rem;
            font-variant-numeric: tabular-nums;
            line-height: 1.42;
            overflow-wrap: anywhere;
        }

        .clerk-field-source {
            color: var(--clerk-faint);
            font-size: 0.71rem;
            grid-column: 2 / 4;
            margin-top: -0.35rem;
            overflow-wrap: anywhere;
        }

        .clerk-field-tag {
            border-radius: 999px;
            font-size: 0.68rem;
            font-weight: 650;
            padding: 0.3rem 0.5rem;
            white-space: nowrap;
        }

        .clerk-field-tag-ok { background: var(--clerk-green-soft); color: var(--clerk-green); }
        .clerk-field-tag-check { background: var(--clerk-amber-soft); color: var(--clerk-amber); }
        .clerk-field-tag-unknown { background: #f2f4f7; color: #667085; }

        .clerk-preview-shell {
            align-items: center;
            background: #f8f9fb;
            border: 1px solid #e7eaf0;
            border-radius: 9px;
            color: var(--clerk-muted);
            display: flex;
            justify-content: center;
            min-height: 24rem;
            overflow: hidden;
        }

        .clerk-preview-empty {
            max-width: 29ch;
            padding: 2rem;
            text-align: center;
        }

        .clerk-preview-meta {
            align-items: center;
            color: var(--clerk-muted);
            display: flex;
            font-size: 0.73rem;
            gap: 0.55rem;
            justify-content: space-between;
            margin-top: 0.72rem;
        }

        .clerk-preview-meta code {
            background: transparent;
            color: #667085;
            font-family: var(--clerk-font);
            font-size: inherit;
            padding: 0;
        }

        .clerk-queue-bar {
            align-items: center;
            border-bottom: 1px solid var(--clerk-line);
            color: var(--clerk-muted);
            display: flex;
            font-size: 0.77rem;
            gap: 0.55rem;
            justify-content: space-between;
            margin-bottom: 1.1rem;
            padding-bottom: 0.8rem;
        }

        .clerk-queue-id {
            color: #475467;
            font-variant-numeric: tabular-nums;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .clerk-empty {
            background: var(--clerk-surface);
            border: 1px solid var(--clerk-line);
            border-radius: 11px;
            color: var(--clerk-muted);
            padding: 2rem;
            text-align: center;
        }

        .stAlert {
            border-radius: 8px;
        }

        @media (max-width: 960px) {
            [data-testid="stSidebar"] {
                min-width: 0;
            }

            .main .block-container {
                padding: 1.25rem 1rem 4rem;
            }

            .clerk-page-head {
                display: block;
                margin-bottom: 1.25rem;
            }

            .clerk-page-copy {
                max-width: 100%;
            }

            .clerk-workspace,
            .st-key-review-workspace {
                padding: 1rem;
            }
        }

        @media (max-width: 540px) {
            .clerk-review-field {
                grid-template-columns: 1fr auto;
            }

            .clerk-field-value,
            .clerk-field-source {
                grid-column: 1 / -1;
            }

            .clerk-field-source {
                margin-top: -0.5rem;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            *,
            *::before,
            *::after {
                scroll-behavior: auto !important;
                transition-duration: 0.01ms !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
