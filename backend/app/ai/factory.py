"""Provider selection (docs/03 §11: "selected by config, not hard-coded")."""

from functools import lru_cache

from app.ai.base import LLMProvider
from app.ai.providers.fake import FakeProvider
from app.ai.providers.gemini import GeminiProvider
from app.core.config import Settings, get_settings


def build_provider(settings: Settings) -> LLMProvider:
    if settings.ai_provider == "fake":
        return FakeProvider()
    if settings.ai_provider == "gemini":
        return GeminiProvider(settings)
    raise AssertionError(f"unhandled ai_provider: {settings.ai_provider!r}")  # exhaustiveness


@lru_cache
def get_provider() -> LLMProvider:
    return build_provider(get_settings())
