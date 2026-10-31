"""Async engine and session factory.

Every process (worker, scheduler, seed, tests) builds its sessions here so pool settings and
`expire_on_commit=False` are consistent everywhere.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(url: str, *, pool_size: int = 5) -> AsyncEngine:
    """Create an async engine. `pool_pre_ping` survives Postgres restarts in dev."""
    return create_async_engine(url, pool_size=pool_size, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)
