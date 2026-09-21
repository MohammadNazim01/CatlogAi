import logging

import pytest

from app import main
from app.api import health


async def test_warm_up_opens_both_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def db() -> None:
        called.append("db")

    async def redis() -> None:
        called.append("redis")

    monkeypatch.setattr(health, "check_database", db)
    monkeypatch.setattr(health, "check_redis", redis)
    await main.warm_up()
    assert sorted(called) == ["db", "redis"]


async def test_warm_up_failure_is_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def down() -> None:
        raise ConnectionError("db down")

    async def ok() -> None:
        return None

    monkeypatch.setattr(health, "check_database", down)
    monkeypatch.setattr(health, "check_redis", ok)
    with caplog.at_level(logging.WARNING):
        await main.warm_up()  # must not raise: the app should still start
    assert any("warm-up failed" in r.message for r in caplog.records)
