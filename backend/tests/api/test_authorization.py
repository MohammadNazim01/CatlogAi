"""get_current_user and require_role: every way to be unauthenticated looks the same, and
authorization is decided by the database, not by claims inside a token."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.core import security
from app.core.config import get_settings
from app.db.enums import Role
from app.db.models import User
from tests.authutils import PASSWORD, bearer, error_of, signup_and_login
from tests.factories import make_user

ME = "/api/v1/auth/me"


def add_admin_route(app: FastAPI) -> None:
    @app.get("/t/admin-only", dependencies=[Depends(require_role(Role.ADMIN))])
    async def admin_only() -> dict[str, bool]:
        return {"ok": True}


def b64(d: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


class TestAuthentication:
    async def test_valid_token(self, api: AsyncClient) -> None:
        access, _ = await signup_and_login(api)
        assert (await api.get(ME, headers=bearer(access))).status_code == 200

    async def test_missing_header_includes_www_authenticate(self, api: AsyncClient) -> None:
        r = await api.get(ME)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert error_of(r)["code"] == "UNAUTHENTICATED"

    async def test_every_kind_of_bad_credential_gets_the_same_response(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, refresh_token = await signup_and_login(api)
        s = get_settings()
        uid = uuid.uuid4()
        now = datetime.now(UTC)
        claims = {
            "sub": str(uid),
            "role": "SELLER",
            "typ": "access",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        unsigned = (
            f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({**claims, 'iat': 1, 'exp': 9999999999})}."
        )
        bad_headers = {
            "no scheme": {"Authorization": access},
            "basic auth": {"Authorization": "Basic dXNlcjpwYXNz"},
            "garbage": bearer("not.a.jwt"),
            "empty bearer": {"Authorization": "Bearer "},
            "expired": bearer(
                security.create_access_token(
                    uid, Role.SELLER, settings=s, now=now - timedelta(hours=2)
                )
            ),
            "wrong secret": bearer(jwt.encode(claims, "x" * 40, algorithm="HS256")),
            "alg none": bearer(unsigned),
            "refresh token as bearer": bearer(refresh_token),
            "user does not exist": bearer(
                security.create_access_token(uid, Role.SELLER, settings=s)
            ),
        }
        seen = set()
        for name, headers in bad_headers.items():
            r = await api.get(ME, headers=headers)
            assert r.status_code == 401, name
            e = error_of(r)
            seen.add((e["code"], e["message"]))
        assert seen == {("UNAUTHENTICATED", "Authentication required.")}

    async def test_deactivation_takes_effect_immediately(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await signup_and_login(api)
        await session.execute(text("UPDATE users SET is_active = false"))
        assert (await api.get(ME, headers=bearer(access))).status_code == 401

    async def test_deleted_user_token_is_rejected(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, _ = await signup_and_login(api)
        await session.execute(text("DELETE FROM users"))
        assert (await api.get(ME, headers=bearer(access))).status_code == 401


class TestRoles:
    async def _admin_token(self, session: AsyncSession) -> tuple[User, str]:
        admin = await make_user(
            session, role=Role.ADMIN, password_hash=security.hash_password(PASSWORD)
        )
        return admin, security.create_access_token(admin.id, admin.role, settings=get_settings())

    async def test_admin_route_allows_admin(
        self, app: FastAPI, api: AsyncClient, session: AsyncSession
    ) -> None:
        add_admin_route(app)
        _, token = await self._admin_token(session)
        assert (await api.get("/t/admin-only", headers=bearer(token))).status_code == 200

    async def test_admin_route_forbids_seller(self, app: FastAPI, api: AsyncClient) -> None:
        add_admin_route(app)
        access, _ = await signup_and_login(api)
        r = await api.get("/t/admin-only", headers=bearer(access))
        assert r.status_code == 403 and error_of(r)["code"] == "FORBIDDEN"

    async def test_admin_route_requires_authentication_first(
        self, app: FastAPI, api: AsyncClient
    ) -> None:
        add_admin_route(app)
        assert (await api.get("/t/admin-only")).status_code == 401

    async def test_a_stale_admin_claim_does_not_grant_access_after_demotion(
        self, app: FastAPI, api: AsyncClient, session: AsyncSession
    ) -> None:
        add_admin_route(app)
        admin, token = await self._admin_token(session)  # token says ADMIN
        admin.role = Role.SELLER  # ...but the database now says SELLER
        await session.flush()
        assert (await api.get("/t/admin-only", headers=bearer(token))).status_code == 403

    async def test_a_forged_admin_claim_cannot_be_used(
        self, app: FastAPI, api: AsyncClient
    ) -> None:
        add_admin_route(app)
        access, _ = await signup_and_login(api)
        header, payload, sig = access.split(".")
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        body["role"] = "ADMIN"
        forged = ".".join([header, b64(body), sig])
        assert (await api.get("/t/admin-only", headers=bearer(forged))).status_code == 401
