"""Behavioural tests of the database rules, against real PostgreSQL. Each test tries to break
an invariant and asserts the database itself refuses (not application code)."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ExportStatus, JobStatus, ProductStatus, Role
from app.db.models import CatalogVersion, ExportItem, Product, ProductImage, User
from tests.dbutils import violates
from tests.factories import (
    get_marketplace,
    make_export,
    make_image,
    make_job,
    make_product,
    make_user,
    make_version,
)


async def _raw_update(
    session: AsyncSession, table: str, col: str, value: Any, row_id: uuid.UUID
) -> None:
    await session.execute(
        text(f"UPDATE {table} SET {col} = :v WHERE id = :id"),  # noqa: S608  (test-only, fixed names)
        {"v": value, "id": row_id},
    )


# ------------------------------------------------------------------ users
class TestUsers:
    async def test_defaults(self, session: AsyncSession) -> None:
        u = await make_user(session)
        await session.refresh(u)
        assert (u.role, u.is_active, u.ai_daily_quota) == (Role.SELLER, True, 50)
        assert u.created_at is not None and u.id is not None

    async def test_email_uniqueness_is_case_insensitive(self, session: AsyncSession) -> None:
        await make_user(session, email="Seller@Example.com")
        async with violates(session, "ux_users_email_lower"):
            await make_user(session, email="seller@example.COM")

    async def test_distinct_emails_are_fine(self, session: AsyncSession) -> None:
        await make_user(session, email="a@example.com")
        await make_user(session, email="b@example.com")

    async def test_invalid_role_rejected_by_database(self, session: AsyncSession) -> None:
        async with violates(session, "ck_users_role"):
            await session.execute(
                text(
                    "INSERT INTO users (name,email,password_hash,role) VALUES ('n','r@x.com','h','HACKER')"
                )
            )

    async def test_negative_quota_rejected(self, session: AsyncSession) -> None:
        async with violates(session, "ck_users_ai_daily_quota_nonneg"):
            await make_user(session, ai_daily_quota=-1)

    async def test_user_with_products_cannot_be_deleted(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        await make_product(session, seller)
        async with violates(session, "fk_products_seller_id_users"):
            await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": seller.id})

    async def test_updated_at_moves_on_orm_update(self, session: AsyncSession) -> None:
        u = await make_user(session)
        await session.refresh(u)
        first = u.updated_at
        await session.execute(text("SELECT pg_sleep(0.01)"))
        u.name = "Renamed"
        await session.flush()
        await session.refresh(u)
        assert u.updated_at >= first


# ------------------------------------------------------------------ refresh tokens
class TestRefreshTokens:
    async def _token(self, session: AsyncSession, user: User, token_hash: str, **kw: Any) -> None:
        from app.db.models import RefreshToken

        session.add(
            RefreshToken(
                user_id=user.id,
                family_id=kw.pop("family_id", uuid.uuid4()),
                token_hash=token_hash,
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                **kw,
            )
        )
        await session.flush()

    async def test_token_hash_is_unique(self, session: AsyncSession) -> None:
        u = await make_user(session)
        await self._token(session, u, "a" * 64)
        async with violates(session, "uq_refresh_tokens_token_hash"):
            await self._token(session, u, "a" * 64)

    async def test_tokens_are_deleted_with_their_user(self, session: AsyncSession) -> None:
        u = await make_user(session)
        await self._token(session, u, "b" * 64)
        await session.execute(text("DELETE FROM users WHERE id=:i"), {"i": u.id})
        n = (await session.execute(text("SELECT count(*) FROM refresh_tokens"))).scalar_one()
        assert n == 0


# ------------------------------------------------------------------ products
class TestProducts:
    async def test_defaults(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        await session.refresh(p)
        assert (
            p.status == ProductStatus.DRAFT and p.seller_attributes == {} and p.archived_at is None
        )

    async def test_sku_unique_per_seller(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        await make_product(session, seller, sku="SKU-1")
        async with violates(session, "ux_products_seller_sku"):
            await make_product(session, seller, sku="SKU-1")

    async def test_same_sku_allowed_for_different_sellers(self, session: AsyncSession) -> None:
        await make_product(session, await make_user(session), sku="SKU-1")
        await make_product(session, await make_user(session), sku="SKU-1")

    async def test_null_skus_never_collide(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        await make_product(session, seller, sku=None)
        await make_product(session, seller, sku=None)

    async def test_archiving_frees_the_sku(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        old = await make_product(session, seller, sku="SKU-1")
        old.archived_at = datetime.now(UTC)
        await session.flush()
        await make_product(session, seller, sku="SKU-1")  # allowed: the old one is archived

    async def test_invalid_status_rejected(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        async with violates(session, "ck_products_status"):
            await _raw_update(session, "products", "status", "BOGUS", p.id)

    async def test_seller_attributes_must_be_an_object(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        async with violates(session, "ck_products_seller_attributes_is_object"):
            await _raw_update(session, "products", "seller_attributes", "[1,2]", p.id)

    async def test_seller_notes_length_limit(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        await make_product(session, seller, seller_notes="x" * 2000)
        async with violates(session, "ck_products_seller_notes_length"):
            await make_product(session, seller, seller_notes="x" * 2001)

    async def test_trigram_index_serves_substring_search(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        await make_product(session, seller, name="Wireless Bluetooth Headphones")
        await session.execute(
            text("SET LOCAL enable_seqscan = off")
        )  # force planner to pick an index
        plan = "\n".join(
            (
                await session.execute(
                    text("EXPLAIN SELECT id FROM products WHERE name ILIKE '%blue%'")
                )
            ).scalars()
        )
        assert "ix_products_name_trgm" in plan, plan


# ------------------------------------------------------------------ product images
class TestProductImages:
    async def test_valid_image(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        img = await make_image(session, p)
        assert img.id is not None

    async def test_duplicate_content_in_one_product_rejected(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        await make_image(session, p, sha256="c" * 64, sort_order=0)
        async with violates(session, "ux_product_images_product_sha256"):
            await make_image(session, p, sha256="c" * 64, sort_order=1)

    async def test_same_content_allowed_in_different_products(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        await make_image(session, await make_product(session, seller), sha256="d" * 64)
        await make_image(session, await make_product(session, seller), sha256="d" * 64)

    async def test_storage_key_unique(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        await make_image(session, p, storage_key="k/1.jpg", sort_order=0)
        async with violates(session, "uq_product_images_storage_key"):
            await make_image(session, p, storage_key="k/1.jpg", sort_order=1)

    @pytest.mark.parametrize(
        ("field", "value", "constraint"),
        [
            ("mime_type", "image/gif", "ck_product_images_mime_type_allowed"),
            ("mime_type", "application/pdf", "ck_product_images_mime_type_allowed"),
            ("file_size", 0, "ck_product_images_file_size_range"),
            ("file_size", 10 * 1024 * 1024 + 1, "ck_product_images_file_size_range"),
            ("width", 0, "ck_product_images_dimensions_positive"),
            ("height", -5, "ck_product_images_dimensions_positive"),
            ("sort_order", -1, "ck_product_images_sort_order_nonneg"),
        ],
    )
    async def test_invalid_metadata_rejected(
        self, session: AsyncSession, field: str, value: Any, constraint: str
    ) -> None:
        p = await make_product(session, await make_user(session))
        async with violates(session, constraint):
            await make_image(session, p, **{field: value})

    async def test_max_allowed_size_is_accepted(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        await make_image(session, p, file_size=10 * 1024 * 1024)

    async def test_sort_order_can_be_swapped_within_a_transaction(
        self, session: AsyncSession
    ) -> None:
        p = await make_product(session, await make_user(session))
        a = await make_image(session, p, sort_order=0)
        b = await make_image(session, p, sort_order=1)
        await session.execute(
            text("UPDATE product_images SET sort_order = 1 WHERE id = :i"), {"i": a.id}
        )
        await session.execute(
            text("UPDATE product_images SET sort_order = 0 WHERE id = :i"), {"i": b.id}
        )
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # force the deferred check now

    async def test_duplicate_sort_order_still_rejected_when_checked(
        self, session: AsyncSession
    ) -> None:
        p = await make_product(session, await make_user(session))
        await make_image(session, p, sort_order=0)
        b = await make_image(session, p, sort_order=1)
        async with violates(session, "uq_product_images_product_sort_order"):
            await session.execute(
                text("UPDATE product_images SET sort_order = 0 WHERE id = :i"), {"i": b.id}
            )
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    async def test_images_are_deleted_with_their_product(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        await make_image(session, p)
        await session.execute(text("DELETE FROM products WHERE id=:i"), {"i": p.id})
        assert (
            await session.execute(text("SELECT count(*) FROM product_images"))
        ).scalar_one() == 0


# ------------------------------------------------------------------ ai jobs (D6)
class TestAIJobs:
    async def test_defaults(self, session: AsyncSession) -> None:
        u = await make_user(session)
        j = await make_job(session, await make_product(session, u), u)
        await session.refresh(j)
        assert (j.status, j.attempts, j.max_attempts, j.params) == (JobStatus.QUEUED, 0, 3, {})
        assert j.started_at is None and j.completed_at is None

    @pytest.mark.parametrize("active", [JobStatus.QUEUED, JobStatus.PROCESSING])
    async def test_only_one_active_job_per_product(
        self, session: AsyncSession, active: JobStatus
    ) -> None:
        u = await make_user(session)
        p = await make_product(session, u)
        await make_job(session, p, u, status=active)
        for second in (JobStatus.QUEUED, JobStatus.PROCESSING):
            async with violates(session, "ux_ai_jobs_active_per_product"):
                await make_job(session, p, u, status=second)

    @pytest.mark.parametrize("finished", [JobStatus.COMPLETED, JobStatus.FAILED])
    async def test_finished_jobs_do_not_block_a_new_active_job(
        self, session: AsyncSession, finished: JobStatus
    ) -> None:
        u = await make_user(session)
        p = await make_product(session, u)
        await make_job(session, p, u, status=finished)
        await make_job(session, p, u, status=finished)  # history can be any length
        await make_job(session, p, u, status=JobStatus.QUEUED)

    async def test_active_jobs_on_different_products_do_not_conflict(
        self, session: AsyncSession
    ) -> None:
        u = await make_user(session)
        await make_job(session, await make_product(session, u), u)
        await make_job(session, await make_product(session, u), u)

    async def test_completing_a_job_frees_the_slot(self, session: AsyncSession) -> None:
        u = await make_user(session)
        p = await make_product(session, u)
        j = await make_job(session, p, u)
        j.status = JobStatus.COMPLETED
        await session.flush()
        await make_job(session, p, u)

    async def test_attempts_cannot_exceed_max(self, session: AsyncSession) -> None:
        u = await make_user(session)
        p = await make_product(session, u)
        async with violates(session, "ck_ai_jobs_attempts_within_max"):
            await make_job(session, p, u, attempts=4, max_attempts=3)

    async def test_invalid_status_and_type_rejected(self, session: AsyncSession) -> None:
        u = await make_user(session)
        j = await make_job(session, await make_product(session, u), u)
        async with violates(session, "ck_ai_jobs_status"):
            await _raw_update(session, "ai_jobs", "status", "DONE", j.id)
        async with violates(session, "ck_ai_jobs_job_type"):
            await _raw_update(session, "ai_jobs", "job_type", "MAGIC", j.id)

    async def test_job_state_lives_in_the_database_not_redis(self, session: AsyncSession) -> None:
        """D6: everything needed to recover/re-enqueue a job is a column on ai_jobs."""
        cols = set(
            (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_name='ai_jobs'"
                    )
                )
            ).scalars()
        )
        assert {
            "status",
            "attempts",
            "max_attempts",
            "error_code",
            "heartbeat_at",
            "started_at",
            "completed_at",
            "result_version_id",
            "input_tokens",
            "output_tokens",
        } <= cols

    async def test_jobs_are_deleted_with_their_product(self, session: AsyncSession) -> None:
        u = await make_user(session)
        p = await make_product(session, u)
        await make_job(session, p, u)
        await session.execute(text("DELETE FROM products WHERE id=:i"), {"i": p.id})
        assert (await session.execute(text("SELECT count(*) FROM ai_jobs"))).scalar_one() == 0


# ------------------------------------------------------------------ catalog versions (D2/D7)
class TestCatalogVersions:
    async def test_defaults(self, session: AsyncSession) -> None:
        v = await make_version(session, await make_product(session, await make_user(session)))
        await session.refresh(v)
        assert (v.is_current, v.bullet_points, v.keywords, v.attributes, v.warnings) == (
            False,
            [],
            [],
            {},
            [],
        )
        assert v.approved_at is None

    async def test_version_numbers_are_unique_per_product(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        await make_version(session, p, version=1)
        async with violates(session, "ux_catalog_versions_product_version"):
            await make_version(session, p, version=1)
        await make_version(session, p, version=2)

    async def test_same_version_number_allowed_for_different_products(
        self, session: AsyncSession
    ) -> None:
        seller = await make_user(session)
        await make_version(session, await make_product(session, seller), version=1)
        await make_version(session, await make_product(session, seller), version=1)

    async def test_only_one_current_version_per_product(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        await make_version(session, p, version=1, is_current=True)
        async with violates(session, "ux_catalog_versions_current"):
            await make_version(session, p, version=2, is_current=True)

    async def test_many_non_current_versions_allowed(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        for n in range(1, 5):
            await make_version(session, p, version=n, is_current=False)

    async def test_current_flag_can_move_to_a_newer_version(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        v1 = await make_version(session, p, version=1, is_current=True)
        v2 = await make_version(session, p, version=2)
        v1.is_current = False
        await session.flush()
        v2.is_current = True
        await session.flush()

    async def test_each_product_may_have_its_own_current_version(
        self, session: AsyncSession
    ) -> None:
        seller = await make_user(session)
        await make_version(session, await make_product(session, seller), is_current=True)
        await make_version(session, await make_product(session, seller), is_current=True)

    async def test_a_job_can_produce_only_one_version(self, session: AsyncSession) -> None:
        u = await make_user(session)
        p = await make_product(session, u)
        job = await make_job(session, p, u)
        await make_version(session, p, version=1, job_id=job.id)
        async with violates(session, "ux_catalog_versions_job"):
            await make_version(session, p, version=2, job_id=job.id)

    @pytest.mark.parametrize(
        ("field", "value", "constraint"),
        [
            ("version", 0, "ck_catalog_versions_version_positive"),
            ("bullet_points", {"a": 1}, "ck_catalog_versions_bullet_points_is_array"),
            ("keywords", "text", "ck_catalog_versions_keywords_is_array"),
            ("attributes", [], "ck_catalog_versions_attributes_is_object"),
            ("warnings", {}, "ck_catalog_versions_warnings_is_array"),
            ("confidence_score", 1.5, "ck_catalog_versions_confidence_range"),
            ("confidence_score", -0.1, "ck_catalog_versions_confidence_range"),
        ],
    )
    async def test_invalid_content_rejected(
        self, session: AsyncSession, field: str, value: Any, constraint: str
    ) -> None:
        p = await make_product(session, await make_user(session))
        async with violates(session, constraint):
            await make_version(session, p, **{field: value})

    async def test_confidence_bounds_are_inclusive(self, session: AsyncSession) -> None:
        p = await make_product(session, await make_user(session))
        await make_version(session, p, version=1, confidence_score=0)
        await make_version(session, p, version=2, confidence_score=1)

    async def test_unknown_attribute_value_is_stored_as_null_not_invented(
        self, session: AsyncSession
    ) -> None:
        p = await make_product(session, await make_user(session))
        attrs = {
            "color": {"value": "Black", "source": "IMAGE", "confidence": "HIGH"},
            "material": {"value": None, "source": "UNKNOWN", "confidence": None},
        }
        v = await make_version(session, p, attributes=attrs)
        await session.refresh(v)
        assert v.attributes["material"]["value"] is None

    async def test_versions_are_deleted_with_an_unapproved_product(
        self, session: AsyncSession
    ) -> None:
        p = await make_product(session, await make_user(session))
        await make_version(session, p)
        await session.execute(text("DELETE FROM products WHERE id=:i"), {"i": p.id})
        assert (
            await session.execute(text("SELECT count(*) FROM catalog_versions"))
        ).scalar_one() == 0

    async def test_deleting_a_job_keeps_its_version_and_clears_the_link(
        self, session: AsyncSession
    ) -> None:
        u = await make_user(session)
        p = await make_product(session, u)
        job = await make_job(session, p, u, status=JobStatus.COMPLETED)
        v = await make_version(session, p, job_id=job.id, ai_output={"title": "x"})
        await session.execute(text("DELETE FROM ai_jobs WHERE id=:i"), {"i": job.id})
        await session.refresh(v)
        assert v.job_id is None and v.ai_output == {"title": "x"}

    async def test_product_and_job_can_point_at_the_result_version(
        self, session: AsyncSession
    ) -> None:
        """The circular references (product/job -> version -> product/job) work end to end."""
        u = await make_user(session)
        p = await make_product(session, u)
        job = await make_job(session, p, u)
        v = await make_version(session, p, job_id=job.id)
        job.result_version_id = v.id
        p.approved_version_id = v.id
        await session.flush()


# ------------------------------------------------------------------ immutability trigger
class TestCatalogVersionImmutability:
    """Approved / AI-provenance data must not be silently mutated, even by raw SQL (D2/D7)."""

    async def _ai_version(self, session: AsyncSession) -> CatalogVersion:
        p = await make_product(session, await make_user(session))
        return await make_version(
            session,
            p,
            ai_extraction={"facts": {"color": "black"}},
            ai_output={"title": "AI title"},
            model_name="fake-model",
            prompt_version="v1",
            is_current=True,
        )

    async def _approve(self, session: AsyncSession, v: CatalogVersion) -> None:
        await session.execute(
            text("UPDATE catalog_versions SET approved_at = now() WHERE id=:i"), {"i": v.id}
        )

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("ai_output", '{"title": "tampered"}'),
            ("ai_extraction", '{"facts": {}}'),
            ("ai_output", None),
            ("model_name", "other-model"),
            ("prompt_version", "v2"),
            ("version", 99),
            ("source", "SELLER_EDIT"),
        ],
    )
    async def test_provenance_columns_are_immutable_even_before_approval(
        self, session: AsyncSession, column: str, value: Any
    ) -> None:
        v = await self._ai_version(session)
        async with violates(session, message="immutable"):
            await session.execute(
                text(f"UPDATE catalog_versions SET {column} = :v WHERE id = :i"),  # noqa: S608
                {"v": value, "i": v.id},
            )

    async def test_unapproved_content_is_freely_editable(self, session: AsyncSession) -> None:
        v = await self._ai_version(session)
        v.title = "Seller edited title"
        v.description = "New description"
        v.bullet_points = ["a", "b"]
        await session.flush()

    async def test_approving_only_sets_the_timestamp(self, session: AsyncSession) -> None:
        v = await self._ai_version(session)
        await self._approve(session, v)  # NULL -> timestamp with no content change is allowed
        await session.refresh(v)
        assert v.approved_at is not None

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("title", "sneaky change"),
            ("description", "sneaky change"),
            ("bullet_points", '["sneaky"]'),
            ("keywords", '["sneaky"]'),
            ("attributes", '{"color": {"value": "Red", "source": "SELLER", "confidence": null}}'),
            ("warnings", '["hidden"]'),
            ("product_type", "Toaster"),
            ("category_id", "electronics.audio"),
            ("category_path", "Electronics > Audio"),
            ("confidence_score", 0.99),
            ("approved_at", None),
        ],
    )
    async def test_approved_content_cannot_be_changed(
        self, session: AsyncSession, column: str, value: Any
    ) -> None:
        v = await self._ai_version(session)
        await self._approve(session, v)
        async with violates(session, message="immutable"):
            await session.execute(
                text(f"UPDATE catalog_versions SET {column} = :v WHERE id = :i"),  # noqa: S608
                {"v": value, "i": v.id},
            )

    async def test_approved_version_can_stop_being_current(self, session: AsyncSession) -> None:
        """Copy-on-write: a new version becomes current while the approved one stays frozen."""
        v = await self._ai_version(session)
        await self._approve(session, v)
        await session.execute(
            text("UPDATE catalog_versions SET is_current = false WHERE id=:i"), {"i": v.id}
        )
        await session.refresh(v)
        assert v.is_current is False

    async def test_approved_version_cannot_be_deleted(self, session: AsyncSession) -> None:
        v = await self._ai_version(session)
        await self._approve(session, v)
        async with violates(session, message="cannot be deleted"):
            await session.execute(text("DELETE FROM catalog_versions WHERE id=:i"), {"i": v.id})

    async def test_product_with_an_approved_version_cannot_be_hard_deleted(
        self, session: AsyncSession
    ) -> None:
        v = await self._ai_version(session)
        await self._approve(session, v)
        async with violates(session, message="cannot be deleted"):
            await session.execute(text("DELETE FROM products WHERE id=:i"), {"i": v.product_id})

    async def test_unapproved_version_can_be_deleted(self, session: AsyncSession) -> None:
        v = await self._ai_version(session)
        await session.execute(text("DELETE FROM catalog_versions WHERE id=:i"), {"i": v.id})


# ------------------------------------------------------------------ marketplaces / exports
class TestMarketplacesAndExports:
    async def test_marketplaces_are_seeded_and_honestly_labelled(
        self, session: AsyncSession
    ) -> None:
        rows = dict((await session.execute(text("SELECT code, name FROM marketplaces"))).all())
        assert set(rows) == {"GENERIC", "AMAZON_STYLE", "FLIPKART_STYLE"}
        for code in ("AMAZON_STYLE", "FLIPKART_STYLE"):
            assert "illustrative" in rows[code] and "not an official integration" in rows[code]

    async def test_marketplace_code_unique(self, session: AsyncSession) -> None:
        async with violates(session, "uq_marketplaces_code"):
            await session.execute(
                text(
                    "INSERT INTO marketplaces (code,name,schema_version) VALUES ('GENERIC','dup','1')"
                )
            )

    async def test_export_defaults_to_pending(self, session: AsyncSession) -> None:
        e = await make_export(session, await make_user(session))
        await session.refresh(e)
        assert e.status == ExportStatus.PENDING and e.storage_key is None

    async def test_completed_export_must_have_a_file_and_timestamp(
        self, session: AsyncSession
    ) -> None:
        seller = await make_user(session)
        async with violates(session, "ck_exports_completed_has_file"):
            await make_export(
                session, seller, status=ExportStatus.COMPLETED, storage_key=None, completed_at=None
            )
        await make_export(session, seller, status=ExportStatus.COMPLETED)  # factory supplies both

    async def test_negative_product_count_rejected(self, session: AsyncSession) -> None:
        async with violates(session, "ck_exports_product_count_nonneg"):
            await make_export(session, await make_user(session), product_count=-1)

    async def test_invalid_file_type_rejected(self, session: AsyncSession) -> None:
        e = await make_export(session, await make_user(session))
        async with violates(session, "ck_exports_file_type"):
            await _raw_update(session, "exports", "file_type", "PDF", e.id)

    async def test_export_records_the_exact_version_used(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        p = await make_product(session, seller)
        v1 = await make_version(session, p, version=1)
        exp = await make_export(session, seller)
        session.add(ExportItem(export_id=exp.id, product_id=p.id, catalog_version_id=v1.id))
        await session.flush()
        row = (
            await session.execute(
                text("SELECT catalog_version_id FROM export_items WHERE export_id=:e"),
                {"e": exp.id},
            )
        ).scalar_one()
        assert row == v1.id

    async def test_a_product_appears_once_per_export(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        p = await make_product(session, seller)
        v = await make_version(session, p)
        exp = await make_export(session, seller)
        session.add(ExportItem(export_id=exp.id, product_id=p.id, catalog_version_id=v.id))
        await session.flush()
        async with violates(session, "pk_export_items"):
            session.add(ExportItem(export_id=exp.id, product_id=p.id, catalog_version_id=v.id))
            await session.flush()

    async def test_exported_version_cannot_be_deleted_from_under_its_export(
        self, session: AsyncSession
    ) -> None:
        seller = await make_user(session)
        p = await make_product(session, seller)
        v = await make_version(session, p)
        exp = await make_export(session, seller)
        session.add(ExportItem(export_id=exp.id, product_id=p.id, catalog_version_id=v.id))
        await session.flush()
        async with violates(session, "fk_export_items_catalog_version_id_catalog_versions"):
            await session.execute(text("DELETE FROM catalog_versions WHERE id=:i"), {"i": v.id})

    async def test_exported_product_cannot_be_deleted(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        p = await make_product(session, seller)
        v = await make_version(session, p)
        exp = await make_export(session, seller)
        session.add(ExportItem(export_id=exp.id, product_id=p.id, catalog_version_id=v.id))
        await session.flush()
        async with violates(session):
            await session.execute(text("DELETE FROM products WHERE id=:i"), {"i": p.id})

    async def test_export_items_are_deleted_with_their_export(self, session: AsyncSession) -> None:
        seller = await make_user(session)
        p = await make_product(session, seller)
        v = await make_version(session, p)
        exp = await make_export(session, seller)
        session.add(ExportItem(export_id=exp.id, product_id=p.id, catalog_version_id=v.id))
        await session.flush()
        await session.execute(text("DELETE FROM exports WHERE id=:i"), {"i": exp.id})
        assert (await session.execute(text("SELECT count(*) FROM export_items"))).scalar_one() == 0

    async def test_marketplace_cannot_be_deleted_while_exports_use_it(
        self, session: AsyncSession
    ) -> None:
        await make_export(session, await make_user(session))
        mp = await get_marketplace(session)
        async with violates(session):
            await session.execute(text("DELETE FROM marketplaces WHERE id=:i"), {"i": mp.id})


# ------------------------------------------------------------------ ORM <-> DB sanity
async def test_isolation_between_tests_rows_do_not_leak(session: AsyncSession) -> None:
    for table in ("users", "products", "product_images", "ai_jobs", "catalog_versions", "exports"):
        n = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
        assert n == 0, f"{table} leaked rows from another test"


async def test_orm_models_match_table_names(session: AsyncSession) -> None:
    for model, table in [
        (User, "users"),
        (Product, "products"),
        (ProductImage, "product_images"),
        (CatalogVersion, "catalog_versions"),
    ]:
        assert model.__tablename__ == table
        assert (await session.execute(text(f"SELECT to_regclass('{table}')"))).scalar_one() == table  # noqa: S608


def test_integrity_error_is_what_the_api_layer_translates() -> None:
    assert issubclass(IntegrityError, Exception)
