"""asyncpg connection pool helper.

Read-path queries use raw SQL for TimescaleDB chunk exclusion
performance.  This module provides the pool lifecycle and a
convenience query runner.
"""

from __future__ import annotations

import time
from typing import Any

from metrics import QUERY_DB_LATENCY


async def create_pool(dsn: str, min_size: int = 2, max_size: int = 10, command_timeout: float = 30.0) -> Any:
    """Create and return an asyncpg connection pool."""
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "missing optional dependency 'asyncpg'; install requirements.txt"
        ) from exc

    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
        server_settings={"timezone": "UTC"},
    )
    return pool


async def fetch_rows(pool: Any, query: str, args: list[Any], query_type: str = "select") -> list[Any]:
    """Execute a query and return rows with latency tracking."""
    t0 = time.monotonic()
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    elapsed_ms = (time.monotonic() - t0) * 1000
    QUERY_DB_LATENCY.labels(query_type=query_type).observe(elapsed_ms)
    return rows


async def fetch_val(pool: Any, query: str, args: list[Any], query_type: str = "count") -> Any:
    """Execute a query and return a single value."""
    t0 = time.monotonic()
    async with pool.acquire() as conn:
        val = await conn.fetchval(query, *args)
    elapsed_ms = (time.monotonic() - t0) * 1000
    QUERY_DB_LATENCY.labels(query_type=query_type).observe(elapsed_ms)
    return val
