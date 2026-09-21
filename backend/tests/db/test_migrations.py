"""Migration lifecycle against a fresh database on the real PostgreSQL server."""

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.dbutils import alembic_config, database_url, run_alembic, temporary_database

EXPECTED_TABLES = {
    "users",
    "refresh_tokens",
    "products",
    "product_images",
    "ai_jobs",
    "catalog_versions",
    "marketplaces",
    "exports",
    "export_items",
}


async def _scalars(url: object, sql: str) -> list[object]:
    engine = create_async_engine(url, poolclass=NullPool)  # type: ignore[arg-type]
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(sql))).scalars())
    finally:
        await engine.dispose()


async def _tables(url: object) -> set[object]:
    return set(await _scalars(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))


def test_there_is_exactly_one_migration_head() -> None:
    script = ScriptDirectory.from_config(alembic_config(database_url("unused")))
    assert len(script.get_heads()) == 1


async def test_upgrade_head_on_fresh_database() -> None:
    async with temporary_database("catalogai_migration_test") as url:
        await run_alembic(alembic_config(url), "upgrade", "head")

        assert await _tables(url) == EXPECTED_TABLES | {"alembic_version"}
        assert await _scalars(url, "SELECT version_num FROM alembic_version") == ["0001"]
        # The migration itself must install the extension (fresh DB had none).
        assert await _scalars(url, "SELECT extname FROM pg_extension WHERE extname='pg_trgm'") == [
            "pg_trgm"
        ]
        codes = await _scalars(url, "SELECT code FROM marketplaces ORDER BY code")
        assert codes == ["AMAZON_STYLE", "FLIPKART_STYLE", "GENERIC"]


async def test_upgrade_is_idempotent() -> None:
    async with temporary_database("catalogai_migration_test") as url:
        cfg = alembic_config(url)
        await run_alembic(cfg, "upgrade", "head")
        await run_alembic(cfg, "upgrade", "head")  # already at head: must be a no-op
        assert await _scalars(url, "SELECT count(*) FROM marketplaces") == [3]


async def test_alembic_check_reports_no_model_drift() -> None:
    """`alembic check` raises if the models and the migrated schema disagree (types, columns,
    nullability, server defaults, indexes, FKs). It does not compare CHECK constraints or
    partial-index predicates, which test_schema.py verifies directly against pg_catalog."""
    async with temporary_database("catalogai_migration_test") as url:
        cfg = alembic_config(url)
        await run_alembic(cfg, "upgrade", "head")
        await run_alembic(cfg, "check")


async def test_downgrade_to_base_removes_everything_then_upgrade_works_again() -> None:
    async with temporary_database("catalogai_migration_test") as url:
        cfg = alembic_config(url)
        await run_alembic(cfg, "upgrade", "head")
        await run_alembic(cfg, "downgrade", "base")

        assert await _tables(url) == {"alembic_version"}
        assert (
            await _scalars(
                url, "SELECT proname FROM pg_proc WHERE proname='catalog_versions_guard'"
            )
            == []
        )
        assert await _scalars(url, "SELECT extname FROM pg_extension WHERE extname='pg_trgm'") == []

        await run_alembic(cfg, "upgrade", "head")
        assert await _tables(url) == EXPECTED_TABLES | {"alembic_version"}
