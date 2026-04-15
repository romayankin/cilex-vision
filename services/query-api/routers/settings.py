"""Runtime-adjustable configuration settings stored in the ``settings`` table.

Exposes admin-only GET/PUT for values a user may want to change without a
config-file edit + redeploy (e.g., inference thumbnail cap). Values take
effect on the next worker restart unless the consuming service polls.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth.audit import _client_hostname, _client_ip, _write_audit_log
from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

THUMBNAIL_MAX_OPTIONS = [1, 5, 10, 20, 50, 100]
THUMBNAIL_ENV_VAR = "INFERENCE_THUMBNAIL__MAX_PER_TRACK"
THUMBNAIL_DEFAULT = 50

INFERENCE_HEALTH_URL = os.environ.get(
    "INFERENCE_HEALTH_URL", "http://inference-worker:9091/health"
)


async def _ensure_settings_table(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


@router.get("/thumbnails")
async def get_thumbnail_settings(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = request.app.state.db_pool
    await _ensure_settings_table(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM settings WHERE key = 'thumbnail_max_per_track'"
        )

    if row is not None:
        try:
            current = int(row["value"])
        except ValueError:
            current = THUMBNAIL_DEFAULT
    else:
        current = int(os.environ.get(THUMBNAIL_ENV_VAR, str(THUMBNAIL_DEFAULT)))

    return {
        "max_per_track": current,
        "options": THUMBNAIL_MAX_OPTIONS,
    }


class ThumbnailSettingsRequest(BaseModel):
    max_per_track: int


@router.put("/thumbnails")
async def update_thumbnail_settings(
    body: ThumbnailSettingsRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    if body.max_per_track not in THUMBNAIL_MAX_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"max_per_track must be one of {THUMBNAIL_MAX_OPTIONS}",
        )

    pool = request.app.state.db_pool
    await _ensure_settings_table(pool)

    async with pool.acquire() as conn:
        prev = await conn.fetchrow(
            "SELECT value FROM settings WHERE key = 'thumbnail_max_per_track'"
        )
        await conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ('thumbnail_max_per_track', $1, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            str(body.max_per_track),
        )

    old_value: int | None = None
    if prev is not None:
        try:
            old_value = int(prev["value"])
        except ValueError:
            old_value = None

    logger.info(
        "thumbnail_max_per_track set to %d by %s",
        body.max_per_track,
        user.username,
    )

    try:
        await _write_audit_log(
            pool=pool,
            user_id=user.user_id,
            action="UPDATE",
            resource_type="settings",
            resource_id="thumbnail_max_per_track",
            details={
                "description": (
                    f"Thumbnail setting changed to {body.max_per_track} per track"
                ),
                "username": user.username,
                "old_value": old_value,
                "new_value": body.max_per_track,
            },
            ip_address=_client_ip(request),
            hostname=_client_hostname(request),
        )
        request.state.audit_written = True
    except Exception:
        logger.warning("Audit write (settings) failed", exc_info=True)

    return {
        "max_per_track": body.max_per_track,
        "note": "Setting saved. The detection service will pick it up within 30 seconds.",
    }


@router.get("/thumbnails/status")
async def thumbnail_setting_status(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Check whether the inference worker has picked up the current setting."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = request.app.state.db_pool
    await _ensure_settings_table(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM settings WHERE key = 'thumbnail_max_per_track'"
        )
    db_value: int | None
    if row is not None:
        try:
            db_value = int(row["value"])
        except (ValueError, TypeError):
            db_value = None
    else:
        db_value = None

    worker_value: int | None = None
    worker_reachable = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(INFERENCE_HEALTH_URL)
            if resp.status_code == 200:
                data = resp.json()
                raw = data.get("thumbnail_max_per_track")
                if raw is not None:
                    try:
                        worker_value = int(raw)
                    except (ValueError, TypeError):
                        worker_value = None
                worker_reachable = True
    except Exception:
        pass

    synced = (
        worker_reachable
        and db_value is not None
        and worker_value == db_value
    )

    return {
        "db_value": db_value,
        "worker_value": worker_value,
        "worker_reachable": worker_reachable,
        "synced": synced,
    }


@router.get("/access-log")
async def get_access_log_settings(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = request.app.state.db_pool
    await _ensure_settings_table(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM settings WHERE key = 'access_log_enabled'"
        )

    enabled = row is not None and str(row["value"]).lower() in ("true", "1", "yes")
    return {"enabled": enabled}


class AccessLogSettingsRequest(BaseModel):
    enabled: bool


@router.put("/access-log")
async def update_access_log_settings(
    body: AccessLogSettingsRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = request.app.state.db_pool
    await _ensure_settings_table(pool)

    async with pool.acquire() as conn:
        prev = await conn.fetchrow(
            "SELECT value FROM settings WHERE key = 'access_log_enabled'"
        )
        await conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ('access_log_enabled', $1, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            "true" if body.enabled else "false",
        )

    old_enabled = (
        prev is not None and str(prev["value"]).lower() in ("true", "1", "yes")
    )

    logger.info(
        "access_log_enabled set to %s by %s", body.enabled, user.username
    )

    try:
        await _write_audit_log(
            pool=pool,
            user_id=user.user_id,
            action="UPDATE",
            resource_type="settings",
            resource_id="access_log_enabled",
            details={
                "description": (
                    f"Access log {'enabled' if body.enabled else 'disabled'}"
                ),
                "username": user.username,
                "old_value": old_enabled,
                "new_value": body.enabled,
            },
            ip_address=_client_ip(request),
            hostname=_client_hostname(request),
        )
        request.state.audit_written = True
    except Exception:
        logger.warning("Audit write (access-log setting) failed", exc_info=True)

    return {"enabled": body.enabled}
