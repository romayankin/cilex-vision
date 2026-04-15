"""Audit + access logging middleware.

Splits per HTTP method:
  - GET/HEAD/OPTIONS → ``access_log`` (hypertable, 90-day retention). Volume
    is dominated by reads and compliance only needs "who looked at what,
    when" for a bounded window.
  - Mutating methods → ``audit_logs`` (permanent). Route handlers can emit
    a rich audit entry themselves and set ``request.state.audit_written =
    True`` to suppress the generic middleware fallback.

Health/metrics/docs paths are skipped. Writes are best-effort — a failure
is logged but never blocks the response.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from metrics import AUDIT_ERRORS, AUDIT_WRITES

logger = logging.getLogger(__name__)


SKIP_PATHS = {"/health", "/ready", "/metrics", "/docs", "/openapi.json"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}

_access_log_enabled: bool = False
_access_log_checked_at: float = 0.0
_ACCESS_LOG_CACHE_TTL = 30.0  # seconds — re-check DB every 30s


async def _is_access_log_enabled(pool: Any) -> bool:
    """Check settings table with a TTL cache so we don't hit the DB per request."""
    global _access_log_enabled, _access_log_checked_at

    now = time.monotonic()
    if now - _access_log_checked_at < _ACCESS_LOG_CACHE_TTL:
        return _access_log_enabled

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM settings WHERE key = 'access_log_enabled'"
            )
        _access_log_enabled = (
            row is not None and str(row["value"]).lower() in ("true", "1", "yes")
        )
    except Exception:
        pass  # keep previous value on error

    _access_log_checked_at = now
    return _access_log_enabled


class AuditMiddleware(BaseHTTPMiddleware):
    """Route each request to access_log (reads) or audit_logs (mutations)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000

        path = request.url.path
        if path in SKIP_PATHS:
            return response

        user_id = getattr(request.state, "audit_user_id", None)
        username = getattr(request.state, "audit_username", None)
        pool = getattr(request.app.state, "db_pool", None)
        if pool is None:
            return response

        ip_address = _client_ip(request)
        hostname = _client_hostname(request)
        method = request.method

        if method in READ_METHODS:
            if await _is_access_log_enabled(pool):
                try:
                    await _write_access_log(
                        pool=pool,
                        user_id=user_id,
                        username=username,
                        method=method,
                        path=path,
                        query_string=str(request.url.query),
                        status_code=response.status_code,
                        latency_ms=round(elapsed_ms, 2),
                        ip_address=ip_address,
                        hostname=hostname,
                    )
                except Exception:
                    logger.warning("Access log write failed", exc_info=True)
            return response

        # Mutating request — skip if the route already wrote a rich audit entry.
        if getattr(request.state, "audit_written", False):
            return response

        try:
            await _write_audit_log(
                pool=pool,
                user_id=user_id,
                action=method,
                resource_type=_resource_type(path),
                resource_id=_resource_id(path),
                details={
                    "path": path,
                    "query": str(request.url.query),
                    "status": response.status_code,
                    "latency_ms": round(elapsed_ms, 2),
                    "username": username,
                },
                ip_address=ip_address,
                hostname=hostname,
            )
            AUDIT_WRITES.inc()
        except Exception:
            AUDIT_ERRORS.inc()
            logger.warning("Audit log write failed", exc_info=True)

        return response


async def _write_audit_log(
    pool: Any,
    user_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    details: dict,
    ip_address: str | None,
    hostname: str | None = None,
) -> None:
    """INSERT one audit_logs row."""
    import json  # noqa: PLC0415

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_logs (log_id, user_id, action, resource_type, resource_id, details_jsonb, ip_address, hostname)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
            """,
            uuid.uuid4(),
            uuid.UUID(user_id) if user_id else None,
            action,
            resource_type,
            resource_id,
            json.dumps(details),
            ip_address,
            hostname,
        )


async def _write_access_log(
    pool: Any,
    user_id: str | None,
    username: str | None,
    method: str,
    path: str,
    query_string: str,
    status_code: int,
    latency_ms: float,
    ip_address: str | None,
    hostname: str | None,
) -> None:
    """INSERT one access_log row."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO access_log
                (user_id, username, method, path, query_string, status_code,
                 latency_ms, ip_address, hostname)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            uuid.UUID(user_id) if user_id else None,
            username,
            method,
            path,
            query_string,
            status_code,
            latency_ms,
            ip_address,
            hostname,
        )


def _resource_type(path: str) -> str:
    """Extract resource type from URL path."""
    parts = path.strip("/").split("/")
    if parts:
        return parts[0]
    return "unknown"


def _resource_id(path: str) -> str | None:
    """Extract resource ID from URL path if present."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2:
        return parts[1]
    return None


def _client_ip(request: Request) -> str | None:
    """Extract client IP, respecting X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _client_hostname(request: Request) -> str | None:
    """Extract the Host header (how the client reached us)."""
    return request.headers.get("x-forwarded-host") or request.headers.get("host")


__all__ = [
    "AuditMiddleware",
    "_write_audit_log",
    "_write_access_log",
    "_client_ip",
    "_client_hostname",
]
