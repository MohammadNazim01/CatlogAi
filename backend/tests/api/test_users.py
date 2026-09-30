import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RefreshToken
from tests.authutils import (
    PASSWORD,
    bearer,
    error_of,
    login,
    refresh,
    refresh_cookie,
    register,
    signup_and_login,
)  # fmt: skip

ME = "/api/v1/users/me"
PASSWORD_URL = "/api/v1/users/me/password"
NEW_PASSWORD = "a-brand-new-passphrase"


class TestProfile:
    async def test_update_name(self, api: AsyncClient) -> None:
        access, _ = await signup_and_login(api)
        r = await api.patch(ME, json={"name": "  Renamed Seller "}, headers=bearer(access))
        assert r.status_code == 200 and r.json()["name"] == "Renamed Seller"
        assert (await api.get("/api/v1/auth/me", headers=bearer(access))).json()[
            "name"
        ] == "Renamed Seller"

    @pytest.mark.parametrize(
        "extra", [{"email": "x@example.com"}, {"role": "ADMIN"}, {"ai_daily_quota": 1}]
    )
    async def test_only_the_name_is_editable(
        self, api: AsyncClient, extra: dict[str, object]
    ) -> None:
        access, _ = await signup_and_login(api)
        r = await api.patch(ME, json={"name": "ok", **extra}, headers=bearer(access))
        assert r.status_code == 422

    async def test_blank_name_rejected(self, api: AsyncClient) -> None:
        access, _ = await signup_and_login(api)
        assert (await api.patch(ME, json={"name": "  "}, headers=bearer(access))).status_code == 422

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        assert (await api.patch(ME, json={"name": "x"})).status_code == 401


class TestChangePassword:
    async def test_success_switches_the_password_and_keeps_this_device_logged_in(
        self, api: AsyncClient
    ) -> None:
        access, _ = await signup_and_login(api)
        r = await api.post(
            PASSWORD_URL,
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=bearer(access),
        )
        assert r.status_code == 200 and r.json()["access_token"]
        new_refresh = refresh_cookie(r)

        assert (await login(api, password=PASSWORD)).status_code == 401  # old password is dead
        assert (await login(api, password=NEW_PASSWORD)).status_code == 200
        assert (
            await refresh(api, new_refresh)
        ).status_code == 200  # this device's fresh session works

    async def test_all_other_sessions_are_revoked(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        access, phone = await signup_and_login(api)
        laptop = refresh_cookie(await login(api))
        await api.post(
            PASSWORD_URL,
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=bearer(access),
        )
        assert (await refresh(api, phone)).status_code == 401
        assert (await refresh(api, laptop)).status_code == 401

    async def test_wrong_current_password(self, api: AsyncClient, session: AsyncSession) -> None:
        access, t = await signup_and_login(api)
        r = await api.post(
            PASSWORD_URL,
            json={"current_password": "not-my-password", "new_password": NEW_PASSWORD},
            headers=bearer(access),
        )
        assert r.status_code == 400 and error_of(r)["code"] == "WRONG_PASSWORD"
        assert (await login(api)).status_code == 200  # unchanged
        assert (await refresh(api, t)).status_code == 200  # sessions untouched by a failed attempt

    async def test_new_password_must_meet_the_length_rule(self, api: AsyncClient) -> None:
        access, _ = await signup_and_login(api)
        r = await api.post(
            PASSWORD_URL,
            json={"current_password": PASSWORD, "new_password": "tiny"},
            headers=bearer(access),
        )
        assert r.status_code == 422

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        r = await api.post(
            PASSWORD_URL, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
        )
        assert r.status_code == 401

    async def test_other_users_sessions_are_never_touched(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        await register(api, "victim@example.com")
        victim_refresh = refresh_cookie(await login(api, "victim@example.com"))
        access, _ = await signup_and_login(api, "attacker@example.com")
        await api.post(
            PASSWORD_URL,
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=bearer(access),
        )
        assert (await refresh(api, victim_refresh)).status_code == 200
        rows = (await session.execute(select(RefreshToken))).scalars().all()
        assert sum(1 for t in rows if t.revoked_at is None) >= 2
