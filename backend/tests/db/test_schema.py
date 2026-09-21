"""Verifies the migrated schema directly against pg_catalog. This complements `alembic check`,
which cannot see CHECK constraints or partial-index predicates. Expected names come from the
Phase 1 spec, not from the models, so a drift in either direction is caught."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CONSTRAINT_ERRORS

# index name -> substrings that must appear in its definition
INDEXES = {
    "ux_users_email_lower": ["UNIQUE", "lower((email)::text)"],
    "ux_products_seller_sku": [
        "UNIQUE",
        "(seller_id, sku)",
        "sku IS NOT NULL",
        "archived_at IS NULL",
    ],
    "ix_products_seller_status_updated": ["(seller_id, status, updated_at DESC)"],
    "ix_products_seller_created": ["(seller_id, created_at DESC)"],
    "ix_products_name_trgm": ["USING gin", "gin_trgm_ops"],
    "ux_ai_jobs_active_per_product": ["UNIQUE", "(product_id)", "QUEUED", "PROCESSING"],
    "ix_ai_jobs_product_created": ["(product_id, created_at DESC)"],
    "ix_ai_jobs_status_heartbeat": ["(status, heartbeat_at)"],
    "ix_ai_jobs_requester_created": ["(requested_by, created_at)"],
    "ux_catalog_versions_current": ["UNIQUE", "(product_id)", "WHERE is_current"],
    "ux_catalog_versions_job": ["UNIQUE", "(job_id)", "job_id IS NOT NULL"],
    "ix_refresh_tokens_user_id": ["(user_id)"],
    "ix_refresh_tokens_family_id": ["(family_id)"],
    "ix_refresh_tokens_expires_at": ["(expires_at)"],
    "ix_exports_seller_created": ["(seller_id, created_at DESC)"],
    "ix_export_items_product_id": ["(product_id)"],
}

CHECKS = {
    "ck_users_role",
    "ck_users_ai_daily_quota_nonneg",
    "ck_products_status",
    "ck_products_seller_notes_length",
    "ck_products_seller_attributes_is_object",
    "ck_product_images_mime_type_allowed",
    "ck_product_images_file_size_range",
    "ck_product_images_dimensions_positive",
    "ck_product_images_sort_order_nonneg",
    "ck_ai_jobs_job_type",
    "ck_ai_jobs_status",
    "ck_ai_jobs_attempts_within_max",
    "ck_ai_jobs_params_is_object",
    "ck_catalog_versions_source",
    "ck_catalog_versions_version_positive",
    "ck_catalog_versions_bullet_points_is_array",
    "ck_catalog_versions_keywords_is_array",
    "ck_catalog_versions_attributes_is_object",
    "ck_catalog_versions_warnings_is_array",
    "ck_catalog_versions_confidence_range",
    "ck_exports_file_type",
    "ck_exports_status",
    "ck_exports_product_count_nonneg",
    "ck_exports_completed_has_file",
}

# FK constraint -> pg confdeltype (a = no action, r = restrict, c = cascade, n = set null)
FOREIGN_KEYS = {
    "fk_products_seller_id_users": "r",
    "fk_products_approved_version_id_catalog_versions": "n",
    "fk_product_images_product_id_products": "c",
    "fk_ai_jobs_product_id_products": "c",
    "fk_ai_jobs_requested_by_users": "r",
    "fk_ai_jobs_result_version_id_catalog_versions": "n",
    "fk_catalog_versions_product_id_products": "c",
    "fk_catalog_versions_job_id_ai_jobs": "n",
    "fk_refresh_tokens_user_id_users": "c",
    "fk_refresh_tokens_replaced_by_id_refresh_tokens": "n",
    "fk_exports_seller_id_users": "r",
    "fk_exports_marketplace_id_marketplaces": "a",
    "fk_export_items_export_id_exports": "c",
    "fk_export_items_product_id_products": "r",
    "fk_export_items_catalog_version_id_catalog_versions": "r",
}

UNIQUES = {
    "uq_marketplaces_code",
    "uq_refresh_tokens_token_hash",
    "uq_product_images_storage_key",
    "ux_product_images_product_sha256",
    "uq_product_images_product_sort_order",
    "ux_catalog_versions_product_version",
}


@pytest.mark.parametrize("name", sorted(INDEXES))
async def test_index_exists_with_expected_definition(session: AsyncSession, name: str) -> None:
    row = (
        await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=:n"),
            {"n": name},
        )
    ).scalar_one_or_none()
    assert row is not None, f"index {name} is missing"
    for fragment in INDEXES[name]:
        assert fragment in row, f"{name}: expected {fragment!r} in {row!r}"


async def test_all_check_constraints_exist(session: AsyncSession) -> None:
    found = set(
        (
            await session.execute(text("SELECT conname FROM pg_constraint WHERE contype='c'"))
        ).scalars()
    )
    assert CHECKS <= found, f"missing: {sorted(CHECKS - found)}"


async def test_foreign_keys_exist_with_expected_delete_rules(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text("SELECT conname, confdeltype::text FROM pg_constraint WHERE contype='f'")
        )
    ).all()
    found = {name: rule for name, rule in rows}
    assert set(FOREIGN_KEYS) <= set(found), f"missing: {sorted(set(FOREIGN_KEYS) - set(found))}"
    for name, rule in FOREIGN_KEYS.items():
        assert found[name] == rule, f"{name}: on-delete rule {found[name]!r}, expected {rule!r}"


async def test_unique_constraints_exist(session: AsyncSession) -> None:
    found = set(
        (
            await session.execute(text("SELECT conname FROM pg_constraint WHERE contype='u'"))
        ).scalars()
    )
    assert UNIQUES <= found, f"missing: {sorted(UNIQUES - found)}"


async def test_sort_order_uniqueness_is_deferrable(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text(
                "SELECT condeferrable, condeferred FROM pg_constraint "
                "WHERE conname='uq_product_images_product_sort_order'"
            )
        )
    ).one()
    assert tuple(row) == (True, True)


async def test_every_mapped_error_constraint_exists_in_the_database(session: AsyncSession) -> None:
    """core/exceptions.CONSTRAINT_ERRORS maps constraint names to API error codes. A renamed or
    missing constraint would silently turn a clean 409 into a 500, so pin them here."""
    names = set(
        (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint UNION SELECT indexname FROM pg_indexes "
                    "WHERE schemaname='public'"
                )
            )
        ).scalars()
    )
    assert set(CONSTRAINT_ERRORS) <= names, f"unknown: {sorted(set(CONSTRAINT_ERRORS) - names)}"


async def test_enum_columns_are_plain_varchar_not_native_enums(session: AsyncSession) -> None:
    n = (await session.execute(text("SELECT count(*) FROM pg_type WHERE typtype='e'"))).scalar_one()
    assert n == 0


async def test_all_timestamps_are_timezone_aware(session: AsyncSession) -> None:
    bad = (
        (
            await session.execute(
                text(
                    "SELECT table_name||'.'||column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND data_type='timestamp without time zone'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert bad == []


async def test_bulk_of_data_is_never_stored_as_binary(session: AsyncSession) -> None:
    """Images live in object storage; the DB stores metadata only (spec section 3)."""
    bad = (
        (
            await session.execute(
                text(
                    "SELECT table_name||'.'||column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND data_type='bytea'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert bad == []
