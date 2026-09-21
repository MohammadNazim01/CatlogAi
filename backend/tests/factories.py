"""Minimal valid rows for tests. Each helper flushes so constraint errors surface immediately."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ExportFileType, ExportStatus, JobType, VersionSource
from app.db.models import (
    AIJob,
    CatalogVersion,
    Export,
    Marketplace,
    Product,
    ProductImage,
    User,
)


async def _save[T](session: AsyncSession, obj: T) -> T:
    session.add(obj)
    await session.flush()
    return obj


async def make_user(session: AsyncSession, **kw: Any) -> User:
    kw.setdefault("name", "Test Seller")
    kw.setdefault("email", f"user-{uuid.uuid4().hex[:10]}@example.com")
    kw.setdefault("password_hash", "not-a-real-hash")
    return await _save(session, User(**kw))


async def make_product(session: AsyncSession, seller: User, **kw: Any) -> Product:
    kw.setdefault("name", "Test Product")
    return await _save(session, Product(seller_id=seller.id, **kw))


async def make_image(session: AsyncSession, product: Product, **kw: Any) -> ProductImage:
    n = uuid.uuid4().hex
    kw.setdefault("storage_key", f"products/{product.seller_id}/{product.id}/{n}.jpg")
    kw.setdefault("original_filename", "photo.jpg")
    kw.setdefault("mime_type", "image/jpeg")
    kw.setdefault("file_size", 1234)
    kw.setdefault("width", 800)
    kw.setdefault("height", 600)
    kw.setdefault("sha256", (n + n)[:64])
    kw.setdefault("sort_order", 0)
    return await _save(session, ProductImage(product_id=product.id, **kw))


async def make_job(session: AsyncSession, product: Product, user: User, **kw: Any) -> AIJob:
    kw.setdefault("job_type", JobType.GENERATE_CATALOG)
    return await _save(session, AIJob(product_id=product.id, requested_by=user.id, **kw))


async def make_version(session: AsyncSession, product: Product, **kw: Any) -> CatalogVersion:
    kw.setdefault("version", 1)
    kw.setdefault("source", VersionSource.AI_GENERATED)
    kw.setdefault("title", "Wireless Over-Ear Headphones")
    return await _save(session, CatalogVersion(product_id=product.id, **kw))


async def get_marketplace(session: AsyncSession, code: str = "GENERIC") -> Marketplace:
    return (await session.execute(select(Marketplace).where(Marketplace.code == code))).scalar_one()


async def make_export(session: AsyncSession, seller: User, **kw: Any) -> Export:
    mp = await get_marketplace(session)
    kw.setdefault("file_type", ExportFileType.CSV)
    kw.setdefault("product_count", 1)
    kw.setdefault("schema_version", mp.schema_version)
    if kw.get("status") == ExportStatus.COMPLETED:
        kw.setdefault("storage_key", f"exports/{seller.id}/{uuid.uuid4().hex}.csv")
        kw.setdefault("completed_at", datetime.now(UTC))
    return await _save(session, Export(seller_id=seller.id, marketplace_id=mp.id, **kw))
