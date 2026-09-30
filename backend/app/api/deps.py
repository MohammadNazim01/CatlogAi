"""FastAPI dependencies shared by all routers."""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import ForbiddenError, UnauthenticatedError
from app.core.security import InvalidAccessTokenError, decode_access_token
from app.db.enums import Role
from app.db.models import User
from app.db.session import get_db
from app.repositories.user_repo import UserRepository

logger = logging.getLogger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_bearer = HTTPBearer(auto_error=False)  # we raise our own uniform error instead

CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "catalogai"


def _unauthenticated() -> UnauthenticatedError:
    # Every authentication failure looks identical to the client; reasons go to the log only.
    return UnauthenticatedError(
        "UNAUTHENTICATED", "Authentication required.", headers={"WWW-Authenticate": "Bearer"}
    )


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: DbSession,
    settings: SettingsDep,
) -> User:
    if creds is None:
        raise _unauthenticated()
    try:
        claims = decode_access_token(creds.credentials, settings=settings)
    except InvalidAccessTokenError as exc:
        logger.debug("access token rejected: %s", exc.reason)
        raise _unauthenticated() from exc

    # Loaded from the database on every request: deactivation takes effect immediately, and
    # authorization uses the stored role, never the (possibly stale) role claim in the token.
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        raise _unauthenticated()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: Role) -> Callable[[User], Awaitable[User]]:
    async def dependency(user: CurrentUser) -> User:
        if user.role not in roles:
            raise ForbiddenError("FORBIDDEN", "You do not have permission to do this.")
        return user

    return dependency


async def require_csrf_header(
    x_requested_with: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> None:
    """Cookie-authenticated endpoints (refresh, logout) demand a custom header. A cross-site
    form or <img> cannot set it, and a cross-origin fetch that does must pass a CORS preflight,
    which only our allowlisted origins succeed at. This backs up SameSite=Strict."""
    if x_requested_with != CSRF_HEADER_VALUE:
        raise ForbiddenError("CSRF_CHECK_FAILED", "Missing or invalid request header.")
