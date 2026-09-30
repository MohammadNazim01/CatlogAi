"""Authentication use cases. Owns the transaction boundary: methods commit before returning."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import BadRequestError, ForbiddenError, UnauthenticatedError
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password_async,
    hash_refresh_token,
    needs_rehash,
    refresh_token_expiry,
    verify_dummy_async,
    verify_password_async,
)  # fmt: skip
from app.db.models import RefreshToken, User
from app.repositories.refresh_token_repo import RefreshTokenRepository
from app.repositories.user_repo import UserRepository, normalize_email

logger = logging.getLogger(__name__)

MAX_USER_AGENT_LENGTH = 255


@dataclass(frozen=True)
class SessionTokens:
    access_token: str
    refresh_token: str  # raw value; goes into the cookie, only its hash is stored
    expires_in: int


def _invalid_credentials() -> UnauthenticatedError:
    # One message for unknown email and wrong password, so responses are not an oracle.
    return UnauthenticatedError("INVALID_CREDENTIALS", "Invalid email or password.")


def _invalid_refresh() -> UnauthenticatedError:
    return UnauthenticatedError("INVALID_REFRESH_TOKEN", "Session expired. Please log in again.")


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.users = UserRepository(session)
        self.tokens = RefreshTokenRepository(session)

    # ------------------------------------------------------------------ account
    async def register(self, *, name: str, email: str, password: str) -> User:
        user = User(
            name=name.strip(),
            email=normalize_email(email),
            password_hash=await hash_password_async(password),
            # role, quota and is_active come from column defaults: never from the client.
        )
        self.users.add(user)
        # A duplicate email violates ux_users_email_lower; the global handler turns that into a
        # 409 EMAIL_TAKEN. Relying on the constraint (not a pre-check) is race-free.
        await self.session.commit()
        return user

    async def update_profile(self, user: User, *, name: str) -> User:
        user.name = name.strip()
        await self.session.commit()
        return user

    async def change_password(
        self, user: User, *, current_password: str, new_password: str, user_agent: str | None
    ) -> SessionTokens:
        if not await verify_password_async(user.password_hash, current_password):
            raise BadRequestError("WRONG_PASSWORD", "Current password is incorrect.")
        user.password_hash = await hash_password_async(new_password)
        now = datetime.now(UTC)
        # Every existing session dies (a stolen session must not survive a password change).
        # The caller gets a fresh one, so the current device stays logged in.
        await self.tokens.revoke_all_for_user(user.id, now)
        tokens = self._issue_session(user, user_agent, family_id=uuid.uuid4())
        await self.session.commit()
        logger.info("password changed", extra={"user_id": str(user.id)})
        return tokens

    # ------------------------------------------------------------------ sessions
    async def login(
        self, *, email: str, password: str, user_agent: str | None
    ) -> tuple[User, SessionTokens]:
        user = await self.users.get_by_email(email)
        if user is None:
            await verify_dummy_async(password)  # same CPU cost as a real check
            raise _invalid_credentials()
        if not await verify_password_async(user.password_hash, password):
            raise _invalid_credentials()
        # Only after the password is proven: a wrong guess must not learn account state.
        if not user.is_active:
            raise ForbiddenError("ACCOUNT_DISABLED", "This account has been disabled.")

        if needs_rehash(user.password_hash):  # transparently upgrade old hash parameters
            user.password_hash = await hash_password_async(password)

        tokens = self._issue_session(user, user_agent, family_id=uuid.uuid4())
        await self.session.commit()
        logger.info("login succeeded", extra={"user_id": str(user.id)})
        return user, tokens

    async def refresh(self, *, raw_token: str, user_agent: str | None) -> SessionTokens:
        now = datetime.now(UTC)
        row = await self.tokens.get_by_hash_for_update(hash_refresh_token(raw_token))
        if row is None:
            raise _invalid_refresh()

        if row.revoked_at is not None:
            if row.replaced_by_id is not None:
                # This token was already rotated, so someone is replaying an old copy: either a
                # thief or a client that lost the rotation response. Kill the whole session
                # family so neither party keeps access, and make the legitimate user log in.
                await self.tokens.revoke_family(row.family_id, now)
                await self.session.commit()  # persist the revocation even though we raise
                logger.warning(
                    "refresh token reuse detected; family revoked",
                    extra={"user_id": str(row.user_id), "family_id": str(row.family_id)},
                )
                raise UnauthenticatedError(
                    "REFRESH_TOKEN_REUSED",
                    "Session invalidated for your security. Please log in again.",
                )
            raise _invalid_refresh()  # revoked by logout / password change: plainly invalid

        if row.expires_at <= now:
            raise _invalid_refresh()

        user = await self.users.get(row.user_id)
        if user is None or not user.is_active:
            await self.tokens.revoke_family(row.family_id, now)
            await self.session.commit()
            raise _invalid_refresh()

        new_tokens, new_row = self._issue_session_with_row(
            user, user_agent, family_id=row.family_id
        )
        # INSERT the new row before pointing the old one at it: the unit of work would otherwise
        # emit the UPDATE first and violate the replaced_by_id foreign key.
        await self.session.flush()
        row.revoked_at = now
        row.replaced_by_id = new_row.id
        await self.session.commit()
        return new_tokens

    async def logout(self, raw_token: str | None) -> None:
        """Idempotent: an unknown, expired or missing token is not an error."""
        if not raw_token:
            return
        row = await self.tokens.get_by_hash_for_update(hash_refresh_token(raw_token))
        if row is None:
            return
        await self.tokens.revoke_family(row.family_id, datetime.now(UTC))
        await self.session.commit()

    # ------------------------------------------------------------------ helpers
    def _issue_session(
        self, user: User, user_agent: str | None, *, family_id: uuid.UUID
    ) -> SessionTokens:
        return self._issue_session_with_row(user, user_agent, family_id=family_id)[0]

    def _issue_session_with_row(
        self, user: User, user_agent: str | None, *, family_id: uuid.UUID
    ) -> tuple[SessionTokens, RefreshToken]:
        raw = generate_refresh_token()
        row = RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            family_id=family_id,
            token_hash=hash_refresh_token(raw),
            expires_at=refresh_token_expiry(settings=self.settings),
            user_agent=user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else None,
        )
        self.tokens.add(row)
        access = create_access_token(user.id, user.role, settings=self.settings)
        return (
            SessionTokens(access, raw, expires_in=self.settings.access_token_ttl_min * 60),
            row,
        )
