import uuid
from datetime import datetime

from pydantic import BaseModel


class ImageOut(BaseModel):
    id: uuid.UUID
    original_filename: str
    mime_type: str
    file_size: int
    width: int
    height: int
    sort_order: int
    created_at: datetime
    url: str  # presigned GET, computed fresh for every response
    url_expires_at: datetime
