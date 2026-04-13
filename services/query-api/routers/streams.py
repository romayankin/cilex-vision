"""GET /streams — return browser-playable stream URLs for all cameras."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Request

from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/streams", tags=["streams"])


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
