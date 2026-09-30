import pytest
from argon2 import PasswordHasher

from app.core import security


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argon2 at production cost takes ~50-100 ms per hash, which adds up over hundreds of API
    tests. Production parameters are covered by tests/unit/test_security.py."""
    monkeypatch.setattr(
        security, "_hasher", PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    )
    security._dummy_hash.cache_clear()
