from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.api.v1.router import api_router
from app.core.redis import get_redis
from app.db.session import get_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await get_engine().dispose()
    await get_redis().aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="CatalogAI API", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(api_router)
    return app


app = create_app()
