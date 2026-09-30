import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        # Matches the lower(email) unique index, so the lookup is indexed and case-insensitive.
        stmt = select(User).where(func.lower(User.email) == normalize_email(email))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def add(self, user: User) -> None:
        self.session.add(user)
