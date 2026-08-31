"""Actor attribution and read access for trigger-owned audit history."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.db.models import AuditEntry


def _clean_actor(actor: str) -> str:
    cleaned = actor.strip()
    if not cleaned:
        raise ValueError("audit actor must not be empty")
    return cleaned


@asynccontextmanager
async def audit_actor(session: AsyncSession, actor: str) -> AsyncIterator[None]:
    """Set the actor for audit events made in the enclosing transaction.

    PostgreSQL receives a transaction-local custom setting that the database trigger
    reads. SQLite is only supported for the documented local demo; it retains the
    value on the session for observability because PostgreSQL trigger guarantees do
    not exist there.
    """

    cleaned = _clean_actor(actor)
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        await session.execute(
            text("SELECT set_config('clerksan.actor', :actor, true)"), {"actor": cleaned}
        )
        yield
        return

    previous = session.info.get("clerksan.actor")
    session.info["clerksan.actor"] = cleaned
    try:
        yield
    finally:
        if previous is None:
            session.info.pop("clerksan.actor", None)
        else:
            session.info["clerksan.actor"] = previous


def _entry_to_dict(entry: AuditEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "actor": entry.actor,
        "table_name": entry.table_name,
        "row_pk": entry.row_pk,
        "action": entry.action,
        "field": entry.field,
        "old_value": entry.old_value,
        "new_value": entry.new_value,
        "at": entry.at,
    }


async def read_history(
    session: AsyncSession, *, table: str, row_pk: str, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    """Return one row's audit history in newest-first order."""

    if not table.strip() or not row_pk.strip():
        raise ValueError("table and row_pk must not be empty")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if offset < 0:
        raise ValueError("offset must not be negative")

    result = await session.scalars(
        select(AuditEntry)
        .where(AuditEntry.table_name == table, AuditEntry.row_pk == row_pk)
        .order_by(AuditEntry.at.desc(), AuditEntry.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_entry_to_dict(entry) for entry in result]
