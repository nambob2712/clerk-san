"""Static contracts for the PostgreSQL-only recurring-bill migration."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def normalized_sql() -> str:
    return re.sub(r"\s+", " ", (ROOT / "migrations/0005_recurring_bills.sql").read_text()).lower()


def normalized_versioning_sql() -> str:
    return re.sub(
        r"\s+", " ", (ROOT / "migrations/0007_recurring_bill_versions.sql").read_text()
    ).lower()


def test_recurring_bills_link_one_verified_source_to_one_normalized_period() -> None:
    sql = normalized_sql()

    assert "create table if not exists issuers" in sql
    assert "create table if not exists recurring_bills" in sql
    assert "verified_record_id uuid not null references verified_records(id)" in sql
    assert "document_id uuid not null references documents(id)" in sql
    assert "unique (verified_record_id)" in sql
    assert "unique (issuer_id, billing_period)" in sql
    assert "billing_period = date_trunc('month', billing_period)::date" in sql
    assert "consumption_unit is not null" in sql
    assert "create trigger recurring_bills_source_document_guard" in sql


def test_recurring_bill_payment_changes_are_trigger_audited() -> None:
    sql = normalized_sql()

    assert "create or replace function audit_recurring_bill_payment_update" in sql
    assert "after update of payment_status, paid_at, reviewer on recurring_bills" in sql
    for field in ("payment_status", "paid_at", "reviewer"):
        assert f"'recurring_bills', new.id::text, 'update', '{field}'" in sql


def test_reprocessing_preserves_bill_history_and_audits_reviewed_bill_corrections() -> None:
    sql = normalized_versioning_sql()

    assert "add column if not exists review_corrections jsonb" in sql
    assert "add column if not exists superseded_at timestamptz" in sql
    assert "drop constraint if exists recurring_bills_issuer_period_key" in sql
    assert "create unique index if not exists recurring_bills_active_issuer_period_key" in sql
    assert "where superseded_at is null" in sql
    assert "create trigger recurring_bills_projection_immutable" in sql
    assert "create trigger recurring_bills_review_correction_audit" in sql
    for field in (
        "issuer_name",
        "issuer_kind",
        "billing_period",
        "due_date",
        "consumption_value",
        "consumption_unit",
    ):
        assert f"when '{field}'" in sql
