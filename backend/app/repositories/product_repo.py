import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ProductStatus
from app.db.models import Product

SortField = Literal["name", "created_at", "updated_at"]

_SORT_COLUMNS = {
    "name": Product.name,
    "created_at": Product.created_at,
    "updated_at": Product.updated_at,
}

# Escaped so a seller's search text can never smuggle in its own SQL wildcards.
_LIKE_ESCAPES = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


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

    async def list_owned(
        self,
        seller_id: uuid.UUID,
        *,
        q: str | None,
        statuses: Sequence[ProductStatus] | None,
        category: str | None,
        archived: bool,
        sort_field: SortField,
        sort_desc: bool,
        page: int,
        page_size: int,
    ) -> tuple[Sequence[Product], int]:
        conditions = [
            Product.seller_id == seller_id,
            Product.archived_at.is_not(None) if archived else Product.archived_at.is_(None),
        ]
        if statuses:
            conditions.append(Product.status.in_(statuses))
        if category:
            conditions.append(Product.category == category)
        if q:
            pattern = f"%{q.translate(_LIKE_ESCAPES)}%"
            conditions.append(
                or_(
                    Product.name.ilike(pattern, escape="\\"),
                    Product.sku.ilike(pattern, escape="\\"),
                )
            )

        total = (
            await self.session.execute(select(func.count()).select_from(Product).where(*conditions))
        ).scalar_one()

        column = _SORT_COLUMNS[sort_field]
        order = column.desc() if sort_desc else column.asc()
        stmt = (
            select(Product)
            .where(*conditions)
            .order_by(order, Product.id)  # tiebreaker: stable pagination even with equal values
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return rows, total


class AdminProductRepository:
    """Unscoped reads for `/admin/*`-style routes only. A separate class on purpose: nothing in
    seller-facing code can reach an unscoped lookup by importing the wrong repository."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, product_id: uuid.UUID) -> Product | None:
        return await self.session.get(Product, product_id)
