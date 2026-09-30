import uuid
from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from fastapi import status as http_status

from app.api.deps import CurrentUser, DbSession, SettingsDep, StorageDep
from app.db.models import ProductImage
from app.schemas.image import ImageOut
from app.services.image_service import ImageService

# No shared prefix: creation/listing are nested under the product, deletion is top-level and
# resolves ownership via image -> product -> seller (docs/02 §3.3).
router = APIRouter(tags=["images"])


async def _to_out(service: ImageService, image: ProductImage) -> ImageOut:
    url, expires_at = await service.presigned_url(image)
    return ImageOut(
        id=image.id,
        original_filename=image.original_filename,
        mime_type=image.mime_type,
        file_size=image.file_size,
        width=image.width,
        height=image.height,
        sort_order=image.sort_order,
        created_at=image.created_at,
        url=url,
        url_expires_at=expires_at,
    )


@router.post(
    "/products/{product_id}/images",
    response_model=list[ImageOut],
    status_code=http_status.HTTP_201_CREATED,
)
async def upload_images(
    product_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    storage: StorageDep,
    files: Annotated[list[UploadFile], File()],
) -> list[ImageOut]:
    service = ImageService(session, storage, settings)
    images = await service.upload(product_id, user.id, files)
    return [await _to_out(service, image) for image in images]


@router.get("/products/{product_id}/images", response_model=list[ImageOut])
async def list_images(
    product_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    storage: StorageDep,
) -> list[ImageOut]:
    service = ImageService(session, storage, settings)
    images = await service.list_images(product_id, user.id)
    return [await _to_out(service, image) for image in images]


@router.delete("/images/{image_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_image(
    image_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    storage: StorageDep,
) -> None:
    await ImageService(session, storage, settings).delete(image_id, user.id)
