"""GET /tracks — paginated track query from PostgreSQL.

Tracks are stored in the ``local_tracks`` relational table (not a
hypertable), so there is no chunk exclusion concern.  Joins to
``track_attributes`` for the detail endpoint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from auth.jwt import get_camera_filter, get_current_user, require_role
from schemas import (
    TrackAttributeResponse,
    TrackDetailResponse,
    TrackListResponse,
    TrackSummaryResponse,
    UserClaims,
)
from utils.db import fetch_rows, fetch_val

router = APIRouter(prefix="/tracks", tags=["tracks"])


@router.get(
    "",
    response_model=TrackListResponse,
    dependencies=[require_role("admin", "operator", "viewer", "engineering")],
)
async def list_tracks(
    request: Request,
    camera_id: Optional[str] = Query(None, description="Filter by camera ID"),
    start: Optional[datetime] = Query(None, description="Tracks starting after this time"),
    end: Optional[datetime] = Query(None, description="Tracks starting before this time"),
    object_class: Optional[str] = Query(None, alias="class", description="Filter by object class"),
    state: Optional[str] = Query(None, description="Filter by track state"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=1000),
    user: UserClaims = Depends(get_current_user),
) -> TrackListResponse:
    """Query local tracks with filtering and pagination."""
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

    if start:
        param_idx += 1
        conditions.append(f"start_time >= ${param_idx}")
        args.append(start)
    if end:
        param_idx += 1
        conditions.append(f"start_time < ${param_idx}")
        args.append(end)

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
    if state:
        states = [s.strip() for s in state.split(",") if s.strip()]
        if len(states) == 1:
            param_idx += 1
            conditions.append(f"state = ${param_idx}")
            args.append(states[0])
        elif len(states) > 1:
            param_idx += 1
            conditions.append(f"state = ANY(${param_idx})")
            args.append(states)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_sql = f"SELECT COUNT(*) FROM local_tracks {where}"
    total = await fetch_val(pool, count_sql, args, query_type="tracks_count")

    param_idx += 1
    limit_param = param_idx
    param_idx += 1
    offset_param = param_idx
    data_args = [*args, limit, offset]

    data_sql = f"""
        SELECT local_track_id, camera_id, object_class, state,
               mean_confidence, start_time, end_time, tracker_version, created_at
        FROM local_tracks
        {where}
        ORDER BY start_time DESC
        LIMIT ${limit_param} OFFSET ${offset_param}
    """

    rows = await fetch_rows(pool, data_sql, data_args, query_type="tracks")

    tracks = [
        TrackSummaryResponse(
            local_track_id=str(row["local_track_id"]),
            camera_id=row["camera_id"],
            object_class=row["object_class"],
            state=row["state"],
            mean_confidence=row["mean_confidence"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            tracker_version=row["tracker_version"],
            created_at=row["created_at"],
        )
        for row in rows
    ]

    return TrackListResponse(
        tracks=tracks,
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{local_track_id}",
    response_model=TrackDetailResponse,
    dependencies=[require_role("admin", "operator", "viewer", "engineering")],
)
async def get_track_detail(
    local_track_id: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> TrackDetailResponse:
    """Get track detail with attributes and thumbnail URL."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool
    camera_filter = get_camera_filter(user)

    # Fetch track
    track_sql = """
        SELECT local_track_id, camera_id, object_class, state,
               mean_confidence, start_time, end_time, tracker_version, created_at
        FROM local_tracks
        WHERE local_track_id = $1
    """
    rows = await fetch_rows(pool, track_sql, [local_track_id], query_type="track_detail")

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track {local_track_id} not found",
        )

    track_row = rows[0]

    # Camera scope check
    if camera_filter is not None and track_row["camera_id"] not in camera_filter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track {local_track_id} not found",
        )

    # Fetch attributes
    attr_sql = """
        SELECT attribute_id, attribute_type, color_value, confidence,
               model_version, observed_at
        FROM track_attributes
        WHERE local_track_id = $1
        ORDER BY observed_at DESC
    """
    attr_rows = await fetch_rows(pool, attr_sql, [local_track_id], query_type="track_attributes")

    attributes = [
        TrackAttributeResponse(
            attribute_id=str(row["attribute_id"]),
            attribute_type=row["attribute_type"],
            color_value=row["color_value"],
            confidence=row["confidence"],
            model_version=row["model_version"],
            observed_at=row["observed_at"],
        )
        for row in attr_rows
    ]

    # Thumbnail generation would require stored frame URIs;
    # for now return None until the frame-reference table exists
    thumbnail_url = None

    return TrackDetailResponse(
        local_track_id=str(track_row["local_track_id"]),
        camera_id=track_row["camera_id"],
        object_class=track_row["object_class"],
        state=track_row["state"],
        mean_confidence=track_row["mean_confidence"],
        start_time=track_row["start_time"],
        end_time=track_row["end_time"],
        tracker_version=track_row["tracker_version"],
        created_at=track_row["created_at"],
        attributes=attributes,
        thumbnail_url=thumbnail_url,
    )
