"""Runtime-adjustable configuration settings stored in the ``settings`` table.

Exposes admin-only GET/PUT for values a user may want to change without a
config-file edit + redeploy (e.g., inference thumbnail cap). Values take
effect on the next worker restart unless the consuming service polls.
"""

from __future__ import annotations

import logging
import os
from typing import Any

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
    except Exception:
        logger.warning("Audit write (settings) failed", exc_info=True)

    return {
        "max_per_track": body.max_per_track,
        "note": "Setting saved. Takes effect on next inference-worker restart.",
    }
