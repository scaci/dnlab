"""Alembic migration environment for the auth DB.

Uses the same asyncpg driver as the runtime (no separate psycopg2
dependency). Online migrations spin up a short-lived async engine and
run migrations inside a sync bridge; offline mode emits raw SQL.

The DB URL is pulled from ``app.config.settings.AUTH_DATABASE_URL`` —
Alembic never needs its own copy of the secret.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make `app` importable when running `alembic` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.db import Base  # noqa: E402
from app.auth import models  # noqa: E402,F401  -- register models with Base
from app.config import settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.AUTH_DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without opening a DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
