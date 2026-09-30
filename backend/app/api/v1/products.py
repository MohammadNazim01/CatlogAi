import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi import status as http_status

from app.api.deps import CurrentUser, DbSession
from app.db.enums import ProductStatus
from app.db.models import Product
from app.repositories.product_repo import SortField
from app.schemas.common import Page
from app.schemas.product import (
    ProductCreate,
    ProductListItem,
    ProductOut,
    ProductSort,
    ProductUpdate,
)
from app.services.product_service import ProductService

router = APIRouter(prefix="/products", tags=["products"])

# ProductSort is the API-facing whitelist (docs/02 §1); this maps each token to the plain
# (column, direction) pair the repository understands.
_SORT_PARSE: dict[ProductSort, tuple[SortField, bool]] = {
    ProductSort.UPDATED_AT: ("updated_at", False),
    ProductSort.UPDATED_AT_DESC: ("updated_at", True),
    ProductSort.CREATED_AT: ("created_at", False),
    ProductSort.CREATED_AT_DESC: ("created_at", True),
    ProductSort.NAME: ("name", False),
    ProductSort.NAME_DESC: ("name", True),
}


def _to_out(product: Product) -> ProductOut:
    return ProductOut(
        id=product.id,
        name=product.name,
        sku=product.sku,
        category=product.category,
        brand=product.brand,
        seller_notes=product.seller_notes,
        seller_attributes=product.seller_attributes,
        status=product.status,
        review_note=product.review_note,
        archived_at=product.archived_at,
        image_count=0,  # image upload is a later milestone; no product can have images yet
        primary_image_url=None,  # presigned URLs arrive with image upload
        current_version=None,  # catalog versions arrive with the AI milestone
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


@router.post("", response_model=ProductOut, status_code=http_status.HTTP_201_CREATED)
async def create_product(body: ProductCreate, user: CurrentUser, session: DbSession) -> ProductOut:
    product = await ProductService(session).create(user.id, body)
    return _to_out(product)


@router.get("", response_model=Page[ProductListItem])
async def list_products(
    user: CurrentUser,
    session: DbSession,
    q: Annotated[str | None, Query(max_length=200)] = None,
    statuses: Annotated[list[ProductStatus] | None, Query(alias="status")] = None,
    category: Annotated[str | None, Query(max_length=255)] = None,
    archived: bool = False,
    sort: ProductSort = ProductSort.UPDATED_AT_DESC,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Page[ProductListItem]:
    sort_field, sort_desc = _SORT_PARSE[sort]
    rows, total = await ProductService(session).list_owned(
        user.id,
        q=q,
        statuses=statuses,
        category=category,
        archived=archived,
        sort_field=sort_field,
        sort_desc=sort_desc,
        page=page,
        page_size=page_size,
    )
    items = [
        ProductListItem(
            id=p.id,
            name=p.name,
            sku=p.sku,
            status=p.status,
            primary_image_url=None,
            updated_at=p.updated_at,
        )
        for p in rows
    ]
    return Page(items=items, total=total, page=page, page_size=page_size)


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: uuid.UUID, user: CurrentUser, session: DbSession) -> ProductOut:
    product = await ProductService(session).get_owned(product_id, user.id)
    return _to_out(product)


@router.patch("/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: uuid.UUID, body: ProductUpdate, user: CurrentUser, session: DbSession
) -> ProductOut:
    product = await ProductService(session).update(product_id, user.id, body)
    return _to_out(product)


@router.delete("/{product_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def archive_product(product_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    await ProductService(session).archive(product_id, user.id)
