"""Product ownership: the one place seller-facing code turns an id into a row."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models import Product
from app.repositories.product_repo import ProductRepository


def _not_found() -> NotFoundError:
    # Same code and message whether the id doesn't exist or belongs to another seller: the
    # response must not tell an attacker which one it was.
    return NotFoundError("PRODUCT_NOT_FOUND", "Product not found.")


class ProductService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.products = ProductRepository(session)

    async def get_owned(self, product_id: uuid.UUID, seller_id: uuid.UUID) -> Product:
        """Raises NotFoundError for a missing id and for another seller's product alike.
        Ownership is enforced by the repository query itself, not by a check here — there is no
        code path in which the row leaves the database before the seller_id filter is applied."""
        product = await self.products.get_owned(product_id, seller_id)
        if product is None:
            raise _not_found()
        return product
