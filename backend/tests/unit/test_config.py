import pytest
from pydantic import ValidationError

from app.core.config import DEV_JWT_SECRET, Settings

VALID_PROD = {
    "app_env": "production",
    "jwt_secret_key": "x" * 48,
    "cookie_secure": True,
    "cors_origins": [],
    "database_url": "postgresql+asyncpg://u:Str0ngPw@db:5432/catalogai",
    "redis_url": "redis://:Str0ngPw@redis:6379/0",
    "ai_provider": "gemini",
    "gemini_api_key": "test-key",
}


def make(**overrides: object) -> Settings:
    # _env_file=None: never let a developer's local .env leak into these tests.
    return Settings(_env_file=None, **{**VALID_PROD, **overrides})  # type: ignore[arg-type]


def test_development_defaults_work_with_zero_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("APP_ENV", "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "AI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.app_env == "development"
    assert s.jwt_secret_key.get_secret_value() == DEV_JWT_SECRET
    assert s.ai_provider == "fake"


def test_valid_production_config_is_accepted() -> None:
    assert make().is_production


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"jwt_secret_key": DEV_JWT_SECRET}, "JWT_SECRET_KEY"),
        ({"jwt_secret_key": "short"}, "JWT_SECRET_KEY"),
        ({"cookie_secure": False}, "COOKIE_SECURE"),
        ({"cors_origins": ["*"]}, "CORS_ORIGINS"),
        ({"database_url": "postgresql+asyncpg://u:change-me-dev-only@db/x"}, "placeholder"),
        ({"redis_url": "redis://:change-me-dev-only@redis/0"}, "placeholder"),
        ({"ai_provider": "fake", "gemini_api_key": None}, "AI_PROVIDER=fake"),
    ],
)
def test_production_rejects_unsafe_config(override: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        make(**override)


def test_all_problems_are_reported_together() -> None:
    with pytest.raises(ValidationError) as exc:
        make(cookie_secure=False, cors_origins=["*"])
    assert "COOKIE_SECURE" in str(exc.value) and "CORS_ORIGINS" in str(exc.value)


def test_gemini_requires_key_in_any_environment() -> None:
    with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
        Settings(_env_file=None, app_env="development", ai_provider="gemini", gemini_api_key="")


def test_cors_origins_parse_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://a.test, http://b.test ,")
    assert Settings(_env_file=None).cors_origins == ["http://a.test", "http://b.test"]


def test_blank_optional_values_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    s = Settings(_env_file=None)
    assert s.s3_endpoint_url is None and s.gemini_api_key is None


def test_secrets_are_not_leaked_by_repr() -> None:
    assert "x" * 48 not in repr(make())
