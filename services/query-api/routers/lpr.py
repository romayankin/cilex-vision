"""GET /lpr/results — search license plate recognition results."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request

from auth.jwt import get_camera_filter, get_current_user, require_role
from schemas import (
    LprResultListResponse,
    LprResultResponse,
    PlateBoundingBox,
    UserClaims,
)
from utils.db import fetch_rows, fetch_val

router = APIRouter(prefix="/lpr", tags=["lpr"])


@router.get(
    "/results",
    response_model=LprResultListResponse,
    dependencies=[require_role("admin", "operator")],
)
async def search_lpr_results(
    request: Request,
    plate_text: str = Query(..., min_length=1, description="Plate text search string."),
    match_mode: Literal["exact", "prefix", "wildcard"] = Query(
        "exact",
        description="Exact match, case-insensitive prefix, or shell-style wildcard search.",
    ),
    camera_id: Optional[str] = Query(None, description="Filter by camera ID"),
    start: Optional[datetime] = Query(None, description="Results after this time"),
    end: Optional[datetime] = Query(None, description="Results before this time"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=1000),
    user: UserClaims = Depends(get_current_user),
) -> LprResultListResponse:
    """Search recognized license plates."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    pool = request.app.state.db_pool
    camera_filter = get_camera_filter(user)

    conditions: list[str] = []
    args: list[object] = []
    param_idx = 0

    if match_mode == "exact":
        param_idx += 1
        conditions.append(f"plate_text = ${param_idx}")
        args.append(plate_text.upper())
    elif match_mode == "prefix":
        param_idx += 1
        conditions.append(f"plate_text ILIKE ${param_idx}")
        args.append(f"{plate_text.upper()}%")
    else:
        pattern = plate_text.upper().replace("*", "%").replace("?", "_")
        param_idx += 1
        conditions.append(f"plate_text ILIKE ${param_idx}")
        args.append(pattern)

    if camera_id:
        param_idx += 1
        conditions.append(f"camera_id = ${param_idx}")
        args.append(camera_id)
    if camera_filter is not None:
        param_idx += 1
        conditions.append(f"camera_id = ANY(${param_idx})")
        args.append(camera_filter)
    if start:
        param_idx += 1
        conditions.append(f"detected_at >= ${param_idx}")
        args.append(start)
    if end:
        param_idx += 1
        conditions.append(f"detected_at < ${param_idx}")
        args.append(end)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    count_sql = f"SELECT COUNT(*) FROM lpr_results {where}"
    total = await fetch_val(pool, count_sql, args, query_type="lpr_results_count")

    param_idx += 1
    limit_param = param_idx
    param_idx += 1
    offset_param = param_idx
    data_args = [*args, limit, offset]

    data_sql = f"""
        SELECT result_id, local_track_id, camera_id, plate_text, plate_confidence,
               country_format, plate_bbox_x, plate_bbox_y, plate_bbox_w, plate_bbox_h,
               detected_at, model_version
        FROM lpr_results
        {where}
        ORDER BY detected_at DESC
        LIMIT ${limit_param} OFFSET ${offset_param}
    """
    rows = await fetch_rows(pool, data_sql, data_args, query_type="lpr_results")

    results = [
        LprResultResponse(
            result_id=str(row["result_id"]),
            local_track_id=str(row["local_track_id"]),
            camera_id=row["camera_id"],
            plate_text=row["plate_text"],
            plate_confidence=row["plate_confidence"],
            country_format=row["country_format"],
            plate_bbox=PlateBoundingBox(
                x=row["plate_bbox_x"],
                y=row["plate_bbox_y"],
                w=row["plate_bbox_w"],
                h=row["plate_bbox_h"],
            ),
            detected_at=row["detected_at"],
            model_version=row["model_version"],
        )
        for row in rows
    ]

    return LprResultListResponse(
        results=results,
        total=total or 0,
        offset=offset,
        limit=limit,
    )
