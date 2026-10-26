"""Async database engine, session factory, and health check."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def normalize_database_url(raw_url: str) -> URL:
    """Return a SQLAlchemy URL that uses the asyncpg driver.

    Supabase hands out ``postgres://`` / ``postgresql://`` URLs; SQLAlchemy needs
    the explicit ``+asyncpg`` driver.
    """
    url = raw_url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = f"postgresql+asyncpg://{url[len(prefix) :]}"
            break
    return make_url(url)


def build_engine_options(url: URL) -> tuple[URL, dict[str, object]]:
    """Translate libpq-style query params into asyncpg connect arguments.

    Supabase's transaction pooler (port 6543) does not support prepared
    statements across connections, so ``statement_cache_size=0`` is forced.
    ``sslmode`` is not an asyncpg kwarg and is removed from the URL.
    """
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    pgbouncer = str(query.pop("pgbouncer", "")).lower() in {"1", "true", "yes", "on"}
    raw_cache_size = query.pop("statement_cache_size", None)

    url = url.set(query=query)

    connect_args: dict[str, object] = {}
    if sslmode is not None:
        # asyncpg takes a bool or an SSLContext; Supabase's CA is publicly
        # trusted, so the default context is correct for require/verify-*.
        connect_args["ssl"] = sslmode.lower() not in {"disable", "allow", "prefer"}

    transaction_pooler = url.port == 6543 or pgbouncer
    if raw_cache_size is not None:
        connect_args["statement_cache_size"] = int(raw_cache_size)
    elif transaction_pooler:
        connect_args["statement_cache_size"] = 0

    return url, connect_args


def get_engine() -> AsyncEngine:
    """Create (once) and return the process-wide async engine."""
    global _engine

    if _engine is None:
        settings = get_settings()
        url, connect_args = build_engine_options(normalize_database_url(settings.database_url))
        kwargs: dict[str, object] = {
            "echo": False,
            "pool_pre_ping": True,
            "connect_args": connect_args,
        }

        if url.get_backend_name() != "sqlite":
            kwargs.update(
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
                pool_recycle=1800,
            )

        _engine = create_async_engine(url, **kwargs)  # type: ignore[arg-type]
        logger.info(
            "database engine created",
            extra={
                "host": url.host,
                "port": url.port,
                "database": url.database,
                "statement_cache_size": connect_args.get("statement_cache_size"),
            },
        )

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory."""
    global _session_factory

    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )

    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields one session per request."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Session context manager for background tasks."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def ping_database(session: AsyncSession | None = None) -> tuple[bool, str | None]:
    """Return ``(ok, error)`` for the health endpoint."""
    try:
        if session is not None:
            await session.execute(text("select 1"))
        else:
            async with session_scope() as own_session:
                await own_session.execute(text("select 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 - health checks must not raise
        logger.warning("database health check failed", extra={"error": str(exc)})
        return False, str(exc)


async def dispose_engine() -> None:
    """Close pooled connections on shutdown."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        logger.info("database engine disposed")

    _engine = None
    _session_factory = None
