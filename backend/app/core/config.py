"""Application settings, loaded once from the environment (and .env files in development).

`extra="ignore"` lets the shared repo-level .env carry variables that only compose reads
(POSTGRES_*, MINIO_*, ...). Production settings are validated at startup so a misconfigured
deployment refuses to boot instead of running with dev defaults.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Public on purpose: it exists only so development works with zero setup, and production
# validation rejects it.
DEV_JWT_SECRET = "dev-only-not-a-secret-replace-me-with-32+-random-bytes"  # noqa: S105
DEV_PLACEHOLDER = "change-me"
MIN_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    # Later files win; both are optional. ../.env is the repo-level file when running from backend/.
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Development defaults match .env.example so `uvicorn` works against the compose services.
    database_url: str = (
        "postgresql+asyncpg://catalogai:change-me-dev-only@localhost:55432/catalogai"
    )
    redis_url: str = "redis://:change-me-dev-only@localhost:6379/0"

    # --- Auth ---
    jwt_secret_key: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_algorithm: Literal["HS256"] = "HS256"  # pinned: a single service needs no asymmetric keys
    access_token_ttl_min: int = Field(default=15, ge=1, le=120)
    refresh_token_ttl_days: int = Field(default=14, ge=1, le=90)
    cookie_secure: bool = False
    # Comma-separated in the environment. Empty in same-origin production deployments.
    cors_origins: Annotated[list[str], NoDecode] = []

    # --- Object storage. Blank keys => default AWS credential chain. ---
    s3_endpoint_url: str | None = None
    s3_region: str = "ap-south-1"
    s3_bucket: str = "catalogai-dev"
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_presign_ttl_image_sec: int = Field(default=600, ge=1)

    # --- Image upload limits (docs/03 §14). max_image_bytes matches the DB CHECK constraint
    # on product_images.file_size; raising it here without a matching migration would just
    # trade a clean 413 for an opaque database error. ---
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_images_per_product: int = Field(default=8, ge=1, le=100)
    max_image_pixels: int = Field(default=40_000_000, ge=1)

    # --- AI (placeholders only in Phase 2; provider code arrives in Phase 4) ---
    ai_provider: Literal["fake", "gemini"] = "fake"
    gemini_api_key: SecretStr | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("s3_endpoint_url", "s3_access_key_id", "gemini_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _validate_environment(self) -> "Settings":
        if self.ai_provider == "gemini" and self.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is required when AI_PROVIDER=gemini")
        if self.app_env == "production":
            self._validate_production()
        return self

    def _validate_production(self) -> None:
        problems: list[str] = []
        secret = self.jwt_secret_key.get_secret_value()
        if secret == DEV_JWT_SECRET or len(secret) < MIN_JWT_SECRET_LENGTH:
            problems.append(
                f"JWT_SECRET_KEY must be a non-default value of >= {MIN_JWT_SECRET_LENGTH} chars"
            )
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true")
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must not contain '*'")
        if DEV_PLACEHOLDER in self.database_url or DEV_PLACEHOLDER in self.redis_url:
            problems.append("DATABASE_URL/REDIS_URL still contain the dev placeholder password")
        if self.ai_provider == "fake":
            problems.append("AI_PROVIDER=fake is not allowed in production")
        if problems:
            raise ValueError("invalid production configuration: " + "; ".join(problems))

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
