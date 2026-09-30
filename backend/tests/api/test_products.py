"""Product Management API (Step 8): CRUD, ownership, SKU uniqueness, soft archive, pagination
and search — the Phase 1 §3.2 contract. AI/catalog transitions besides the PROCESSING lock are
out of scope; those ship with the AI milestone."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from app.db.enums import ProductStatus, Role
from app.db.models import User
from app.repositories.user_repo import UserRepository
from tests.authutils import PASSWORD, bearer, error_of, signup_and_login
from tests.factories import make_product, make_user

PRODUCTS = "/api/v1/products"


async def _signed_up_seller(
    api: AsyncClient, session: AsyncSession, email: str = "seller@example.com"
) -> tuple[str, User]:
    access, _ = await signup_and_login(api, email)
    user = await UserRepository(session).get_by_email(email)
    assert user is not None
    return access, user


async def _admin_token(session: AsyncSession) -> str:
    admin = await make_user(
        session, role=Role.ADMIN, password_hash=security.hash_password(PASSWORD)
    )
    return security.create_access_token(admin.id, admin.role, settings=get_settings())


class TestCreate:
    async def test_minimal_payload(self, api: AsyncClient, session: AsyncSession) -> None:
        access, _ = await _signed_up_seller(api, session)
        r = await api.post(PRODUCTS, json={"name": "Wireless Mouse"}, headers=bearer(access))
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Wireless Mouse"
        assert body["status"] == "DRAFT"
        assert body["sku"] is None and body["archived_at"] is None
        assert body["image_count"] == 0
        assert body["primary_image_url"] is None
        assert body["current_version"] is None
        assert uuid.UUID(body["id"])

    async def test_full_payload(self, api: AsyncClient, session: AsyncSession) -> None:
        access, _ = await _signed_up_seller(api, session)
        payload = {
            "name": "Steel Water Bottle",
            "sku": "SKU-001",
            "category": "Kitchen",
            "brand": "Acme",
            "seller_notes": "Ships in a box",
            "seller_attributes": {"capacity": "1L", "material": "steel"},
        }
        r = await api.post(PRODUCTS, json=payload, headers=bearer(access))
        assert r.status_code == 201
        body = r.json()
        for key, value in payload.items():
            assert body[key] == value

    async def test_role_and_status_are_not_client_settable(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await _signed_up_seller(api, session)
        r = await api.post(
            PRODUCTS, json={"name": "x", "status": "APPROVED"}, headers=bearer(access)
        )
        assert r.status_code == 422

    async def test_blank_name_rejected(self, api: AsyncClient, session: AsyncSession) -> None:
        access, _ = await _signed_up_seller(api, session)
        r = await api.post(PRODUCTS, json={"name": "   "}, headers=bearer(access))
        assert r.status_code == 422

    async def test_too_many_seller_attributes_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await _signed_up_seller(api, session)
        attrs = {f"key{i}": "v" for i in range(31)}
        r = await api.post(
            PRODUCTS, json={"name": "x", "seller_attributes": attrs}, headers=bearer(access)
        )
        assert r.status_code == 422

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        r = await api.post(PRODUCTS, json={"name": "x"})
        assert r.status_code == 401


class TestSkuUniqueness:
    async def test_duplicate_sku_for_the_same_seller_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await _signed_up_seller(api, session)
        await api.post(PRODUCTS, json={"name": "A", "sku": "DUP"}, headers=bearer(access))
        r = await api.post(PRODUCTS, json={"name": "B", "sku": "DUP"}, headers=bearer(access))
        assert r.status_code == 409
        assert error_of(r)["code"] == "SKU_TAKEN"

    async def test_same_sku_allowed_for_different_sellers(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access_a, _ = await _signed_up_seller(api, session, "a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "b@example.com")
        r1 = await api.post(PRODUCTS, json={"name": "A", "sku": "SHARED"}, headers=bearer(access_a))
        r2 = await api.post(PRODUCTS, json={"name": "B", "sku": "SHARED"}, headers=bearer(access_b))
        assert r1.status_code == 201 and r2.status_code == 201

    async def test_multiple_products_without_a_sku_do_not_conflict(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await _signed_up_seller(api, session)
        r1 = await api.post(PRODUCTS, json={"name": "A"}, headers=bearer(access))
        r2 = await api.post(PRODUCTS, json={"name": "B"}, headers=bearer(access))
        assert r1.status_code == 201 and r2.status_code == 201


class TestGet:
    async def test_seller_can_get_their_own_product(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.get(f"{PRODUCTS}/{product.id}", headers=bearer(access))
        assert r.status_code == 200 and r.json()["id"] == str(product.id)

    async def test_seller_b_cannot_get_seller_as_product(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, seller_a = await _signed_up_seller(api, session, "a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "b@example.com")
        product = await make_product(session, seller_a)
        r = await api.get(f"{PRODUCTS}/{product.id}", headers=bearer(access_b))
        assert r.status_code == 404
        assert error_of(r)["code"] == "PRODUCT_NOT_FOUND"

    async def test_missing_and_not_owned_are_indistinguishable(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, seller_a = await _signed_up_seller(api, session, "a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "b@example.com")
        product = await make_product(session, seller_a)

        not_owned = await api.get(f"{PRODUCTS}/{product.id}", headers=bearer(access_b))
        missing = await api.get(f"{PRODUCTS}/{uuid.uuid4()}", headers=bearer(access_b))
        assert not_owned.status_code == missing.status_code == 404
        assert error_of(not_owned)["code"] == error_of(missing)["code"] == "PRODUCT_NOT_FOUND"
        assert error_of(not_owned)["message"] == error_of(missing)["message"]

    async def test_admin_has_no_special_access_to_a_sellers_product(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Phase 1 defines no admin route for products, so an admin caller is just another
        non-owning user here: the ownership check applies to every role alike."""
        seller = await make_user(session)
        product = await make_product(session, seller)
        admin_token = await _admin_token(session)
        r = await api.get(f"{PRODUCTS}/{product.id}", headers=bearer(admin_token))
        assert r.status_code == 404
        assert error_of(r)["code"] == "PRODUCT_NOT_FOUND"

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        r = await api.get(f"{PRODUCTS}/{uuid.uuid4()}")
        assert r.status_code == 401


class TestUpdate:
    async def test_only_provided_fields_change(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller, name="Original", brand="OldBrand")
        r = await api.patch(
            f"{PRODUCTS}/{product.id}", json={"name": "Updated"}, headers=bearer(access)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Updated"
        assert body["brand"] == "OldBrand"  # untouched

    async def test_explicit_null_clears_a_nullable_field(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller, sku="TO-CLEAR")
        r = await api.patch(f"{PRODUCTS}/{product.id}", json={"sku": None}, headers=bearer(access))
        assert r.status_code == 200 and r.json()["sku"] is None

    async def test_name_cannot_be_set_to_null(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.patch(f"{PRODUCTS}/{product.id}", json={"name": None}, headers=bearer(access))
        assert r.status_code == 422

    async def test_seller_attributes_cannot_be_set_to_null(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.patch(
            f"{PRODUCTS}/{product.id}", json={"seller_attributes": None}, headers=bearer(access)
        )
        assert r.status_code == 422

    async def test_updating_to_a_taken_sku_is_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        await make_product(session, seller, sku="TAKEN")
        other = await make_product(session, seller, sku="FREE")
        r = await api.patch(f"{PRODUCTS}/{other.id}", json={"sku": "TAKEN"}, headers=bearer(access))
        assert r.status_code == 409
        assert error_of(r)["code"] == "SKU_TAKEN"

    async def test_seller_b_cannot_update_seller_as_product(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, seller_a = await _signed_up_seller(api, session, "a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "b@example.com")
        product = await make_product(session, seller_a)
        r = await api.patch(
            f"{PRODUCTS}/{product.id}", json={"name": "Hijacked"}, headers=bearer(access_b)
        )
        assert r.status_code == 404

    async def test_locked_while_an_ai_job_is_processing(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller, status=ProductStatus.PROCESSING)
        r = await api.patch(
            f"{PRODUCTS}/{product.id}", json={"name": "New"}, headers=bearer(access)
        )
        assert r.status_code == 409
        assert error_of(r)["code"] == "PRODUCT_LOCKED"

    async def test_unknown_field_rejected(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.patch(
            f"{PRODUCTS}/{product.id}", json={"status": "APPROVED"}, headers=bearer(access)
        )
        assert r.status_code == 422

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        r = await api.patch(f"{PRODUCTS}/{uuid.uuid4()}", json={"name": "x"})
        assert r.status_code == 401


class TestArchive:
    async def test_delete_archives_without_deleting_the_row(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.delete(f"{PRODUCTS}/{product.id}", headers=bearer(access))
        assert r.status_code == 204

        got = await api.get(f"{PRODUCTS}/{product.id}", headers=bearer(access))
        assert got.status_code == 200  # still fetchable directly
        assert got.json()["archived_at"] is not None
        assert got.json()["status"] == "DRAFT"  # archiving never touches workflow status

    async def test_delete_is_idempotent(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        first = await api.delete(f"{PRODUCTS}/{product.id}", headers=bearer(access))
        second = await api.delete(f"{PRODUCTS}/{product.id}", headers=bearer(access))
        assert first.status_code == second.status_code == 204

    async def test_restore_via_patch(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        await api.delete(f"{PRODUCTS}/{product.id}", headers=bearer(access))
        r = await api.patch(
            f"{PRODUCTS}/{product.id}", json={"archived": False}, headers=bearer(access)
        )
        assert r.status_code == 200 and r.json()["archived_at"] is None

    async def test_archive_via_patch(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller)
        r = await api.patch(
            f"{PRODUCTS}/{product.id}", json={"archived": True}, headers=bearer(access)
        )
        assert r.status_code == 200 and r.json()["archived_at"] is not None

    async def test_archiving_frees_the_sku_for_a_new_product(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller, sku="REUSE-ME")
        await api.delete(f"{PRODUCTS}/{product.id}", headers=bearer(access))
        r = await api.post(
            PRODUCTS, json={"name": "New", "sku": "REUSE-ME"}, headers=bearer(access)
        )
        assert r.status_code == 201

    async def test_locked_while_an_ai_job_is_processing(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        product = await make_product(session, seller, status=ProductStatus.PROCESSING)
        r = await api.delete(f"{PRODUCTS}/{product.id}", headers=bearer(access))
        assert r.status_code == 409
        assert error_of(r)["code"] == "PRODUCT_LOCKED"

    async def test_seller_b_cannot_archive_seller_as_product(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, seller_a = await _signed_up_seller(api, session, "a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "b@example.com")
        product = await make_product(session, seller_a)
        r = await api.delete(f"{PRODUCTS}/{product.id}", headers=bearer(access_b))
        assert r.status_code == 404

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        r = await api.delete(f"{PRODUCTS}/{uuid.uuid4()}")
        assert r.status_code == 401


class TestList:
    async def test_lists_only_the_callers_products(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access_a, seller_a = await _signed_up_seller(api, session, "a@example.com")
        access_b, seller_b = await _signed_up_seller(api, session, "b@example.com")
        await make_product(session, seller_a, name="Mine")
        await make_product(session, seller_b, name="Theirs")

        r = await api.get(PRODUCTS, headers=bearer(access_a))
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert [item["name"] for item in body["items"]] == ["Mine"]

    async def test_response_shape(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        await make_product(session, seller)
        r = await api.get(PRODUCTS, headers=bearer(access))
        body = r.json()
        assert set(body.keys()) == {"items", "total", "page", "page_size"}
        item = body["items"][0]
        assert set(item.keys()) == {
            "id",
            "name",
            "sku",
            "status",
            "primary_image_url",
            "updated_at",
        }

    async def test_pagination(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        for i in range(5):
            await make_product(session, seller, name=f"P{i}")

        page1 = (
            await api.get(PRODUCTS, params={"page": 1, "page_size": 2}, headers=bearer(access))
        ).json()
        page2 = (
            await api.get(PRODUCTS, params={"page": 2, "page_size": 2}, headers=bearer(access))
        ).json()
        page3 = (
            await api.get(PRODUCTS, params={"page": 3, "page_size": 2}, headers=bearer(access))
        ).json()

        assert page1["total"] == page2["total"] == page3["total"] == 5
        assert len(page1["items"]) == len(page2["items"]) == 2
        assert len(page3["items"]) == 1
        ids = {i["id"] for p in (page1, page2, page3) for i in p["items"]}
        assert len(ids) == 5  # no overlap, nothing missing

    async def test_page_size_over_100_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await _signed_up_seller(api, session)
        r = await api.get(PRODUCTS, params={"page_size": 101}, headers=bearer(access))
        assert r.status_code == 422

    async def test_archived_filter(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        keep = await make_product(session, seller, name="Keep")
        gone = await make_product(session, seller, name="Gone")
        await api.delete(f"{PRODUCTS}/{gone.id}", headers=bearer(access))

        active = await api.get(PRODUCTS, headers=bearer(access))
        archived = await api.get(PRODUCTS, params={"archived": True}, headers=bearer(access))

        assert [i["name"] for i in active.json()["items"]] == ["Keep"]
        assert [i["name"] for i in archived.json()["items"]] == ["Gone"]
        assert str(keep.id) not in [i["id"] for i in archived.json()["items"]]

    async def test_status_filter_is_repeatable(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        await make_product(session, seller, name="D", status=ProductStatus.DRAFT)
        await make_product(session, seller, name="R", status=ProductStatus.REVIEW)
        await make_product(session, seller, name="A", status=ProductStatus.APPROVED)

        r = await api.get(
            PRODUCTS, params=[("status", "DRAFT"), ("status", "REVIEW")], headers=bearer(access)
        )
        names = {i["name"] for i in r.json()["items"]}
        assert names == {"D", "R"}

    async def test_status_filter_rejects_unknown_value(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await _signed_up_seller(api, session)
        r = await api.get(PRODUCTS, params={"status": "NOT_A_STATUS"}, headers=bearer(access))
        assert r.status_code == 422

    async def test_category_filter(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        await make_product(session, seller, name="Cup", category="Kitchen")
        await make_product(session, seller, name="Shirt", category="Apparel")
        r = await api.get(PRODUCTS, params={"category": "Kitchen"}, headers=bearer(access))
        assert [i["name"] for i in r.json()["items"]] == ["Cup"]

    async def test_q_matches_name_or_sku(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        await make_product(session, seller, name="Blue Widget", sku="ABC")
        await make_product(session, seller, name="Red Gadget", sku="XYZ-BLUE")
        await make_product(session, seller, name="Green Thing", sku="OTHER")

        r = await api.get(PRODUCTS, params={"q": "blue"}, headers=bearer(access))
        names = {i["name"] for i in r.json()["items"]}
        assert names == {"Blue Widget", "Red Gadget"}

    async def test_q_wildcard_characters_are_treated_literally(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, seller = await _signed_up_seller(api, session)
        await make_product(session, seller, name="50% off")
        await make_product(session, seller, name="Something else entirely")

        r = await api.get(PRODUCTS, params={"q": "50%"}, headers=bearer(access))
        names = [i["name"] for i in r.json()["items"]]
        assert names == ["50% off"]  # a literal '%' must not match every row

    async def test_sort_by_name_ascending(self, api: AsyncClient, session: AsyncSession) -> None:
        access, seller = await _signed_up_seller(api, session)
        await make_product(session, seller, name="Charlie")
        await make_product(session, seller, name="Alpha")
        await make_product(session, seller, name="Bravo")

        r = await api.get(PRODUCTS, params={"sort": "name"}, headers=bearer(access))
        assert [i["name"] for i in r.json()["items"]] == ["Alpha", "Bravo", "Charlie"]

    async def test_sort_rejects_unwhitelisted_value(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await _signed_up_seller(api, session)
        r = await api.get(PRODUCTS, params={"sort": "seller_notes"}, headers=bearer(access))
        assert r.status_code == 422

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        assert (await api.get(PRODUCTS)).status_code == 401
