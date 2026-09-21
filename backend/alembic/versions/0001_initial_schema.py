"""Initial schema: all tables, indexes, constraints, immutability trigger, marketplace seed.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Database-level immutability for catalog versions (D2/D7). Service code also enforces this,
# but a bug or a manual UPDATE must not be able to rewrite approved or AI-provenance data.
CATALOG_VERSION_GUARD_FUNCTION = """
CREATE FUNCTION catalog_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.approved_at IS NOT NULL THEN
            RAISE EXCEPTION 'catalog_versions: approved version % cannot be deleted', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- Never changeable. (job_id may only be cleared, by ON DELETE SET NULL.)
    IF NEW.product_id IS DISTINCT FROM OLD.product_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.ai_extraction IS DISTINCT FROM OLD.ai_extraction
       OR NEW.ai_output IS DISTINCT FROM OLD.ai_output
       OR NEW.model_name IS DISTINCT FROM OLD.model_name
       OR NEW.prompt_version IS DISTINCT FROM OLD.prompt_version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (NEW.job_id IS DISTINCT FROM OLD.job_id AND NEW.job_id IS NOT NULL) THEN
        RAISE EXCEPTION 'catalog_versions: provenance columns are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- Once approved, the content is frozen. is_current and updated_at may still change.
    IF OLD.approved_at IS NOT NULL AND (
           NEW.approved_at IS DISTINCT FROM OLD.approved_at
        OR NEW.product_type IS DISTINCT FROM OLD.product_type
        OR NEW.category_id IS DISTINCT FROM OLD.category_id
        OR NEW.category_path IS DISTINCT FROM OLD.category_path
        OR NEW.title IS DISTINCT FROM OLD.title
        OR NEW.description IS DISTINCT FROM OLD.description
        OR NEW.bullet_points IS DISTINCT FROM OLD.bullet_points
        OR NEW.keywords IS DISTINCT FROM OLD.keywords
        OR NEW.attributes IS DISTINCT FROM OLD.attributes
        OR NEW.warnings IS DISTINCT FROM OLD.warnings
        OR NEW.confidence_score IS DISTINCT FROM OLD.confidence_score) THEN
        RAISE EXCEPTION 'catalog_versions: approved version % is immutable', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END;
$$
"""

CATALOG_VERSION_GUARD_TRIGGER = """
CREATE TRIGGER catalog_versions_guard
BEFORE UPDATE OR DELETE ON catalog_versions
FOR EACH ROW EXECUTE FUNCTION catalog_versions_guard()
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")  # trigram index on products.name

    op.create_table(
        "marketplaces",
        sa.Column("id", sa.SmallInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_marketplaces")),
        sa.UniqueConstraint("code", name=op.f("uq_marketplaces_code")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "SELLER", "ADMIN", name="role", native_enum=False, create_constraint=True, length=16
            ),
            server_default="SELLER",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("ai_daily_quota", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("ai_daily_quota >= 0", name=op.f("ck_users_ai_daily_quota_nonneg")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "ux_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "exports",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("seller_id", sa.Uuid(), nullable=False),
        sa.Column("marketplace_id", sa.SmallInteger(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column(
            "file_type",
            sa.Enum(
                "CSV", "XLSX", name="file_type", native_enum=False, create_constraint=True, length=8
            ),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("product_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "COMPLETED",
                "FAILED",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'COMPLETED' OR (storage_key IS NOT NULL AND completed_at IS NOT NULL)",
            name=op.f("ck_exports_completed_has_file"),
        ),
        sa.CheckConstraint("product_count >= 0", name=op.f("ck_exports_product_count_nonneg")),
        sa.ForeignKeyConstraint(
            ["marketplace_id"],
            ["marketplaces.id"],
            name=op.f("fk_exports_marketplace_id_marketplaces"),
        ),
        sa.ForeignKeyConstraint(
            ["seller_id"],
            ["users.id"],
            name=op.f("fk_exports_seller_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exports")),
    )
    op.create_index(
        "ix_exports_seller_created",
        "exports",
        ["seller_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("seller_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("brand", sa.String(length=120), nullable=True),
        sa.Column("seller_notes", sa.Text(), nullable=True),
        sa.Column(
            "seller_attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "PROCESSING",
                "REVIEW",
                "APPROVED",
                "REJECTED",
                "EXPORTED",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("approved_version_id", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(seller_attributes) = 'object'",
            name=op.f("ck_products_seller_attributes_is_object"),
        ),
        sa.CheckConstraint(
            "char_length(seller_notes) <= 2000", name=op.f("ck_products_seller_notes_length")
        ),
        sa.ForeignKeyConstraint(
            ["seller_id"],
            ["users.id"],
            name=op.f("fk_products_seller_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
    )
    op.create_index(
        "ix_products_name_trgm",
        "products",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_products_seller_created",
        "products",
        ["seller_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_products_seller_status_updated",
        "products",
        ["seller_id", "status", sa.literal_column("updated_at DESC")],
        unique=False,
    )
    op.create_index(
        "ux_products_seller_sku",
        "products",
        ["seller_id", "sku"],
        unique=True,
        postgresql_where=sa.text("sku IS NOT NULL AND archived_at IS NULL"),
    )
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"], unique=False)
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"], unique=False)
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"], unique=False)
    op.create_table(
        "ai_jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column(
            "job_type",
            sa.Enum(
                "GENERATE_CATALOG",
                "REGENERATE_SECTION",
                name="job_type",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "PROCESSING",
                "COMPLETED",
                "FAILED",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="QUEUED",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model_name", sa.String(length=80), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("result_version_id", sa.Uuid(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name=op.f("ck_ai_jobs_params_is_object")
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts",
            name=op.f("ck_ai_jobs_attempts_within_max"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_ai_jobs_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_ai_jobs_requested_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_jobs")),
    )
    op.create_index(
        "ix_ai_jobs_product_created",
        "ai_jobs",
        ["product_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_ai_jobs_requester_created", "ai_jobs", ["requested_by", "created_at"], unique=False
    )
    op.create_index(
        "ix_ai_jobs_status_heartbeat", "ai_jobs", ["status", "heartbeat_at"], unique=False
    )
    op.create_index(
        "ux_ai_jobs_active_per_product",
        "ai_jobs",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'PROCESSING')"),
    )
    op.create_table(
        "product_images",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=50), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mime_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name=op.f("ck_product_images_mime_type_allowed"),
        ),
        sa.CheckConstraint(
            "file_size BETWEEN 1 AND 10485760", name=op.f("ck_product_images_file_size_range")
        ),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_product_images_sort_order_nonneg")),
        sa.CheckConstraint(
            "width > 0 AND height > 0", name=op.f("ck_product_images_dimensions_positive")
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_images_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_images")),
        sa.UniqueConstraint("product_id", "sha256", name="ux_product_images_product_sha256"),
        sa.UniqueConstraint(
            "product_id",
            "sort_order",
            deferrable=True,
            initially="DEFERRED",
            name="uq_product_images_product_sort_order",
        ),
        sa.UniqueConstraint("storage_key", name=op.f("uq_product_images_storage_key")),
    )
    op.create_table(
        "catalog_versions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "AI_GENERATED",
                "SELLER_EDIT",
                "RESTORED",
                name="source",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("product_type", sa.String(length=200), nullable=True),
        sa.Column("category_id", sa.String(length=64), nullable=True),
        sa.Column("category_path", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "bullet_points",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "keywords",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence_score", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("ai_extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ai_output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_name", sa.String(length=80), nullable=True),
        sa.Column("prompt_version", sa.String(length=16), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'",
            name=op.f("ck_catalog_versions_attributes_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(bullet_points) = 'array'",
            name=op.f("ck_catalog_versions_bullet_points_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(keywords) = 'array'", name=op.f("ck_catalog_versions_keywords_is_array")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(warnings) = 'array'", name=op.f("ck_catalog_versions_warnings_is_array")
        ),
        sa.CheckConstraint(
            "confidence_score BETWEEN 0 AND 1", name=op.f("ck_catalog_versions_confidence_range")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_catalog_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ai_jobs.id"],
            name=op.f("fk_catalog_versions_job_id_ai_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_catalog_versions_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_versions")),
        sa.UniqueConstraint("product_id", "version", name="ux_catalog_versions_product_version"),
    )
    op.create_index(
        "ux_catalog_versions_current",
        "catalog_versions",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "ux_catalog_versions_job",
        "catalog_versions",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("job_id IS NOT NULL"),
    )
    op.create_table(
        "export_items",
        sa.Column("export_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_version_id"],
            ["catalog_versions.id"],
            name=op.f("fk_export_items_catalog_version_id_catalog_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["export_id"],
            ["exports.id"],
            name=op.f("fk_export_items_export_id_exports"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_export_items_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("export_id", "product_id", name=op.f("pk_export_items")),
    )
    op.create_index("ix_export_items_product_id", "export_items", ["product_id"], unique=False)

    # Circular references (products/ai_jobs -> catalog_versions -> products/ai_jobs) are added
    # once every table exists.
    op.create_foreign_key(
        "fk_products_approved_version_id_catalog_versions",
        "products",
        "catalog_versions",
        ["approved_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ai_jobs_result_version_id_catalog_versions",
        "ai_jobs",
        "catalog_versions",
        ["result_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute(CATALOG_VERSION_GUARD_FUNCTION)
    op.execute(CATALOG_VERSION_GUARD_TRIGGER)

    marketplaces = sa.table(
        "marketplaces",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("schema_version", sa.String),
    )
    # "-style" schemas are illustrative rule sets inspired by public seller-guide conventions,
    # not official specifications or integrations.
    op.bulk_insert(
        marketplaces,
        [
            {"code": "GENERIC", "name": "Generic", "schema_version": "1"},
            {
                "code": "AMAZON_STYLE",
                "name": "Amazon-style (illustrative, not an official integration)",
                "schema_version": "1",
            },
            {
                "code": "FLIPKART_STYLE",
                "name": "Flipkart-style (illustrative, not an official integration)",
                "schema_version": "1",
            },
        ],
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS catalog_versions_guard ON catalog_versions")
    op.execute("DROP FUNCTION IF EXISTS catalog_versions_guard()")
    op.drop_constraint(
        "fk_ai_jobs_result_version_id_catalog_versions", "ai_jobs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_products_approved_version_id_catalog_versions", "products", type_="foreignkey"
    )
    op.drop_index("ix_export_items_product_id", table_name="export_items")
    op.drop_table("export_items")
    op.drop_index(
        "ux_catalog_versions_job",
        table_name="catalog_versions",
        postgresql_where=sa.text("job_id IS NOT NULL"),
    )
    op.drop_index(
        "ux_catalog_versions_current",
        table_name="catalog_versions",
        postgresql_where=sa.text("is_current"),
    )
    op.drop_table("catalog_versions")
    op.drop_table("product_images")
    op.drop_index(
        "ux_ai_jobs_active_per_product",
        table_name="ai_jobs",
        postgresql_where=sa.text("status IN ('QUEUED', 'PROCESSING')"),
    )
    op.drop_index("ix_ai_jobs_status_heartbeat", table_name="ai_jobs")
    op.drop_index("ix_ai_jobs_requester_created", table_name="ai_jobs")
    op.drop_index("ix_ai_jobs_product_created", table_name="ai_jobs")
    op.drop_table("ai_jobs")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index(
        "ux_products_seller_sku",
        table_name="products",
        postgresql_where=sa.text("sku IS NOT NULL AND archived_at IS NULL"),
    )
    op.drop_index("ix_products_seller_status_updated", table_name="products")
    op.drop_index("ix_products_seller_created", table_name="products")
    op.drop_index(
        "ix_products_name_trgm",
        table_name="products",
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.drop_table("products")
    op.drop_index("ix_exports_seller_created", table_name="exports")
    op.drop_table("exports")
    op.drop_index("ux_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("marketplaces")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
