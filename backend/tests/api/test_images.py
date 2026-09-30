"""Secure product image upload & media management (Step 9): real integration tests against
PostgreSQL and MinIO — no mocked storage except the one dedicated test for storage failure,
where a real outage can't be induced on demand. docs/02 §3.3, docs/03 §14."""

import io
import uuid

import httpx
from fastapi import FastAPI
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.enums import ProductStatus
from app.db.models import ProductImage, User
from app.repositories.user_repo import UserRepository
from app.storage.s3 import get_storage
from tests.authutils import bearer, error_of, signup_and_login
from tests.factories import make_image, make_product

PRODUCTS = "/api/v1/products"


def _images_url(product_id: object) -> str:
    return f"{PRODUCTS}/{product_id}/images"


async def _signed_up_seller(
    api: AsyncClient, session: AsyncSession, email: str = "seller@example.com"
) -> tuple[str, User]:
    access, _ = await signup_and_login(api, email)
    user = await UserRepository(session).get_by_email(email)
    assert user is not None
    return access, user


# ------------------------------------------------------------------ test image fixtures
def _jpeg(width: int = 64, height: int = 64, color: tuple[int, int, int] = (200, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


def _png(width: int = 64, height: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (width, height), (10, 20, 30, 128)).save(buf, format="PNG")
    return buf.getvalue()


def _webp(width: int = 64, height: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (5, 5, 5)).save(buf, format="WEBP")
    return buf.getvalue()


def _gif(width: int = 64, height: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (0, 255, 0)).save(buf, format="GIF")
    return buf.getvalue()


def _jpeg_with_gps(width: int = 64, height: int = 64) -> bytes:
    img = Image.new("RGB", (width, height), (77, 88, 99))
    exif = img.getexif()
    exif[0x0110] = "TestCamera"  # Model
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"  # GPSLatitudeRef
    gps[3] = "E"  # GPSLongitudeRef
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _upload_file(name: str, data: bytes, content_type: str) -> tuple[str, tuple[str, bytes, str]]:
    return "files", (name, data, content_type)


class TestUploadHappyPath:
    async def test_valid_jpeg_upload(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("photo.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 201
        body = r.json()
        assert len(body) == 1
        item = body[0]
        assert item["mime_type"] == "image/jpeg"
        assert item["width"] == 64 and item["height"] == 64
        assert item["sort_order"] == 0
        assert item["original_filename"] == "photo.jpg"
        assert item["url"].startswith("http")
        assert uuid.UUID(item["id"])

    async def test_valid_png_and_webp_upload(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.post(
            _images_url(product.id),
            files=[
                _upload_file("a.png", _png(), "image/png"),
                _upload_file("b.webp", _webp(), "image/webp"),
            ],
            headers=bearer(access),
        )
        assert r.status_code == 201
        mimes = {i["mime_type"] for i in r.json()}
        assert mimes == {"image/png", "image/webp"}

    async def test_multiple_images_get_sequential_sort_order(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.post(
            _images_url(product.id),
            files=[
                # Colors far enough apart that lossy JPEG quantization can't collapse two of
                # them into identical output bytes (which would look like a duplicate upload).
                _upload_file("1.jpg", _jpeg(color=(220, 20, 20)), "image/jpeg"),
                _upload_file("2.jpg", _jpeg(color=(20, 220, 20)), "image/jpeg"),
                _upload_file("3.jpg", _jpeg(color=(20, 20, 220)), "image/jpeg"),
            ],
            headers=bearer(access),
        )
        assert r.status_code == 201
        orders = sorted(i["sort_order"] for i in r.json())
        assert orders == [0, 1, 2]

    async def test_second_batch_continues_the_sort_order(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        await api.post(
            _images_url(product.id),
            files=[_upload_file("1.jpg", _jpeg(color=(1, 2, 3)), "image/jpeg")],
            headers=bearer(access),
        )
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("2.jpg", _jpeg(color=(4, 5, 6)), "image/jpeg")],
            headers=bearer(access),
        )
        assert r.json()[0]["sort_order"] == 1

    async def test_filename_is_never_trusted_for_the_storage_key(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """A path-traversal-looking filename must not leak into the storage key or crash
        anything; it is stored purely as a display string, sanitized to its basename."""
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("../../etc/passwd.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 201
        assert r.json()[0]["original_filename"] == "passwd.jpg"

        row = (await session.execute(select(ProductImage))).scalar_one()
        assert ".." not in row.storage_key and "etc" not in row.storage_key
        assert row.storage_key.startswith(f"products/{seller.id}/{product.id}/")


class TestUploadValidation:
    async def test_content_that_is_not_an_image_at_all_is_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        garbage = b"this is definitely not an image" * 10
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("fake.jpg", garbage, "image/jpeg")],  # spoofed Content-Type
            headers=bearer(access),
        )
        assert r.status_code == 422
        assert error_of(r)["code"] == "IMAGE_INVALID"

    async def test_a_real_but_disallowed_format_is_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """GIF is a perfectly valid image Pillow can decode, but it isn't on the allowlist —
        this proves the check is against the *detected* format, not just "did it open"."""
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("a.gif", _gif(), "image/jpeg")],  # Content-Type lies too
            headers=bearer(access),
        )
        assert r.status_code == 415
        assert error_of(r)["code"] == "UNSUPPORTED_MEDIA_TYPE"

    async def test_truncated_malformed_image_is_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        truncated = _jpeg()[: len(_jpeg()) // 2]
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("broken.jpg", truncated, "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 422
        assert error_of(r)["code"] == "IMAGE_INVALID"

    async def test_oversized_file_is_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        too_big = b"\xff" * (get_settings().max_image_bytes + 1)
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("huge.jpg", too_big, "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 413
        assert error_of(r)["code"] == "FILE_TOO_LARGE"

    async def test_one_side_too_large_is_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        wide = _jpeg(width=8001, height=10)
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("wide.jpg", wide, "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 422
        assert error_of(r)["code"] == "IMAGE_INVALID"

    async def test_pixel_bomb_total_pixel_count_is_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        bomb = _jpeg(width=7000, height=7000)  # 49MP, both sides <= 8000
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("bomb.jpg", bomb, "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 422
        assert error_of(r)["code"] == "IMAGE_INVALID"

    async def test_no_files_rejected(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.post(_images_url(product.id), files=[], headers=bearer(access))
        assert r.status_code == 422

    async def test_more_than_eight_files_in_one_request_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        files = [_upload_file(f"{i}.jpg", _jpeg(color=(i, i, i)), "image/jpeg") for i in range(9)]
        r = await api.post(_images_url(product.id), files=files, headers=bearer(access))
        assert r.status_code == 422

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        r = await api.post(
            _images_url(uuid.uuid4()), files=[_upload_file("a.jpg", _jpeg(), "image/jpeg")]
        )
        assert r.status_code == 401


class TestExifStripping:
    async def test_gps_and_exif_are_stripped_from_the_stored_image(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("gps.jpg", _jpeg_with_gps(), "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 201
        url = r.json()[0]["url"]

        async with httpx.AsyncClient() as raw:
            stored = await raw.get(url)
        assert stored.status_code == 200
        reloaded = Image.open(io.BytesIO(stored.content))
        exif = reloaded.getexif()
        assert len(exif) == 0  # nothing survived re-encoding, GPS included
        assert 0x8825 not in exif

    async def test_the_stored_image_is_genuinely_re_encoded(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        original = _jpeg_with_gps()
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("gps.jpg", original, "image/jpeg")],
            headers=bearer(access),
        )
        url = r.json()[0]["url"]
        async with httpx.AsyncClient() as raw:
            stored = (await raw.get(url)).content

        assert stored != original  # re-encoded, not a byte-for-byte passthrough
        img = Image.open(io.BytesIO(stored))
        assert img.format == "JPEG"
        assert img.size == (64, 64)


class TestList:
    async def test_lists_with_fresh_presigned_urls(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        await api.post(
            _images_url(product.id),
            files=[_upload_file("a.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        r = await api.get(_images_url(product.id), headers=bearer(access))
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["url"].startswith("http")

    async def test_seller_b_cannot_list_seller_as_images(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, seller_a = await _signed_up_seller(api, session, "a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "b@example.com")
        product = await make_product(session, seller_a)
        r = await api.get(_images_url(product.id), headers=bearer(access_b))
        assert r.status_code == 404
        assert error_of(r)["code"] == "PRODUCT_NOT_FOUND"

    async def test_missing_and_not_owned_product_are_indistinguishable(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, seller_a = await _signed_up_seller(api, session, "a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "b@example.com")
        product = await make_product(session, seller_a)

        not_owned = await api.get(_images_url(product.id), headers=bearer(access_b))
        missing = await api.get(_images_url(uuid.uuid4()), headers=bearer(access_b))
        assert not_owned.status_code == missing.status_code == 404
        assert error_of(not_owned)["code"] == error_of(missing)["code"] == "PRODUCT_NOT_FOUND"
        assert error_of(not_owned)["message"] == error_of(missing)["message"]

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        r = await api.get(_images_url(uuid.uuid4()))
        assert r.status_code == 401


class TestDelete:
    async def test_delete_removes_it_from_the_listing(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        uploaded = (
            await api.post(
                _images_url(product.id),
                files=[_upload_file("a.jpg", _jpeg(), "image/jpeg")],
                headers=bearer(access),
            )
        ).json()[0]

        r = await api.delete(f"/api/v1/images/{uploaded['id']}", headers=bearer(access))
        assert r.status_code == 204

        listing = await api.get(_images_url(product.id), headers=bearer(access))
        assert listing.json() == []

    async def test_delete_removes_the_object_from_storage(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        await api.post(
            _images_url(product.id),
            files=[_upload_file("a.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        row = (await session.execute(select(ProductImage))).scalar_one()
        key = row.storage_key

        await api.delete(f"/api/v1/images/{row.id}", headers=bearer(access))

        settings = get_settings()
        anon_url = f"{settings.s3_endpoint_url}/{settings.s3_bucket}/{key}"
        async with httpx.AsyncClient() as raw:
            after_delete = await raw.get(anon_url)
        assert after_delete.status_code != 200

    async def test_seller_b_cannot_delete_seller_as_image(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, seller_a = await _signed_up_seller(api, session, "a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "b@example.com")
        product = await make_product(session, seller_a)
        image = await make_image(session, product)

        r = await api.delete(f"/api/v1/images/{image.id}", headers=bearer(access_b))
        assert r.status_code == 404
        assert error_of(r)["code"] == "IMAGE_NOT_FOUND"

    async def test_missing_image_404s(self, api: AsyncClient, session: AsyncSession) -> None:
        access, _ = await _signed_up_seller(api, session)
        r = await api.delete(f"/api/v1/images/{uuid.uuid4()}", headers=bearer(access))
        assert r.status_code == 404
        assert error_of(r)["code"] == "IMAGE_NOT_FOUND"

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        r = await api.delete(f"/api/v1/images/{uuid.uuid4()}")
        assert r.status_code == 401


class TestProductLevelRules:
    async def test_upload_locked_while_an_ai_job_is_processing(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller, status=ProductStatus.PROCESSING)
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("a.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 409
        assert error_of(r)["code"] == "PRODUCT_LOCKED"

    async def test_delete_locked_while_an_ai_job_is_processing(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller, status=ProductStatus.PROCESSING)
        image = await make_image(session, product)
        r = await api.delete(f"/api/v1/images/{image.id}", headers=bearer(access))
        assert r.status_code == 409
        assert error_of(r)["code"] == "PRODUCT_LOCKED"

    async def test_image_limit_reached(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        for i in range(get_settings().max_images_per_product):
            await make_image(session, product, sort_order=i)

        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("one_too_many.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 409
        assert error_of(r)["code"] == "IMAGE_LIMIT_REACHED"

    async def test_duplicate_image_content_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        data = _jpeg(color=(9, 9, 9))
        await api.post(
            _images_url(product.id),
            files=[_upload_file("first.jpg", data, "image/jpeg")],
            headers=bearer(access),
        )
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("again.jpg", data, "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 409
        assert error_of(r)["code"] == "DUPLICATE_IMAGE"

    async def test_duplicate_image_leaves_no_orphaned_row(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        # Captured before the failing request: it triggers a rollback, which expires every
        # object the session is tracking (including `product`) — a later plain attribute
        # read on it would try to lazily refresh outside of an awaited context.
        product_id = product.id
        data = _jpeg(color=(11, 12, 13))
        await api.post(
            _images_url(product_id),
            files=[_upload_file("first.jpg", data, "image/jpeg")],
            headers=bearer(access),
        )
        await api.post(
            _images_url(product_id),
            files=[_upload_file("again.jpg", data, "image/jpeg")],
            headers=bearer(access),
        )
        count = (
            (
                await session.execute(
                    select(ProductImage).where(ProductImage.product_id == product_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 1


class TestStorageFailure:
    async def test_storage_failure_returns_a_clean_error_and_persists_nothing(
        self, app: FastAPI, api: AsyncClient, session: AsyncSession
    ) -> None:
        class _FailingStorage:
            async def put(self, key: str, data: bytes, *, content_type: str) -> None:
                raise RuntimeError("s3.amazonaws.com connection refused: internal detail")

            async def delete(self, key: str) -> None:
                return None

            async def presigned_get_url(self, key: str, *, expires_in: int) -> str:
                raise RuntimeError("should not be called")

        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        app.dependency_overrides[get_storage] = lambda: _FailingStorage()

        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("a.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        assert r.status_code == 503
        body = error_of(r)
        assert body["code"] == "STORAGE_UNAVAILABLE"
        assert "s3.amazonaws.com" not in body["message"]  # no internal detail leaks out
        assert "RuntimeError" not in body["message"]

        rows = (
            (
                await session.execute(
                    select(ProductImage).where(ProductImage.product_id == product.id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


class TestPrivateBucket:
    async def test_stored_object_cannot_be_fetched_anonymously(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        await api.post(
            _images_url(product.id),
            files=[_upload_file("a.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        row = (await session.execute(select(ProductImage))).scalar_one()
        settings = get_settings()
        anon_url = f"{settings.s3_endpoint_url}/{settings.s3_bucket}/{row.storage_key}"

        async with httpx.AsyncClient() as raw:
            r = await raw.get(anon_url)
        assert r.status_code != 200

    async def test_presigned_url_works_and_matches_stored_content(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.post(
            _images_url(product.id),
            files=[_upload_file("a.jpg", _jpeg(), "image/jpeg")],
            headers=bearer(access),
        )
        item = r.json()[0]

        async with httpx.AsyncClient() as raw:
            fetched = await raw.get(item["url"])
        assert fetched.status_code == 200
        assert fetched.headers["content-type"] == "image/jpeg"
        fetched_img = Image.open(io.BytesIO(fetched.content))
        assert fetched_img.size == (64, 64)
