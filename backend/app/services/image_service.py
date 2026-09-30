"""Image upload pipeline (docs/03 §14): sniff by content, reject anything unsafe, re-encode to
strip metadata (EXIF/GPS included), hash the *stored* bytes, then persist. Runs the CPU-bound
Pillow work in a threadpool so it never blocks the event loop."""

import hashlib
import io
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import UploadFile
from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    UnprocessableError,
    UnsupportedMediaTypeError,
)
from app.db.enums import ProductStatus
from app.db.models import Product, ProductImage
from app.repositories.image_repo import ImageRepository
from app.repositories.product_repo import ProductRepository
from app.storage.base import Storage

logger = logging.getLogger(__name__)

# format detected by Pillow (from actual bytes, never the client's Content-Type) -> stored mime
_ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
_MAX_SIDE_PX = 8000
_LOSSY_QUALITY = 85
_MIN_FILES = 1
_MAX_FILES_PER_REQUEST = 8

# Defense in depth on top of the explicit header-size check in _process_image: Pillow's own
# decompression-bomb guard, in case any code path ever reaches .load() before that check.
Image.MAX_IMAGE_PIXELS = 40_000_000


def _not_found_product() -> NotFoundError:
    return NotFoundError("PRODUCT_NOT_FOUND", "Product not found.")


def _not_found_image() -> NotFoundError:
    return NotFoundError("IMAGE_NOT_FOUND", "Image not found.")


def _locked() -> ConflictError:
    return ConflictError(
        "PRODUCT_LOCKED", "This product has an AI job in progress and cannot be edited."
    )


def _invalid() -> UnprocessableError:
    return UnprocessableError("IMAGE_INVALID", "The file is not a valid image.")


def _bad_request_count() -> UnprocessableError:
    return UnprocessableError(
        "VALIDATION_ERROR", f"Between {_MIN_FILES} and {_MAX_FILES_PER_REQUEST} files are required."
    )


def _safe_original_filename(name: str | None) -> str:
    # Display-only (never used to build a path or a storage key), but a client-supplied name
    # is still untrusted input: strip any directory components before it is ever shown back.
    if not name:
        return "image"
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (base or "image")[:255]


@dataclass(frozen=True)
class _Processed:
    data: bytes
    format: str  # one of _ALLOWED_FORMATS' keys
    mime_type: str
    width: int
    height: int
    sha256: str


def _process_image(raw: bytes, *, max_pixels: int) -> _Processed:
    """Pure and synchronous — the caller runs this in a threadpool. Never trusts the client's
    Content-Type or filename; the *content* is the only thing that decides what this is."""
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()  # cheap structural check; `probe` is unusable for anything else now
    except Exception as exc:
        raise _invalid() from exc

    fmt = probe.format
    if fmt not in _ALLOWED_FORMATS:
        raise UnsupportedMediaTypeError(
            "UNSUPPORTED_MEDIA_TYPE", "Only JPEG, PNG and WEBP images are accepted."
        )

    try:
        # verify() already consumed `probe`; reopen fresh. Annotated as the base `Image.Image`
        # since it's reassigned below to whatever exif_transpose()/convert() return.
        img: Image.Image = Image.open(io.BytesIO(raw))
        width, height = img.size
    except Exception as exc:
        raise _invalid() from exc

    # Pixel-bomb guard: reject on the header-declared size, *before* decoding any pixel data.
    if width > _MAX_SIDE_PX or height > _MAX_SIDE_PX or width * height > max_pixels:
        raise _invalid()

    try:
        img = ImageOps.exif_transpose(img)  # bake in rotation before the EXIF is discarded
        img.load()  # only now, with size already known-safe, decode the full image
    except Exception as exc:
        raise _invalid() from exc

    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")  # JPEG has no alpha channel

    out = io.BytesIO()
    save_kwargs: dict[str, object] = {"optimize": True}
    if fmt in ("JPEG", "WEBP"):
        save_kwargs["quality"] = _LOSSY_QUALITY
    # A fresh buffer, no `exif=`/`icc_profile=` kwarg: nothing from the original file's
    # metadata (EXIF, GPS, XMP, colour profile) survives. This also defeats polyglot payloads
    # and anything else riding along outside the actual pixel data.
    img.save(out, format=fmt, **save_kwargs)
    data = out.getvalue()

    return _Processed(
        data=data,
        format=fmt,
        mime_type=_ALLOWED_FORMATS[fmt],
        width=img.width,
        height=img.height,
        sha256=hashlib.sha256(data).hexdigest(),
    )


async def _read_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Reads in chunks and stops the instant the limit is crossed, instead of buffering an
    arbitrarily large upload before checking its size."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                "FILE_TOO_LARGE", f"Each image must be at most {max_bytes} bytes."
            )
        chunks.append(chunk)
    return b"".join(chunks)


class ImageService:
    def __init__(self, session: AsyncSession, storage: Storage, settings: Settings) -> None:
        self.session = session
        self.storage = storage
        self.settings = settings
        self.products = ProductRepository(session)
        self.images = ImageRepository(session)

    async def _owned_product(self, product_id: uuid.UUID, seller_id: uuid.UUID) -> Product:
        product = await self.products.get_owned(product_id, seller_id)
        if product is None:
            raise _not_found_product()
        return product

    async def list_images(
        self, product_id: uuid.UUID, seller_id: uuid.UUID
    ) -> Sequence[ProductImage]:
        await self._owned_product(product_id, seller_id)  # 404 before touching image rows
        return await self.images.list_for_product(product_id)

    async def upload(
        self, product_id: uuid.UUID, seller_id: uuid.UUID, files: list[UploadFile]
    ) -> Sequence[ProductImage]:
        if not (_MIN_FILES <= len(files) <= _MAX_FILES_PER_REQUEST):
            raise _bad_request_count()

        product = await self._owned_product(product_id, seller_id)
        if product.status == ProductStatus.PROCESSING:
            raise _locked()

        existing = await self.images.count_for_product(product_id)
        if existing + len(files) > self.settings.max_images_per_product:
            raise ConflictError(
                "IMAGE_LIMIT_REACHED", "This product already has the maximum number of images."
            )

        # Validate and re-encode every file *before* anything touches storage or the database:
        # the whole request is all-or-nothing.
        processed: list[_Processed] = []
        for f in files:
            raw = await _read_bounded(f, self.settings.max_image_bytes)
            processed.append(
                await run_in_threadpool(
                    _process_image, raw, max_pixels=self.settings.max_image_pixels
                )
            )

        next_order = await self.images.next_sort_order(product_id)
        uploaded_keys: list[str] = []
        rows: list[ProductImage] = []
        try:
            for i, (f, p) in enumerate(zip(files, processed, strict=True)):
                image_id = uuid.uuid4()
                key = f"products/{seller_id}/{product_id}/{image_id}.{_EXTENSIONS[p.format]}"
                try:
                    await self.storage.put(key, p.data, content_type=p.mime_type)
                except Exception as exc:
                    raise ServiceUnavailableError(
                        "STORAGE_UNAVAILABLE",
                        "Image storage is temporarily unavailable. Please try again.",
                    ) from exc
                uploaded_keys.append(key)

                row = ProductImage(
                    id=image_id,
                    product_id=product_id,
                    storage_key=key,
                    original_filename=_safe_original_filename(f.filename),
                    mime_type=p.mime_type,
                    file_size=len(p.data),
                    width=p.width,
                    height=p.height,
                    sha256=p.sha256,
                    sort_order=next_order + i,
                )
                self.images.add(row)
                rows.append(row)
            # A duplicate (product_id, sha256) violates ux_product_images_product_sha256; the
            # global handler turns that into 409 DUPLICATE_IMAGE.
            await self.session.commit()
        except Exception:
            # DB rollback is *not* done here: services own commit, the session's own context
            # manager rolls back on an exception it sees propagate (app/db/session.py). Doing
            # it again here would fight that. Cleaning up the objects we already wrote to
            # storage is this layer's job, since nothing else knows about `uploaded_keys`.
            for key in uploaded_keys:
                await self._safe_delete(key)
            raise
        return rows

    async def delete(self, image_id: uuid.UUID, seller_id: uuid.UUID) -> None:
        found = await self.images.get_owned(image_id, seller_id)
        if found is None:
            raise _not_found_image()
        image, product_status = found
        if product_status == ProductStatus.PROCESSING:
            raise _locked()

        key = image.storage_key
        await self.images.delete(image)
        await self.session.commit()  # the DB row is the source of truth; drop it first
        await self._safe_delete(key)  # best effort: a failed object delete is swept later

    async def presigned_url(self, image: ProductImage) -> tuple[str, datetime]:
        ttl = self.settings.s3_presign_ttl_image_sec
        try:
            url = await self.storage.presigned_get_url(image.storage_key, expires_in=ttl)
        except Exception as exc:
            raise ServiceUnavailableError(
                "STORAGE_UNAVAILABLE", "Image storage is temporarily unavailable. Please try again."
            ) from exc
        return url, datetime.now(UTC) + timedelta(seconds=ttl)

    async def _safe_delete(self, key: str) -> None:
        try:
            await self.storage.delete(key)
        except Exception:
            logger.warning("failed to clean up object %s after a failed upload", key, exc_info=True)
