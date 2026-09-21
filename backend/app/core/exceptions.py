"""Domain errors, the stable error envelope, and global exception handlers.

Services raise AppError subclasses; routes never catch them. The frontend switches on
`error.code`, never on `message`.
"""

import logging
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.middleware import REQUEST_ID_HEADER, get_request_id

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


class BadRequestError(AppError):
    status_code = 400


class UnauthenticatedError(AppError):
    status_code = 401


class ForbiddenError(AppError):
    status_code = 403


class NotFoundError(AppError):
    status_code = 404


class ConflictError(AppError):
    status_code = 409


class UnprocessableError(AppError):
    status_code = 422


class RateLimitedError(AppError):
    status_code = 429

    def __init__(self, code: str, message: str, *, retry_after: int) -> None:
        super().__init__(code, message, headers={"Retry-After": str(max(1, retry_after))})


class ServiceUnavailableError(AppError):
    status_code = 503


# DB constraint name -> (error code, user message). Constraint names are part of the schema
# contract (see docs/01); tests assert every name here exists in the migrated database.
CONSTRAINT_ERRORS: dict[str, tuple[str, str]] = {
    "ux_users_email_lower": ("EMAIL_TAKEN", "An account with this email already exists."),
    "ux_products_seller_sku": ("SKU_TAKEN", "You already have a product with this SKU."),
    "ux_product_images_product_sha256": ("DUPLICATE_IMAGE", "This image was already uploaded."),
    "ux_ai_jobs_active_per_product": ("JOB_ALREADY_ACTIVE", "An AI job is already running."),
    "ux_catalog_versions_product_version": (
        "VERSION_CONFLICT",
        "The catalog was modified concurrently.",
    ),
    "ux_catalog_versions_current": ("VERSION_CONFLICT", "The catalog was modified concurrently."),
}

_HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    *,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status_code, headers=headers)


def _constraint_name(exc: IntegrityError) -> str | None:
    """Find the violated constraint's name. SQLAlchemy wraps the driver error, which for asyncpg
    is chained via __cause__; the name is not otherwise exposed."""
    orig: BaseException | None = exc.orig
    while orig is not None:
        name = getattr(orig, "constraint_name", None)
        if isinstance(name, str):
            return name
        orig = orig.__cause__
    return None


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(AppError, exc)
    return error_response(
        exc.status_code, exc.code, exc.message, get_request_id(request.scope),
        details=exc.details, headers=exc.headers,
    )  # fmt: skip


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    # Deliberately omit `input`/`ctx`: they can echo submitted values such as passwords.
    details = [
        {
            "field": ".".join(str(p) for p in err["loc"] if p not in ("body", "query", "path")),
            "issue": err["type"],
            "message": err["msg"],
        }
        for err in exc.errors()
    ]
    return error_response(
        422, "VALIDATION_ERROR", "Request validation failed.", get_request_id(request.scope),
        details=details,
    )  # fmt: skip


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
    return error_response(
        exc.status_code, code, str(exc.detail), get_request_id(request.scope),
        headers=dict(exc.headers) if exc.headers else None,
    )  # fmt: skip


async def _integrity_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(IntegrityError, exc)
    request_id = get_request_id(request.scope)
    mapped = CONSTRAINT_ERRORS.get(_constraint_name(exc) or "")
    if mapped is None:
        # An unmapped violation is a bug (missing pre-check or mapping), not a client error.
        logger.error("unmapped integrity error", exc_info=exc)
        return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.", request_id)
    return error_response(409, mapped[0], mapped[1], request_id)


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    request_id = get_request_id(request.scope)
    logger.error("unhandled exception", exc_info=exc)
    # Runs in ServerErrorMiddleware, outside RequestContextMiddleware, so set the header here.
    return error_response(
        500, "INTERNAL_ERROR", "An unexpected error occurred.", request_id,
        headers={REQUEST_ID_HEADER: request_id},
    )  # fmt: skip


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(IntegrityError, _integrity_error)
    app.add_exception_handler(Exception, _unhandled)
