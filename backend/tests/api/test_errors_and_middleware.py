import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthenticatedError,
    UnprocessableError,
)  # fmt: skip
from app.main import create_app


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str


class _PgError(Exception):
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


def _integrity(constraint: str | None) -> IntegrityError:
    # SQLAlchemy's asyncpg adapter exception chains the driver error via __cause__.
    adapted = Exception("adapted")
    adapted.__cause__ = _PgError(constraint) if constraint else Exception("no name")
    return IntegrityError("INSERT ...", {}, adapted)


def _build() -> FastAPI:
    app = create_app()

    @app.get("/t/not-found")
    async def _nf() -> None:
        raise NotFoundError("PRODUCT_NOT_FOUND", "Product not found.")

    @app.get("/t/conflict")
    async def _cf() -> None:
        raise ConflictError(
            "INVALID_STATE_TRANSITION", "Nope.", details=[{"field": "status", "issue": "x"}]
        )

    @app.get("/t/status/{n}")
    async def _st(n: int) -> None:
        errs: dict[int, AppError] = {
            401: UnauthenticatedError("UNAUTHENTICATED", "Login required."),
            403: ForbiddenError("FORBIDDEN", "No."),
            422: UnprocessableError("CATALOG_INVALID", "Bad catalog."),
            429: RateLimitedError("RATE_LIMITED", "Slow down.", retry_after=30),
        }
        raise errs[n]

    @app.post("/t/body")
    async def _body(b: Body) -> dict[str, str]:
        return {"ok": b.email}

    @app.get("/t/boom")
    async def _boom() -> None:
        raise RuntimeError("db password is hunter2")

    @app.get("/t/integrity/{name}")
    async def _ie(name: str) -> None:
        raise _integrity(None if name == "unnamed" else name)

    return app


@pytest.fixture
async def c() -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False: behave like a real server, which turns crashes into 500s.
    transport = ASGITransport(app=_build(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _err(r: Any) -> dict[str, Any]:
    body = r.json()
    assert set(body) == {"error"}
    err: dict[str, Any] = body["error"]
    assert err["request_id"] == r.headers["x-request-id"]
    return err


async def test_app_error_uses_envelope(c: AsyncClient) -> None:
    r = await c.get("/t/not-found")
    assert r.status_code == 404
    e = _err(r)
    assert (e["code"], e["message"]) == ("PRODUCT_NOT_FOUND", "Product not found.")
    assert "details" not in e


async def test_details_are_passed_through(c: AsyncClient) -> None:
    r = await c.get("/t/conflict")
    assert r.status_code == 409 and _err(r)["details"] == [{"field": "status", "issue": "x"}]


@pytest.mark.parametrize(
    ("n", "code"), [(401, "UNAUTHENTICATED"), (403, "FORBIDDEN"), (422, "CATALOG_INVALID")]
)
async def test_status_mapping(c: AsyncClient, n: int, code: str) -> None:
    r = await c.get(f"/t/status/{n}")
    assert r.status_code == n and _err(r)["code"] == code


async def test_rate_limited_sets_retry_after(c: AsyncClient) -> None:
    r = await c.get("/t/status/429")
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert _err(r)["code"] == "RATE_LIMITED"


async def test_validation_error_lists_fields_and_never_echoes_input(c: AsyncClient) -> None:
    r = await c.post(
        "/t/body", json={"email": "a@b.co", "password": "SuperSecret123", "role": "ADMIN"}
    )
    assert r.status_code == 422
    e = _err(r)
    assert e["code"] == "VALIDATION_ERROR"
    assert {d["field"] for d in e["details"]} == {"role"}
    assert "SuperSecret123" not in r.text and "ADMIN" not in r.text


async def test_missing_field_reports_dotted_path(c: AsyncClient) -> None:
    r = await c.post("/t/body", json={"email": "a@b.co"})
    assert [d["field"] for d in _err(r)["details"]] == ["password"]


async def test_unknown_route_and_wrong_method_use_envelope(c: AsyncClient) -> None:
    r = await c.get("/nope")
    assert r.status_code == 404 and _err(r)["code"] == "NOT_FOUND"
    r = await c.delete("/health/live")
    assert r.status_code == 405 and _err(r)["code"] == "METHOD_NOT_ALLOWED"
    assert "allow" in r.headers


async def test_unhandled_exception_is_generic_and_logged(
    c: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        r = await c.get("/t/boom")
    assert r.status_code == 500
    e = _err(r)
    assert e["code"] == "INTERNAL_ERROR" and "hunter2" not in r.text
    assert any(rec.exc_info and "hunter2" in str(rec.exc_info[1]) for rec in caplog.records)


async def test_known_constraint_maps_to_409(c: AsyncClient) -> None:
    r = await c.get("/t/integrity/ux_users_email_lower")
    assert r.status_code == 409 and _err(r)["code"] == "EMAIL_TAKEN"


async def test_unmapped_constraint_is_500_not_a_leak(c: AsyncClient) -> None:
    for name in ("ux_some_new_thing", "unnamed"):
        r = await c.get(f"/t/integrity/{name}")
        assert r.status_code == 500 and _err(r)["code"] == "INTERNAL_ERROR"


async def test_request_id_generated_when_absent(c: AsyncClient) -> None:
    r = await c.get("/health/live")
    assert len(r.headers["x-request-id"]) == 32


async def test_valid_inbound_request_id_is_echoed(c: AsyncClient) -> None:
    r = await c.get("/health/live", headers={"X-Request-ID": "trace-abc-12345"})
    assert r.headers["x-request-id"] == "trace-abc-12345"


async def test_malicious_inbound_request_id_is_replaced(c: AsyncClient) -> None:
    r = await c.get("/health/live", headers={"X-Request-ID": 'x"}\nINJECTED {"level":"CRITICAL"'})
    assert "INJECTED" not in r.headers["x-request-id"] and len(r.headers["x-request-id"]) == 32


async def test_access_log_has_request_id_and_omits_query_string(
    c: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.access"):
        await c.get("/t/not-found?token=secret123", headers={"X-Request-ID": "trace-abc-12345"})
    rec = next(r for r in caplog.records if r.name == "app.access")
    assert (rec.path, rec.status) == ("/t/not-found", 404)  # type: ignore[attr-defined]
    assert "secret123" not in str(rec.__dict__)


async def test_cors_allows_configured_origin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173")
    config.get_settings.cache_clear()
    try:
        transport = ASGITransport(app=create_app(), raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            ok = await client.get("/health/live", headers={"Origin": "http://localhost:5173"})
            bad = await client.get("/health/live", headers={"Origin": "http://evil.test"})
            pre = await client.options(
                "/health/live",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                },
            )
    finally:
        config.get_settings.cache_clear()
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert ok.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in bad.headers
    assert pre.status_code == 200
