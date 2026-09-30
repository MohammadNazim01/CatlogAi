import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ProductStatus
from app.db.models import Product, ProductImage


class ImageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, image: ProductImage) -> None:
        self.session.add(image)

    async def delete(self, image: ProductImage) -> None:
        await self.session.delete(image)

    async def list_for_product(self, product_id: uuid.UUID) -> Sequence[ProductImage]:
        # The parent product's ownership must already be checked by the caller; this alone
        # does not scope by seller (there's no seller_id on this table to filter by directly).
        stmt = (
            select(ProductImage)
            .where(ProductImage.product_id == product_id)
            .order_by(ProductImage.sort_order)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count_for_product(self, product_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(ProductImage).where(
            ProductImage.product_id == product_id
        )  # fmt: skip
        return (await self.session.execute(stmt)).scalar_one()

    async def next_sort_order(self, product_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(ProductImage.sort_order) + 1, 0)).where(
            ProductImage.product_id == product_id
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def get_owned(
        self, image_id: uuid.UUID, seller_id: uuid.UUID
    ) -> tuple[ProductImage, ProductStatus] | None:
        """Ownership resolved via image -> product -> seller in one query (docs/02 §3.3); the
        parent's status comes back in the same round trip so the caller can enforce the
        PRODUCT_LOCKED rule without a second query."""
        stmt = (
            select(ProductImage, Product.status)
            .join(Product, Product.id == ProductImage.product_id)
            .where(ProductImage.id == image_id, Product.seller_id == seller_id)
        )
        row = (await self.session.execute(stmt)).first()
        return None if row is None else (row[0], row[1])
