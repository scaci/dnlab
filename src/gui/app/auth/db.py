"""Async SQLAlchemy engine and session factory for the auth DB.

The engine connects to Postgres over loopback (see
``deploy/auth/docker-compose.yml``). Connection URL is resolved from
``settings.AUTH_DATABASE_URL`` and must use the asyncpg driver so it
cooperates with FastAPI's event loop.

Use :func:`get_session` as a FastAPI dependency in auth-aware routes.
Alembic has its own connection setup in ``alembic/env.py``; do not
import the runtime engine from migration code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all auth ORM models."""


engine = create_async_engine(
    settings.AUTH_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    future=True,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an ``AsyncSession`` per request."""
    async with AsyncSessionLocal() as session:
        yield session
