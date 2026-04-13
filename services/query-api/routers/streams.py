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
    # go2rtc PUT replaces the stream's sources. Use a single ffmpeg source that
    # transcodes the RTSP feed to H.264 at 720p — needed because 1440p H.265
    # can overwhelm browser MSE and strain the CPU.
    src = f"ffmpeg:{rtsp_url}#video=h264#width=1280"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.put(
            f"{GO2RTC_INTERNAL}/api/streams",
            params={"name": camera_id, "src": src},
        )
        # 400 appears when the mounted config is read-only, but the stream is
        # still registered in memory. Only log real failures.
        if resp.status_code >= 500:
            logger.warning("go2rtc PUT failed: %s %s", resp.status_code, resp.text)


async def sync_all_to_go2rtc(pool) -> int:
    """Register DB cameras with go2rtc, skipping those already configured.

    Cameras defined in the mounted config (infra/dev/go2rtc.yaml) already have
    a two-source setup (raw RTSP + ffmpeg transcode) that we must not clobber,
    since PUT on go2rtc replaces the stream's sources.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{GO2RTC_INTERNAL}/api/streams")
            existing = set(resp.json().keys()) if resp.status_code == 200 else set()
        except Exception:
            existing = set()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT camera_id, rtsp_uri FROM cameras "
            "WHERE rtsp_uri IS NOT NULL AND rtsp_uri <> ''"
        )
    count = 0
    for r in rows:
        cam_id = r["camera_id"]
        if cam_id in existing:
            continue
        try:
            await _register_go2rtc(cam_id, r["rtsp_uri"])
            count += 1
        except Exception as exc:
            logger.warning("go2rtc sync failed for %s: %s", cam_id, exc)
    return count


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
