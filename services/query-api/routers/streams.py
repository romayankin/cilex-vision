"""GET /streams — return browser-playable stream URLs for all cameras.

POST /streams/cameras — admin-only, add a camera to the DB and register with go2rtc.
DELETE /streams/cameras/{camera_id} — admin-only, remove a camera and unregister.
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/streams", tags=["streams"])

GO2RTC_INTERNAL = os.environ.get("GO2RTC_INTERNAL_URL", "http://go2rtc:1984")
DEFAULT_SITE_ID = "00000000-0000-0000-0000-000000000001"


def _get_go2rtc_public(request: Request) -> str:
    """Derive the public go2rtc URL from the request or env."""
    public = os.environ.get("GO2RTC_PUBLIC_URL")
    if public:
        return public.rstrip("/")
    host = request.headers.get("host", "localhost").split(":")[0]
    return f"http://{host}:1984"


@router.get("")
async def list_streams(
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Return stream URLs for all cameras the user can access."""
    pool = request.app.state.db_pool
    base = _get_go2rtc_public(request)

    async with pool.acquire() as conn:
        if user.role == "admin":
            rows = await conn.fetch(
                "SELECT camera_id, name, status, rtsp_uri FROM cameras"
            )
        elif user.camera_scope:
            rows = await conn.fetch(
                "SELECT camera_id, name, status, rtsp_uri FROM cameras "
                "WHERE camera_id = ANY($1)",
                user.camera_scope,
            )
        else:
            rows = []

    streams = []
    for r in rows:
        cam_id = r["camera_id"]
        streams.append({
            "camera_id": cam_id,
            "name": r["name"],
            "status": r["status"],
            "has_rtsp": bool(r["rtsp_uri"]),
            "mse_url": f"{base}/api/stream.mp4?src={cam_id}",
            "webrtc_url": f"{base}/api/webrtc?src={cam_id}",
            "hls_url": f"{base}/api/stream.m3u8?src={cam_id}",
            "snapshot_url": f"{base}/api/frame.jpeg?src={cam_id}",
        })

    return {"streams": streams}


class AddCameraRequest(BaseModel):
    camera_id: str
    name: str
    rtsp_url: str
    site_id: str = DEFAULT_SITE_ID


async def _register_go2rtc(camera_id: str, rtsp_url: str) -> None:
    # go2rtc API: name=<stream_id>, src=<source_url>. Multiple PUTs append sources.
    async with httpx.AsyncClient(timeout=5.0) as client:
        r1 = await client.put(
            f"{GO2RTC_INTERNAL}/api/streams",
            params={"name": camera_id, "src": rtsp_url},
        )
        r2 = await client.put(
            f"{GO2RTC_INTERNAL}/api/streams",
            params={"name": camera_id, "src": f"ffmpeg:{camera_id}#video=h264"},
        )
        # go2rtc returns 400 when the mounted config is read-only, but the
        # stream is still registered in memory. Only log real failures.
        for resp in (r1, r2):
            if resp.status_code >= 500:
                logger.warning("go2rtc PUT failed: %s %s", resp.status_code, resp.text)


async def _unregister_go2rtc(camera_id: str) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.delete(
            f"{GO2RTC_INTERNAL}/api/streams",
            params={"src": camera_id},
        )


@router.post("/cameras")
async def add_camera(
    body: AddCameraRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Add a camera: insert into DB + register with go2rtc."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO cameras (camera_id, site_id, name, rtsp_uri, status, config_json)
               VALUES ($1, $2, $3, $4, 'online', '{}'::jsonb)
               ON CONFLICT (camera_id) DO UPDATE
               SET name = EXCLUDED.name, rtsp_uri = EXCLUDED.rtsp_uri, status = 'online'""",
            body.camera_id, body.site_id, body.name, body.rtsp_url,
        )

    try:
        await _register_go2rtc(body.camera_id, body.rtsp_url)
    except Exception as exc:
        logger.warning("Failed to register stream %s with go2rtc: %s", body.camera_id, exc)

    return {"camera_id": body.camera_id, "status": "added"}


@router.delete("/cameras/{camera_id}")
async def remove_camera(
    camera_id: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Remove a camera from DB and go2rtc."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cameras WHERE camera_id = $1", camera_id)

    try:
        await _unregister_go2rtc(camera_id)
    except Exception as exc:
        logger.warning("Failed to unregister stream %s from go2rtc: %s", camera_id, exc)

    return {"camera_id": camera_id, "status": "removed"}
