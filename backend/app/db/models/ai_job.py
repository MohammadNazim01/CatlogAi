import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum, uuid_pk
from app.db.enums import JobStatus, JobType


class AIJob(Base):
    """Durable source of truth for AI work (D6). Redis only delivers job ids to workers; if
    Redis is lost, every job's state and history is still here and can be re-enqueued."""

    __tablename__ = "ai_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()  # also the queue job id, which makes enqueueing idempotent
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    job_type: Mapped[JobType] = mapped_column(str_enum(JobType, "job_type", 24))
    params: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "status"),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(default=3, server_default=text("3"))
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_message: Mapped[str | None] = mapped_column(Text)  # sanitized, user-safe
    provider: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    result_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "catalog_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_ai_jobs_result_version_id_catalog_versions",
        )
    )
    heartbeat_at: Mapped[datetime | None]  # lets the sweeper detect a dead worker
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())  # = queued at
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="attempts_within_max"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="params_is_object"),
        # At most one active job per product, enforced by the database (no racy check-then-insert).
        Index(
            "ux_ai_jobs_active_per_product",
            "product_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'PROCESSING')"),
        ),  # fmt: skip
        Index("ix_ai_jobs_product_created", "product_id", text("created_at DESC")),
        Index("ix_ai_jobs_status_heartbeat", "status", "heartbeat_at"),  # sweeper
        Index("ix_ai_jobs_requester_created", "requested_by", "created_at"),  # daily quota count
    )
