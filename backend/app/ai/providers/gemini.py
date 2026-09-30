"""Gemini adapter — the only file in the app that imports the google-genai SDK. Business
services depend on `LLMProvider` (app/ai/base.py) and never on this module or the SDK
directly, so switching or adding a provider never touches them.

Import order matters here: google-genai 2.25.0 has a circular-import bug in its own
`__init__.py` (a cold `import google.genai` fails with "cannot import name 'types' from
partially initialized module"). Importing `google.genai.types` first works around it — this is
a real bug in the SDK, reproduced outside this project, not an artifact of this codebase."""

from collections.abc import Sequence

import google.genai.types as genai_types  # import-order workaround, see module docstring
import httpx
from google.genai import Client, errors
from pydantic import BaseModel

from app.ai.base import (
    ImageInput,
    ProviderOutputError,
    ProviderPermanentError,
    ProviderTransientError,
    StructuredResult,
    Usage,
    parse_structured_response,
)
from app.core.config import Settings

_RATE_LIMITED = 429


class GeminiProvider:
    """Structured output is requested via the SDK's own response_schema/response_mime_type
    mode (docs/03 §11: "use each provider's schema-enforced mode"), and still re-validated
    with Pydantic — provider-side enforcement improves reliability, it isn't a semantic
    guarantee."""

    def __init__(self, settings: Settings) -> None:
        if settings.gemini_api_key is None:
            raise ProviderPermanentError("GEMINI_API_KEY is not configured")
        self._client = Client(api_key=settings.gemini_api_key.get_secret_value())

    async def generate_structured[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        images: Sequence[ImageInput] = (),
        schema: type[T],
        timeout_s: float,
    ) -> StructuredResult[T]:
        parts: list[genai_types.Part] = [genai_types.Part.from_text(text=user_text)]
        for image in images:
            parts.append(genai_types.Part.from_bytes(data=image.data, mime_type=image.mime_type))

        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            http_options=genai_types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=genai_types.Content(role="user", parts=parts),
                config=config,
            )
        except errors.ServerError as exc:
            raise ProviderTransientError(f"Gemini server error: {exc}") from exc
        except errors.ClientError as exc:
            if exc.code == _RATE_LIMITED:
                raise ProviderTransientError(f"Gemini rate limited: {exc}") from exc
            raise ProviderPermanentError(f"Gemini rejected the request: {exc}") from exc
        except errors.APIError as exc:  # anything else the SDK classified as an API error
            raise ProviderPermanentError(f"Gemini API error: {exc}") from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderTransientError(f"Gemini request failed: {exc}") from exc

        raw = response.text
        if raw is None:
            raise ProviderOutputError("Gemini returned no text content")

        parsed = response.parsed
        result = parsed if isinstance(parsed, schema) else parse_structured_response(raw, schema)

        usage = response.usage_metadata
        return StructuredResult(
            parsed=result,
            usage=Usage(
                input_tokens=(usage.prompt_token_count or 0) if usage else 0,
                output_tokens=(usage.candidates_token_count or 0) if usage else 0,
            ),
            raw=raw,
        )
