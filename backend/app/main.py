import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.core.redis import get_redis
from app.db.session import get_engine

logger = logging.getLogger(__name__)
WARM_UP_TIMEOUT_S = 30


async def warm_up() -> None:
    """Open the first DB/Redis connections before serving traffic.

    The first connection pays for driver imports and SQLAlchemy's first-connect handshake,
    which can take seconds on a busy host and would make the first readiness probe fail.
    Non-fatal on purpose: if a dependency is down, /health/ready reports it and the app
    recovers by itself once the dependency returns.
    """
    try:
        async with asyncio.timeout(WARM_UP_TIMEOUT_S):
            await asyncio.gather(health.check_database(), health.check_redis())
    except Exception:
        logger.warning("dependency warm-up failed; /health/ready will report status", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await warm_up()
    yield
    await get_engine().dispose()
    await get_redis().aclose()


def create_app() -> FastAPI:
    settings = get_settings()  # fails fast on invalid production configuration
    setup_logging(settings.log_level)

    docs_off = {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(
        title="CatalogAI API",
        version="0.1.0",
        lifespan=lifespan,
        **(docs_off if settings.is_production else {}),  # type: ignore[arg-type]
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router)

    # add_middleware: last added is outermost. CORS must wrap request-context so that
    # preflight and error responses still get CORS headers.
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Requested-With", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
        )
    return app


app = create_app()
