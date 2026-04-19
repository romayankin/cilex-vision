"""Microservice management — health, restart, logs, diagnostics. Admin-only.

GET  /admin/services                    — list all containers + watchdog state
POST /admin/services/{name}/restart     — manual restart (audited)
GET  /admin/services/{name}/logs        — recent log lines
GET  /admin/services/{name}/diagnostics — run diagnostic checks
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth.audit import _client_hostname, _client_ip, _write_audit_log
from auth.jwt import get_current_user
from schemas import UserClaims
from service_watchdog import ONESHOT_CONTAINERS
from utils.docker_client import (
    get_container_logs,
    list_containers,
    restart_container,
    run_diagnostics,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/services", tags=["services"])


def _require_admin(user: UserClaims) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


@router.get("")
async def list_services(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """List every container with status, health, and watchdog tracking."""
    _require_admin(user)
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    containers = await list_containers()

    watchdog = getattr(request.app.state, "service_watchdog", None)
    restart_states = watchdog.get_restart_states() if watchdog else {}

    services: list[dict[str, Any]] = []
    for c in containers:
        svc: dict[str, Any] = {
            "name": c.name,
            "status": c.status,
            "health": c.health,
            "image": c.image,
            "started_at": c.started_at,
            "uptime_seconds": c.uptime_seconds,
            "exit_code": c.exit_code,
            "restart_count": c.restart_count,
            "is_oneshot": c.name in ONESHOT_CONTAINERS,
        }
        rs = restart_states.get(c.name)
        if rs:
            svc["watchdog"] = rs
        services.append(svc)

    return {"services": services}


@router.post("/{name}/restart")
async def manual_restart(
    name: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Manually restart a container. Logged to audit."""
    _require_admin(user)
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    success, message = await restart_container(name)

    watchdog = getattr(request.app.state, "service_watchdog", None)
    if watchdog:
        watchdog.clear_restart_state(name)

    if success:
        try:
            async with request.app.state.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE service_toggles "
                    "SET enabled = true, updated_at = NOW(), updated_by = $1 "
                    "WHERE service_name = $2 AND enabled = false",
                    user.username, name,
                )
        except Exception:
            logger.warning("Failed to clear disabled toggle for %s", name, exc_info=True)

    try:
        await _write_audit_log(
            pool=request.app.state.db_pool,
            user_id=user.user_id,
            action="SERVICE_RESTART_SUCCESS" if success else "SERVICE_RESTART_FAILED",
            resource_type="service",
            resource_id=name,
            details={
                "initiated_by": "manual",
                "username": user.username,
                "container": name,
                "success": success,
                "message": message,
            },
            ip_address=_client_ip(request),
            hostname=_client_hostname(request),
        )
        request.state.audit_written = True
    except Exception:
        logger.warning("Audit write (service restart) failed", exc_info=True)

    if not success:
        raise HTTPException(status_code=500, detail=message)

    return {"name": name, "restarted": True, "message": message}


@router.get("/{name}/logs")
async def service_logs(
    name: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
    tail: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Get recent log lines for a container (capped at 500)."""
    _require_admin(user)
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    logs = await get_container_logs(name, tail=tail)
    return {"name": name, "logs": logs, "tail": tail}


@router.get("/{name}/diagnostics")
async def service_diagnostics(
    name: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Run diagnostic checks for a container."""
    _require_admin(user)
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    results = await run_diagnostics(name)
    return {
        "name": name,
        "diagnostics": [
            {"check": r.check, "status": r.status, "message": r.message, "resolution": r.resolution}
            for r in results
        ],
    }
