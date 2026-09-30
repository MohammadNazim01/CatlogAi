import os

# Must be set before app modules read settings.
os.environ.setdefault("APP_ENV", "test")

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db.session import get_db  # noqa: E402
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


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
async def api(app: FastAPI, session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP client for the full app, wired to the per-test database session (rolled back after
    each test). Behaves like a real server: unhandled exceptions become 500 responses."""

    async def use_test_session() -> AsyncIterator[AsyncSession]:
        try:
            yield session
        except Exception:
            # A real request gets a fresh session; here the session is shared across requests,
            # so undo the failed unit of work (the SAVEPOINT) like closing a session would.
            await session.rollback()
            raise

    app.dependency_overrides[get_db] = use_test_session
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
