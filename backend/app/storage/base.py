"""Object storage, behind a Protocol so services never depend on a specific client library
(docs/01 §1: "providers/AI + storage adapters behind Protocols"). One implementation today
(`S3Storage`, used for both MinIO in dev and AWS S3 in prod); a test double can satisfy this
Protocol structurally without inheriting from anything."""

from typing import Protocol


class Storage(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        """Write an object. Raises on failure; never partially writes from the caller's view."""
        ...

    async def delete(self, key: str) -> None:
        """Remove an object. Deleting a key that doesn't exist is not an error."""
        ...

    async def presigned_get_url(self, key: str, *, expires_in: int) -> str:
        """A short-lived, signed GET URL. The bucket itself is never public."""
        ...
