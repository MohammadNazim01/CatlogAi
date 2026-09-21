import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum, uuid_pk
from app.db.enums import ProductStatus

if TYPE_CHECKING:
    from app.db.models.catalog_version import CatalogVersion
    from app.db.models.product_image import ProductImage


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = uuid_pk()
    # RESTRICT: sellers are deactivated, never hard-deleted, so their data cannot vanish.
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    sku: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(255))  # seller's hint, not the AI category
    brand: Mapped[str | None] = mapped_column(String(120))
    seller_notes: Mapped[str | None] = mapped_column(Text)
    # Facts the seller explicitly supplies (dimensions, material, ...). The only allowed source
    # for dimensions: the AI is never permitted to invent them.
    seller_attributes: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[ProductStatus] = mapped_column(
        str_enum(ProductStatus, "status"),
        default=ProductStatus.DRAFT,
        server_default=ProductStatus.DRAFT.value,
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    # Circular FK (versions also point at products), hence use_alter: created after both tables.
    approved_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "catalog_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_products_approved_version_id_catalog_versions",
        )
    )
    archived_at: Mapped[datetime | None]  # soft delete (orthogonal to workflow status)

    images: Mapped[list["ProductImage"]] = relationship(
        lazy="raise", order_by="ProductImage.sort_order", passive_deletes=True
    )
    versions: Mapped[list["CatalogVersion"]] = relationship(
        lazy="raise",
        foreign_keys="CatalogVersion.product_id",
        order_by="CatalogVersion.version.desc()",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("char_length(seller_notes) <= 2000", name="seller_notes_length"),
        CheckConstraint(
            "jsonb_typeof(seller_attributes) = 'object'", name="seller_attributes_is_object"
        ),
        # SKU unique per seller, reusable once the product is archived.
        Index(
            "ux_products_seller_sku",
            "seller_id",
            "sku",
            unique=True,
            postgresql_where=text("sku IS NOT NULL AND archived_at IS NULL"),
        ),  # fmt: skip
        Index("ix_products_seller_status_updated", "seller_id", "status", text("updated_at DESC")),
        Index("ix_products_seller_created", "seller_id", text("created_at DESC")),
        # A btree cannot serve ILIKE '%term%'; trigram GIN can. Requires the pg_trgm extension.
        Index(
            "ix_products_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),  # fmt: skip
    )
