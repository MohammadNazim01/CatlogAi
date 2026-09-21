import asyncio

import pytest
from httpx import AsyncClient

from app.api import health


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise ConnectionError("secret-host:5432 refused")


async def _hang() -> None:
    await asyncio.sleep(60)


async def test_live_needs_no_dependencies(client: AsyncClient) -> None:
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_ok(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "check_database", _ok)
    monkeypatch.setattr(health, "check_redis", _ok)
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_ready_503_and_no_error_leak(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "check_database", _boom)
    monkeypatch.setattr(health, "check_redis", _ok)
    r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": "fail", "redis": "ok"}
    assert "secret-host" not in r.text


async def test_ready_times_out_slow_dependency(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(health, "check_database", _ok)
    monkeypatch.setattr(health, "check_redis", _hang)
    r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["redis"] == "fail"
