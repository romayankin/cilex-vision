"""GET /audit — admin-only audit log viewer.

Reads rows out of the ``audit_logs`` table written by the AuditMiddleware
and by the rich-audit call sites (purge, quota update, settings,
watchdog auto-purge). Filter/paginate for the admin UI.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["audit"])


def _parse_details(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


@router.get("")
async def list_audit_logs(
    request: Request,
    action: Optional[str] = Query(
        None,
        description="Filter by action: PURGE, AUTO_PURGE, UPDATE, GET, POST, DELETE",
    ),
    resource_type: Optional[str] = Query(
        None, description="Filter by resource type (storage, settings, ...)"
    ),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """List audit log entries. Admin only."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB pool not initialised")

    conditions: list[str] = []
    args: list[object] = []
    idx = 0

    if action:
        idx += 1
        conditions.append(f"action = ${idx}")
        args.append(action)
    if resource_type:
        idx += 1
        conditions.append(f"resource_type = ${idx}")
        args.append(resource_type)
    if start is not None:
        idx += 1
        conditions.append(f"created_at >= ${idx}")
        args.append(start)
    if end is not None:
        idx += 1
        conditions.append(f"created_at <= ${idx}")
        args.append(end)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    list_sql = (
        "SELECT log_id, user_id, action, resource_type, resource_id, "
        "details_jsonb, ip_address, hostname, created_at "
        f"FROM audit_logs {where} "
        f"ORDER BY created_at DESC OFFSET ${idx + 1} LIMIT ${idx + 2}"
    )
    count_sql = f"SELECT COUNT(*) FROM audit_logs {where}"

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_sql, *args)
        rows = await conn.fetch(list_sql, *args, offset, limit)

    logs = []
    for r in rows:
        details = _parse_details(r["details_jsonb"]) or {}
        logs.append({
            "log_id": str(r["log_id"]),
            "user_id": str(r["user_id"]) if r["user_id"] else None,
            "username": details.get("username"),
            "action": r["action"],
            "resource_type": r["resource_type"],
            "resource_id": r["resource_id"],
            "description": details.get("description"),
            "details": details,
            "ip_address": r["ip_address"],
            "hostname": r["hostname"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {
        "logs": logs,
        "total": int(total or 0),
        "offset": offset,
        "limit": limit,
    }


@router.get("/actions")
async def list_audit_actions(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the distinct (action, resource_type) values for UI filters."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB pool not initialised")

    async with pool.acquire() as conn:
        actions = await conn.fetch(
            "SELECT DISTINCT action FROM audit_logs ORDER BY action"
        )
        resources = await conn.fetch(
            "SELECT DISTINCT resource_type FROM audit_logs ORDER BY resource_type"
        )

    return {
        "actions": [r["action"] for r in actions],
        "resource_types": [r["resource_type"] for r in resources],
    }


@router.get("/access")
async def list_access_logs(
    request: Request,
    username: Optional[str] = Query(None),
    path_contains: Optional[str] = Query(
        None, description="Filter by path substring, e.g. 'detections'"
    ),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """List access log entries. Admin only."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB pool not initialised")

    conditions: list[str] = []
    args: list[object] = []
    idx = 0

    if username:
        idx += 1
        conditions.append(f"username = ${idx}")
        args.append(username)
    if path_contains:
        idx += 1
        conditions.append(f"path LIKE ${idx}")
        args.append(f"%{path_contains}%")
    if start is not None:
        idx += 1
        conditions.append(f"created_at >= ${idx}")
        args.append(start)
    if end is not None:
        idx += 1
        conditions.append(f"created_at <= ${idx}")
        args.append(end)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    list_sql = (
        "SELECT id, user_id, username, method, path, query_string, status_code, "
        "latency_ms, ip_address, hostname, created_at "
        f"FROM access_log {where} "
        f"ORDER BY created_at DESC OFFSET ${idx + 1} LIMIT ${idx + 2}"
    )
    count_sql = f"SELECT COUNT(*) FROM access_log {where}"

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_sql, *args)
        rows = await conn.fetch(list_sql, *args, offset, limit)

    logs = [
        {
            "id": int(r["id"]),
            "user_id": str(r["user_id"]) if r["user_id"] else None,
            "username": r["username"],
            "method": r["method"],
            "path": r["path"],
            "query_string": r["query_string"],
            "status_code": r["status_code"],
            "latency_ms": r["latency_ms"],
            "ip_address": r["ip_address"],
            "hostname": r["hostname"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]

    return {
        "logs": logs,
        "total": int(total or 0),
        "offset": offset,
        "limit": limit,
    }


@router.get("/access/stats")
async def access_log_stats(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Access log summary stats for the admin dashboard."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB pool not initialised")

    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM access_log")
        today = await conn.fetchval(
            "SELECT COUNT(*) FROM access_log WHERE created_at >= CURRENT_DATE"
        )
        top_paths = await conn.fetch(
            """
            SELECT path, COUNT(*) AS hits
            FROM access_log
            WHERE created_at >= CURRENT_DATE
            GROUP BY path
            ORDER BY hits DESC
            LIMIT 10
            """
        )
        top_users = await conn.fetch(
            """
            SELECT COALESCE(username, 'anonymous') AS username, COUNT(*) AS hits
            FROM access_log
            WHERE created_at >= CURRENT_DATE
            GROUP BY username
            ORDER BY hits DESC
            LIMIT 10
            """
        )

    return {
        "total_entries": int(total or 0),
        "today_entries": int(today or 0),
        "retention_days": 90,
        "top_paths_today": [
            {"path": r["path"], "hits": int(r["hits"])} for r in top_paths
        ],
        "top_users_today": [
            {"username": r["username"], "hits": int(r["hits"])} for r in top_users
        ],
    }
