"""Security primitives: password hashing, access-token JWTs, opaque refresh tokens.

Pure functions with no database access. Policy (rotation, reuse detection, who may do what)
lives in the auth service; this module only answers "is this value valid?".
"""

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings, get_settings
from app.db.enums import Role

# --------------------------------------------------------------------------- passwords
# Argon2id with argon2-cffi's defaults (RFC 9106 "low memory" profile: 64 MiB, 3 passes).
# Each hash embeds its own random salt and parameters, so parameters can be raised later and
# old hashes upgraded on next login (see needs_rehash).
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """False for a wrong password AND for a malformed stored hash; never raises."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@lru_cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def verify_dummy(password: str) -> None:
    """Burn the same CPU as a real verification. Login calls this when the email is unknown so
    response time does not reveal whether an account exists."""
    verify_password(_dummy_hash(), password)


# Argon2 is deliberately slow (tens of ms) and CPU-bound: never call the sync versions on the
# event loop from request handlers.
async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password_hash: str, password: str) -> bool:
    return await asyncio.to_thread(verify_password, password_hash, password)


async def verify_dummy_async(password: str) -> None:
    await asyncio.to_thread(verify_dummy, password)


# --------------------------------------------------------------------------- access tokens
ACCESS_TOKEN_TYPE = "access"  # noqa: S105  (a claim value, not a secret)
_REQUIRED_CLAIMS = ["sub", "role", "iat", "exp", "typ"]


class InvalidAccessTokenError(Exception):
    """Any reason an access token is unusable. `reason` is for logs only; clients must get one
    uniform 401 so failures do not become an oracle."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    role: Role


def create_access_token(
    user_id: uuid.UUID,
    role: Role,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> str:
    s = settings or get_settings()
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "typ": ACCESS_TOKEN_TYPE,
        "iat": issued,
        "exp": issued + timedelta(minutes=s.access_token_ttl_min),
    }
    return jwt.encode(payload, s.jwt_secret_key.get_secret_value(), algorithm=s.jwt_algorithm)


def decode_access_token(token: str, *, settings: Settings | None = None) -> AccessTokenClaims:
    s = settings or get_settings()
    try:
        # algorithms is pinned: tokens using "none" or any other algorithm are rejected, which
        # blocks algorithm-confusion attacks. Expiry is checked; required claims must exist.
        payload = jwt.decode(
            token,
            s.jwt_secret_key.get_secret_value(),
            algorithms=[s.jwt_algorithm],
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise InvalidAccessTokenError(type(exc).__name__) from exc

    if payload["typ"] != ACCESS_TOKEN_TYPE:
        raise InvalidAccessTokenError("wrong token type")
    try:
        return AccessTokenClaims(user_id=uuid.UUID(payload["sub"]), role=Role(payload["role"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidAccessTokenError("malformed claims") from exc


# --------------------------------------------------------------------------- refresh tokens
# Refresh tokens are opaque random strings, not JWTs, so they can be revoked. Only the SHA-256
# hash is stored. A fast hash is correct here (unlike passwords): the input already has 256 bits
# of entropy, so there is nothing to brute-force, and lookups by hash stay cheap.
REFRESH_TOKEN_BYTES = 32  # 256 bits


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()  # 64 hex chars -> CHAR(64)


def refresh_token_expiry(
    *, settings: Settings | None = None, now: datetime | None = None
) -> datetime:
    s = settings or get_settings()
    return (now or datetime.now(UTC)) + timedelta(days=s.refresh_token_ttl_days)
