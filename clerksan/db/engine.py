"""Async SQLAlchemy engine and transactional session helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from clerksan.config import Settings, get_settings

_engines: dict[str, AsyncEngine] = {}
_sessionmakers: dict[str, async_sessionmaker[AsyncSession]] = {}


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the cached async engine for the supplied settings."""
    active_settings = settings or get_settings()
    url = active_settings.database_url
    if url not in _engines:
        kwargs: dict[str, object] = {"echo": active_settings.sql_echo, "pool_pre_ping": True}
        if not url.startswith("sqlite+"):
            kwargs.update(pool_size=max(2, active_settings.worker_concurrency + 1), max_overflow=2)
        _engines[url] = create_async_engine(url, **kwargs)
    return _engines[url]


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return the session factory paired with ``get_engine``."""
    active_settings = settings or get_settings()
    url = active_settings.database_url
    if url not in _sessionmakers:
        _sessionmakers[url] = async_sessionmaker(
            get_engine(active_settings), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmakers[url]


@asynccontextmanager
async def get_session(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Yield one transactional session and commit or roll back automatically."""
    session = get_sessionmaker(settings)()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engines() -> None:
    """Dispose cached engines; used by shutdown hooks and isolated tests."""
    engines = list(_engines.values())
    _engines.clear()
    _sessionmakers.clear()
    for engine in engines:
        await engine.dispose()
