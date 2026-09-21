"""Application settings, loaded once from the environment (and .env in development)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    # Development defaults match .env.example so `uvicorn` works against the compose services.
    database_url: str = Field(
        default="postgresql+asyncpg://catalogai:change-me-dev-only@localhost:55432/catalogai"
    )
    redis_url: str = Field(default="redis://:change-me-dev-only@localhost:6379/0")


@lru_cache
def get_settings() -> Settings:
    return Settings()
