"""S3-compatible object storage. Same code talks to MinIO in dev and AWS S3 in prod — only
`S3_ENDPOINT_URL` differs (docs/03 §14, D14). boto3 is synchronous, so every call runs in a
threadpool; the client itself is thread-safe and built once per process."""

from functools import lru_cache
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.storage.base import Storage

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class S3Storage:
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=(
                settings.s3_secret_access_key.get_secret_value()
                if settings.s3_secret_access_key
                else None
            ),
            # Path-style addressing: MinIO doesn't do virtual-hosted-style bucket DNS routing.
            # Also works against real S3, so one client config serves both.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        await run_in_threadpool(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def delete(self, key: str) -> None:
        await run_in_threadpool(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def presigned_get_url(self, key: str, *, expires_in: int) -> str:
        return await run_in_threadpool(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )


@lru_cache
def get_storage() -> Storage:
    return S3Storage(get_settings())
