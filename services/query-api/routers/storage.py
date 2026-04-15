"""Admin-only MinIO storage management endpoints.

GET  /storage/buckets   — bucket sizes, object counts, cluster usage
GET  /storage/config    — bucket purposes and configuration notes
POST /storage/purge     — delete objects older than N hours from a bucket

Bucket sizes come from MinIO's native Prometheus metrics
(/minio/v2/metrics/bucket) — walking list_objects on frame-blobs (millions
of JPEGs) is too slow to block an HTTP request. For purge we do need to
walk, so those requests may take minutes for large buckets.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth.audit import _client_hostname, _client_ip, _write_audit_log
from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage", tags=["storage"])

MINIO_METRICS_BUCKET = "http://minio:9000/minio/v2/metrics/bucket"
MINIO_METRICS_CLUSTER = "http://minio:9000/minio/v2/metrics/cluster"
METRICS_TIMEOUT_S = 5.0

# Buckets the UI is allowed to purge — the rest (raw-video, archive-warm,
# mtmc-checkpoints) are either long-lived data or safety-critical.
ALLOWED_PURGE_BUCKETS = {
    "frame-blobs",
    "decoded-frames",
    "debug-traces",
    "thumbnails",
    "event-clips",
}

BUCKET_CATALOG = [
    {
        "name": "frame-blobs",
        "purpose": "Camera snapshots captured when motion is detected — individual JPEG photos, not video",
        "retention_days": 7,
        "planned": False,
    },
    {
        "name": "decoded-frames",
        "purpose": "Smaller copies of camera snapshots, resized for the AI detector to process",
        "retention_days": 3,
        "planned": False,
    },
    {
        "name": "event-clips",
        "purpose": "Short video clips stitched together when an event occurs (e.g., person enters and leaves a zone)",
        "retention_days": 90,
        "planned": False,
    },
    {
        "name": "debug-traces",
        "purpose": "Diagnostic snapshots used by engineers to debug AI detection issues",
        "retention_days": 30,
        "planned": False,
    },
    {
        "name": "thumbnails",
        "purpose": "Cropped images of detected objects — a person or vehicle cut out from the full frame",
        "retention_days": 30,
        "planned": False,
    },
    {
        "name": "archive-warm",
        "purpose": "Planned feature — automatic archival of old data to slower/cheaper storage",
        "retention_days": None,
        "planned": True,
    },
    {
        "name": "raw-video",
        "purpose": "Planned feature — continuous DVR-style video recording",
        "retention_days": 30,
        "planned": True,
    },
    {
        "name": "mtmc-checkpoints",
        "purpose": "State files for tracking the same person across multiple cameras",
        "retention_days": None,
        "planned": False,
    },
]


@dataclass
class PurgeState:
    active: bool = False
    bucket: str = ""
    started_by: str = ""
    started_at: str = ""
    older_than_hours: int = 0
    deleted: int = 0
    freed: int = 0
    initial_size: int = 0
    cancel_requested: threading.Event = field(default_factory=threading.Event)


_purge_state = PurgeState()
_purge_lock = threading.Lock()


def _human_size(size_bytes: float) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _parse_prom_line(line: str) -> tuple[str, dict[str, str], float] | None:
    """Very small Prometheus text-format parser for label={"k": "v"} name value."""
    try:
        head, _, value_str = line.rpartition(" ")
        value = float(value_str)
    except ValueError:
        return None
    if "{" in head:
        name, _, rest = head.partition("{")
        labels_str, _, _ = rest.partition("}")
        labels: dict[str, str] = {}
        for part in labels_str.split(","):
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            labels[k.strip()] = v.strip().strip('"')
    else:
        name = head
        labels = {}
    return name, labels, value


async def _fetch_minio_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("MinIO metrics fetch failed: %s", exc)
        return None
    if resp.status_code != 200:
        return None
    return resp.text


@router.get("")
async def get_storage_status(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the live storage watchdog stats + human-readable fields."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    watchdog = getattr(request.app.state, "storage_watchdog", None)
    if watchdog is None:
        raise HTTPException(status_code=503, detail="Watchdog not initialised")

    stats = watchdog.stats
    if not stats:
        return {
            "quota_percent": watchdog.quota_percent,
            "ready": False,
            "message": "Watchdog has not completed its first check yet",
        }

    bucket_sizes_human = {
        name: _human_size(size) for name, size in stats["bucket_sizes"].items()
    }
    return {
        **stats,
        "disk_total_human": _human_size(stats["disk_total"]),
        "disk_used_human": _human_size(stats["disk_used"]),
        "disk_free_human": _human_size(stats["disk_free"]),
        "non_video_used_human": _human_size(stats["non_video_used"]),
        "assignable_human": _human_size(stats["assignable"]),
        "video_bytes_human": _human_size(stats["video_bytes"]),
        "quota_bytes_human": _human_size(stats["quota_bytes"]),
        "bucket_sizes_human": bucket_sizes_human,
        "ready": True,
    }


class QuotaUpdateRequest(BaseModel):
    percent: int = Field(ge=10, le=90)


@router.put("/quota")
async def update_quota(
    body: QuotaUpdateRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Change the video storage quota at runtime."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    watchdog = getattr(request.app.state, "storage_watchdog", None)
    if watchdog is None:
        raise HTTPException(status_code=503, detail="Watchdog not initialised")

    old_percent = watchdog.quota_percent
    new_percent = watchdog.set_quota_percent(body.percent)
    logger.info("Storage quota updated to %d%% by %s", new_percent, user.username)

    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            await _write_audit_log(
                pool=pool,
                user_id=user.user_id,
                action="UPDATE",
                resource_type="storage_quota",
                resource_id="quota_percent",
                details={
                    "description": f"Storage quota changed from {old_percent}% to {new_percent}%",
                    "username": user.username,
                    "old_value": old_percent,
                    "new_value": new_percent,
                },
                ip_address=_client_ip(request),
                hostname=_client_hostname(request),
            )
            request.state.audit_written = True
        except Exception:
            logger.warning("Audit write (quota update) failed", exc_info=True)

    return {"quota_percent": new_percent}


@router.get("/buckets")
async def list_buckets(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Return size, object count, and creation date for each MinIO bucket."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    minio_client = request.app.state.minio_client
    if minio_client is None:
        raise HTTPException(status_code=503, detail="MinIO client not initialised")

    # Bucket list + creation dates from S3 API (one small call).
    try:
        raw_buckets = await asyncio.to_thread(minio_client.list_buckets)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MinIO list failed: {exc}")

    bucket_sizes: dict[str, float] = {}
    bucket_objects: dict[str, float] = {}
    cluster_total = 0.0
    cluster_free = 0.0

    async with httpx.AsyncClient(timeout=METRICS_TIMEOUT_S) as client:
        bucket_text = await _fetch_minio_text(client, MINIO_METRICS_BUCKET)
        cluster_text = await _fetch_minio_text(client, MINIO_METRICS_CLUSTER)

    if bucket_text:
        for line in bucket_text.splitlines():
            if not line or line.startswith("#"):
                continue
            parsed = _parse_prom_line(line)
            if parsed is None:
                continue
            name, labels, value = parsed
            bucket = labels.get("bucket")
            if not bucket:
                continue
            if name == "minio_bucket_usage_total_bytes":
                bucket_sizes[bucket] = value
            elif name == "minio_bucket_usage_object_total":
                bucket_objects[bucket] = value

    if cluster_text:
        for line in cluster_text.splitlines():
            if not line or line.startswith("#"):
                continue
            parsed = _parse_prom_line(line)
            if parsed is None:
                continue
            name, _, value = parsed
            if name == "minio_cluster_capacity_usable_total_bytes":
                cluster_total = value
            elif name == "minio_cluster_capacity_usable_free_bytes":
                cluster_free = value

    buckets = []
    total_used = 0.0
    for bucket in raw_buckets:
        size = bucket_sizes.get(bucket.name, 0.0)
        count = int(bucket_objects.get(bucket.name, 0))
        total_used += size
        buckets.append({
            "name": bucket.name,
            "size_bytes": int(size),
            "size_human": _human_size(size),
            "object_count": count,
            "created": bucket.creation_date.isoformat() if bucket.creation_date else None,
            "purgeable": bucket.name in ALLOWED_PURGE_BUCKETS,
        })

    buckets.sort(key=lambda b: b["size_bytes"], reverse=True)

    return {
        "buckets": buckets,
        "total_used_bytes": int(total_used),
        "total_used_human": _human_size(total_used),
        "cluster_total_bytes": int(cluster_total),
        "cluster_total_human": _human_size(cluster_total) if cluster_total else None,
        "cluster_free_bytes": int(cluster_free),
        "cluster_free_human": _human_size(cluster_free) if cluster_total else None,
        "usage_percent": round((cluster_total - cluster_free) / cluster_total * 100, 1)
        if cluster_total
        else None,
    }


@router.get("/config")
async def get_storage_config(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the static storage configuration so the admin UI can explain it."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    return {
        "endpoint": "minio:9000",
        "console_port": 9001,
        "buckets": BUCKET_CATALOG,
        "volume_name": "infra_minio_data",
        "volume_path": "/data",
        "note": (
            "Storage lives on Docker volume 'infra_minio_data' mounted at /data "
            "inside the minio container. To relocate to NFS or a different host "
            "path, edit the minio service in infra/docker-compose.yml."
        ),
    }


class PurgeRequest(BaseModel):
    bucket: str
    older_than_hours: int = Field(ge=0, le=24 * 365)


def _do_purge(
    minio_client: Any,
    bucket: str,
    cutoff: datetime,
    state: PurgeState,
) -> tuple[int, int, bool]:
    """Blocking purge loop with progress reporting and cancellation.

    Returns (deleted_count, freed_bytes, cancelled). Progress is written
    to ``state`` after each 1000-object batch so pollers see it in real time.
    """
    from minio.deleteobjects import DeleteObject  # noqa: PLC0415

    deleted = 0
    freed = 0
    pending: list[DeleteObject] = []
    cancelled = False

    def flush(batch: list[DeleteObject]) -> None:
        errors = list(minio_client.remove_objects(bucket, batch))
        for err in errors:
            logger.warning("purge delete error in %s: %s", bucket, err)

    for obj in minio_client.list_objects(bucket, recursive=True):
        if state.cancel_requested.is_set():
            cancelled = True
            break

        if obj.last_modified is None or obj.last_modified >= cutoff:
            continue

        pending.append(DeleteObject(obj.object_name))
        freed += obj.size or 0
        deleted += 1

        if len(pending) >= 1000:
            flush(pending)
            pending = []
            state.deleted = deleted
            state.freed = freed

    if pending and not cancelled:
        flush(pending)

    state.deleted = deleted
    state.freed = freed

    return deleted, freed, cancelled


@router.post("/purge")
async def purge_bucket(
    body: PurgeRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete every object in ``body.bucket`` older than ``older_than_hours``.

    ``older_than_hours=0`` purges everything (the "Purge ALL" UI option).
    The walk runs synchronously in a worker thread — for million-object
    buckets this request may take a few minutes; the client should show a
    progress spinner and not time out.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if body.bucket not in ALLOWED_PURGE_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail=f"Bucket '{body.bucket}' is not in the purge allowlist",
        )

    minio_client = request.app.state.minio_client
    if minio_client is None:
        raise HTTPException(status_code=503, detail="MinIO client not initialised")

    with _purge_lock:
        if _purge_state.active:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "purge_in_progress",
                    "message": (
                        f"A purge is already running on bucket '{_purge_state.bucket}' "
                        f"(started by {_purge_state.started_by}). "
                        f"Wait for it to complete or cancel it first."
                    ),
                    "bucket": _purge_state.bucket,
                    "started_by": _purge_state.started_by,
                    "started_at": _purge_state.started_at,
                    "deleted": _purge_state.deleted,
                    "freed": _purge_state.freed,
                },
            )

        initial_size = 0
        watchdog = getattr(request.app.state, "storage_watchdog", None)
        if watchdog is not None and watchdog.stats:
            initial_size = int(
                watchdog.stats.get("bucket_sizes", {}).get(body.bucket, 0)
            )

        _purge_state.active = True
        _purge_state.bucket = body.bucket
        _purge_state.started_by = user.username
        _purge_state.started_at = datetime.now(timezone.utc).isoformat()
        _purge_state.older_than_hours = body.older_than_hours
        _purge_state.deleted = 0
        _purge_state.freed = 0
        _purge_state.initial_size = initial_size
        _purge_state.cancel_requested.clear()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=body.older_than_hours)

    try:
        deleted, freed, cancelled = await asyncio.to_thread(
            _do_purge, minio_client, body.bucket, cutoff, _purge_state
        )
    finally:
        with _purge_lock:
            _purge_state.active = False

    logger.info(
        "Purge on bucket=%s older_than_hours=%d: deleted=%d freed=%s cancelled=%s",
        body.bucket, body.older_than_hours, deleted, _human_size(freed), cancelled,
    )

    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        hostname = _client_hostname(request)
        if cancelled:
            description = (
                f"Purge CANCELLED on bucket '{body.bucket}' "
                f"after deleting {deleted} objects"
            )
        elif body.older_than_hours == 0:
            description = f"Purge ALL objects from bucket '{body.bucket}'"
        else:
            description = (
                f"Purge objects older than {body.older_than_hours}h "
                f"from bucket '{body.bucket}'"
            )
        try:
            await _write_audit_log(
                pool=pool,
                user_id=user.user_id,
                action="PURGE",
                resource_type="storage",
                resource_id=body.bucket,
                details={
                    "description": description,
                    "username": user.username,
                    "bucket": body.bucket,
                    "older_than_hours": body.older_than_hours,
                    "cutoff": cutoff.isoformat(),
                    "deleted_objects": deleted,
                    "freed_bytes": freed,
                    "freed_human": _human_size(freed),
                    "cancelled": cancelled,
                    "client_hostname": hostname,
                },
                ip_address=_client_ip(request),
                hostname=hostname,
            )
            request.state.audit_written = True
        except Exception:
            logger.warning("Audit write (purge) failed", exc_info=True)

    return {
        "bucket": body.bucket,
        "older_than_hours": body.older_than_hours,
        "deleted_objects": deleted,
        "freed_bytes": freed,
        "freed_human": _human_size(freed),
        "cutoff": cutoff.isoformat(),
        "cancelled": cancelled,
    }


@router.get("/purge/status")
async def purge_status(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Return progress for the currently-running purge, if any."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    if not _purge_state.active:
        return {"active": False}

    elapsed = 0.0
    try:
        started = datetime.fromisoformat(_purge_state.started_at)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    except Exception:
        pass

    progress_pct = 0.0
    if _purge_state.initial_size > 0:
        progress_pct = min(
            99.0, (_purge_state.freed / _purge_state.initial_size) * 100
        )

    return {
        "active": True,
        "bucket": _purge_state.bucket,
        "started_by": _purge_state.started_by,
        "started_at": _purge_state.started_at,
        "older_than_hours": _purge_state.older_than_hours,
        "deleted": _purge_state.deleted,
        "freed": _purge_state.freed,
        "freed_human": _human_size(_purge_state.freed),
        "initial_size": _purge_state.initial_size,
        "initial_size_human": _human_size(_purge_state.initial_size),
        "progress_pct": round(progress_pct, 1),
        "elapsed_seconds": round(elapsed, 1),
        "cancel_requested": _purge_state.cancel_requested.is_set(),
    }


@router.post("/purge/cancel")
async def cancel_purge(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Request cancellation of the running purge. Already-deleted data is lost."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    if not _purge_state.active:
        raise HTTPException(status_code=400, detail="No purge is currently running")

    bucket = _purge_state.bucket
    deleted_so_far = _purge_state.deleted
    freed_so_far = _purge_state.freed

    _purge_state.cancel_requested.set()

    logger.info(
        "Purge cancel requested by %s for bucket=%s (deleted_so_far=%d)",
        user.username, bucket, deleted_so_far,
    )

    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            await _write_audit_log(
                pool=pool,
                user_id=user.user_id,
                action="CANCEL_PURGE",
                resource_type="storage",
                resource_id=bucket,
                details={
                    "description": (
                        f"Purge cancel requested for bucket '{bucket}'"
                    ),
                    "username": user.username,
                    "deleted_so_far": deleted_so_far,
                    "freed_so_far": freed_so_far,
                    "freed_human": _human_size(freed_so_far),
                },
                ip_address=_client_ip(request),
                hostname=_client_hostname(request),
            )
            request.state.audit_written = True
        except Exception:
            logger.warning("Audit write (purge cancel) failed", exc_info=True)

    return {
        "message": "Cancel requested. The purge will stop after the current batch.",
        "bucket": bucket,
        "deleted_so_far": deleted_so_far,
        "freed_so_far": freed_so_far,
    }
