from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from clerksan.api.main import create_app
from clerksan.bills.service import record_verified_bill
from clerksan.config import Settings
from clerksan.db.engine import get_session
from clerksan.db.models import DocumentClass
from clerksan.db.repositories import DocumentRepo, ExtractionRepo, VerifiedRepo


async def seed_bill(settings: Settings) -> tuple[UUID, UUID]:
    async with get_session(settings) as session:
        document_id = await DocumentRepo(session).create_with_raw(
            filename="electricity.pdf",
            content_path="/tmp/electricity.pdf",
            sha256="d" * 64,
            mime="application/pdf",
            doc_class=DocumentClass.RECURRING_BILL,
        )
        extraction_id = await ExtractionRepo(session).add(
            document_id,
            payload={
                "transaction_date": {"value": "2026-07-13", "confidence": 0.99},
                "total_amount": {"value": 1200, "confidence": 0.99},
                "counterparty": {"value": "東京電力", "confidence": 0.99},
                "currency": {"value": "JPY", "confidence": 0.99},
            },
            field_confidences={},
            model_name="test",
            prompt_version="test",
            actor="worker",
        )
        verified_id = await VerifiedRepo(session).promote(
            extraction_id,
            1,
            corrections={},
            reviewer="reviewer",
        )
        bill = await record_verified_bill(
            session,
            verified_record_id=verified_id,
            issuer_name="東京電力",
            issuer_kind="electric",
            billing_period=dt.date.today().replace(day=1),
            due_date=dt.date.today(),
            consumption_value=42,
            consumption_unit="kWh",
        )
        return bill.id, bill.issuer_id


def test_bills_routes_list_analyze_remind_and_mark_paid(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'bills-route.sqlite'}",
        storage_dir=tmp_path / "storage",
        demo_mode=True,
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        bill_id, issuer_id = asyncio.run(seed_bill(settings))

        listed = client.get("/bills")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == str(bill_id)
        assert listed.json()[0]["issuer_id"] == str(issuer_id)
        assert listed.json()[0]["payment_status"] == "unpaid"

        analysis = client.get(f"/bills/{issuer_id}/analysis")
        assert analysis.status_code == 200
        assert (
            analysis.json()["comparisons"][0]["billing_period"]
            == dt.date.today().replace(day=1).isoformat()
        )

        reminders = client.get("/bills/reminders?days_ahead=0")
        assert reminders.status_code == 200
        assert reminders.json()["upcoming"][0]["id"] == str(bill_id)

        invalid_actor = client.post(f"/bills/{bill_id}/mark-paid?actor=%20%20")
        assert invalid_actor.status_code == 422
        assert invalid_actor.json()["detail"] == "actor must not be empty"

        paid = client.post(f"/bills/{bill_id}/mark-paid?actor=reviewer")
        assert paid.status_code == 200
        assert paid.json()["status"] == "paid"

        assert client.get("/bills").json()[0]["payment_status"] == "paid"
