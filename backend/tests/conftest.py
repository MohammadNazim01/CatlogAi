import os

# Must be set before app modules read settings.
os.environ.setdefault("APP_ENV", "test")

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.main import create_app  # noqa: E402
from tests.dbutils import alembic_config, run_alembic, temporary_database  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """A throwaway database on the real PostgreSQL server, built by running the actual
    migrations, so tests exercise the same schema production gets."""
    async with temporary_database("catalogai_test") as url:
        await run_alembic(alembic_config(url), "upgrade", "head")
        engine = create_async_engine(url, poolclass=NullPool)
        yield engine
        await engine.dispose()


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test isolation: everything happens in a transaction that is rolled back.
    session.commit() only releases a SAVEPOINT, so service code that commits still works."""
    async with db_engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield s
        finally:
            await s.close()
            await outer.rollback()
