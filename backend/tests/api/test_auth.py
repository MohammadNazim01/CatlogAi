import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core import security
from app.core.config import Settings, get_settings
from app.db.models import RefreshToken, User
from app.db.session import get_db
from tests.authutils import (
    CSRF,
    PASSWORD,
    bearer,
    error_of,
    login,
    refresh,
    refresh_cookie,
    register,
    sha256,
    signup_and_login,
)  # fmt: skip
from tests.factories import make_user


async def user_by_email(session: AsyncSession, email: str) -> User:
    return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def tokens_of(session: AsyncSession, user: User) -> list[RefreshToken]:
    rows = await session.execute(
        select(RefreshToken)
        .where(RefreshToken.user_id == user.id)
        .order_by(RefreshToken.created_at)
    )
    return list(rows.scalars())


# ------------------------------------------------------------------ register
class TestRegister:
    async def test_creates_a_seller(self, api: AsyncClient, session: AsyncSession) -> None:
        r = await register(api, "  New.Seller@Example.COM ")
        assert r.status_code == 201
        body = r.json()
        assert body["email"] == "new.seller@example.com"  # normalized
        assert body["role"] == "SELLER" and body["ai_daily_quota"] == 50
        assert set(body) == {"id", "name", "email", "role", "ai_daily_quota", "created_at"}
        assert "password" not in r.text

    async def test_password_is_stored_only_as_an_argon2_hash(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        await register(api, "a@example.com")
        u = await user_by_email(session, "a@example.com")
        assert u.password_hash.startswith("$argon2id$") and PASSWORD not in u.password_hash

    async def test_duplicate_email_is_409_even_with_different_case(self, api: AsyncClient) -> None:
        assert (await register(api, "dup@example.com")).status_code == 201
        r = await register(api, "DUP@example.com")
        assert r.status_code == 409 and error_of(r)["code"] == "EMAIL_TAKEN"

    async def test_the_app_still_works_after_a_conflict(self, api: AsyncClient) -> None:
        await register(api, "dup@example.com")
        await register(api, "dup@example.com")
        assert (await register(api, "other@example.com")).status_code == 201

    @pytest.mark.parametrize(
        "extra", [{"role": "ADMIN"}, {"is_active": True}, {"ai_daily_quota": 9999}]
    )
    async def test_mass_assignment_is_rejected(
        self, api: AsyncClient, extra: dict[str, object]
    ) -> None:
        r = await api.post(
            "/api/v1/auth/register",
            json={"name": "x", "email": "m@example.com", "password": PASSWORD, **extra},
        )
        assert r.status_code == 422 and error_of(r)["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("password", ["Zq9-Zq9", "123456789", "x" * 129])
    async def test_password_length_is_enforced(self, api: AsyncClient, password: str) -> None:
        r = await register(api, "p@example.com", password=password)
        assert r.status_code == 422
        assert password not in r.text  # never echoed back

    @pytest.mark.parametrize("email", ["not-an-email", "a@", "@b.com", ""])
    async def test_invalid_email_rejected(self, api: AsyncClient, email: str) -> None:
        assert (await register(api, email)).status_code == 422

    async def test_blank_name_rejected(self, api: AsyncClient) -> None:
        assert (await register(api, name="   ")).status_code == 422


# ------------------------------------------------------------------ login
class TestLogin:
    async def test_success_returns_access_token_and_hardened_cookie(self, api: AsyncClient) -> None:
        await register(api)
        r = await login(api)
        assert r.status_code == 200
        body = r.json()
        assert body["token_type"] == "bearer" and body["expires_in"] == 15 * 60
        assert "refresh_token" not in body  # the refresh token is only ever in the cookie
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert "path=/api/v1/auth" in cookie and f"max-age={14 * 86400}" in cookie

    async def test_secure_flag_follows_configuration(self, app: FastAPI, api: AsyncClient) -> None:
        app.dependency_overrides[get_settings] = lambda: Settings(
            _env_file=None, cookie_secure=True
        )
        await register(api)
        r = await api.post(
            "/api/v1/auth/login", json={"email": "seller@example.com", "password": PASSWORD}
        )
        assert "secure" in r.headers["set-cookie"].lower()

    async def test_the_access_token_authenticates(self, api: AsyncClient) -> None:
        access, _ = await signup_and_login(api)
        r = await api.get("/api/v1/auth/me", headers=bearer(access))
        assert r.status_code == 200 and r.json()["email"] == "seller@example.com"

    async def test_only_the_hash_of_the_refresh_token_is_stored(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, raw = await signup_and_login(api)
        (row,) = await tokens_of(session, await user_by_email(session, "seller@example.com"))
        assert row.token_hash == sha256(raw) and raw not in row.token_hash
        assert row.revoked_at is None and row.expires_at > datetime.now(UTC) + timedelta(days=13)

    async def test_email_is_case_insensitive(self, api: AsyncClient) -> None:
        await register(api, "case@example.com")
        assert (await login(api, "CASE@Example.com")).status_code == 200

    async def test_wrong_password_and_unknown_email_are_indistinguishable(
        self, api: AsyncClient
    ) -> None:
        await register(api)
        wrong = await login(api, password="definitely-wrong")
        unknown = await login(api, email="nobody@example.com")
        assert wrong.status_code == unknown.status_code == 401
        a, b = error_of(wrong), error_of(unknown)
        assert (a["code"], a["message"]) == (b["code"], b["message"]) == (
            "INVALID_CREDENTIALS", "Invalid email or password.",
        )  # fmt: skip
        assert "set-cookie" not in wrong.headers and "set-cookie" not in unknown.headers

    async def test_unknown_email_still_burns_argon2_time(
        self, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        real = security.verify_dummy
        monkeypatch.setattr(security, "verify_dummy", lambda pw: (calls.append(pw), real(pw))[1])
        await login(api, email="ghost@example.com", password="whatever-guess")
        assert calls == ["whatever-guess"]

    async def test_disabled_account_gets_403_only_with_the_right_password(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        await register(api)
        (await user_by_email(session, "seller@example.com")).is_active = False
        await session.flush()
        ok = await login(api)
        assert ok.status_code == 403 and error_of(ok)["code"] == "ACCOUNT_DISABLED"
        bad = await login(api, password="wrong-password-1")
        assert bad.status_code == 401  # a wrong guess learns nothing about account state

    async def test_old_password_hash_is_upgraded_on_login(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
        await make_user(session, email="old@example.com", password_hash=weak)
        assert security.needs_rehash(weak)
        assert (await login(api, "old@example.com")).status_code == 200
        upgraded = (await user_by_email(session, "old@example.com")).password_hash
        assert upgraded != weak and not security.needs_rehash(upgraded)
        assert (await login(api, "old@example.com")).status_code == 200  # still works after upgrade

    async def test_each_login_starts_a_new_session_family(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        await register(api)
        await login(api)
        await login(api)
        rows = await tokens_of(session, await user_by_email(session, "seller@example.com"))
        assert len(rows) == 2 and rows[0].family_id != rows[1].family_id

    async def test_role_cannot_be_smuggled_in_login(self, api: AsyncClient) -> None:
        await register(api)
        r = await api.post(
            "/api/v1/auth/login",
            json={"email": "seller@example.com", "password": PASSWORD, "role": "ADMIN"},
        )
        assert r.status_code == 422


# ------------------------------------------------------------------ refresh: rotation & reuse detection
class TestRefresh:
    async def test_rotation_issues_a_new_token_and_retires_the_old_one(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, first = await signup_and_login(api)
        r = await refresh(api, first)
        assert r.status_code == 200
        second = refresh_cookie(r)
        assert second != first and r.json()["access_token"]

        old, new = await tokens_of(session, await user_by_email(session, "seller@example.com"))
        assert old.token_hash == sha256(first) and old.revoked_at is not None
        assert old.replaced_by_id == new.id and new.family_id == old.family_id
        assert new.token_hash == sha256(second) and new.revoked_at is None

    async def test_the_new_access_token_works_and_the_chain_can_continue(
        self, api: AsyncClient
    ) -> None:
        _, t1 = await signup_and_login(api)
        r2 = await refresh(api, t1)
        t2 = refresh_cookie(r2)
        assert (
            await api.get("/api/v1/auth/me", headers=bearer(r2.json()["access_token"]))
        ).status_code == 200
        r3 = await refresh(api, t2)
        assert r3.status_code == 200 and refresh_cookie(r3) not in (t1, t2)

    async def test_reusing_a_rotated_token_revokes_the_whole_family(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, stolen = await signup_and_login(api)
        legit = refresh_cookie(
            await refresh(api, stolen)
        )  # the real user rotates; `stolen` is now old

        replay = await refresh(api, stolen)  # the thief presents the old copy
        assert replay.status_code == 401 and error_of(replay)["code"] == "REFRESH_TOKEN_REUSED"

        # The legitimate user's newest token died with the family: both parties must log in again.
        after = await refresh(api, legit)
        assert after.status_code == 401
        rows = await tokens_of(session, await user_by_email(session, "seller@example.com"))
        assert all(t.revoked_at is not None for t in rows)

    async def test_reuse_only_affects_that_session_not_the_users_other_devices(
        self, api: AsyncClient
    ) -> None:
        await register(api)
        r1, r2 = await login(api), await login(api)  # two devices
        phone, laptop = refresh_cookie(r1), refresh_cookie(r2)
        await refresh(api, phone)
        assert error_of(await refresh(api, phone))["code"] == "REFRESH_TOKEN_REUSED"
        assert (await refresh(api, laptop)).status_code == 200  # other family untouched

    async def test_replaying_a_token_revoked_by_logout_is_invalid_not_reuse(
        self, api: AsyncClient
    ) -> None:
        _, t = await signup_and_login(api)
        await api.post("/api/v1/auth/logout", headers={**CSRF, "Cookie": f"refresh_token={t}"})
        r = await refresh(api, t)
        assert r.status_code == 401 and error_of(r)["code"] == "INVALID_REFRESH_TOKEN"

    @pytest.mark.parametrize("token", [None, "", "garbage", "a" * 43, "../../etc/passwd"])
    async def test_missing_or_bogus_tokens_are_rejected(
        self, api: AsyncClient, token: str | None
    ) -> None:
        r = await refresh(api, token)
        assert r.status_code == 401 and error_of(r)["code"] == "INVALID_REFRESH_TOKEN"

    async def test_expired_token_is_rejected(self, api: AsyncClient, session: AsyncSession) -> None:
        _, t = await signup_and_login(api)
        await session.execute(
            text("UPDATE refresh_tokens SET expires_at = now() - interval '1 second'")
        )
        r = await refresh(api, t)
        assert r.status_code == 401 and error_of(r)["code"] == "INVALID_REFRESH_TOKEN"

    async def test_deactivated_user_cannot_refresh_and_loses_the_family(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        _, t = await signup_and_login(api)
        (await user_by_email(session, "seller@example.com")).is_active = False
        await session.flush()
        assert (await refresh(api, t)).status_code == 401
        rows = await tokens_of(session, await user_by_email(session, "seller@example.com"))
        assert all(x.revoked_at is not None for x in rows)

    async def test_csrf_header_is_required(self, api: AsyncClient) -> None:
        _, t = await signup_and_login(api)
        r = await refresh(api, t, csrf=False)
        assert r.status_code == 403 and error_of(r)["code"] == "CSRF_CHECK_FAILED"
        assert (await refresh(api, t)).status_code == 200  # and the token was not consumed above

    async def test_a_failed_refresh_clears_the_dead_cookie(self, api: AsyncClient) -> None:
        r = await refresh(api, "garbage")
        assert (
            "refresh_token=" in r.headers["set-cookie"]
            and "max-age=0" in r.headers["set-cookie"].lower()
        )

    async def test_an_access_token_is_not_accepted_as_a_refresh_token(
        self, api: AsyncClient
    ) -> None:
        access, _ = await signup_and_login(api)
        assert (await refresh(api, access)).status_code == 401

    async def test_concurrent_refresh_with_one_token_yields_exactly_one_winner(
        self, app: FastAPI, db_engine: AsyncEngine
    ) -> None:
        """Real concurrency against committed data: the row lock serializes the two requests, so
        the loser sees the rotation and is treated as reuse (strict by design)."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(db_engine, expire_on_commit=False)

        async def real_session() -> object:
            async with maker() as s:
                yield s

        app.dependency_overrides[get_db] = real_session  # type: ignore[assignment]
        email = f"race-{uuid.uuid4().hex[:8]}@example.com"
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                await register(c, email)
                token = refresh_cookie(await login(c, email))
                results = await asyncio.gather(refresh(c, token), refresh(c, token))
            codes = sorted(r.status_code for r in results)
            assert codes == [200, 401]
            loser = next(r for r in results if r.status_code == 401)
            assert error_of(loser)["code"] == "REFRESH_TOKEN_REUSED"
        finally:
            async with db_engine.begin() as conn:  # this test committed real rows: clean up
                await conn.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})


# ------------------------------------------------------------------ logout
class TestLogout:
    async def test_logout_kills_the_session_and_clears_the_cookie(self, api: AsyncClient) -> None:
        _, t = await signup_and_login(api)
        r = await api.post("/api/v1/auth/logout", headers={**CSRF, "Cookie": f"refresh_token={t}"})
        assert r.status_code == 204 and "max-age=0" in r.headers["set-cookie"].lower()
        assert (await refresh(api, t)).status_code == 401

    async def test_logout_only_ends_that_devices_session(self, api: AsyncClient) -> None:
        await register(api)
        phone, laptop = refresh_cookie(await login(api)), refresh_cookie(await login(api))
        await api.post("/api/v1/auth/logout", headers={**CSRF, "Cookie": f"refresh_token={phone}"})
        assert (await refresh(api, phone)).status_code == 401
        assert (await refresh(api, laptop)).status_code == 200

    @pytest.mark.parametrize("cookie", [None, "refresh_token=unknown-token-value"])
    async def test_logout_is_idempotent(self, api: AsyncClient, cookie: str | None) -> None:
        api.cookies.clear()
        headers = {**CSRF, **({"Cookie": cookie} if cookie else {})}
        assert (await api.post("/api/v1/auth/logout", headers=headers)).status_code == 204

    async def test_logout_requires_the_csrf_header(self, api: AsyncClient) -> None:
        _, t = await signup_and_login(api)
        r = await api.post("/api/v1/auth/logout", headers={"Cookie": f"refresh_token={t}"})
        assert r.status_code == 403
        assert (await refresh(api, t)).status_code == 200  # session survived the blocked request

    async def test_the_access_token_expires_on_its_own_not_at_logout(
        self, api: AsyncClient
    ) -> None:
        """Documented trade-off of stateless access tokens: they live out their 15 minutes."""
        access, t = await signup_and_login(api)
        await api.post("/api/v1/auth/logout", headers={**CSRF, "Cookie": f"refresh_token={t}"})
        assert (await api.get("/api/v1/auth/me", headers=bearer(access))).status_code == 200
