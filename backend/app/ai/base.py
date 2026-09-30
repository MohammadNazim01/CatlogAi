"""Provider abstraction (docs/03 §11). Business/service code depends only on this module,
never on a specific SDK or a concrete provider class. `LLMProvider` is a `Protocol`, so
`FakeProvider` and `GeminiProvider` need no shared base class — either just has to have the
right shape."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError


class ProviderError(Exception):
    """Base for every provider-level failure."""


class ProviderTransientError(ProviderError):
    """429, 5xx, timeout, network — safe to retry."""


class ProviderPermanentError(ProviderError):
    """400, content refusal, auth, bad configuration — retrying won't help."""


class ProviderOutputError(ProviderError):
    """The call itself succeeded, but the response didn't validate against the requested
    schema. Distinct from the two above: this is a content problem, not a transport one."""


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    mime_type: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    parsed: T
    usage: Usage
    raw: str  # the provider's raw text response, kept for logging/debugging


class LLMProvider(Protocol):
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
        """Ask the model to produce output matching `schema`. Implementations should use the
        provider's own schema-enforced/structured-output mode where one exists, but must still
        validate the result with Pydantic before returning it — provider-side enforcement
        improves reliability, it isn't a semantic guarantee (docs/03 §11)."""
        ...


def parse_structured_response[T: BaseModel](raw: str, schema: type[T]) -> T:
    """Shared by every provider implementation. Raises `ProviderOutputError` for anything that
    isn't valid JSON matching `schema` — malformed syntax and schema mismatches alike, since
    Pydantic v2's JSON validation reports both through the same `ValidationError`."""
    try:
        return schema.model_validate_json(raw)
    except ValidationError as exc:
        raise ProviderOutputError(f"response did not match {schema.__name__}: {exc}") from exc
