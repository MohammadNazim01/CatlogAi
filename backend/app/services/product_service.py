"""Product CRUD and the one state-machine rule this milestone owns: a product with a running
AI job (status PROCESSING) has its facts locked (docs/01 §3.10, docs/02 §3.2)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.db.enums import ProductStatus
from app.db.models import Product
from app.repositories.product_repo import ProductRepository, SortField
from app.schemas.product import ProductCreate, ProductUpdate


def _not_found() -> NotFoundError:
    # Same code and message whether the id doesn't exist or belongs to another seller: the
    # response must not tell an attacker which one it was.
    return NotFoundError("PRODUCT_NOT_FOUND", "Product not found.")


def _locked() -> ConflictError:
    return ConflictError(
        "PRODUCT_LOCKED", "This product has an AI job in progress and cannot be edited."
    )


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
        return await self.products.list_owned(
            seller_id,
            q=q,
            statuses=statuses,
            category=category,
            archived=archived,
            sort_field=sort_field,
            sort_desc=sort_desc,
            page=page,
            page_size=page_size,
        )

    async def create(self, seller_id: uuid.UUID, data: ProductCreate) -> Product:
        product = Product(
            seller_id=seller_id,
            name=data.name,
            sku=data.sku,
            category=data.category,
            brand=data.brand,
            seller_notes=data.seller_notes,
            seller_attributes=data.seller_attributes,
            # status, archived_at, review_note, approved_version_id: column defaults only.
        )
        self.products.add(product)
        # A duplicate (seller_id, sku) violates ux_products_seller_sku; the global handler turns
        # that into 409 SKU_TAKEN. Relying on the constraint (not a pre-check) is race-free.
        await self.session.commit()
        return product

    async def update(
        self, product_id: uuid.UUID, seller_id: uuid.UUID, data: ProductUpdate
    ) -> Product:
        product = await self.get_owned(product_id, seller_id)
        if product.status == ProductStatus.PROCESSING:
            raise _locked()

        # exclude_unset: a field the client left out of the PATCH body is not touched at all,
        # not overwritten with a default. `archived` is handled separately below, since it maps
        # to a derived column (archived_at), not a same-named attribute.
        changes = data.model_dump(exclude_unset=True, exclude={"archived"})
        for field, value in changes.items():
            setattr(product, field, value)

        if data.archived is True:
            product.archived_at = product.archived_at or datetime.now(UTC)
        elif data.archived is False:
            product.archived_at = None

        # A duplicate (seller_id, sku) violates ux_products_seller_sku -> 409 SKU_TAKEN.
        await self.session.commit()
        # `updated_at` is server-computed (TimestampMixin's onupdate=func.now()): the commit
        # expires it, and a later plain attribute read would try to lazy-load it outside of an
        # awaited context and blow up with MissingGreenlet. Refresh it explicitly instead.
        await self.session.refresh(product)
        return product

    async def archive(self, product_id: uuid.UUID, seller_id: uuid.UUID) -> None:
        """Soft archive (D11): sets archived_at, never deletes the row. Allowed except while
        PROCESSING. Idempotent — archiving an already-archived product is a no-op, not an
        error, matching DELETE's idempotency contract."""
        product = await self.get_owned(product_id, seller_id)
        if product.status == ProductStatus.PROCESSING:
            raise _locked()
        if product.archived_at is None:
            product.archived_at = datetime.now(UTC)
            await self.session.commit()
            await self.session.refresh(product)
