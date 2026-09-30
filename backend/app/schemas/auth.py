from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

from app.schemas.user import Name

# Length over composition rules (NIST 800-63B). The upper bound caps Argon2 work per request.
Password = Annotated[str, StringConstraints(min_length=10, max_length=128)]


class RegisterRequest(BaseModel):
    # extra="forbid": a client sending {"role": "ADMIN"} gets a 422 instead of being ignored.
    model_config = ConfigDict(extra="forbid")

    name: Name
    email: EmailStr
    password: Password


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # No minimum: a wrong short password is a normal failed login, not a validation error.
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    new_password: Password


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth token type, not a secret)
    expires_in: int  # seconds
