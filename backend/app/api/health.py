"""Liveness/readiness probes. Unversioned on purpose: they are ops endpoints, not product API."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.redis import get_redis
from app.db.session import get_engine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])

PROBE_TIMEOUT_S = 5.0  # generous: readiness is off the hot path and cold connections can be slow


async def check_database() -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def check_redis() -> None:
    await get_redis().ping()


async def _probe(name: str, check: Callable[[], Awaitable[None]]) -> str:
    try:
        await asyncio.wait_for(check(), timeout=PROBE_TIMEOUT_S)
    except Exception:
        # Details go to the log only; the response must not leak hostnames or driver errors.
        logger.exception("readiness check failed: %s", name)
        return "fail"
    return "ok"


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response) -> dict[str, object]:
    # Looked up at call time (not import time) so tests can replace the checks.
    checks = {"database": check_database, "redis": check_redis}
    results = dict(
        zip(
            checks,
            await asyncio.gather(*(_probe(n, c) for n, c in checks.items())),
            strict=True,
        )
    )
    healthy = all(v == "ok" for v in results.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "unavailable", "checks": results}
