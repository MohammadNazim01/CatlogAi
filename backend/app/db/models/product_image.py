import uuid
from datetime import datetime

from sqlalchemy import CHAR, CheckConstraint, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, uuid_pk

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ProductImage(Base):
    """Metadata only. The bytes live in object storage under `storage_key`."""

    __tablename__ = "product_images"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    storage_key: Mapped[str] = mapped_column(
        String(512), unique=True
    )  # server-generated, never from filename
    original_filename: Mapped[str] = mapped_column(String(255))  # display only
    mime_type: Mapped[str] = mapped_column(
        String(50)
    )  # from server-side sniffing, not the client header
    file_size: Mapped[int]  # bytes of the stored object
    width: Mapped[int]
    height: Mapped[int]
    sha256: Mapped[str] = mapped_column(CHAR(64))
    sort_order: Mapped[int]  # 0 = primary image
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "mime_type IN ('image/jpeg', 'image/png', 'image/webp')", name="mime_type_allowed"
        ),
        CheckConstraint(f"file_size BETWEEN 1 AND {MAX_IMAGE_BYTES}", name="file_size_range"),
        CheckConstraint("width > 0 AND height > 0", name="dimensions_positive"),
        CheckConstraint("sort_order >= 0", name="sort_order_nonneg"),
        UniqueConstraint("product_id", "sha256", name="ux_product_images_product_sha256"),
        # Deferred so a reorder can swap two positions inside one transaction.
        UniqueConstraint(
            "product_id",
            "sort_order",
            name="uq_product_images_product_sort_order",
            deferrable=True,
            initially="DEFERRED",
        ),  # fmt: skip
    )
