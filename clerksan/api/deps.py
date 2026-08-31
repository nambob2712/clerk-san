"""Shared FastAPI dependencies for request-scoped application services."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.config import Settings
from clerksan.db.engine import get_session


def settings_from_request(request: Request) -> Settings:
    return request.app.state.settings


async def database_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_session(settings_from_request(request)) as session:
        yield session
