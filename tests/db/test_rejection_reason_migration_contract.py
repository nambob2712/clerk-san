"""Static contracts for immutable, auditable extraction rejection reasons."""

from __future__ import annotations

import re
from pathlib import Path


def _sql() -> str:
    migration = (
        Path(__file__).resolve().parents[2] / "migrations" / "0012_extraction_rejection_reason.sql"
    )
    return re.sub(r"\s+", " ", migration.read_text(encoding="utf-8")).lower()


def test_rejection_reason_is_immutable_and_trigger_audited() -> None:
    sql = _sql()

    assert "add column if not exists rejection_reason text" in sql
    assert "legacy rejection reason unavailable at migration" in sql
    assert "where status = 'rejected'" in sql
    assert "extracted_records_rejection_reason_nonblank" in sql
    assert "create trigger extracted_records_rejection_reason_guard" in sql
    assert "old.status = 'pending_review'" in sql
    assert "new.status = 'rejected'" in sql
    assert "after update of status, version, reviewer, rejection_reason, reviewed_at" in sql
    assert "'rejection_reason'" in sql
    assert sql.index("create trigger extracted_records_lifecycle_audit") < sql.index(
        "update extracted_records"
    )
