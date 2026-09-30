"""Small helpers so API tests read as scenarios."""

import hashlib
from typing import Any

from httpx import AsyncClient, Response

PASSWORD = "correct-horse-battery"
CSRF = {"X-Requested-With": "catalogai"}


async def register(api: AsyncClient, email: str = "seller@example.com", password: str = PASSWORD,
                   name: str = "Sam Seller") -> Response:  # fmt: skip
    return await api.post(
        "/api/v1/auth/register", json={"name": name, "email": email, "password": password}
    )


async def login(
    api: AsyncClient, email: str = "seller@example.com", password: str = PASSWORD
) -> Response:
    api.cookies.clear()
    return await api.post("/api/v1/auth/login", json={"email": email, "password": password})


async def signup_and_login(api: AsyncClient, email: str = "seller@example.com") -> tuple[str, str]:
    """Returns (access_token, refresh_token)."""
    assert (await register(api, email)).status_code == 201
    r = await login(api, email)
    assert r.status_code == 200, r.text
    return r.json()["access_token"], refresh_cookie(r)


def refresh_cookie(response: Response) -> str:
    return response.cookies["refresh_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def refresh(api: AsyncClient, token: str | None, *, csrf: bool = True) -> Response:
    """POST /auth/refresh presenting `token` explicitly (not via the client's cookie jar)."""
    api.cookies.clear()
    headers: dict[str, str] = dict(CSRF) if csrf else {}
    if token is not None:
        headers["Cookie"] = f"refresh_token={token}"
    return await api.post("/api/v1/auth/refresh", headers=headers)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def error_of(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body
