"""GET /debug/traces — debug trace listing and retrieval from MinIO.

Restricted to engineering and admin roles.  Traces are stored by the
inference worker's TraceCollector in the ``debug-traces`` MinIO bucket
with key format ``{camera_id}/{date}/{trace_id}.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from auth.jwt import get_current_user, require_role
from schemas import (
    DebugTraceListResponse,
    DebugTraceSummary,
    UserClaims,
)
from utils.minio_urls import generate_signed_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get(
    "/traces",
    response_model=DebugTraceListResponse,
    dependencies=[require_role("engineering", "admin")],
)
async def list_debug_traces(
    request: Request,
    camera_id: str = Query(..., description="Camera ID (required)"),
    start: Optional[str] = Query(None, description="Start date inclusive (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date inclusive (YYYY-MM-DD)"),
    track_id: Optional[str] = Query(None, description="Filter by track ID"),
    limit: int = Query(50, ge=1, le=500),
    user: UserClaims = Depends(get_current_user),
) -> DebugTraceListResponse:
    """List debug traces from MinIO with signed URLs."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    minio_client = request.app.state.minio_client
    settings = request.app.state.settings

    if minio_client is None:
        return DebugTraceListResponse(traces=[], total=0)

    bucket = settings.minio.debug_bucket
    prefix = f"{camera_id}/"

    traces: list[DebugTraceSummary] = []
    try:
        objects = await asyncio.to_thread(
            minio_client.list_objects,
            bucket,
            prefix=prefix,
            recursive=True,
        )

        for obj in objects:
            parts = obj.object_name.split("/")
            if len(parts) != 3 or not parts[2].endswith(".json"):
                continue

            obj_camera_id = parts[0]
            obj_date = parts[1]
            trace_id = parts[2][:-5]  # strip .json

            # Date range filtering
            if start and obj_date < start:
                continue
            if end and obj_date > end:
                continue

            url = generate_signed_url(
                minio_client,
                f"s3://{bucket}/{obj.object_name}",
                expiry_s=settings.minio.signed_url_expiry_s,
            )

            traces.append(
                DebugTraceSummary(
                    trace_id=trace_id,
                    camera_id=obj_camera_id,
                    date=obj_date,
                    url=url,
                    size_bytes=obj.size,
                )
            )

            if len(traces) >= limit:
                break
    except Exception:
        logger.warning("Failed to list debug traces", exc_info=True)

    # track_id post-filter: download matching traces to check track_ids
    if track_id and traces:
        filtered: list[DebugTraceSummary] = []
        for t in traces:
            obj_name = f"{t.camera_id}/{t.date}/{t.trace_id}.json"
            try:
                response = await asyncio.to_thread(
                    minio_client.get_object, bucket, obj_name
                )
                data = json.loads(response.read())
                response.close()
                response.release_conn()
                if track_id in data.get("track_ids", []):
                    filtered.append(t)
            except Exception:
                continue
        traces = filtered

    return DebugTraceListResponse(traces=traces, total=len(traces))


@router.get(
    "/traces/{trace_id}",
    dependencies=[require_role("engineering", "admin")],
)
async def get_debug_trace(
    request: Request,
    trace_id: str,
    camera_id: str = Query(..., description="Camera ID"),
    date: Optional[str] = Query(None, description="Trace date (YYYY-MM-DD)"),
    user: UserClaims = Depends(get_current_user),
) -> dict:
    """Fetch full debug trace JSON from MinIO."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    minio_client = request.app.state.minio_client
    settings = request.app.state.settings

    if minio_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MinIO not configured",
        )

    bucket = settings.minio.debug_bucket

    # Direct lookup when date is provided
    if date:
        object_name = f"{camera_id}/{date}/{trace_id}.json"
        try:
            response = await asyncio.to_thread(
                minio_client.get_object, bucket, object_name
            )
            data = json.loads(response.read())
            response.close()
            response.release_conn()
            return data
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Trace not found",
            )

    # Without date, scan camera prefix for the trace_id
    prefix = f"{camera_id}/"
    try:
        objects = await asyncio.to_thread(
            minio_client.list_objects, bucket, prefix=prefix, recursive=True
        )
        for obj in objects:
            if obj.object_name.endswith(f"/{trace_id}.json"):
                response = await asyncio.to_thread(
                    minio_client.get_object, bucket, obj.object_name
                )
                data = json.loads(response.read())
                response.close()
                response.release_conn()
                return data
    except Exception:
        pass

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Trace not found",
    )
