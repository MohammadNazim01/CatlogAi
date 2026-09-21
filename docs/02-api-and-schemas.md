# CatalogAI — Phase 1 Spec, Part 2: Pydantic Schemas & API Contract

Covers spec items 8–9.

## 1. API conventions

- Base path `/api/v1`. JSON only (except multipart image upload). Timestamps ISO-8601 UTC. IDs are UUIDs.
- Auth: `Authorization: Bearer <access_token>`. The refresh token travels **only** in an `HttpOnly` cookie scoped to `/api/v1/auth`.
- Lists: `?page=1&page_size=20` (max 100) → `{"items":[…],"total":N,"page":1,"page_size":20}`. Offset pagination is fine at this scale; cursor pagination would be premature.
- Sorting: `?sort=-updated_at` (whitelisted fields only).
- **Unknown request fields are rejected** (`extra="forbid"`), which prevents mass assignment (e.g. someone sending `"role":"ADMIN"` to register).
- **Error envelope** for every non-2xx:
```json
{ "error": { "code": "PRODUCT_NOT_FOUND", "message": "Product not found.",
             "details": [ {"field": "title", "issue": "too_long"} ], "request_id": "01J…" } }
```
- Ownership failures return **404**, not 403.
- Un-versioned ops endpoints: `GET /health/live`, `GET /health/ready`.
- Idempotency: state-changing endpoints are naturally idempotent where it matters (generate returns the active job if one exists; approve on an approved product returns the same result).

## 2. Pydantic schemas (v2)

```python
# schemas/common.py
class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

class Page(BaseModel, Generic[T]):
    items: list[T]; total: int; page: int; page_size: int

class ErrorBody(BaseModel):
    code: str; message: str; details: list[dict] | None = None; request_id: str
```

```python
# schemas/auth.py
Password = Annotated[str, StringConstraints(min_length=10, max_length=128)]  # length over composition rules (NIST 800-63B)

class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    email: EmailStr
    password: Password                       # note: no `role` field. Role is never client-settable.

class LoginRequest(BaseModel):  email: EmailStr; password: str
class TokenResponse(BaseModel): access_token: str; token_type: Literal["bearer"] = "bearer"; expires_in: int
class UserOut(ORMModel):        id: UUID; name: str; email: EmailStr; role: Role; ai_daily_quota: int; created_at: datetime
class UpdateProfileRequest(BaseModel): name: str | None = None
class ChangePasswordRequest(BaseModel): current_password: str; new_password: Password
```

```python
# schemas/product.py
class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: constr(strip_whitespace=True, min_length=1, max_length=200)
    sku: constr(max_length=64) | None = None
    category: constr(max_length=255) | None = None
    brand: constr(max_length=120) | None = None
    seller_notes: constr(max_length=2000) | None = None
    seller_attributes: dict[constr(max_length=64), constr(max_length=200)] = {}   # e.g. {"dimensions": "20x10x5 cm"}; ≤ 30 keys (validator)

class ProductUpdate(BaseModel):            # PATCH: every field optional; only provided fields change
    model_config = ConfigDict(extra="forbid")
    name: … | None = None;  sku: … | None = None; category: … | None = None
    brand: … | None = None; seller_notes: … | None = None; seller_attributes: … | None = None
    archived: bool | None = None           # true → set archived_at; false → restore

class ProductOut(ORMModel):
    id: UUID; name: str; sku: str | None; category: str | None; brand: str | None
    seller_notes: str | None; seller_attributes: dict[str, str]
    status: ProductStatus; review_note: str | None; archived_at: datetime | None
    image_count: int; primary_image_url: str | None      # presigned, short-lived
    current_version: int | None
    created_at: datetime; updated_at: datetime

class ProductListItem(ORMModel):   # lighter row for tables
    id: UUID; name: str; sku: str | None; status: ProductStatus
    primary_image_url: str | None; updated_at: datetime
```

```python
# schemas/image.py
class ImageOut(ORMModel):
    id: UUID; original_filename: str; mime_type: str; file_size: int
    width: int; height: int; sort_order: int; created_at: datetime
    url: str; url_expires_at: datetime           # presigned GET, computed per response
class ImageReorderRequest(BaseModel): image_ids: list[UUID]   # full ordered list; must equal the product's set
```

```python
# schemas/catalog.py — typed JSON shapes (used to validate AI output AND seller edits)
class AttrSource(StrEnum):  IMAGE = "IMAGE"; SELLER = "SELLER"; UNKNOWN = "UNKNOWN"
class AttrConfidence(StrEnum): HIGH="HIGH"; MEDIUM="MEDIUM"; LOW="LOW"

class AttributeValue(BaseModel):
    value: str | None                      # None ⇔ source == UNKNOWN
    source: AttrSource
    confidence: AttrConfidence | None = None
    @model_validator(mode="after")
    def unknown_means_null(self):
        if (self.source == AttrSource.UNKNOWN) != (self.value is None):
            raise ValueError("value must be null exactly when source is UNKNOWN")
        return self

class CatalogContent(BaseModel):           # the editable part
    model_config = ConfigDict(extra="forbid")
    product_type: constr(max_length=200) | None
    category_id: str | None                # validated against taxonomy in the service
    title: constr(strip_whitespace=True, min_length=1, max_length=500)
    description: constr(max_length=5000)
    bullet_points: conlist(constr(min_length=1, max_length=500), max_length=10)
    keywords: conlist(constr(min_length=1, max_length=80), max_length=30)
    attributes: dict[constr(max_length=64), AttributeValue]

class CatalogVersionOut(ORMModel):
    id: UUID; version: int; source: VersionSource; is_current: bool
    product_type: str | None; category_id: str | None; category_path: str | None
    title: str | None; description: str | None
    bullet_points: list[str]; keywords: list[str]; attributes: dict[str, AttributeValue]
    warnings: list[Warning]; confidence_score: float | None
    model_name: str | None; prompt_version: str | None
    approved_at: datetime | None; created_at: datetime; updated_at: datetime

class CatalogPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version: int                      # optimistic concurrency: 409 if it is not the current version
    title: … | None = None; description: … | None = None; bullet_points: … | None = None
    keywords: … | None = None; attributes: … | None = None; category_id: … | None = None
    product_type: … | None = None

class RejectRequest(BaseModel): note: constr(min_length=1, max_length=1000)
```

```python
# schemas/ai.py
class AIJobOut(ORMModel):
    job_id: UUID = Field(validation_alias="id")
    status: JobStatus; job_type: JobType
    attempts: int; max_attempts: int
    error_code: str | None; error_message: str | None
    created_at: datetime; started_at: datetime | None; completed_at: datetime | None
    result_version: int | None             # populated once COMPLETED

class RegenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: Literal["title","description","bullet_points","keywords","attributes","all"] = "all"
    instructions: constr(max_length=500) | None = None      # "make it shorter", treated as untrusted data in the prompt
```

```python
# schemas/marketplace.py
class Issue(BaseModel):
    code: str                              # e.g. TITLE_TOO_LONG, MISSING_REQUIRED_ATTRIBUTE
    severity: Literal["ERROR","WARNING"]
    field: str; message: str
class ValidationResult(BaseModel):
    marketplace: str; schema_version: str; valid: bool   # valid ⇔ no ERROR issues
    issues: list[Issue]
class MarketplaceOut(ORMModel): code: str; name: str; schema_version: str; is_active: bool
class FieldSpecOut(BaseModel):  name: str; required: bool; type: str; max_length: int | None; allowed_values: list[str] | None
```

```python
# schemas/export.py
class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    marketplace: str                       # code
    file_type: Literal["CSV","XLSX"]
    product_ids: conlist(UUID, min_length=1, max_length=500)
    skip_invalid: bool = False             # false → any invalid product rejects the whole request (422)
class ExportOut(ORMModel):
    id: UUID; marketplace: str; schema_version: str; file_type: str
    product_count: int; file_size: int | None; status: ExportStatus
    error_message: str | None; created_at: datetime; completed_at: datetime | None
class ExportDownload(BaseModel): url: str; expires_in: int
class ExportPreflightResult(BaseModel): results: dict[UUID, ValidationResult]; exportable_ids: list[UUID]
```

```python
# schemas/dashboard.py
class DashboardSummary(BaseModel):
    total_products: int; processing_products: int; review_products: int
    approved_products: int; failed_ai_jobs_7d: int; exports_total: int
    ai_quota_used_today: int; ai_quota_limit: int
```

## 3. Endpoint contracts

Legend: 🔓 public · 👤 any authenticated user · 🛡 ADMIN only. Every 👤 endpoint additionally scopes data to the caller.
Common errors on all 👤 endpoints: `401 UNAUTHENTICATED`, `422 VALIDATION_ERROR`, `429 RATE_LIMITED`.

### 3.1 Auth

| Endpoint | Auth | Request | Success | Errors / notes |
|---|---|---|---|---|
| `POST /auth/register` | 🔓 | `RegisterRequest` | `201` `UserOut` | `409 EMAIL_TAKEN`. Rate limit 5/hr/IP. Always creates `SELLER`. |
| `POST /auth/login` | 🔓 | `LoginRequest` | `200` `TokenResponse` + `Set-Cookie: refresh_token` (HttpOnly, Secure, SameSite=Strict, Path=/api/v1/auth) | `401 INVALID_CREDENTIALS` (identical message for unknown email and wrong password), `403 ACCOUNT_DISABLED`. Limit 5/min per IP+email. |
| `POST /auth/refresh` | cookie | none (cookie) | `200` `TokenResponse` + rotated cookie | `401 INVALID_REFRESH_TOKEN`; reuse of a rotated token ⇒ family revoked + `401 REFRESH_TOKEN_REUSED`. Requires `X-Requested-With: catalogai` header (CSRF defense in depth). |
| `POST /auth/logout` | cookie | none | `204`; clears cookie; revokes the token family | Idempotent. |
| `GET /auth/me` | 👤 | none | `200` `UserOut` | |
| `PATCH /users/me` *(added for /settings)* | 👤 | `UpdateProfileRequest` | `200` `UserOut` | |
| `POST /users/me/password` *(added)* | 👤 | `ChangePasswordRequest` | `204`; revokes all other refresh-token families | `400 WRONG_PASSWORD` |

### 3.2 Products

| Endpoint | Request | Success | Errors / notes |
|---|---|---|---|
| `POST /products` | `ProductCreate` | `201` `ProductOut` (status DRAFT) | `409 SKU_TAKEN` |
| `GET /products` | query: `q` (name/SKU trigram search), `status` (repeatable), `category`, `archived` (default false), `sort`, `page`, `page_size` | `200` `Page[ProductListItem]` | Always `WHERE seller_id = :me`. |
| `GET /products/{id}` | | `200` `ProductOut` | `404 PRODUCT_NOT_FOUND` |
| `PATCH /products/{id}` | `ProductUpdate` | `200` `ProductOut` | `409 PRODUCT_LOCKED` if status is PROCESSING (facts must not change under a running job); `409 SKU_TAKEN`. |
| `DELETE /products/{id}` | | `204` | Soft archive (D11). Allowed except while PROCESSING (`409`). Idempotent. Undo through `PATCH archived:false`. |

### 3.3 Images

| Endpoint | Request | Success | Errors / notes |
|---|---|---|---|
| `POST /products/{id}/images` | `multipart/form-data`, field `files` (1–8 files) | `201` `list[ImageOut]` | `413 FILE_TOO_LARGE` (>10 MB), `415 UNSUPPORTED_MEDIA_TYPE` (sniffed, not header-trusted), `422 IMAGE_INVALID` (corrupt/pixel-bomb/too big dimensions), `409 IMAGE_LIMIT_REACHED`, `409 DUPLICATE_IMAGE`, `409 PRODUCT_LOCKED`. All-or-nothing per request. |
| `GET /products/{id}/images` | | `200` `list[ImageOut]` (fresh presigned URLs) | |
| `PUT /products/{id}/images/order` *(added)* | `ImageReorderRequest` | `200` `list[ImageOut]` | `422` if ids don't match the product's images |
| `DELETE /images/{id}` | | `204` | Ownership resolved via image→product→seller in one query. `404` otherwise. `409 PRODUCT_LOCKED` while PROCESSING. |

### 3.4 AI

| Endpoint | Request | Success | Errors / notes |
|---|---|---|---|
| `POST /products/{id}/ai/generate` | none | **`202`** `AIJobOut` (`status: QUEUED`) | `409 NO_IMAGES`, `409 CATALOG_EXISTS` (use regenerate), `429 AI_QUOTA_EXCEEDED`, `503 QUEUE_UNAVAILABLE`. If an active job already exists returns `200` with that job (idempotent). Product → PROCESSING in the same transaction as job creation. |
| `POST /products/{id}/ai/regenerate` | `RegenerateRequest` | `202` `AIJobOut` | as above, plus `409 NO_CATALOG` when a section is requested and no version exists. |
| `GET /products/{id}/ai/status` | | `200` `AIJobOut` (latest job) | `404 NO_JOBS` |
| `GET /products/{id}/ai/result` | | `200` `CatalogVersionOut` (version produced by the latest COMPLETED job) | `404 NO_RESULT`. For a running or failed job, use `/status`. |
| `GET /products/{id}/ai/jobs` *(added)* | `page` | `200` `Page[AIJobOut]` | Job history and failure diagnostics. |

### 3.5 Catalog

| Endpoint | Request | Success | Errors / notes |
|---|---|---|---|
| `GET /products/{id}/catalog` | | `200` `CatalogVersionOut` (current) | `404 NO_CATALOG` |
| `GET /products/{id}/catalog/versions` *(added)* | | `200` `list[CatalogVersionOut]` (desc) | History. |
| `PATCH /products/{id}/catalog` | `CatalogPatch` | `200` `CatalogVersionOut` | `409 VERSION_CONFLICT` (stale `base_version`), `409 PRODUCT_LOCKED` (PROCESSING), `422 UNKNOWN_CATEGORY`. Copy-on-write when the current version is approved. |
| `POST /products/{id}/catalog/restore` *(added)* | `{"version": 2}` | `200` `CatalogVersionOut` (new version, source RESTORED) | `404`. |
| `POST /products/{id}/catalog/approve` | none | `200` `ProductOut` (APPROVED) | `409 INVALID_STATE_TRANSITION`; `422 CATALOG_INVALID` with `Issue[]` from GENERIC schema. Sets `approved_at`, `products.approved_version_id`. |
| `POST /products/{id}/catalog/reject` | `RejectRequest` | `200` `ProductOut` (REJECTED) | `409 INVALID_STATE_TRANSITION` |

### 3.6 Marketplace

| Endpoint | Request | Success | Errors |
|---|---|---|---|
| `GET /marketplaces` | | `200` `list[MarketplaceOut]` (active) | |
| `GET /marketplaces/{code}/schema` *(added)* | | `200` `list[FieldSpecOut]` | UI can show required fields up front. `404 MARKETPLACE_NOT_FOUND` |
| `POST /products/{id}/validate/{marketplace}` | none | `200` `ValidationResult` (always 200: invalid is a *result*, not an error) | `404`, `409 NO_CATALOG`. Validates the approved version if one exists, else the current one. |

### 3.7 Exports

| Endpoint | Request | Success | Errors / notes |
|---|---|---|---|
| `POST /exports/preflight` *(added)* | `{marketplace, product_ids}` | `200` `ExportPreflightResult` | Lets the UI show per-product problems before committing. |
| `POST /exports` | `ExportCreate` | `201` `ExportOut` | `404` if any product isn't the caller's, with the same code as a missing one; `409 PRODUCT_NOT_APPROVED` (status must be APPROVED/EXPORTED); `422 EXPORT_VALIDATION_FAILED` with per-product issues when `skip_invalid=false`; `422 NOTHING_TO_EXPORT`; `422 TOO_MANY_PRODUCTS` (>500). |
| `GET /exports` | `page` | `200` `Page[ExportOut]` | |
| `GET /exports/{id}` | | `200` `ExportOut` | |
| `GET /exports/{id}/download` *(added)* | | `200` `ExportDownload` (presigned URL, 5 min, `Content-Disposition: attachment`) | `409 EXPORT_NOT_READY`. |

### 3.8 Dashboard

| Endpoint | Success |
|---|---|
| `GET /dashboard/summary` | `200` `DashboardSummary`, a single SQL statement with `COUNT(*) FILTER (WHERE …)` |
| `GET /dashboard/products?limit=10` | `200` `list[ProductListItem]`, recent by `updated_at` |
| `GET /dashboard/exports?limit=10` | `200` `list[ExportOut]` |

### 3.9 Admin (small, and it justifies the ADMIN role; Phase 10 optional)

| Endpoint | Purpose |
|---|---|
| `GET /admin/users` | Paginated user list |
| `PATCH /admin/users/{id}` | `is_active`, `ai_daily_quota`. Deactivation revokes refresh tokens. |
| `GET /admin/ai-jobs?status=FAILED` | Cross-tenant operational view (the *only* place tenant scoping is deliberately bypassed; tested separately). |

### 3.10 Error code catalogue (stable contract; the frontend switches on `code`, never on `message`)

`UNAUTHENTICATED 401` · `INVALID_CREDENTIALS 401` · `INVALID_REFRESH_TOKEN 401` · `REFRESH_TOKEN_REUSED 401` · `FORBIDDEN 403` · `ACCOUNT_DISABLED 403` · `*_NOT_FOUND 404` · `EMAIL_TAKEN/SKU_TAKEN/DUPLICATE_IMAGE/INVALID_STATE_TRANSITION/PRODUCT_LOCKED/VERSION_CONFLICT/NO_IMAGES/CATALOG_EXISTS/IMAGE_LIMIT_REACHED 409` · `FILE_TOO_LARGE 413` · `UNSUPPORTED_MEDIA_TYPE 415` · `VALIDATION_ERROR/IMAGE_INVALID/CATALOG_INVALID/EXPORT_VALIDATION_FAILED 422` · `RATE_LIMITED/AI_QUOTA_EXCEEDED 429` · `INTERNAL_ERROR 500` · `QUEUE_UNAVAILABLE/PROVIDER_UNAVAILABLE 503`.

### 3.11 Deviations from your endpoint list (summary)

Added: `users/me` (Settings page needs it), image reorder, job history, catalog versions/restore, marketplace schema, export preflight, export download, admin trio, health endpoints. Kept your `POST …/validate/{marketplace}` even though it is side-effect-free. I'd normally use GET, but your shape is acceptable and matches your spec. `PATCH …/catalog` gains `base_version` for optimistic concurrency, which stops two browser tabs silently overwriting each other.
