import uuid

from sqlalchemy import CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, str_enum, uuid_pk
from app.db.enums import Role


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(320))  # normalized (trim + lower) by the service
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(
        str_enum(Role, "role"), default=Role.SELLER, server_default=Role.SELLER.value
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    ai_daily_quota: Mapped[int] = mapped_column(default=50, server_default=text("50"))

    __table_args__ = (
        # Defense in depth: uniqueness holds even if a code path forgets to normalize.
        Index("ux_users_email_lower", text("lower(email)"), unique=True),
        CheckConstraint("ai_daily_quota >= 0", name="ai_daily_quota_nonneg"),
    )
