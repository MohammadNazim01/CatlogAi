"""Importing this package registers every model on Base.metadata (Alembic relies on it)."""

from app.db.models.ai_job import AIJob
from app.db.models.catalog_version import CatalogVersion
from app.db.models.export import Export, ExportItem
from app.db.models.marketplace import Marketplace
from app.db.models.product import Product
from app.db.models.product_image import ProductImage
from app.db.models.refresh_token import RefreshToken
from app.db.models.user import User

__all__ = [
    "AIJob", "CatalogVersion", "Export", "ExportItem", "Marketplace",
    "Product", "ProductImage", "RefreshToken", "User",
]  # fmt: skip
