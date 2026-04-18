"""Admin-controlled service toggles (enable/disable optional services).

Starts/stops Docker containers based on the settings. Core services
(Kafka, Postgres, etc.) are never exposed — the seeded service_toggles
rows are the only surface area.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth.audit import _client_hostname, _client_ip, _write_audit_log
from auth.jwt import get_current_user
from schemas import UserClaims
from utils.db import fetch_rows
from utils.docker_client import get_docker_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/toggles", tags=["toggles"])


class ServiceToggle(BaseModel):
    service_name: str
    enabled: bool
    description: str | None = None
    impact: str | None = None
    ram_savings_mb: int | None = None
    container_status: str | None = None
    updated_at: datetime | None = None


class ToggleUpdateRequest(BaseModel):
    enabled: bool


class ToggleListResponse(BaseModel):
    toggles: list[ServiceToggle]


def _require_admin(user: UserClaims) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


async def _get_container_status(service_name: str) -> str:
    """Best-effort container state lookup. Returns 'not_found' if missing."""

    def _lookup() -> str:
        client = get_docker_client()
        try:
            container = client.containers.get(service_name)
            return container.status
        except Exception:
            return "not_found"
        finally:
            client.close()

    try:
        return await asyncio.to_thread(_lookup)
    except Exception as exc:
        logger.debug("container status lookup failed for %s: %s", service_name, exc)
        return "unknown"


@router.get("", response_model=ToggleListResponse)
async def list_toggles(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> ToggleListResponse:
    _require_admin(user)
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool
    rows = await fetch_rows(
        pool,
        "SELECT service_name, enabled, description, impact, ram_savings_mb, updated_at "
        "FROM service_toggles ORDER BY service_name",
        [],
        query_type="list_toggles",
    )

    toggles: list[ServiceToggle] = []
    for r in rows:
        status_str = await _get_container_status(r["service_name"])
        toggles.append(
            ServiceToggle(
                service_name=r["service_name"],
                enabled=r["enabled"],
                description=r["description"],
                impact=r["impact"],
                ram_savings_mb=r["ram_savings_mb"],
                container_status=status_str,
                updated_at=r["updated_at"],
            )
        )

    return ToggleListResponse(toggles=toggles)


@router.put("/{service_name}", response_model=ServiceToggle)
async def update_toggle(
    service_name: str,
    body: ToggleUpdateRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> ServiceToggle:
    _require_admin(user)
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool

    rows = await fetch_rows(
        pool,
        "SELECT service_name, description, impact, ram_savings_mb "
        "FROM service_toggles WHERE service_name = $1",
        [service_name],
        query_type="get_toggle",
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"Service {service_name} is not toggleable",
        )

    await fetch_rows(
        pool,
        "UPDATE service_toggles "
        "SET enabled = $2, updated_at = NOW(), updated_by = $3 "
        "WHERE service_name = $1",
        [service_name, body.enabled, user.username],
        query_type="update_toggle",
    )

    def _apply() -> tuple[bool, str]:
        client = get_docker_client()
        try:
            container = client.containers.get(service_name)
            if body.enabled:
                if container.status != "running":
                    container.start()
                    return True, "started"
                return True, "already running"
            if container.status == "running":
                container.stop(timeout=30)
                return True, "stopped"
            return True, f"already {container.status}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            client.close()

    success, message = await asyncio.to_thread(_apply)
    logger.info(
        "toggle %s enabled=%s by=%s result=%s",
        service_name,
        body.enabled,
        user.username,
        message,
    )

    try:
        await _write_audit_log(
            pool=pool,
            user_id=user.user_id,
            action="SERVICE_TOGGLE_SUCCESS" if success else "SERVICE_TOGGLE_FAILED",
            resource_type="service_toggle",
            resource_id=service_name,
            details={
                "username": user.username,
                "enabled": body.enabled,
                "result": message,
            },
            ip_address=_client_ip(request),
            hostname=_client_hostname(request),
        )
        request.state.audit_written = True
    except Exception:
        logger.warning("Audit write (service toggle) failed", exc_info=True)

    if not success:
        raise HTTPException(
            status_code=500,
            detail=f"Docker action failed: {message}",
        )

    status_str = await _get_container_status(service_name)
    row = rows[0]
    return ServiceToggle(
        service_name=service_name,
        enabled=body.enabled,
        description=row["description"],
        impact=row["impact"],
        ram_savings_mb=row["ram_savings_mb"],
        container_status=status_str,
        updated_at=datetime.now(timezone.utc),
    )
