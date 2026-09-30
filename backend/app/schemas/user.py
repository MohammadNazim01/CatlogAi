import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

from app.db.enums import Role

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: EmailStr
    role: Role
    ai_daily_quota: int
    created_at: datetime


class UpdateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # email and role are not client-editable

    name: Name
