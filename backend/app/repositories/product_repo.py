import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Product


class ProductRepository:
    """Tenant-scoped reads/writes. `get_owned` is the only way to fetch a single product by id;
    there is deliberately no bare `get(id)` here, so a caller can't forget the seller_id filter
    (see docs/03 §10)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, product: Product) -> None:
        self.session.add(product)

    async def get_owned(self, product_id: uuid.UUID, seller_id: uuid.UUID) -> Product | None:
        """A product that doesn't exist and one that belongs to another seller are the same
        result: None. The caller must not turn that into two different responses."""
        stmt = select(Product).where(Product.id == product_id, Product.seller_id == seller_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class AdminProductRepository:
    """Unscoped reads for `/admin/*`-style routes only. A separate class on purpose: nothing in
    seller-facing code can reach an unscoped lookup by importing the wrong repository."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, product_id: uuid.UUID) -> Product | None:
        return await self.session.get(Product, product_id)
