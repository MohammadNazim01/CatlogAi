"""Ownership & authorization foundation (Step 6).

Two layers are proven independently:
- repository/service level, directly against the database, with no HTTP or routing involved
  at all — this is what "enforced at the repository layer, not the frontend" actually means;
- one thin FastAPI route per case, reusing the existing `CurrentUser` / `require_role`
  primitives, to prove the pieces integrate the way a real endpoint would use them.
"""

import uuid

import pytest
from fastapi import Depends, FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, require_role
from app.core import security
from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.db.enums import Role
from app.db.models import User
from app.repositories.product_repo import AdminProductRepository, ProductRepository
from app.repositories.user_repo import UserRepository
from app.services.product_service import ProductService
from tests.authutils import PASSWORD, bearer, error_of, signup_and_login
from tests.factories import make_product, make_user


def add_product_routes(app: FastAPI) -> None:
    @app.get("/t/products/{product_id}")
    async def get_product(
        product_id: uuid.UUID, user: CurrentUser, session: DbSession
    ) -> dict[str, str]:
        product = await ProductService(session).get_owned(product_id, user.id)
        return {"id": str(product.id), "name": product.name}

    @app.get(
        "/t/admin/products/{product_id}",
        dependencies=[Depends(require_role(Role.ADMIN))],
    )
    async def admin_get_product(product_id: uuid.UUID, session: DbSession) -> dict[str, str]:
        product = await AdminProductRepository(session).get(product_id)
        if product is None:
            raise NotFoundError("PRODUCT_NOT_FOUND", "Product not found.")
        return {"id": str(product.id), "seller_id": str(product.seller_id)}


async def _signed_up_seller(
    api: AsyncClient, session: AsyncSession, email: str
) -> tuple[str, User]:
    """Registers and logs in through the real endpoints, then loads the row the same request
    created — so the product we attach belongs to the exact user the token authenticates as."""
    access, _ = await signup_and_login(api, email)
    user = await UserRepository(session).get_by_email(email)
    assert user is not None
    return access, user


async def _admin_token(session: AsyncSession) -> str:
    admin = await make_user(
        session, role=Role.ADMIN, password_hash=security.hash_password(PASSWORD)
    )
    return security.create_access_token(admin.id, admin.role, settings=get_settings())


# ------------------------------------------------------------------ repository / service layer
class TestProductRepository:
    """No app, no routes, no tokens: the ownership check itself, against real Postgres."""

    async def test_get_owned_returns_the_sellers_own_product(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        product = await make_product(session, seller)
        found = await ProductRepository(session).get_owned(product.id, seller.id)
        assert found is not None and found.id == product.id

    async def test_get_owned_returns_none_for_another_sellers_product(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session)
        other = await make_user(session)
        product = await make_product(session, owner)
        assert await ProductRepository(session).get_owned(product.id, other.id) is None

    async def test_get_owned_returns_none_for_a_missing_id(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        assert await ProductRepository(session).get_owned(uuid.uuid4(), seller.id) is None


class TestProductService:
    async def test_returns_the_product_when_owned(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        product = await make_product(session, seller)
        found = await ProductService(session).get_owned(product.id, seller.id)
        assert found.id == product.id

    async def test_missing_and_not_owned_raise_the_identical_error(
        self, session: AsyncSession
    ) -> None:
        """The service must not let a caller (or an attacker probing IDs) tell "doesn't exist"
        apart from "exists, but isn't yours"."""
        owner = await make_user(session)
        other = await make_user(session)
        product = await make_product(session, owner)
        service = ProductService(session)

        with pytest.raises(NotFoundError) as not_owned:
            await service.get_owned(product.id, other.id)
        with pytest.raises(NotFoundError) as missing:
            await service.get_owned(uuid.uuid4(), other.id)

        assert (not_owned.value.code, not_owned.value.message) == (
            missing.value.code,
            missing.value.message,
        )
        assert not_owned.value.code == "PRODUCT_NOT_FOUND"


# ------------------------------------------------------------------ through the API primitives
class TestOwnershipThroughTheApi:
    async def test_seller_a_can_access_seller_as_product(
        self, app: FastAPI, api: AsyncClient, session: AsyncSession
    ) -> None:
        add_product_routes(app)
        access_a, seller_a = await _signed_up_seller(api, session, "seller-a@example.com")
        product = await make_product(session, seller_a)

        r = await api.get(f"/t/products/{product.id}", headers=bearer(access_a))
        assert r.status_code == 200
        assert r.json()["id"] == str(product.id)

    async def test_seller_b_cannot_access_seller_as_product(
        self, app: FastAPI, api: AsyncClient, session: AsyncSession
    ) -> None:
        add_product_routes(app)
        _, seller_a = await _signed_up_seller(api, session, "seller-a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "seller-b@example.com")
        product = await make_product(session, seller_a)

        r = await api.get(f"/t/products/{product.id}", headers=bearer(access_b))
        assert r.status_code == 404
        assert error_of(r)["code"] == "PRODUCT_NOT_FOUND"

    async def test_a_missing_product_and_someone_elses_product_look_identical(
        self, app: FastAPI, api: AsyncClient, session: AsyncSession
    ) -> None:
        add_product_routes(app)
        _, seller_a = await _signed_up_seller(api, session, "seller-a@example.com")
        access_b, _ = await _signed_up_seller(api, session, "seller-b@example.com")
        product = await make_product(session, seller_a)

        not_owned = await api.get(f"/t/products/{product.id}", headers=bearer(access_b))
        missing = await api.get(f"/t/products/{uuid.uuid4()}", headers=bearer(access_b))
        not_owned_body, missing_body = error_of(not_owned), error_of(missing)

        assert not_owned.status_code == missing.status_code == 404
        assert (not_owned_body["code"], not_owned_body["message"]) == (
            missing_body["code"],
            missing_body["message"],
        )
        assert not_owned_body["code"] == "PRODUCT_NOT_FOUND"


class TestAdminGate:
    """The admin path is a separate route and a separate (unscoped) repository — proving that
    `require_role` gates it, and that ownership scoping plays no part in the decision."""

    async def test_admin_route_requires_authentication_first(
        self, app: FastAPI, api: AsyncClient
    ) -> None:
        add_product_routes(app)
        assert (await api.get(f"/t/admin/products/{uuid.uuid4()}")).status_code == 401

    async def test_admin_route_forbids_a_seller(self, app: FastAPI, api: AsyncClient) -> None:
        add_product_routes(app)
        access, _ = await signup_and_login(api)
        r = await api.get(f"/t/admin/products/{uuid.uuid4()}", headers=bearer(access))
        assert r.status_code == 403
        assert error_of(r)["code"] == "FORBIDDEN"

    async def test_admin_can_access_any_sellers_product(
        self, app: FastAPI, api: AsyncClient, session: AsyncSession
    ) -> None:
        add_product_routes(app)
        seller = await make_user(session)
        product = await make_product(session, seller)
        admin_token = await _admin_token(session)

        r = await api.get(f"/t/admin/products/{product.id}", headers=bearer(admin_token))
        assert r.status_code == 200
        assert r.json()["seller_id"] == str(seller.id)

    async def test_admin_route_404s_a_genuinely_missing_product(
        self, app: FastAPI, api: AsyncClient, session: AsyncSession
    ) -> None:
        add_product_routes(app)
        admin_token = await _admin_token(session)
        r = await api.get(f"/t/admin/products/{uuid.uuid4()}", headers=bearer(admin_token))
        assert r.status_code == 404
        assert error_of(r)["code"] == "PRODUCT_NOT_FOUND"
