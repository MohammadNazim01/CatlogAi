"""AI provider abstraction (Step 10): fake provider behavior, provider selection, and Gemini's
error mapping. No real network call is made anywhere in this file — Gemini SDK calls are
monkeypatched at the client boundary (docs/03 §11: "FakeProvider ... used by all tests and
CI"; the same discipline extends to unit-testing the Gemini adapter itself)."""

import httpx
import pytest
from google.genai import errors
from pydantic import BaseModel, SecretStr, ValidationError

from app.ai.base import (
    ProviderOutputError,
    ProviderPermanentError,
    ProviderTransientError,
    parse_structured_response,
)
from app.ai.factory import build_provider
from app.ai.providers.fake import FakeProvider
from app.ai.providers.gemini import GeminiProvider
from app.core.config import Settings


class _Widget(BaseModel):
    name: str
    count: int


class _FakeUsage:
    def __init__(self, prompt: int | None, candidates: int | None) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates


class _FakeResponse:
    def __init__(
        self, *, text: str | None, parsed: object | None, usage: _FakeUsage | None = None
    ) -> None:
        self.text = text
        self.parsed = parsed
        self.usage_metadata = usage


def _settings(**overrides: object) -> Settings:
    # _env_file=None: never let a developer's local .env leak into these tests.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _gemini_provider() -> GeminiProvider:
    return GeminiProvider(_settings(ai_provider="gemini", gemini_api_key=SecretStr("test-key")))


class TestFakeProvider:
    async def test_queued_success_is_returned(self) -> None:
        fake = FakeProvider()
        fake.queue_success(_Widget(name="bolt", count=3))
        result = await fake.generate_structured(
            model="x", system="s", user_text="u", schema=_Widget, timeout_s=1.0
        )
        assert result.parsed == _Widget(name="bolt", count=3)
        assert result.usage.input_tokens == 0

    async def test_queued_error_is_raised(self) -> None:
        fake = FakeProvider()
        fake.queue_error(ProviderTransientError("rate limited"))
        with pytest.raises(ProviderTransientError, match="rate limited"):
            await fake.generate_structured(
                model="x", system="s", user_text="u", schema=_Widget, timeout_s=1.0
            )

    async def test_queued_raw_json_is_parsed_and_validated(self) -> None:
        fake = FakeProvider()
        fake.queue_raw('{"name": "nut", "count": 7}')
        result = await fake.generate_structured(
            model="x", system="s", user_text="u", schema=_Widget, timeout_s=1.0
        )
        assert result.parsed == _Widget(name="nut", count=7)

    async def test_malformed_raw_response_raises_output_error(self) -> None:
        fake = FakeProvider()
        fake.queue_raw("not json at all")
        with pytest.raises(ProviderOutputError):
            await fake.generate_structured(
                model="x", system="s", user_text="u", schema=_Widget, timeout_s=1.0
            )

    async def test_raw_json_not_matching_the_schema_raises_output_error(self) -> None:
        fake = FakeProvider()
        fake.queue_raw('{"unexpected": true}')
        with pytest.raises(ProviderOutputError):
            await fake.generate_structured(
                model="x", system="s", user_text="u", schema=_Widget, timeout_s=1.0
            )

    async def test_wrong_type_queued_raises_output_error(self) -> None:
        class _Other(BaseModel):
            x: int

        fake = FakeProvider()
        fake.queue_success(_Other(x=1))
        with pytest.raises(ProviderOutputError):
            await fake.generate_structured(
                model="x", system="s", user_text="u", schema=_Widget, timeout_s=1.0
            )

    async def test_calling_with_nothing_queued_fails_loudly(self) -> None:
        fake = FakeProvider()
        with pytest.raises(ProviderPermanentError):
            await fake.generate_structured(
                model="x", system="s", user_text="u", schema=_Widget, timeout_s=1.0
            )

    async def test_records_every_call(self) -> None:
        fake = FakeProvider()
        fake.queue_success(_Widget(name="a", count=1))
        await fake.generate_structured(
            model="gemini-x", system="sys", user_text="hello", schema=_Widget, timeout_s=5.0
        )
        assert fake.calls == [
            {
                "model": "gemini-x",
                "system": "sys",
                "user_text": "hello",
                "images": [],
                "schema": _Widget,
                "timeout_s": 5.0,
            }
        ]


class TestParseStructuredResponse:
    def test_valid_json_parses(self) -> None:
        assert parse_structured_response('{"name": "a", "count": 1}', _Widget) == _Widget(
            name="a", count=1
        )

    def test_invalid_json_raises_output_error(self) -> None:
        with pytest.raises(ProviderOutputError):
            parse_structured_response("{not json", _Widget)

    def test_schema_mismatch_raises_output_error(self) -> None:
        with pytest.raises(ProviderOutputError):
            parse_structured_response('{"count": "not a number"}', _Widget)


class TestProviderSelection:
    def test_fake_provider_selected_by_default(self) -> None:
        assert isinstance(build_provider(_settings()), FakeProvider)

    def test_gemini_provider_selected_when_configured(self) -> None:
        settings = _settings(ai_provider="gemini", gemini_api_key=SecretStr("test-key"))
        assert isinstance(build_provider(settings), GeminiProvider)


class TestMissingConfiguration:
    def test_settings_reject_gemini_without_an_api_key(self) -> None:
        with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
            _settings(ai_provider="gemini", gemini_api_key=None)

    def test_gemini_provider_refuses_to_construct_without_a_key(self) -> None:
        # Settings' own validator already forbids this combination (above); this proves
        # GeminiProvider defends itself too, for any caller that builds one directly.
        settings = _settings()  # ai_provider="fake" by default, so construction succeeds here
        settings.ai_provider = "gemini"
        settings.gemini_api_key = None
        with pytest.raises(ProviderPermanentError, match="GEMINI_API_KEY"):
            GeminiProvider(settings)


class TestGeminiCallShape:
    """The successful path: the SDK's own `.parsed`, and the fallback to manual validation of
    `.text` when the SDK didn't (or couldn't) parse it."""

    def _patch(
        self, monkeypatch: pytest.MonkeyPatch, provider: GeminiProvider, response: _FakeResponse
    ) -> None:
        async def fake_call(*args: object, **kwargs: object) -> _FakeResponse:
            return response

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_call)

    async def test_uses_the_sdks_own_parsed_result_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _gemini_provider()
        widget = _Widget(name="a", count=1)
        self._patch(
            monkeypatch,
            provider,
            _FakeResponse(text=widget.model_dump_json(), parsed=widget, usage=_FakeUsage(10, 5)),
        )
        result = await provider.generate_structured(
            model="m", system="s", user_text="u", schema=_Widget, timeout_s=1.0
        )
        assert result.parsed == widget
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    async def test_falls_back_to_manual_validation_when_sdk_parsed_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _gemini_provider()
        self._patch(
            monkeypatch, provider, _FakeResponse(text='{"name":"b","count":2}', parsed=None)
        )
        result = await provider.generate_structured(
            model="m", system="s", user_text="u", schema=_Widget, timeout_s=1.0
        )
        assert result.parsed == _Widget(name="b", count=2)
        assert result.usage.input_tokens == 0

    async def test_malformed_text_raises_output_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _gemini_provider()
        self._patch(monkeypatch, provider, _FakeResponse(text="not json", parsed=None))
        with pytest.raises(ProviderOutputError):
            await provider.generate_structured(
                model="m", system="s", user_text="u", schema=_Widget, timeout_s=1.0
            )

    async def test_no_text_content_raises_output_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _gemini_provider()
        self._patch(monkeypatch, provider, _FakeResponse(text=None, parsed=None))
        with pytest.raises(ProviderOutputError):
            await provider.generate_structured(
                model="m", system="s", user_text="u", schema=_Widget, timeout_s=1.0
            )


class TestGeminiErrorMapping:
    """docs/03 §11: 429/5xx/timeout/network -> transient; 400/content-refusal/auth -> permanent."""

    def _patch_raising(
        self, monkeypatch: pytest.MonkeyPatch, provider: GeminiProvider, exc: Exception
    ) -> None:
        async def fake_call(*args: object, **kwargs: object) -> _FakeResponse:
            raise exc

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_call)

    async def _call(self, provider: GeminiProvider) -> None:
        await provider.generate_structured(
            model="m", system="s", user_text="u", schema=_Widget, timeout_s=1.0
        )

    async def test_server_error_is_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _gemini_provider()
        self._patch_raising(
            monkeypatch, provider, errors.ServerError(500, {"error": {"message": "internal"}})
        )
        with pytest.raises(ProviderTransientError):
            await self._call(provider)

    async def test_rate_limit_is_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _gemini_provider()
        self._patch_raising(
            monkeypatch, provider, errors.ClientError(429, {"error": {"message": "slow down"}})
        )
        with pytest.raises(ProviderTransientError):
            await self._call(provider)

    async def test_other_client_error_is_permanent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _gemini_provider()
        self._patch_raising(
            monkeypatch, provider, errors.ClientError(401, {"error": {"message": "bad key"}})
        )
        with pytest.raises(ProviderPermanentError):
            await self._call(provider)

    async def test_bad_request_is_permanent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _gemini_provider()
        self._patch_raising(
            monkeypatch, provider, errors.ClientError(400, {"error": {"message": "refused"}})
        )
        with pytest.raises(ProviderPermanentError):
            await self._call(provider)

    async def test_timeout_is_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _gemini_provider()
        self._patch_raising(monkeypatch, provider, httpx.TimeoutException("timed out"))
        with pytest.raises(ProviderTransientError):
            await self._call(provider)

    async def test_network_error_is_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _gemini_provider()
        self._patch_raising(monkeypatch, provider, httpx.ConnectError("connection refused"))
        with pytest.raises(ProviderTransientError):
            await self._call(provider)
