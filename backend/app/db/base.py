import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, MetaData, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names: Alembic cannot reliably alter/drop unnamed constraints, and
# the API maps violated constraint names to error codes (see core/exceptions.py).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # onupdate fires on ORM UPDATEs only; raw SQL updates must set updated_at themselves.
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


def uuid_pk() -> Mapped[uuid.UUID]:
    # Python-side default so the id is known before flush; server default covers raw SQL inserts.
    return mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def str_enum(enum_cls: type[StrEnum], name: str, length: int = 16) -> Enum:
    """VARCHAR + named CHECK constraint (ck_<table>_<name>)."""
    return Enum(
        enum_cls, name=name, native_enum=False, length=length, create_constraint=True,
        validate_strings=True,
    )  # fmt: skip
