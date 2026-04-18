"""GET /recording/segments — query continuous recording segments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from auth.jwt import get_current_user
from schemas import UserClaims
from utils.db import fetch_rows

router = APIRouter(prefix="/recording", tags=["recording"])


class VideoSegment(BaseModel):
    segment_id: str
    camera_id: str
    start_time: datetime
    end_time: datetime
    duration_s: float
    tier: str
    storage_uri: str
    bytes: int


class SegmentsResponse(BaseModel):
    segments: list[VideoSegment]
    total: int


@router.get("/segments", response_model=SegmentsResponse)
async def list_segments(
    request: Request,
    camera_id: Optional[str] = Query(None),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    tier: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    user: UserClaims = Depends(get_current_user),
) -> SegmentsResponse:
    pool = request.app.state.db_pool

    if not end:
        end = datetime.now(timezone.utc)
    if not start:
        start = end - timedelta(hours=1)

    where = ["start_time BETWEEN $1 AND $2"]
    params: list = [start, end]
    idx = 3
    if camera_id:
        where.append(f"camera_id = ${idx}")
        params.append(camera_id)
        idx += 1
    if tier:
        where.append(f"tier = ${idx}")
        params.append(tier)
        idx += 1

    sql = f"""
        SELECT segment_id, camera_id, start_time, end_time, duration_s,
               tier, storage_uri, bytes
        FROM video_segments
        WHERE {' AND '.join(where)}
        ORDER BY start_time DESC
        LIMIT ${idx}
    """
    params.append(limit)

    rows = await fetch_rows(pool, sql, params, query_type="list_segments")

    segments = [
        VideoSegment(
            segment_id=str(r["segment_id"]),
            camera_id=r["camera_id"],
            start_time=r["start_time"],
            end_time=r["end_time"],
            duration_s=float(r["duration_s"]),
            tier=r["tier"],
            storage_uri=r["storage_uri"],
            bytes=int(r["bytes"]),
        )
        for r in rows
    ]

    return SegmentsResponse(segments=segments, total=len(segments))
