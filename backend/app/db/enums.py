"""Workflow enums. Persisted as VARCHAR + CHECK (not native PG enums): adding a value to a
native enum needs awkward ALTER TYPE migrations."""

from enum import StrEnum


class Role(StrEnum):
    SELLER = "SELLER"
    ADMIN = "ADMIN"


class ProductStatus(StrEnum):
    DRAFT = "DRAFT"
    PROCESSING = "PROCESSING"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPORTED = "EXPORTED"


class JobType(StrEnum):
    GENERATE_CATALOG = "GENERATE_CATALOG"
    REGENERATE_SECTION = "REGENERATE_SECTION"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class VersionSource(StrEnum):
    AI_GENERATED = "AI_GENERATED"
    SELLER_EDIT = "SELLER_EDIT"
    RESTORED = "RESTORED"


class ExportFileType(StrEnum):
    CSV = "CSV"
    XLSX = "XLSX"


class ExportStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
