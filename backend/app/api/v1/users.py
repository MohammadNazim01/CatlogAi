from fastapi import APIRouter, Response

from app.api.deps import CurrentUser, DbSession, SettingsDep
from app.api.v1.auth import UserAgent, set_refresh_cookie, token_response
from app.schemas.auth import ChangePasswordRequest, TokenResponse
from app.schemas.user import UpdateProfileRequest, UserOut
from app.services.auth_service import AuthService

router = APIRouter(prefix="/users", tags=["users"])


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: UpdateProfileRequest, user: CurrentUser, session: DbSession, settings: SettingsDep
) -> UserOut:
    updated = await AuthService(session, settings).update_profile(user, name=body.name)
    return UserOut.model_validate(updated)


@router.post("/me/password", response_model=TokenResponse)
async def change_password(
    body: ChangePasswordRequest,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    user_agent: UserAgent = None,
) -> TokenResponse:
    """Revokes every existing session and returns a fresh one for this device. (The refresh
    cookie is path-scoped to /api/v1/auth, so it is not available here to keep 'this' session.)"""
    tokens = await AuthService(session, settings).change_password(
        user,
        current_password=body.current_password,
        new_password=body.new_password,
        user_agent=user_agent,
    )
    set_refresh_cookie(response, tokens, settings)
    return token_response(tokens)
