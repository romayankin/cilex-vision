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


def _do_purge(minio_client: Any, bucket: str, cutoff: datetime) -> tuple[int, int]:
    """Blocking purge loop — call via asyncio.to_thread.

    Returns (deleted_count, freed_bytes). Batches DeleteObject lists of 1000
    since remove_objects streams errors only when the returned generator is
    consumed, which we force here via list().
    """
    from minio.deleteobjects import DeleteObject  # noqa: PLC0415

    deleted = 0
    freed = 0
    pending: list[DeleteObject] = []

    def flush(batch: list[DeleteObject]) -> None:
        errors = list(minio_client.remove_objects(bucket, batch))
        for err in errors:
            logger.warning("purge delete error in %s: %s", bucket, err)

    for obj in minio_client.list_objects(bucket, recursive=True):
        if obj.last_modified is None or obj.last_modified >= cutoff:
            continue
        pending.append(DeleteObject(obj.object_name))
        freed += obj.size or 0
        deleted += 1
        if len(pending) >= 1000:
            flush(pending)
            pending = []

    if pending:
        flush(pending)

    return deleted, freed


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

    cutoff = datetime.now(timezone.utc) - timedelta(hours=body.older_than_hours)

    deleted, freed = await asyncio.to_thread(
        _do_purge, minio_client, body.bucket, cutoff
    )

    logger.info(
        "Purge on bucket=%s older_than_hours=%d: deleted=%d freed=%s",
        body.bucket, body.older_than_hours, deleted, _human_size(freed),
    )

    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        hostname = _client_hostname(request)
        if body.older_than_hours == 0:
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
                    "client_hostname": hostname,
                },
                ip_address=_client_ip(request),
                hostname=hostname,
            )
        except Exception:
            logger.warning("Audit write (purge) failed", exc_info=True)

    return {
        "bucket": body.bucket,
        "older_than_hours": body.older_than_hours,
        "deleted_objects": deleted,
        "freed_bytes": freed,
        "freed_human": _human_size(freed),
        "cutoff": cutoff.isoformat(),
    }
