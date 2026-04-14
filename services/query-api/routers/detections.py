"""GET /detections — paginated detection query from TimescaleDB.

Uses raw SQL with explicit time range for TimescaleDB chunk exclusion.
The ``WHERE time >= $start AND time < $end`` predicate lets TimescaleDB
skip chunks outside the window, which is critical for query performance
on the 30-day retention hypertable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from auth.jwt import get_camera_filter, get_current_user, require_role
from schemas import (
    BoundingBox,
    DetectionListResponse,
    DetectionResponse,
    UserClaims,
)
from utils.db import fetch_rows, fetch_val
from utils.minio_urls import generate_signed_url

router = APIRouter(prefix="/detections", tags=["detections"])


@router.get(
    "/counts",
    dependencies=[require_role("admin", "operator", "viewer", "engineering")],
)
async def detection_counts(
    request: Request,
    camera_id: Optional[str] = Query(None, description="Filter by camera ID (comma-separated for multiple)"),
    user: UserClaims = Depends(get_current_user),
) -> dict[str, int]:
    """Return detection counts grouped by object_class for the current scope."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool
    camera_filter = get_camera_filter(user)

    conditions: list[str] = []
    args: list[object] = []
    param_idx = 0

    if camera_id:
        cams = [c.strip() for c in camera_id.split(",") if c.strip()]
        if len(cams) == 1:
            param_idx += 1
            conditions.append(f"camera_id = ${param_idx}")
            args.append(cams[0])
        elif len(cams) > 1:
            param_idx += 1
            conditions.append(f"camera_id = ANY(${param_idx})")
            args.append(cams)

    if camera_filter is not None:
        param_idx += 1
        conditions.append(f"camera_id = ANY(${param_idx})")
        args.append(camera_filter)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    rows = await fetch_rows(
        pool,
        f"SELECT object_class, COUNT(*) AS count FROM detections {where} GROUP BY object_class",
        args,
        query_type="detection_counts",
    )
    return {row["object_class"]: int(row["count"]) for row in rows}


@router.get(
    "",
    response_model=DetectionListResponse,
    dependencies=[require_role("admin", "operator", "viewer", "engineering")],
)
async def list_detections(
    request: Request,
    camera_id: Optional[str] = Query(None, description="Filter by camera ID"),
    start: Optional[datetime] = Query(None, description="Start time (inclusive)"),
    end: Optional[datetime] = Query(None, description="End time (exclusive)"),
    object_class: Optional[str] = Query(None, alias="class", description="Filter by object class"),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0, description="Minimum confidence"),
    has_thumbnail: Optional[bool] = Query(None, description="Filter to detections with (true) or without (false) thumbnails"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=1000),
    user: UserClaims = Depends(get_current_user),
) -> DetectionListResponse:
    """Query detections with filtering and pagination."""
    # Tag request for audit logging
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool
    camera_filter = get_camera_filter(user)

    # Build parameterized query
    conditions: list[str] = []
    args: list[object] = []
    param_idx = 0

    # Time range for chunk exclusion (critical for TimescaleDB performance)
    if start:
        param_idx += 1
        conditions.append(f"time >= ${param_idx}")
        args.append(start)
    if end:
        param_idx += 1
        conditions.append(f"time < ${param_idx}")
        args.append(end)

    # Camera scope filtering
    if camera_id:
        cams = [c.strip() for c in camera_id.split(",") if c.strip()]
        if len(cams) == 1:
            param_idx += 1
            conditions.append(f"camera_id = ${param_idx}")
            args.append(cams[0])
        elif len(cams) > 1:
            param_idx += 1
            conditions.append(f"camera_id = ANY(${param_idx})")
            args.append(cams)
    if camera_filter is not None:
        param_idx += 1
        conditions.append(f"camera_id = ANY(${param_idx})")
        args.append(camera_filter)

    if object_class:
        classes = [c.strip() for c in object_class.split(",") if c.strip()]
        if len(classes) == 1:
            param_idx += 1
            conditions.append(f"object_class = ${param_idx}")
            args.append(classes[0])
        elif len(classes) > 1:
            param_idx += 1
            conditions.append(f"object_class = ANY(${param_idx})")
            args.append(classes)

    if min_confidence is not None:
        param_idx += 1
        conditions.append(f"confidence >= ${param_idx}")
        args.append(min_confidence)

    if has_thumbnail is True:
        conditions.append("thumbnail_uri IS NOT NULL")
    elif has_thumbnail is False:
        conditions.append("thumbnail_uri IS NULL")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # Count query
    count_sql = f"SELECT COUNT(*) FROM detections {where}"
    total = await fetch_val(pool, count_sql, args, query_type="detections_count")

    # Data query with pagination
    param_idx += 1
    limit_param = param_idx
    param_idx += 1
    offset_param = param_idx
    data_args = [*args, limit, offset]

    data_sql = f"""
        SELECT time, camera_id, frame_seq, object_class, confidence,
               bbox_x, bbox_y, bbox_w, bbox_h, local_track_id, model_version,
               thumbnail_uri
        FROM detections
        {where}
        ORDER BY time DESC
        LIMIT ${limit_param} OFFSET ${offset_param}
    """

    rows = await fetch_rows(pool, data_sql, data_args, query_type="detections")
    minio_client = request.app.state.minio_client

    detections = [
        DetectionResponse(
            time=row["time"],
            camera_id=row["camera_id"],
            frame_seq=row["frame_seq"],
            object_class=row["object_class"],
            confidence=row["confidence"],
            bbox=BoundingBox(
                x=row["bbox_x"],
                y=row["bbox_y"],
                w=row["bbox_w"],
                h=row["bbox_h"],
            ),
            local_track_id=str(row["local_track_id"]) if row["local_track_id"] else None,
            model_version=row["model_version"],
            thumbnail_url=(
                generate_signed_url(minio_client, row["thumbnail_uri"], request=request)
                if row["thumbnail_uri"]
                else None
            ),
        )
        for row in rows
    ]

    return DetectionListResponse(
        detections=detections,
        total=total or 0,
        offset=offset,
        limit=limit,
    )
