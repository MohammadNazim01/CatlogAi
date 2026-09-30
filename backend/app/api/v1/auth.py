from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, DbSession, SettingsDep, require_csrf_header
from app.core.config import Settings
from app.core.exceptions import UnauthenticatedError, error_response
from app.core.middleware import get_request_id
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.schemas.user import UserOut
from app.services.auth_service import AuthService, SessionTokens

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
# Scoped to the auth endpoints: the browser never sends the refresh token anywhere else.
REFRESH_COOKIE_PATH = "/api/v1/auth"

UserAgent = Annotated[str | None, Header(alias="User-Agent")]


def set_refresh_cookie(response: Response, tokens: SessionTokens, settings: Settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        max_age=settings.refresh_token_ttl_days * 86400,
        httponly=True,  # invisible to JavaScript, so XSS cannot steal it
        secure=settings.cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def token_response(tokens: SessionTokens) -> TokenResponse:
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, session: DbSession, settings: SettingsDep) -> UserOut:
    user = await AuthService(session, settings).register(
        name=body.name, email=body.email, password=body.password
    )
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    user_agent: UserAgent = None,
) -> TokenResponse:
    _, tokens = await AuthService(session, settings).login(
        email=body.email, password=body.password, user_agent=user_agent
    )
    set_refresh_cookie(response, tokens, settings)
    return token_response(tokens)


@router.post("/refresh", response_model=TokenResponse, dependencies=[Depends(require_csrf_header)])
async def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie()] = None,
    user_agent: UserAgent = None,
) -> TokenResponse | JSONResponse:
    try:
        if not refresh_token:
            raise UnauthenticatedError(
                "INVALID_REFRESH_TOKEN", "Session expired. Please log in again."
            )
        tokens = await AuthService(session, settings).refresh(
            raw_token=refresh_token, user_agent=user_agent
        )
    except UnauthenticatedError as exc:
        # A dead cookie is useless, so tell the browser to drop it. The header has to go on the
        # error response itself: headers set on the injected `response` are discarded when an
        # exception is raised, because the exception handler builds a brand-new response.
        failure = error_response(
            exc.status_code,
            exc.code,
            exc.message,
            get_request_id(request.scope),
            headers=exc.headers,
        )
        clear_refresh_cookie(failure, settings)
        return failure
    set_refresh_cookie(response, tokens, settings)
    return token_response(tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf_header)],
)
async def logout(
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> None:
    await AuthService(session, settings).logout(refresh_token)
    clear_refresh_cookie(response, settings)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
