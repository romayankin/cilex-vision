"""GET /events — paginated event query from PostgreSQL.

Events are stored in the ``events`` relational table.  The ``clip_uri``
column contains an ``s3://`` reference that is converted to a signed
MinIO URL (1hr expiry) before returning to the client.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from auth.jwt import get_camera_filter, get_current_user, require_role
from schemas import EventListResponse, EventResponse, UserClaims
from utils.db import fetch_rows, fetch_val
from utils.minio_urls import generate_signed_url

router = APIRouter(prefix="/events", tags=["events"])


def _parse_jsonb(value: Any) -> dict | None:
    """asyncpg returns JSONB as a string by default — decode to a dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


@router.get(
    "",
    response_model=EventListResponse,
    dependencies=[require_role("admin", "operator", "viewer")],
)
async def list_events(
    request: Request,
    site_id: Optional[str] = Query(None, description="Filter by site ID"),
    camera_id: Optional[str] = Query(None, description="Filter by camera ID"),
    start: Optional[datetime] = Query(None, description="Events starting after this time"),
    end: Optional[datetime] = Query(None, description="Events starting before this time"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    state: Optional[str] = Query(None, description="Filter by event state"),
    has_clip: Optional[bool] = Query(
        None, description="Filter to events with (true) or without (false) clips"
    ),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=1000),
    user: UserClaims = Depends(get_current_user),
) -> EventListResponse:
    """Query events with filtering and pagination."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool
    camera_filter = get_camera_filter(user)
    settings = request.app.state.settings

    conditions: list[str] = []
    args: list[object] = []
    param_idx = 0

    if camera_id:
        cams = [c.strip() for c in camera_id.split(",") if c.strip()]
        if len(cams) == 1:
            param_idx += 1
            conditions.append(f"e.camera_id = ${param_idx}")
            args.append(cams[0])
        elif len(cams) > 1:
            param_idx += 1
            conditions.append(f"e.camera_id = ANY(${param_idx})")
            args.append(cams)
    if camera_filter is not None:
        param_idx += 1
        conditions.append(f"e.camera_id = ANY(${param_idx})")
        args.append(camera_filter)

    # Site filter via join to cameras table
    if site_id:
        param_idx += 1
        conditions.append(f"c.site_id = ${param_idx}::uuid")
        args.append(site_id)

    if start:
        param_idx += 1
        conditions.append(f"e.start_time >= ${param_idx}")
        args.append(start)
    if end:
        param_idx += 1
        conditions.append(f"e.start_time < ${param_idx}")
        args.append(end)

    if event_type:
        types = [t.strip() for t in event_type.split(",") if t.strip()]
        if len(types) == 1:
            param_idx += 1
            conditions.append(f"e.event_type = ${param_idx}")
            args.append(types[0])
        elif len(types) > 1:
            param_idx += 1
            conditions.append(f"e.event_type = ANY(${param_idx})")
            args.append(types)

    if state:
        states = [s.strip() for s in state.split(",") if s.strip()]
        if len(states) == 1:
            param_idx += 1
            conditions.append(f"e.state = ${param_idx}")
            args.append(states[0])
        elif len(states) > 1:
            param_idx += 1
            conditions.append(f"e.state = ANY(${param_idx})")
            args.append(states)

    if has_clip is not None:
        if has_clip:
            conditions.append("e.clip_uri IS NOT NULL")
        else:
            conditions.append("e.clip_uri IS NULL")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # Use join when site_id filter is present
    join_clause = ""
    if site_id:
        join_clause = "JOIN cameras c ON e.camera_id = c.camera_id"

    count_sql = f"SELECT COUNT(*) FROM events e {join_clause} {where}"
    total = await fetch_val(pool, count_sql, args, query_type="events_count")

    param_idx += 1
    limit_param = param_idx
    param_idx += 1
    offset_param = param_idx
    data_args = [*args, limit, offset]

    data_sql = f"""
        SELECT e.event_id, e.event_type, e.track_id, e.camera_id,
               e.start_time, e.end_time, e.duration_ms, e.clip_uri,
               e.state, e.metadata_jsonb,
               e.source_capture_ts, e.edge_receive_ts, e.core_ingest_ts
        FROM events e
        {join_clause}
        {where}
        ORDER BY e.start_time DESC
        LIMIT ${limit_param} OFFSET ${offset_param}
    """

    rows = await fetch_rows(pool, data_sql, data_args, query_type="events")

    # Generate signed URLs for clip_uri
    minio_client = getattr(request.app.state, "minio_client", None)
    expiry_s = settings.minio.signed_url_expiry_s

    events = [
        EventResponse(
            event_id=str(row["event_id"]),
            event_type=row["event_type"],
            track_id=str(row["track_id"]) if row["track_id"] else None,
            camera_id=row["camera_id"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            duration_ms=row["duration_ms"],
            clip_url=generate_signed_url(minio_client, row["clip_uri"], expiry_s, request=request)
            if row["clip_uri"]
            else None,
            state=row["state"],
            metadata=_parse_jsonb(row["metadata_jsonb"]),
            source_capture_ts=row["source_capture_ts"],
            edge_receive_ts=row["edge_receive_ts"],
            core_ingest_ts=row["core_ingest_ts"],
        )
        for row in rows
    ]

    return EventListResponse(
        events=events,
        total=total or 0,
        offset=offset,
        limit=limit,
    )
