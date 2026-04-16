"""Camera zone configuration — ROI and loitering zones.

GET  /cameras/{camera_id}/zones   — return zone config
PUT  /cameras/{camera_id}/zones   — update zone config
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth.audit import _client_hostname, _client_ip, _write_audit_log
from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)
router = APIRouter(tags=["zones"])


class ZoneConfig(BaseModel):
    roi: list[list[float]] | None = None
    loitering_zones: list[dict[str, Any]] | None = None


@router.get("/cameras/context")
async def cameras_context(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Camera + zone metadata for AI search context.

    Returns one row per camera with its location_description and the
    list of loitering zones (zone_id, name, duration_s) so the AI
    layer can map natural-language queries like "server room" to the
    underlying zone_id without re-reading config_json.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT camera_id, name, location_description, config_json "
            "FROM cameras ORDER BY camera_id"
        )

    cameras: list[dict[str, Any]] = []
    for row in rows:
        config = row["config_json"] or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}

        zones: list[dict[str, Any]] = []
        for z in config.get("loitering_zones") or []:
            if not isinstance(z, dict):
                continue
            zones.append(
                {
                    "zone_id": z.get("zone_id", ""),
                    "name": z.get("name", ""),
                    "duration_s": z.get("duration_s", 0),
                }
            )

        cameras.append(
            {
                "camera_id": row["camera_id"],
                "name": row["name"],
                "location_description": row["location_description"] or "",
                "zones": zones,
            }
        )

    return {"cameras": cameras}


@router.get("/cameras/{camera_id}/zones")
async def get_camera_zones(
    camera_id: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT config_json FROM cameras WHERE camera_id = $1", camera_id
        )

    if row is None:
        raise HTTPException(404, f"Camera '{camera_id}' not found")

    config = row["config_json"] or {}
    if isinstance(config, str):
        config = json.loads(config)

    return {
        "camera_id": camera_id,
        "roi": config.get("roi"),
        "loitering_zones": config.get("loitering_zones", []),
    }


@router.put("/cameras/{camera_id}/zones")
async def update_camera_zones(
    camera_id: str,
    body: ZoneConfig,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")

    pool = request.app.state.db_pool

    if body.roi is not None:
        if len(body.roi) < 3:
            raise HTTPException(400, "ROI polygon needs at least 3 points")
        for pt in body.roi:
            if len(pt) != 2 or not (0 <= pt[0] <= 1 and 0 <= pt[1] <= 1):
                raise HTTPException(400, "ROI points must be [x, y] in 0-1 range")

    if body.loitering_zones:
        for zone in body.loitering_zones:
            poly = zone.get("polygon", [])
            if len(poly) < 3:
                raise HTTPException(
                    400, "Loitering zone polygon needs at least 3 points"
                )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT config_json FROM cameras WHERE camera_id = $1", camera_id
        )
        if row is None:
            raise HTTPException(404, f"Camera '{camera_id}' not found")

        current = row["config_json"] or {}
        if isinstance(current, str):
            current = json.loads(current)

        if body.roi is not None:
            current["roi"] = body.roi
        if body.loitering_zones is not None:
            current["loitering_zones"] = body.loitering_zones

        await conn.execute(
            "UPDATE cameras SET config_json = $1::jsonb WHERE camera_id = $2",
            json.dumps(current),
            camera_id,
        )

    try:
        await _write_audit_log(
            pool=pool,
            user_id=user.user_id,
            action="UPDATE",
            resource_type="camera_zones",
            resource_id=camera_id,
            details={
                "description": f"Camera zones updated for {camera_id}",
                "username": user.username,
                "roi_points": len(body.roi) if body.roi else 0,
                "loitering_zones": (
                    len(body.loitering_zones) if body.loitering_zones else 0
                ),
            },
            ip_address=_client_ip(request),
            hostname=_client_hostname(request),
        )
        request.state.audit_written = True
    except Exception:
        logger.warning("Audit write (zone update) failed", exc_info=True)

    return {"camera_id": camera_id, "updated": True}
