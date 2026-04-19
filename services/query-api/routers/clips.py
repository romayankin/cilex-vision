"""Clip proxy endpoints for the search UI.

Phase 5 ships the standalone-clip path (`/clips/s3/{key:path}`) so the
new search UI can play motion clips that landed in MinIO via Phase 4's
clip-service motion path.

Phase 9 will add `/clips/range` to this router for segment-range URIs.
"""

from __future__ import annotations

import logging
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth.jwt import get_current_user, require_role
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clips", tags=["clips"])

EVENT_CLIPS_BUCKET = "event-clips"


@router.get(
    "/s3/{key:path}",
    dependencies=[require_role("admin", "operator", "viewer")],
)
async def fetch_s3_clip(
    key: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Stream a standalone clip from the MinIO event-clips bucket."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    if not key or ".." in key or key.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid clip key")

    client = getattr(request.app.state, "minio_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Object storage unavailable")

    try:
        obj = client.get_object(EVENT_CLIPS_BUCKET, key)
    except Exception as exc:  # noqa: BLE001 — minio raises S3Error / others
        logger.warning("clip fetch failed bucket=%s key=%s: %s", EVENT_CLIPS_BUCKET, key, exc)
        raise HTTPException(status_code=404, detail="Clip not found") from exc

    def stream() -> Iterator[bytes]:
        try:
            for chunk in obj.stream(32 * 1024):
                yield chunk
        finally:
            obj.close()
            obj.release_conn()

    return StreamingResponse(stream(), media_type="video/mp4")
