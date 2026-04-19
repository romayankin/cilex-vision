"""GET /events — paginated event query from PostgreSQL.

Events are stored in the ``events`` relational table.  The ``clip_uri``
column contains an ``s3://`` reference that is converted to a signed
MinIO URL (1hr expiry) before returning to the client.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

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
    contains_classes: list[str] = Query(
        default_factory=list,
        description="metadata_jsonb must contain each of these object classes (AND)",
    ),
    colors: list[str] = Query(
        default_factory=list,
        description="metadata_jsonb must contain ANY of these colors across upper/lower/colors",
    ),
    min_duration_s: Optional[float] = Query(
        None, description="motion_interval.duration_s >= this value"
    ),
    max_duration_s: Optional[float] = Query(
        None, description="motion_interval.duration_s <= this value"
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

    # Class containment — uses GIN index idx_events_metadata_gin.
    # Each class becomes its own bound parameter, so values are never
    # interpolated into the SQL string.
    for cls in contains_classes:
        param_idx += 1
        conditions.append(f"e.metadata_jsonb @> ${param_idx}::jsonb")
        args.append(json.dumps({"objects": {cls: {}}}))

    # Color filter — match if any object's attributes contain the color in
    # upper_colors, lower_colors, or colors. Color values are bound parameters
    # (json.dumps wraps user input in JSON-encoded form), never string-pasted.
    for color in colors:
        upper_idx = param_idx + 1
        lower_idx = param_idx + 2
        generic_idx = param_idx + 3
        param_idx += 3
        conditions.append(
            f"""EXISTS (
                SELECT 1 FROM jsonb_each(e.metadata_jsonb -> 'objects') AS o(cls, info)
                WHERE info -> 'attributes' @> ${upper_idx}::jsonb
                   OR info -> 'attributes' @> ${lower_idx}::jsonb
                   OR info -> 'attributes' @> ${generic_idx}::jsonb
            )"""
        )
        args.append(json.dumps({"upper_colors": [color]}))
        args.append(json.dumps({"lower_colors": [color]}))
        args.append(json.dumps({"colors": [color]}))

    if min_duration_s is not None:
        param_idx += 1
        conditions.append(
            f"(e.metadata_jsonb -> 'motion_interval' ->> 'duration_s')::numeric >= ${param_idx}"
        )
        args.append(min_duration_s)
    if max_duration_s is not None:
        param_idx += 1
        conditions.append(
            f"(e.metadata_jsonb -> 'motion_interval' ->> 'duration_s')::numeric <= ${param_idx}"
        )
        args.append(max_duration_s)

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
               e.start_time, e.end_time, e.duration_ms,
               e.clip_uri, e.clip_source_type,
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
            if row["clip_uri"] and (row["clip_source_type"] or "standalone") == "standalone"
            else None,
            clip_uri=row["clip_uri"],
            clip_source_type=row["clip_source_type"],
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


# NOTE: route ordering matters. /timeline MUST be declared BEFORE /{event_id},
# otherwise FastAPI will try to parse the literal "timeline" as a UUID.


@router.get(
    "/timeline",
    dependencies=[require_role("admin", "operator", "viewer")],
)
async def timeline(
    request: Request,
    date: str = Query(..., description="YYYY-MM-DD in the user's local timezone"),
    tz_offset_minutes: int = Query(
        0,
        description="User's timezone offset from UTC in minutes. e.g. +180 for GMT+3, -480 for PST.",
    ),
    camera_ids: list[str] = Query(default_factory=list),
    user: UserClaims = Depends(get_current_user),
):
    """Lightweight response for timeline rendering.

    Returns {camera_id: [event_summaries]} — only fields needed to draw
    blocks, not full metadata. Metadata is fetched on click via /events/{id}.

    date + tz_offset_minutes define the 24-hour window in the user's
    local timezone, converted to UTC before querying.
    """
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool
    camera_filter = get_camera_filter(user)

    try:
        local_midnight = datetime.fromisoformat(date + "T00:00:00+00:00")
    except ValueError as exc:
        raise HTTPException(400, f"Invalid date format: {date}. Expected YYYY-MM-DD.") from exc

    utc_start = local_midnight - timedelta(minutes=tz_offset_minutes)
    utc_end = utc_start + timedelta(days=1)

    where = ["event_type = 'motion'", "start_time >= $1", "start_time < $2"]
    params: list[Any] = [utc_start, utc_end]
    idx = 3

    if camera_ids:
        where.append(f"camera_id = ANY(${idx}::text[])")
        params.append(camera_ids)
        idx += 1

    if camera_filter is not None:
        where.append(f"camera_id = ANY(${idx}::text[])")
        params.append(camera_filter)
        idx += 1

    sql = f"""
        SELECT event_id, camera_id, start_time, end_time, duration_ms, state,
               metadata_jsonb -> 'objects' AS objects_summary
        FROM events
        WHERE {' AND '.join(where)}
        ORDER BY start_time
    """
    rows = await fetch_rows(pool, sql, params, query_type="events_timeline")

    buckets: dict[str, list] = {}
    for r in rows:
        cam = r["camera_id"]
        end = r["end_time"]
        duration_ms = r["duration_ms"]
        if end is None:
            now = datetime.now(timezone.utc)
            end = now
            duration_ms = max(0, int((now - r["start_time"]).total_seconds() * 1000))

        buckets.setdefault(cam, []).append({
            "event_id": str(r["event_id"]),
            "start_time": r["start_time"].isoformat(),
            "end_time": end.isoformat(),
            "duration_ms": duration_ms,
            "state": r["state"],
            "objects_summary": _parse_jsonb(r["objects_summary"]),
        })
    return {
        "date": date,
        "tz_offset_minutes": tz_offset_minutes,
        "utc_start": utc_start.isoformat(),
        "utc_end": utc_end.isoformat(),
        "cameras": buckets,
    }


@router.get(
    "/{event_id}",
    dependencies=[require_role("admin", "operator", "viewer")],
)
async def get_event(
    event_id: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Full detail for a single event, incl. clip_uri and metadata."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool
    camera_filter = get_camera_filter(user)

    try:
        uuid_val = UUID(event_id)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid event_id: {event_id}") from exc

    rows = await fetch_rows(
        pool,
        """
        SELECT event_id, camera_id, start_time, end_time, duration_ms, state,
               clip_uri, clip_source_type, metadata_jsonb
        FROM events
        WHERE event_id = $1::uuid
        """,
        [uuid_val],
        query_type="event_detail",
    )
    if not rows:
        raise HTTPException(404, "Event not found")

    r = rows[0]
    if camera_filter is not None and r["camera_id"] not in camera_filter:
        raise HTTPException(404, "Event not found")

    return {
        "event_id": str(r["event_id"]),
        "camera_id": r["camera_id"],
        "start_time": r["start_time"].isoformat(),
        "end_time": r["end_time"].isoformat() if r["end_time"] else None,
        "duration_ms": r["duration_ms"],
        "state": r["state"],
        "clip_uri": r["clip_uri"],
        "clip_source_type": r["clip_source_type"],
        "metadata": _parse_jsonb(r["metadata_jsonb"]),
    }
