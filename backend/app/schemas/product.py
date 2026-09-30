import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from app.db.enums import ProductStatus

MAX_SELLER_ATTRIBUTES = 30

ProductName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Category = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Brand = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
SellerNotes = Annotated[str, StringConstraints(max_length=2000)]
AttributeKey = Annotated[str, StringConstraints(min_length=1, max_length=64)]
AttributeValue = Annotated[str, StringConstraints(max_length=200)]
SellerAttributes = dict[AttributeKey, AttributeValue]


class ProductSort(StrEnum):
    """Whitelisted sort fields (docs/02 §1: "Sorting ... whitelisted fields only")."""

    UPDATED_AT = "updated_at"
    UPDATED_AT_DESC = "-updated_at"
    CREATED_AT = "created_at"
    CREATED_AT_DESC = "-created_at"
    NAME = "name"
    NAME_DESC = "-name"


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProductName
    sku: Sku | None = None
    category: Category | None = None
    brand: Brand | None = None
    seller_notes: SellerNotes | None = None
    seller_attributes: SellerAttributes = {}

    @field_validator("seller_attributes")
    @classmethod
    def _max_attributes(cls, v: SellerAttributes) -> SellerAttributes:
        if len(v) > MAX_SELLER_ATTRIBUTES:
            raise ValueError(f"at most {MAX_SELLER_ATTRIBUTES} attributes are allowed")
        return v


class ProductUpdate(BaseModel):
    """PATCH: a field left out of the request body is left untouched (the service applies this
    with `model_dump(exclude_unset=True)`). `name` and `seller_attributes` are NOT NULL columns,
    so — unlike `sku`/`category`/`brand`/`seller_notes`, which a client may legitimately clear —
    sending them as an explicit JSON `null` is invalid rather than a no-op."""

    model_config = ConfigDict(extra="forbid")

    name: ProductName | None = None
    sku: Sku | None = None
    category: Category | None = None
    brand: Brand | None = None
    seller_notes: SellerNotes | None = None
    seller_attributes: SellerAttributes | None = None
    archived: bool | None = None  # true -> archive now; false -> restore; None -> don't touch

    @field_validator("name")
    @classmethod
    def _name_not_null(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("name cannot be null")
        return v

    @field_validator("seller_attributes")
    @classmethod
    def _attributes_not_null(cls, v: SellerAttributes | None) -> SellerAttributes | None:
        if v is None:
            raise ValueError("seller_attributes cannot be null")
        if len(v) > MAX_SELLER_ATTRIBUTES:
            raise ValueError(f"at most {MAX_SELLER_ATTRIBUTES} attributes are allowed")
        return v


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sku: str | None
    category: str | None
    brand: str | None
    seller_notes: str | None
    seller_attributes: dict[str, str]
    status: ProductStatus
    review_note: str | None
    archived_at: datetime | None
    image_count: int
    primary_image_url: str | None
    current_version: int | None
    created_at: datetime
    updated_at: datetime


class ProductListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sku: str | None
    status: ProductStatus
    primary_image_url: str | None
    updated_at: datetime
