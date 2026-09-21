import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, str_enum, uuid_pk
from app.db.enums import VersionSource


class CatalogVersion(Base, TimestampMixin):
    """One row per catalog version of a product (D2).

    Immutability is enforced by a database trigger (see the initial migration), not only by
    service code: ai_extraction / ai_output and identity columns can never change, and once
    `approved_at` is set the content columns can never change either. Editing approved content
    creates a new version (copy-on-write) in the service layer.
    """

    __tablename__ = "catalog_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    version: Mapped[int]
    source: Mapped[VersionSource] = mapped_column(str_enum(VersionSource, "source"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_jobs.id", ondelete="SET NULL"))

    # --- Working copy (editable until approved) ---
    product_type: Mapped[str | None] = mapped_column(String(200))
    category_id: Mapped[str | None] = mapped_column(String(64))  # id from taxonomy.json
    category_path: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    bullet_points: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))
    keywords: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))
    # name -> {"value": str|null, "source": IMAGE|SELLER|UNKNOWN, "confidence": HIGH|MEDIUM|LOW}
    attributes: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    warnings: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))
    confidence_score: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 2)
    )  # heuristic, not a probability

    # --- Immutable provenance (D7) ---
    ai_extraction: Mapped[dict[str, Any] | None]  # stage 1 output: facts + evidence
    ai_output: Mapped[dict[str, Any] | None]  # stage 2 validated output
    model_name: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str | None] = mapped_column(String(16))

    is_current: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    approved_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("jsonb_typeof(bullet_points) = 'array'", name="bullet_points_is_array"),
        CheckConstraint("jsonb_typeof(keywords) = 'array'", name="keywords_is_array"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name="attributes_is_object"),
        CheckConstraint("jsonb_typeof(warnings) = 'array'", name="warnings_is_array"),
        CheckConstraint("confidence_score BETWEEN 0 AND 1", name="confidence_range"),
        UniqueConstraint("product_id", "version", name="ux_catalog_versions_product_version"),
        # Exactly <= 1 current version per product, database-enforced.
        Index(
            "ux_catalog_versions_current",
            "product_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),  # fmt: skip
        # A retried job can never produce a second version.
        Index(
            "ux_catalog_versions_job",
            "job_id",
            unique=True,
            postgresql_where=text("job_id IS NOT NULL"),
        ),  # fmt: skip
    )
