"""Audit logging middleware.

Logs every API request to the audit_logs table with who/when/what.
Per security-design.md: audit logging on every data access, 2yr retention.

Uses asyncpg directly (not SQLAlchemy) to avoid blocking the request path.
Audit write failures are logged but never block the response.
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


class AuditMiddleware(BaseHTTPMiddleware):
    """Log every request to the audit_logs table."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000

        # Skip health/metrics endpoints to reduce noise
        path = request.url.path
        if path in ("/health", "/ready", "/metrics", "/docs", "/openapi.json"):
            return response

        # Extract user from request state (set by auth dependency)
        user_id = getattr(request.state, "audit_user_id", None)
        username = getattr(request.state, "audit_username", None)

        # Fire-and-forget audit write
        pool = getattr(request.app.state, "db_pool", None)
        if pool is not None:
            try:
                await _write_audit_log(
                    pool=pool,
                    user_id=user_id,
                    action=request.method,
                    resource_type=_resource_type(path),
                    resource_id=_resource_id(path),
                    details={
                        "path": path,
                        "query": str(request.url.query),
                        "status": response.status_code,
                        "latency_ms": round(elapsed_ms, 2),
                        "username": username,
                    },
                    ip_address=_client_ip(request),
                    hostname=_client_hostname(request),
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


__all__ = ["AuditMiddleware", "_write_audit_log", "_client_ip", "_client_hostname"]
