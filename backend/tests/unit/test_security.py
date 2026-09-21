import base64
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core import security
from app.core.config import Settings
from app.core.security import (
    InvalidAccessTokenError,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    refresh_token_expiry,
    verify_dummy,
    verify_password,
)  # fmt: skip
from app.db.enums import Role

SECRET = "unit-test-secret-" + "x" * 32


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, jwt_secret_key=kw.pop("jwt_secret_key", SECRET), **kw)  # type: ignore[arg-type]


def b64(d: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


# ------------------------------------------------------------------ passwords
class TestPasswords:
    def test_hash_is_argon2id_and_never_contains_the_password(self) -> None:
        h = hash_password("correct horse battery staple")
        assert h.startswith("$argon2id$")
        assert "correct horse" not in h

    def test_same_password_gets_a_different_salt_each_time(self) -> None:
        assert hash_password("same-password-1") != hash_password("same-password-1")

    def test_verify_accepts_correct_and_rejects_wrong(self) -> None:
        h = hash_password("S3cret-passphrase")
        assert verify_password(h, "S3cret-passphrase") is True
        assert verify_password(h, "S3cret-passphrasE") is False
        assert verify_password(h, "") is False

    def test_unicode_and_long_passwords(self) -> None:
        for pw in ("pässwörd-密码-🔐-long-enough", "a" * 128, "with space and\ttab"):
            assert verify_password(hash_password(pw), pw)

    @pytest.mark.parametrize(
        "garbage", ["", "not-a-hash", "$argon2id$broken", "$2b$12$bcrypt-looking"]
    )
    def test_malformed_stored_hash_is_a_failed_login_not_an_exception(self, garbage: str) -> None:
        assert verify_password(garbage, "anything") is False

    def test_weaker_parameters_are_flagged_for_rehash(self) -> None:
        from argon2 import PasswordHasher

        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("pw-for-rehash")
        assert needs_rehash(weak) is True
        assert needs_rehash(hash_password("pw-for-rehash")) is False

    def test_dummy_verification_does_real_argon2_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Login calls verify_dummy for unknown emails; it must cost the same as a real check."""
        calls: list[str] = []
        real = security._hasher

        class SpyHasher:
            def __getattr__(self, name: str) -> object:
                return getattr(real, name)  # everything except verify is the real hasher

            def verify(self, h: str, p: str) -> bool:
                calls.append(h)
                return real.verify(h, p)

        monkeypatch.setattr(security, "_hasher", SpyHasher())
        verify_dummy("whatever-the-attacker-typed")
        assert len(calls) == 1 and calls[0].startswith("$argon2id$")

    def test_dummy_hash_is_stable_within_a_process(self) -> None:
        assert security._dummy_hash() == security._dummy_hash()

    async def test_async_wrappers(self) -> None:
        h = await security.hash_password_async("async-password-1")
        assert await security.verify_password_async(h, "async-password-1")
        assert not await security.verify_password_async(h, "nope")
        await security.verify_dummy_async("x")


# ------------------------------------------------------------------ access tokens
class TestAccessTokens:
    def test_roundtrip(self) -> None:
        uid = uuid.uuid4()
        claims = decode_access_token(
            create_access_token(uid, Role.ADMIN, settings=settings()), settings=settings()
        )
        assert claims.user_id == uid and claims.role == Role.ADMIN

    def test_claims_and_lifetime(self) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        token = create_access_token(
            uuid.uuid4(), Role.SELLER, settings=settings(access_token_ttl_min=15), now=now
        )
        payload = jwt.decode(token, options={"verify_signature": False})
        assert set(payload) == {"sub", "role", "typ", "iat", "exp"}
        assert payload["typ"] == "access" and payload["role"] == "SELLER"
        assert payload["exp"] - payload["iat"] == 15 * 60

    def test_header_pins_hs256(self) -> None:
        token = create_access_token(uuid.uuid4(), Role.SELLER, settings=settings())
        assert jwt.get_unverified_header(token)["alg"] == "HS256"

    def test_expired_token_rejected(self) -> None:
        old = datetime.now(UTC) - timedelta(hours=1)
        token = create_access_token(uuid.uuid4(), Role.SELLER, settings=settings(), now=old)
        with pytest.raises(InvalidAccessTokenError) as e:
            decode_access_token(token, settings=settings())
        assert e.value.reason == "ExpiredSignatureError"

    def test_token_signed_with_another_secret_rejected(self) -> None:
        token = create_access_token(
            uuid.uuid4(), Role.SELLER, settings=settings(jwt_secret_key="other-" + "y" * 40)
        )
        with pytest.raises(InvalidAccessTokenError):
            decode_access_token(token, settings=settings())

    def test_tampered_payload_rejected(self) -> None:
        """Change the role from SELLER to ADMIN while keeping the original signature."""
        token = create_access_token(uuid.uuid4(), Role.SELLER, settings=settings())
        header, payload, sig = token.split(".")
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        body["role"] = "ADMIN"
        forged = ".".join([header, b64(body), sig])
        with pytest.raises(InvalidAccessTokenError):
            decode_access_token(forged, settings=settings())

    def test_alg_none_token_rejected(self) -> None:
        now = int(datetime.now(UTC).timestamp())
        claims = {
            "sub": str(uuid.uuid4()),
            "role": "ADMIN",
            "typ": "access",
            "iat": now,
            "exp": now + 900,
        }
        unsigned = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."
        with pytest.raises(InvalidAccessTokenError):
            decode_access_token(unsigned, settings=settings())

    @pytest.mark.filterwarnings(
        "ignore::jwt.warnings.InsecureKeyLengthWarning"
    )  # short key is deliberate here
    def test_other_hmac_algorithm_rejected_algorithm_confusion(self) -> None:
        now = datetime.now(UTC)
        claims = {
            "sub": str(uuid.uuid4()),
            "role": "SELLER",
            "typ": "access",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        token = jwt.encode(claims, SECRET, algorithm="HS512")  # right secret, wrong algorithm
        with pytest.raises(InvalidAccessTokenError):
            decode_access_token(token, settings=settings())

    def test_wrong_token_type_rejected(self) -> None:
        now = datetime.now(UTC)
        claims = {
            "sub": str(uuid.uuid4()),
            "role": "SELLER",
            "typ": "refresh",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        with pytest.raises(InvalidAccessTokenError, match="wrong token type"):
            decode_access_token(jwt.encode(claims, SECRET, algorithm="HS256"), settings=settings())

    @pytest.mark.parametrize("missing", ["sub", "role", "iat", "exp", "typ"])
    def test_missing_required_claim_rejected(self, missing: str) -> None:
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "sub": str(uuid.uuid4()),
            "role": "SELLER",
            "typ": "access",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        del claims[missing]
        with pytest.raises(InvalidAccessTokenError):
            decode_access_token(jwt.encode(claims, SECRET, algorithm="HS256"), settings=settings())

    @pytest.mark.parametrize(
        ("sub", "role"),
        [("not-a-uuid", "SELLER"), (str(uuid.uuid4()), "SUPERUSER"), (12345, "SELLER")],
    )
    def test_malformed_claim_values_rejected(self, sub: object, role: str) -> None:
        now = datetime.now(UTC)
        claims = {
            "sub": sub,
            "role": role,
            "typ": "access",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        with pytest.raises(InvalidAccessTokenError):
            decode_access_token(jwt.encode(claims, SECRET, algorithm="HS256"), settings=settings())

    @pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "....", " ", "Bearer x.y.z"])
    def test_garbage_rejected_with_the_same_exception_type(self, garbage: str) -> None:
        with pytest.raises(InvalidAccessTokenError):
            decode_access_token(garbage, settings=settings())

    def test_a_refresh_token_is_not_an_access_token(self) -> None:
        with pytest.raises(InvalidAccessTokenError):
            decode_access_token(generate_refresh_token(), settings=settings())


# ------------------------------------------------------------------ refresh tokens
class TestRefreshTokens:
    def test_tokens_are_long_random_and_url_safe(self) -> None:
        t = generate_refresh_token()
        assert len(t) >= 43  # 32 random bytes, base64url
        assert re.fullmatch(r"[A-Za-z0-9_-]+", t)

    def test_tokens_do_not_repeat(self) -> None:
        assert len({generate_refresh_token() for _ in range(1000)}) == 1000

    def test_hash_is_deterministic_64_hex_and_not_the_token(self) -> None:
        t = generate_refresh_token()
        h = hash_refresh_token(t)
        assert h == hash_refresh_token(t)
        assert re.fullmatch(r"[0-9a-f]{64}", h)
        assert t not in h

    def test_different_tokens_hash_differently(self) -> None:
        assert hash_refresh_token("a") != hash_refresh_token("b")

    def test_known_sha256_vector(self) -> None:
        assert (
            hash_refresh_token("abc")
            == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )

    def test_expiry_uses_configured_days(self) -> None:
        now = datetime(2030, 1, 1, tzinfo=UTC)
        assert refresh_token_expiry(
            settings=settings(refresh_token_ttl_days=14), now=now
        ) == now + timedelta(days=14)
