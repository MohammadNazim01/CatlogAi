"""Deterministic, in-process provider — the only one used in tests and CI (docs/03 §11:
"FakeProvider (deterministic; used by all tests and CI)"). Never makes a network call."""

from collections import deque
from collections.abc import Sequence

from pydantic import BaseModel

from app.ai.base import (
    ImageInput,
    ProviderOutputError,
    ProviderPermanentError,
    StructuredResult,
    Usage,
    parse_structured_response,
)

_QueueItem = BaseModel | Exception | str


class FakeProvider:
    """Script each call with `queue_success` / `queue_error` / `queue_raw`, then exercise the
    code under test. Every call is recorded in `calls`, so a test can also assert on what it
    was asked to do (model name, prompt, image count, ...)."""

    def __init__(self) -> None:
        self._queue: deque[_QueueItem] = deque()
        self.calls: list[dict[str, object]] = []

    def queue_success(self, result: BaseModel) -> None:
        self._queue.append(result)

    def queue_error(self, error: Exception) -> None:
        self._queue.append(error)

    def queue_raw(self, raw: str) -> None:
        """Queue a raw response string instead of a ready-made model. Exercises the exact same
        `parse_structured_response` path a real provider's response goes through, including
        the malformed-JSON / schema-mismatch failure case."""
        self._queue.append(raw)

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
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user_text": user_text,
                "images": list(images),
                "schema": schema,
                "timeout_s": timeout_s,
            }
        )
        if not self._queue:
            raise ProviderPermanentError(
                "FakeProvider.generate_structured called with nothing queued — "
                "call queue_success/queue_error/queue_raw first"
            )
        item = self._queue.popleft()

        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return StructuredResult(
                parsed=parse_structured_response(item, schema), usage=Usage(0, 0), raw=item
            )
        if not isinstance(item, schema):
            raise ProviderOutputError(
                f"queued response is a {type(item).__name__}, not the requested {schema.__name__}"
            )
        return StructuredResult(parsed=item, usage=Usage(0, 0), raw=item.model_dump_json())
