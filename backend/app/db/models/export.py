import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum, uuid_pk
from app.db.enums import ExportFileType, ExportStatus


class Export(Base):
    __tablename__ = "exports"

    id: Mapped[uuid.UUID] = uuid_pk()
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    marketplace_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("marketplaces.id"))
    schema_version: Mapped[str] = mapped_column(String(16))  # rule-set version used for this file
    file_type: Mapped[ExportFileType] = mapped_column(str_enum(ExportFileType, "file_type", 8))
    storage_key: Mapped[str | None] = mapped_column(String(512))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    product_count: Mapped[int]
    status: Mapped[ExportStatus] = mapped_column(
        str_enum(ExportStatus, "status"),
        default=ExportStatus.PENDING,
        server_default=ExportStatus.PENDING.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("product_count >= 0", name="product_count_nonneg"),
        # A COMPLETED export must point at a real file.
        CheckConstraint(
            "status <> 'COMPLETED' OR (storage_key IS NOT NULL AND completed_at IS NOT NULL)",
            name="completed_has_file",
        ),
        Index("ix_exports_seller_created", "seller_id", text("created_at DESC")),
    )


class ExportItem(Base):
    """Snapshot: exactly which catalog version of which product went into which export."""

    __tablename__ = "export_items"

    export_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exports.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    # RESTRICT: a version that was exported can never be deleted out from under its export.
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_versions.id", ondelete="RESTRICT")
    )

    __table_args__ = (Index("ix_export_items_product_id", "product_id"),)
