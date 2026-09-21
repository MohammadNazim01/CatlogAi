"""Helpers for tests that need a real PostgreSQL database (never SQLite: partial indexes,
JSONB, triggers and CHECK constraints are the behaviour under test)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import get_settings
from app.core.exceptions import violated_constraint

BACKEND_DIR = Path(__file__).resolve().parent.parent


def database_url(name: str) -> URL:
    """The configured server (DATABASE_URL), with the database name replaced."""
    return make_url(get_settings().database_url).set(database=name)


def alembic_config(url: URL) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    return cfg


async def run_alembic(cfg: Config, fn: str, *args: str) -> None:
    # env.py calls asyncio.run(); run it in a worker thread so it gets its own event loop.
    await asyncio.to_thread(getattr(command, fn), cfg, *args)


@asynccontextmanager
async def temporary_database(name: str) -> AsyncIterator[URL]:
    """Create a brand-new empty database, and drop it afterwards."""
    admin: AsyncEngine = create_async_engine(
        database_url("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield database_url(name)
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@asynccontextmanager
async def violates(
    session: AsyncSession, constraint: str | None = None, message: str | None = None
) -> AsyncIterator[None]:
    """Assert the block raises an integrity error (optionally naming the violated constraint).
    Runs in a SAVEPOINT so the surrounding test transaction stays usable afterwards."""
    with pytest.raises(IntegrityError) as info:
        async with session.begin_nested():
            yield
    if constraint is not None:
        assert violated_constraint(info.value) == constraint
    if message is not None:
        assert message in str(info.value.orig)
