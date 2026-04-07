"""Shared fixtures for topology service tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Add service root to sys.path
SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from api import router  # noqa: E402


# ---------------------------------------------------------------------------
# Fake asyncpg pool (same pattern as query-api)
# ---------------------------------------------------------------------------


class FakePool:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.count_val: int = 0
        self._rows_queue: list[list[dict]] = []
        self._fetchrow_val: dict | None = None
        self._execute_result: str = "DELETE 1"

    def set_rows(self, rows: list[dict]) -> None:
        self.rows = rows
        self._rows_queue = []

    def set_rows_sequence(self, seq: list[list[dict]]) -> None:
        self._rows_queue = list(seq)

    def pop_rows(self) -> list[dict]:
        if self._rows_queue:
            return self._rows_queue.pop(0)
        return self.rows

    def set_count(self, n: int) -> None:
        self.count_val = n

    def set_fetchrow(self, row: dict | None) -> None:
        self._fetchrow_val = row

    def set_execute_result(self, result: str) -> None:
        self._execute_result = result

    def acquire(self) -> FakePoolCtx:
        return FakePoolCtx(self)

    async def close(self) -> None:
        pass


class FakePoolCtx:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> FakeConn:
        return FakeConn(self._pool)

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        return self._pool.pop_rows()

    async def fetchval(self, query: str, *args: Any) -> Any:
        return self._pool.count_val

    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        return self._pool._fetchrow_val

    async def execute(self, query: str, *args: Any) -> str:
        return self._pool._execute_result


# ---------------------------------------------------------------------------
# Settings stub
# ---------------------------------------------------------------------------


class _JwtCfg:
    secret_key = "test-secret"
    algorithm = "HS256"
    cookie_name = "access_token"


class _FakeSettings:
    jwt = _JwtCfg()


# ---------------------------------------------------------------------------
# JWT helper
# ---------------------------------------------------------------------------


def _make_jwt(
    user_id: str = "test-user-id",
    username: str = "testuser",
    role: str = "admin",
    secret: str = "test-secret",
) -> str:
    import jwt as pyjwt

    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "exp": int(datetime.now(tz=timezone.utc).timestamp()) + 3600,
        "iat": int(datetime.now(tz=timezone.utc).timestamp()),
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def app(fake_pool: FakePool) -> FastAPI:
    app = FastAPI()
    app.state.db_pool = fake_pool
    app.state.settings = _FakeSettings()
    app.include_router(router)
    return app


@pytest.fixture
def make_jwt():
    return _make_jwt


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
