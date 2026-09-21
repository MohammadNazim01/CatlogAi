# CatalogAI — Phase 1 Spec, Part 3: Systems, Security, Testing, Ops

Covers spec items 10–22.

## 10. Authentication & authorization flow

**Decisions**

| Topic | Choice | Why | Alternative rejected |
|---|---|---|---|
| Password hashing | **Argon2id** (`argon2-cffi` / `pwdlib`) | Memory-hard; current OWASP first choice. | bcrypt (fine, 72-byte limit, not memory-hard) |
| Access token | JWT, **HS256**, 15 min, claims `sub, role, iat, exp, typ=access` | Stateless verification on every request. One service holds the secret, so asymmetric keys add nothing. | RS256 (needed only when other services verify tokens) |
| Refresh token | **Opaque random 256-bit**, stored as SHA-256 hash in `refresh_tokens`, 14-day TTL, rotated on every use | Revocable, and leakage of the DB doesn't leak usable tokens. | JWT refresh (cannot be revoked without a denylist) |
| Client storage | Access token **in JS memory only**; refresh token in **HttpOnly, Secure, SameSite=Strict cookie**, `Path=/api/v1/auth` | XSS can't read the refresh token; the cookie is only sent to auth endpoints. | Both in `localStorage` (any XSS = full account takeover) |
| Pin algorithm | Decode with `algorithms=["HS256"]` explicitly | Prevents `alg` confusion attacks. | — |
| Deactivation | `get_current_user` loads the user by PK every request and checks `is_active` | Immediate effect; a PK lookup is cheap. | Trust the JWT for 15 min |

**Flows**

```
register: validate → normalize email → argon2id hash → insert (SELLER) → 201
login:    find user (or dummy-hash verify to equalize timing) → verify → is_active?
          → new family_id, create refresh row → access JWT + Set-Cookie
request:  Bearer → decode/verify (sig, exp, typ) → load user → is_active → inject CurrentUser
refresh:  cookie → sha256 → row lookup
            not found / expired      → 401
            revoked_at set (REUSE!)  → revoke entire family → 401 REFRESH_TOKEN_REUSED
            ok → mark revoked, insert new row (same family, replaced_by), new access + cookie
logout:   revoke family, clear cookie
pw change: revoke all families except the current one
```
Reuse detection: if an attacker replays a stolen (already-rotated) token, the family dies, which forces both attacker and victim to re-login.

**Authorization** (three layers)
1. **Authentication**: `Depends(get_current_user)`.
2. **Role**: `Depends(require_role(Role.ADMIN))` for `/admin/*` only.
3. **Ownership (the important one)**: every repository read/write for tenant data takes `seller_id`. Not-found and not-yours are indistinguishable (404). Images/jobs/versions/exports are reached by joining through `products.seller_id`. There are no "get by id" repository methods that skip tenant scoping, except in the admin repository, which is a separate class.

## 11. AI pipeline architecture

**Provider abstraction**

```python
class LLMProvider(Protocol):
    async def generate_structured(
        self, *, model: str, system: str, user_text: str,
        images: Sequence[ImageInput] = (), schema: type[BaseModel],
        timeout_s: float) -> StructuredResult:   # .parsed: BaseModel, .usage: Usage(in, out), .raw
```
Adapters: `AnthropicProvider`, `GeminiProvider` (optional second, proves the abstraction is real), `FakeProvider` (deterministic; used by all tests and CI). Errors are translated into our own `ProviderTransientError` (429, 5xx, timeout, network) and `ProviderPermanentError` (400, content refusal, auth).

**Model choice.** Selected by config (`AI_VISION_MODEL`, `AI_TEXT_MODEL`), not hard-coded.
Recommendation to start: **Anthropic** for stage 1 (strong vision, reliable schema-following through forced tool-use) and a **cheaper small model** for stage 2 (text-only, so vision isn't needed): `claude-sonnet-5` for extraction and `claude-haiku-4-5-20251001` for writing. Gemini Flash-class is the cost-conscious alternative and has a free tier that helps development. **Prices and free-tier limits change, so verify current pricing on the provider pages before you commit budget, and let the eval harness (below) decide with your own data.**

**Pipeline** (`ai/pipeline.py`, called by the worker):

```
1. Load product + seller_attributes + images (must belong to product)
2. Prepare images: fetch from S3, downscale longest edge ≈1568px, JPEG q85, max 5 images sent
3. STAGE 1: EXTRACT   (vision)  → ExtractionResult
     {product_type, category_id (must be from taxonomy enum, or null),
      attributes: {name: {value|null, evidence: IMAGE_VISIBLE | IMAGE_TEXT | UNKNOWN, confidence}},
      visible_text: [strings read from packaging/labels],
      notes}
   Seller-provided facts are merged after the call. The model is told they exist, but they always win and
   are marked source=SELLER. It is *not* asked to re-derive them.
4. STAGE 2: WRITE     (text-only, no images) → CopyResult {title, description, bullet_points, keywords}
   Input = ONLY the facts from stage 1 + seller facts + marketplace-agnostic style rules.
   Model can't see the image, so it can't invent visual facts. It can only rephrase supplied ones.
5. GUARDRAILS (deterministic code, not another LLM)
6. Compute confidence, build CatalogContent, persist as a new CatalogVersion (same transaction as job COMPLETED + product→REVIEW)
```

**Why two stages, not one:** (a) separates *observing* from *writing*, the two places hallucination enters; (b) stage-1 evidence per attribute is auditable and stored in `ai_extraction`; (c) stage 2 is cheaper; (d) regenerating only the title doesn't need to re-run vision. **Cost of this choice:** two calls → more latency; acceptable since it's async.

**Structured output.** Use each provider's schema-enforced mode (forced tool call / JSON-schema response), then **still validate with Pydantic**. Provider-side enforcement improves reliability but isn't a guarantee for *semantic* correctness. On validation failure: one repair call including the validation errors; then fail with `AI_OUTPUT_INVALID`.

**Guardrails** (`ai/guardrails.py`, pure functions with unit tests):

| # | Rule | On violation |
|---|---|---|
| G1 | Pydantic schema valid; `value` null ⇔ `UNKNOWN` | repair once → fail |
| G2 | `category_id` ∈ `taxonomy.json` | set to null (+warning) |
| G3 | **Numeric-claim check:** every number+unit in title/description/bullets (`\d+(\.\d+)?\s?(cm|mm|inch|kg|g|ml|l|w|mah|gb|hz|…)`) must appear in seller input or stage-1 `visible_text` | repair once → keep + `warnings` entry |
| G4 | **Brand:** allowed only if `evidence ∈ {IMAGE_TEXT}` or seller supplied it | drop brand attribute + remove mention → warning |
| G5 | **Unsupported claims:** denylist regex (`waterproof`, `authentic`, `genuine`, `#1`, `best`, `guaranteed`, medical claims…) unless backed by a seller attribute | repair once → warning |
| G6 | Material/dimensions/weight/capacity only if source SELLER or IMAGE_TEXT | force `UNKNOWN` |
| G7 | Length/count limits; dedupe keywords (case-insensitive); strip HTML/markdown | auto-fix |

Warnings are stored in `catalog_versions.warnings` and shown as badges in the review UI (non-blocking; the seller is the final authority and the UI says so).

**Confidence score (honest definition).** `confidence = completeness × mean(attr_confidence)` where `completeness` = known required-attributes ÷ required-attributes for the category, and HIGH/MEDIUM/LOW map to 1.0/0.6/0.3, minus 0.05 per warning (floor 0). This is a **heuristic for sorting/flagging**; label it "completeness score" in the UI and never call it a probability.

**Category taxonomy.** A curated `taxonomy.json` (~60–100 leaf categories, each with `id`, `path`, `required_attributes`, and per-marketplace category mapping). The model chooses an ID from an enum in the schema; it doesn't invent category strings. Marketplace exporters map internal ID → marketplace node. *Not* using a vector DB or RAG here: with ≤100 categories a schema enum in the prompt is simpler and more accurate. (If you later want embeddings-based suggestion for a 5,000-node taxonomy, `pgvector` on the existing Postgres is the route. Don't build it now.)

**Prompt management.** Prompts are versioned files (`prompts/v1/*.md`); `prompt_version` is written to every catalog version. Rules embedded in all prompts: "output UNKNOWN when not directly visible or supplied; never infer material/dimensions/brand/quantity from appearance"; the seller's notes/instructions are wrapped in delimiters and declared to be **data**, not instructions.

**Section regeneration.** Attributes → re-run stage 1 (and merge). Other sections → stage 2 only, given current facts + current text of other sections + the seller's `instructions`. The result is copied into a new version with only that section changed.

**Evaluation harness** (`backend/evals/`, run manually, not in CI, because it costs money): 20–30 labeled products (include deliberately ambiguous images: no visible material, no brand, cropped). Metrics: schema-valid rate, **unsupported-attribute rate** (facts asserted that the label says are unknowable), guardrail trigger rate, avg tokens/cost, p50/p95 latency. Commit the results table to `docs/`. This is the single most convincing artefact for "AI doesn't hallucinate": you *measured* it.

## 12. Background job architecture

**Queue choice: ARQ** (asyncio-native Redis queue). Why: the whole codebase is async SQLAlchemy/httpx; ARQ tasks are plain `async def` sharing that code, and the library surface is tiny. Alternatives: **Celery** (most widely known, but sync-first, heavier, and its async story is awkward), **Dramatiq/RQ** (sync), **Postgres `SKIP LOCKED` queue** (removes Redis dependency and the dual-write problem, and is legitimately a good design; I'd choose it if Redis weren't already in your stack and on your resume). **Caveat to verify at Phase 8:** ARQ has been in maintenance-only mode; check its current status. Mitigation is a two-method `JobQueue` interface (`enqueue(job_id)`), so replacing it touches one file.

**Lifecycle**

```
API (one transaction):  check quota → INSERT ai_jobs(QUEUED) → product.status=PROCESSING → COMMIT
API (after commit):     queue.enqueue("run_ai_job", job_id, _job_id=str(job_id))   # dedup by id
                        if enqueue fails: leave row QUEUED (sweeper will retry) and still return 202
Worker:  claim atomically:
           UPDATE ai_jobs SET status='PROCESSING', attempts=attempts+1, started_at=coalesce(started_at, now()),
                  heartbeat_at=now()
           WHERE id=:id AND status='QUEUED' RETURNING *        -- 0 rows ⇒ another worker has it; exit
         run pipeline (heartbeat every ~15 s)
         success → ONE transaction: insert catalog_version (job_id unique), job=COMPLETED, product=REVIEW
         transient error → attempts < max ? job=QUEUED + ARQ Retry(defer=10s·3^n + jitter) : FAILED
         permanent error → job=FAILED, error_code/message; product → REVIEW if a version exists else DRAFT
Sweeper (ARQ cron, every minute):
         PROCESSING with heartbeat_at < now()-5min  → requeue (if attempts<max) else FAILED(WORKER_LOST)
         QUEUED older than 2 min                    → re-enqueue (idempotent because _job_id)
```

Guarantees and honest limits: **at-least-once execution, effectively-once results.** A retry after a crash can't create a duplicate version, thanks to the `UNIQUE(job_id)` constraint and the atomic claim. The provider call may be billed twice in a crash scenario, and that's accepted.

Settings: `job_timeout=180s`, `max_jobs=4` (I/O bound), provider concurrency semaphore, graceful shutdown (finish or requeue on SIGTERM), `keep_result=0` (results live in Postgres, never in Redis).

**Error classification** (worker): 429/5xx/timeout/connection → transient. 400/401/403 from provider → permanent (and alert, since a 401 means a bad key). Corrupt image / missing S3 object → permanent. `AI_OUTPUT_INVALID` after repair → permanent.

## 13. Redis usage

| Use | Keys | Notes |
|---|---|---|
| ARQ job queue | `arq:*` | Delivery only; truth is in `ai_jobs`. |
| Rate limiting | `rl:{scope}:{id}:{window}` | Atomic `INCR`+`EXPIRE` via a small Lua script (fixed window; simple and explainable. A sliding window is a cheap upgrade). |
| (Optional Phase 8) Job event pub/sub | `job:{id}` | For SSE later. |

**Deliberately not in Redis:** refresh tokens (D3), AI quota (counted from `ai_jobs`; it's the source of truth and must not drift), and dashboard counts (indexed SQL aggregates take a few ms; adding a cache means adding invalidation bugs for no gain).
Config: AOF `appendonly yes` (queue survives a restart), `maxmemory-policy noeviction` (never silently drop queue keys), password required, not exposed to the host network.

**Rate-limit policy** (all configurable):

| Scope | Limit |
|---|---|
| Login | 5/min per IP+email; 20/min per IP |
| Register | 5/hr per IP |
| Refresh | 30/min per IP |
| Image upload | 30 files/min per user |
| AI generate/regenerate | 10/min per user + `ai_daily_quota` per day |
| Everything else | 120/min per user |

Response: `429` + `Retry-After`. If Redis is down: **fail open with an error log and alert**, because a cache-tier outage shouldn't take the product down. Login limiting also has a small in-process fallback. (This is a trade-off; state it in interviews.)

## 14. Object storage strategy

- **One private bucket**, Block Public Access on, SSE-S3 encryption. Prefixes: `products/{seller_id}/{product_id}/{image_id}.{ext}` and `exports/{seller_id}/{export_id}.{ext}`. Keys are **generated by the server** from UUIDs; the user's filename is never part of the key (no path traversal, no collisions, no PII in URLs).
- **Reads:** presigned GET URLs (10 min for images, 5 min for exports, with `Content-Disposition: attachment` for exports). The bucket is never public. `<img>` tags need no CORS.
- **Upload pipeline** (`ImageService`, in a threadpool because Pillow is CPU-bound):
  1. Reject by declared size early (reverse-proxy `client_max_body_size 40m`; per-file ≤ 10 MB enforced while streaming).
  2. Read into a bounded buffer/temp file; **never trust `Content-Type` or the extension**.
  3. Open with Pillow, `verify()`; allow only JPEG/PNG/WebP; reject if pixels > 40 MP (`MAX_IMAGE_PIXELS` decompression-bomb guard) or either side > 8000 px.
  4. **Re-encode** (apply EXIF orientation, then strip all metadata, including GPS) and save as JPEG/WebP/PNG. This also neutralizes polyglot payloads and embedded scripts.
  5. sha256 of the stored bytes → duplicate check.
  6. Upload to S3 → insert DB row → commit. If the commit fails, delete the object (best effort). A nightly cleanup lists objects with no DB row older than 24 h.
- **Delete:** delete the DB row (commit), then the object. A failed object delete is logged and swept later; the user-visible state is already consistent.
- **Client:** `aioboto3` (or boto3 in `run_in_threadpool`). Same code for MinIO and S3, switched by `S3_ENDPOINT_URL`. On EC2, use the **instance IAM role** with a policy scoped to this bucket (`s3:GetObject/PutObject/DeleteObject/ListBucket`), so there are no long-lived keys on the box.
- Trade-off recorded: proxying uploads (D8) uses backend bandwidth/CPU; if traffic grew you'd move to presigned PUT + an async verification step.

## 15. Marketplace validation architecture

**Single source of truth per marketplace**: a declarative spec used by *both* validation and export, so they can't disagree.

```python
@dataclass(frozen=True)
class FieldSpec:
    column: str                       # output column header
    source: str                       # path into the export view: "title", "bullets[0]", "attributes.color", "product.sku"
    required: bool = False
    max_length: int | None = None
    min_length: int | None = None
    allowed_values: frozenset[str] | None = None
    pattern: re.Pattern | None = None
    transform: Callable[[Any], str] | None = None   # e.g. join keywords with ", "

@dataclass(frozen=True)
class MarketplaceSchema:
    code: str; version: str
    fields: tuple[FieldSpec, ...]
    category_map: Mapping[str, str]                 # internal taxonomy id → marketplace category
    required_attributes_by_category: Mapping[str, tuple[str, ...]]
    cross_rules: tuple[Callable[[ExportView], list[Issue]], ...]   # e.g. "keywords total ≤ 250 bytes"
```

`engine.validate(schema, view) -> ValidationResult` produces `Issue(code, severity, field, message)`. `engine.to_row(schema, view) -> dict[str,str]` builds the export row. `ExportView` is a flat, read-only projection of `(product, catalog_version)`.

Rule types: required, length, allowed values, regex format, missing category attributes (via `required_attributes_by_category`), unmapped category, **unknown attribute values** (`source=UNKNOWN` on a required attribute → `MISSING_REQUIRED_ATTRIBUTE`, which is why null-not-guess matters downstream), and cross-field rules. Severity `ERROR` blocks export; `WARNING` doesn't.

Schemas: `GENERIC` (title ≤ 200, description ≤ 2000, ≥ 3 bullets, keywords ≤ 20…, used as the approval gate), `AMAZON_STYLE` (bullet columns `bullet_point_1..5`, title/keyword limits inspired by common marketplace conventions), `FLIPKART_STYLE` (different column names/limits). **All limits are illustrative, configurable defaults inspired by public seller-guide conventions. They are not official specifications and must be described that way in the README and the UI.** Never write "Amazon integration".

Alternatives: JSON Schema per marketplace (good for structure but clumsy for cross-field rules and row mapping), rules in DB (D13). Extension = add one module + one seed row.

## 16. CSV/XLSX export architecture

```
POST /exports
 1. load products via get_owned_many(seller_id, ids)  → any missing ⇒ 404 (no distinction)
 2. status ∈ {APPROVED, EXPORTED} else 409
 3. for each: pick approved version (products.approved_version_id) → ExportView → engine.validate
 4. invalid & !skip_invalid → 422 with per-product issues (nothing created)
 5. INSERT exports(PENDING)
 6. build file in a SpooledTemporaryFile: CSV (stdlib csv) or XLSX (openpyxl write_only)
 7. upload to S3; INSERT export_items(version snapshot); export=COMPLETED; products=EXPORTED — one commit
    on any failure: export=FAILED(error_message), delete partial object, products unchanged
```

Details worth knowing:
- **CSV**: UTF-8 **with BOM** (`utf-8-sig`) so Excel opens Indian-language text correctly; `\r\n`; quoted as needed.
- **CSV/formula injection defense**: any cell whose text begins with `= + - @ \t \r` is prefixed with `'`. AI- or seller-controlled text becomes a spreadsheet formula otherwise. Also applied to XLSX cells (write as explicit strings).
- **XLSX**: `openpyxl` `write_only=True` (constant memory); header row frozen/bold; one sheet `Products`; column order from the schema; metadata sheet (marketplace, schema version, export time).
- Bullets: separate columns when the schema asks; otherwise joined with ` | `. Keywords are joined with `, `.
- Sync in v1 with 500-product cap (D10); export snapshot is in `export_items.catalog_version_id`, so re-exporting after edits yields new data and old exports remain traceable.

## 17. Error-handling strategy

- **Exception hierarchy** `AppError(status_code, code, message)` with subclasses (`NotFoundError`, `ConflictError`, `ValidationFailed`, `RateLimited`, `ProviderUnavailable`…). Services raise domain errors. Routes don't catch them.
- **Global handlers** map: `AppError` → envelope; `RequestValidationError` → 422 `VALIDATION_ERROR` with field paths (no echo of the input values for password fields); `IntegrityError` → known constraint names map to 409 codes (`ux_products_seller_sku` → `SKU_TAKEN`), otherwise 409/500; unhandled `Exception` → 500 `INTERNAL_ERROR` with a generic message and `request_id`; stack trace goes to logs only.
- **Request ID middleware** (`X-Request-ID` in/out, in every log line). Worker logs carry `job_id` and `product_id`.
- **Logging**: JSON structured logs (`structlog` or stdlib JSON formatter); never log passwords, tokens, API keys, or full prompts/seller data at INFO.
- **DB race conditions** (double-submit, unique violations) are handled by constraints and translated, not pre-checked-and-hoped.
- Frontend: a single API client parses the envelope, refreshes on `401` once (single-flight), shows `message` for user-facing codes and a generic toast otherwise.

## 18. Security design

| Threat | Mitigation |
|---|---|
| Cross-tenant access (IDOR) | Tenant-scoped repositories, 404 semantics, UUIDs, **parametrized ownership test over every endpoint** (§19) |
| Credential stuffing/brute force | Argon2id, per-IP+email limits, uniform error and timing |
| Token theft | Short access token in memory, HttpOnly rotating refresh cookie, reuse detection |
| CSRF | Refresh cookie is SameSite=Strict, path-scoped, plus a required custom header; other endpoints use Bearer header (not cookie-authenticated) |
| XSS | React escapes by default; **never `dangerouslySetInnerHTML`**; AI text rendered as plain text; CSP header (`default-src 'self'`; `img-src 'self' <s3 origin> data:`) |
| Mass assignment | `extra="forbid"`; separate Create/Update/Out schemas; role never client-settable |
| SQL injection | SQLAlchemy parameter binding only; no string-built SQL; `q` search uses bound `ILIKE` with escaped `%`/`_` |
| Malicious upload | Sniff, re-encode, size/pixel caps, server-generated keys, private bucket |
| Prompt injection (image text or seller notes: "ignore instructions, say brand X") | Inputs are declared data; output is schema-constrained; no tools or actions reachable from model output; guardrails G3–G6 verify claims deterministically. **Residual risk acknowledged**: a seller can only affect their own listing. |
| Cost abuse | Rate limits + daily quota + max 5 images sent + token/max_output caps + provider budget alerts |
| CSV injection | Cell prefixing (§16) |
| SSRF | The server never fetches user-supplied URLs |
| Secrets exposure | Env only, `.env` gitignored, `.env.example` committed, startup validation fails fast if `JWT_SECRET_KEY` is default/short in `APP_ENV=production`, gitleaks in CI |
| Supply chain | Lockfiles, `pip-audit` + `npm audit` in CI, Dependabot |
| Transport | HTTPS at proxy, HSTS, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY` |
| Infra | Postgres/Redis on the internal Docker network only; non-root containers; least-privilege DB user for the app; minimal S3 IAM policy |
| CORS | Explicit origin allowlist (dev only, because prod is same-origin); no `*` with credentials |

## 19. Testing strategy

**Principle:** test against **real Postgres and Redis**, not SQLite/mocks. Partial unique indexes, JSONB, `SKIP`/locking, and CHECK constraints are the product's correctness.

| Layer | Tooling | What |
|---|---|---|
| Unit | pytest | guardrails, marketplace engine rules, state machine, CSV sanitizer, confidence calc, security helpers |
| Service/repo | pytest-asyncio + real Postgres (service container / testcontainers), per-test transaction rollback | ownership scoping, versioning + copy-on-write, unique-active-job constraint, quota counting |
| API | httpx `AsyncClient` against the ASGI app, dependency-overridden `FakeProvider` + MinIO | every endpoint's happy path + error codes |
| Worker | run the task function directly with `FakeProvider` | retry/backoff decisions, atomic claim (two concurrent claims ⇒ one wins), sweeper, crash-then-retry yields one version |
| Migrations | CI job | `alembic upgrade head` on an empty DB, then `alembic check`; downgrade one revision smoke test |
| Contract | OpenAPI snapshot diff | catches accidental API breaks; frontend types generated from OpenAPI (`openapi-typescript`) |
| Frontend | Vitest + React Testing Library (forms, review editor, polling hook); **one Playwright happy path** (register → create → upload → generate(fake) → approve → export) | |
| AI quality | `evals/` manual harness against the real provider | not in CI |

**Must-have tests (the ones you'll brag about):**
1. **Tenant isolation matrix**: introspect the OpenAPI routes, and for every route that takes a resource ID, assert Seller B gets `404` on Seller A's resource. A new endpoint added without scoping fails CI.
2. Refresh-token rotation and **reuse ⇒ family revoked**.
3. Upload attacks: `.jpg` that's really a script, truncated file, decompression bomb, oversized file, EXIF GPS is stripped.
4. Guardrails: injected numeric claim, invented brand, "waterproof" without support.
5. Concurrency: double `generate` ⇒ one job; parallel `PATCH` with the same `base_version` ⇒ one `409`.
6. CSV injection cell (`=HYPERLINK(...)`) is neutralized in both CSV and XLSX.

CI gates: `ruff`, `mypy`, tests with coverage reported (aim ≥ 80% on `services/`, `ai/`; don't chase the number), migration check, `pip-audit`, frontend `tsc --noEmit` + eslint + vitest + build, Docker image build.

## 20. Docker service architecture

| Service | Image / build | Ports | Notes |
|---|---|---|---|
| `postgres` | `postgres:17` (verify against target RDS version) | internal | volume `pgdata`, healthcheck `pg_isready`, creates `pg_trgm` in the first migration |
| `redis` | `redis:7` | internal | `--appendonly yes --maxmemory-policy noeviction --requirepass`, volume, healthcheck |
| `minio` + `minio-init` | `minio/minio` + `mc` one-shot | 9000/9001 (dev only) | creates the bucket and applies a private policy |
| `migrate` | backend image | none | one-shot `alembic upgrade head`; `backend` and `worker` `depends_on: service_completed_successfully` |
| `backend` | backend Dockerfile (multi-stage, Python 3.12-slim, non-root) | 8000 internal | `uvicorn app.main:app --workers 2` (prod), `--reload` (dev override) |
| `worker` | **same image** | none | `arq app.worker.settings.WorkerSettings`; healthcheck via ARQ health key |
| `frontend` | multi-stage: node build → `nginx` static | 80 | dev: Vite dev server with `/api` proxy |
| `proxy` (prod only) | `caddy` | 80/443 | automatic TLS; `/api/*` → backend, everything else → frontend; security headers |

Files: `docker-compose.yml` (base), `docker-compose.dev.yml` (bind mounts, reload, exposed ports, MinIO), `docker-compose.prod.yml` (proxy, no exposed DB/Redis, restart policies, resource limits). One internal network; only the proxy publishes ports in prod. `.dockerignore` for every image, pinned base image versions, `HEALTHCHECK` for backend (`/health/ready`: DB `SELECT 1`, Redis `PING`, S3 `head_bucket`).

## 21. Environment variable specification

Loaded by `pydantic-settings` (one `Settings` class, validated at startup; failure aborts boot). `.env.example` committed with placeholders; real secrets never in git.

| Variable | Example / default | Secret | Purpose |
|---|---|---|---|
| `APP_ENV` | `development`\|`test`\|`production` | | Enables strict checks in production |
| `LOG_LEVEL` | `INFO` | | |
| `DATABASE_URL` | `postgresql+asyncpg://catalogai:***@postgres:5432/catalogai` | ✔ | |
| `POSTGRES_USER/PASSWORD/DB` | | ✔ | for the compose postgres service |
| `REDIS_URL` | `redis://:***@redis:6379/0` | ✔ | |
| `JWT_SECRET_KEY` | ≥ 32 random bytes | ✔ | production refuses defaults/short values |
| `JWT_ALGORITHM` | `HS256` | | |
| `ACCESS_TOKEN_TTL_MIN` | `15` | | |
| `REFRESH_TOKEN_TTL_DAYS` | `14` | | |
| `COOKIE_SECURE` | `true` (false only in local http) | | |
| `CORS_ORIGINS` | `http://localhost:5173` | | Comma list; empty in same-origin prod |
| `S3_ENDPOINT_URL` | `http://minio:9000` (empty on AWS) | | |
| `S3_REGION` | `ap-south-1` | | |
| `S3_BUCKET` | `catalogai-dev` | | |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | | ✔ | Blank on EC2 ⇒ IAM role credentials |
| `S3_PRESIGN_TTL_IMAGE_SEC` / `_EXPORT_SEC` | `600` / `300` | | |
| `MAX_IMAGE_BYTES` | `10485760` | | |
| `MAX_IMAGES_PER_PRODUCT` | `8` | | |
| `MAX_IMAGE_PIXELS` | `40000000` | | |
| `AI_PROVIDER` | `anthropic`\|`gemini`\|`fake` | | |
| `AI_VISION_MODEL`, `AI_TEXT_MODEL` | model IDs | | |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | | ✔ | |
| `AI_REQUEST_TIMEOUT_SEC` | `60` | | |
| `AI_MAX_ATTEMPTS` | `3` | | |
| `AI_MAX_IMAGES_SENT` | `5` | | |
| `AI_DAILY_QUOTA_DEFAULT` | `50` | | default for new users |
| `PROMPT_VERSION` | `v1` | | |
| `RATE_LIMIT_*` | see §13 | | per-scope overrides |
| `WORKER_MAX_JOBS` | `4` | | |
| `EXPORT_MAX_PRODUCTS` | `500` | | |
| `SENTRY_DSN` | | ✔ | optional |
| `VITE_API_BASE_URL` | `/api/v1` | | build-time, public |

Production secrets: GitHub Actions secrets → deployed to the host; on AWS prefer SSM Parameter Store/Secrets Manager or root-only `.env` on the box with `chmod 600`. Rotating `JWT_SECRET_KEY` invalidates access tokens (acceptable, since they live 15 min).

## 22. Production-readiness considerations

- **Health/readiness**: `/health/live` (process up), `/health/ready` (deps reachable). Compose and the proxy use them.
- **Timeouts everywhere**: provider client 60 s, S3 client timeouts, DB `statement_timeout` (e.g., 15 s API, longer for the worker), uvicorn keep-alive, proxy read timeout. No unbounded waits.
- **Observability**: structured logs with request/job IDs; optional Sentry; optional Prometheus metrics (`http_requests`, `ai_job_duration`, `ai_job_failures`, `queue_depth`). One log line per job with model, tokens, latency, outcome.
- **DB**: pool sized to workers×connections under Postgres `max_connections`; indexes above; avoid N+1 via explicit eager loads (`lazy="raise"` enforces this); pagination caps.
- **Migrations on deploy**: `migrate` one-shot before app start. Use **expand → migrate → contract** for breaking changes. Zero-downtime isn't a goal for a single-node deploy, and you should say so honestly.
- **Backups**: nightly `pg_dump` to S3 (or RDS automated snapshots) and **one documented restore test**. S3 versioning on the bucket.
- **Graceful shutdown**: uvicorn drains requests; worker finishes or requeues in-flight jobs on SIGTERM.
- **Data lifecycle**: expired refresh-token cleanup; orphan-object sweep; export retention (e.g., 90 days, S3 lifecycle rule).
- **Cost controls**: quotas, caps, token logging, provider budget alerts.
- **Deploy path (Phase 10)**: GitHub Actions → build images → push to GHCR → SSH to EC2 → `docker compose pull && up -d` behind Caddy; Postgres either in Compose with a mounted volume + backups (cheapest) or RDS (more realistic; costs more). Document which you chose and why.

### Known limitations (put these in the README; honesty reads as seniority)
Single-node deployment; no real marketplace API integration and marketplace limits are illustrative; AI output can still be wrong, hence human review is mandatory in the workflow; no multi-user organizations/teams; polling instead of push; queue library maintenance status (§12).

### Interview map: what each part proves
Data modelling with partial unique indexes and immutability → §3. Distributed-systems thinking without buzzwords (dual write, idempotent claim, at-least-once) → §12. Security → §10, §14, §18. Applied AI engineering with measurement → §11 evals. Product judgment → state machine, copy-on-write, review gate.

---

## Open decisions I need from you before Phase 2

1. **Confirm or veto D1–D16.** The ones most likely to matter to you are D6 (ARQ + DB source of truth), D7 (two-stage AI), and D8 (upload through backend).
2. **AI provider for development.** Which API key do you actually have or want to pay for (Anthropic, Gemini free tier)? The abstraction supports both, but you need one working key for Phase 4. The `FakeProvider` covers everything before then.
3. **Python version:** default 3.12. Say so if you have a reason for 3.13.
4. **Postgres deployment target** (Compose volume vs RDS). It doesn't block Phase 2, only Phase 10.

**Phase 2 scope once approved:** repo skeleton and Docker Compose (postgres, redis, minio, migrate, backend) → `Settings`, logging, error envelope, request-ID → DB session + Base + all models → first Alembic migration (all tables and indexes) + marketplace seed → auth (register/login/refresh/logout/me, Argon2id, rotation + reuse detection, rate limiting) → tests for auth and the migration check → CI workflow (lint, types, tests).
