"""Storage tier config — budget, proportions, per-tier quality."""

from __future__ import annotations

import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from auth.jwt import get_current_user
from schemas import UserClaims
from utils.db import fetch_rows

router = APIRouter(prefix="/admin/storage-tiers", tags=["storage_tiers"])

DEFAULT_SITE = "00000000-0000-0000-0000-000000000001"


class TierQuality(BaseModel):
    width: int = Field(..., ge=160, le=7680)
    height: int = Field(..., ge=120, le=4320)
    fps: int = Field(..., ge=1, le=60)
    bitrate_kbps: int = Field(..., ge=64, le=50000)


class StorageTierConfig(BaseModel):
    total_budget_gb: float = Field(..., ge=1)
    hot_fraction: float = Field(..., ge=0.05, le=0.90)
    warm_fraction: float = Field(..., ge=0.05, le=0.90)
    cold_fraction: float = Field(..., ge=0.05, le=0.90)
    hot: TierQuality
    warm: TierQuality
    cold: TierQuality
    storage_backend: str = "volume"
    bind_mount_path: Optional[str] = None


class ComputedRetention(BaseModel):
    tier: str
    gb: float
    hours: float
    pretty_duration: str


class ConfigResponse(BaseModel):
    config: StorageTierConfig
    computed: list[ComputedRetention]
    disk_available_gb: float
    disk_total_gb: float
    num_cameras: int


class TierUsage(BaseModel):
    bytes: int
    segments: int
    oldest: Optional[str] = None
    newest: Optional[str] = None


def _compute_retention_hours(gb: float, bitrate_kbps: int, num_cameras: int) -> float:
    """Hours of footage that fit in `gb` for `num_cameras` at `bitrate_kbps`."""
    bytes_per_sec_total = (bitrate_kbps * 1000 / 8) * max(num_cameras, 1)
    if bytes_per_sec_total <= 0:
        return 0.0
    total_bytes = gb * (1024 ** 3)
    return total_bytes / bytes_per_sec_total / 3600


def _pretty_hours(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}m"
    days = int(hours // 24)
    rem_hours = int(hours % 24)
    mins = int((hours - int(hours)) * 60)
    if days > 0:
        return f"{days}d {rem_hours}h"
    if rem_hours > 0:
        return f"{rem_hours}h {mins}m"
    return f"{mins}m"


def _require_admin(user: UserClaims) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


def _validate_fractions(hot: float, warm: float, cold: float) -> None:
    total = hot + warm + cold
    if not (0.99 <= total <= 1.01):
        raise HTTPException(400, f"Fractions must sum to 1.0 (got {total:.3f})")
    for name, f in [("hot", hot), ("warm", warm), ("cold", cold)]:
        if f < 0.05:
            raise HTTPException(400, f"{name} fraction below minimum 5%")


@router.get("", response_model=ConfigResponse)
async def get_config(
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    _require_admin(user)
    pool = request.app.state.db_pool

    rows = await fetch_rows(
        pool,
        "SELECT * FROM storage_tier_config WHERE site_id = $1::uuid",
        [DEFAULT_SITE],
        query_type="get_tier_config",
    )
    if not rows:
        raise HTTPException(404, "Config not found")
    r = rows[0]

    cam_rows = await fetch_rows(
        pool,
        "SELECT COUNT(*) AS n FROM cameras WHERE status != 'disabled'",
        [],
        query_type="count_cameras",
    )
    num_cams = int(cam_rows[0]["n"]) if cam_rows else 0
    if num_cams == 0:
        num_cams = 2  # fallback for retention estimation when no cameras

    config = StorageTierConfig(
        total_budget_gb=float(r["total_budget_gb"]),
        hot_fraction=float(r["hot_fraction"]),
        warm_fraction=float(r["warm_fraction"]),
        cold_fraction=float(r["cold_fraction"]),
        hot=TierQuality(
            width=r["hot_width"], height=r["hot_height"],
            fps=r["hot_fps"], bitrate_kbps=r["hot_bitrate_kbps"],
        ),
        warm=TierQuality(
            width=r["warm_width"], height=r["warm_height"],
            fps=r["warm_fps"], bitrate_kbps=r["warm_bitrate_kbps"],
        ),
        cold=TierQuality(
            width=r["cold_width"], height=r["cold_height"],
            fps=r["cold_fps"], bitrate_kbps=r["cold_bitrate_kbps"],
        ),
        storage_backend=r["storage_backend"],
        bind_mount_path=r["bind_mount_path"],
    )

    computed = []
    for tier, frac, quality in [
        ("hot", config.hot_fraction, config.hot),
        ("warm", config.warm_fraction, config.warm),
        ("cold", config.cold_fraction, config.cold),
    ]:
        gb = config.total_budget_gb * frac
        hours = _compute_retention_hours(gb, quality.bitrate_kbps, num_cams)
        computed.append(ComputedRetention(
            tier=tier, gb=gb, hours=hours, pretty_duration=_pretty_hours(hours),
        ))

    target_path = config.bind_mount_path if (
        config.storage_backend == "bind" and config.bind_mount_path
    ) else "/"
    try:
        du = shutil.disk_usage(target_path)
        disk_total = du.total / (1024 ** 3)
        disk_avail = du.free / (1024 ** 3)
    except OSError:
        disk_total = 0.0
        disk_avail = 0.0

    return ConfigResponse(
        config=config,
        computed=computed,
        disk_available_gb=disk_avail,
        disk_total_gb=disk_total,
        num_cameras=num_cams,
    )


class ConfigUpdateRequest(BaseModel):
    total_budget_gb: float = Field(..., ge=1)
    hot_fraction: float
    warm_fraction: float
    cold_fraction: float
    hot: TierQuality
    warm: TierQuality
    cold: TierQuality
    storage_backend: str = "volume"
    bind_mount_path: Optional[str] = None

    @field_validator("storage_backend")
    @classmethod
    def _backend_choice(cls, v: str) -> str:
        if v not in ("volume", "bind"):
            raise ValueError("storage_backend must be 'volume' or 'bind'")
        return v


@router.put("")
async def update_config(
    body: ConfigUpdateRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    _require_admin(user)
    _validate_fractions(body.hot_fraction, body.warm_fraction, body.cold_fraction)

    if body.storage_backend == "bind" and not body.bind_mount_path:
        raise HTTPException(400, "bind_mount_path required when backend is 'bind'")

    pool = request.app.state.db_pool
    await fetch_rows(
        pool,
        """
        UPDATE storage_tier_config SET
            total_budget_gb = $2,
            hot_fraction = $3, warm_fraction = $4, cold_fraction = $5,
            hot_width = $6, hot_height = $7, hot_fps = $8, hot_bitrate_kbps = $9,
            warm_width = $10, warm_height = $11, warm_fps = $12, warm_bitrate_kbps = $13,
            cold_width = $14, cold_height = $15, cold_fps = $16, cold_bitrate_kbps = $17,
            storage_backend = $18, bind_mount_path = $19,
            updated_at = NOW(), updated_by = $20
        WHERE site_id = $1::uuid
        """,
        [
            DEFAULT_SITE, body.total_budget_gb,
            body.hot_fraction, body.warm_fraction, body.cold_fraction,
            body.hot.width, body.hot.height, body.hot.fps, body.hot.bitrate_kbps,
            body.warm.width, body.warm.height, body.warm.fps, body.warm.bitrate_kbps,
            body.cold.width, body.cold.height, body.cold.fps, body.cold.bitrate_kbps,
            body.storage_backend, body.bind_mount_path, user.username,
        ],
        query_type="update_tier_config",
    )

    return {"status": "ok"}


@router.get("/rebalance/status")
async def rebalance_status(
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Current rebalance job (if any) + last 5 jobs as history."""
    _require_admin(user)
    pool = request.app.state.db_pool

    rows = await fetch_rows(
        pool,
        """
        SELECT job_id, started_at, finished_at, status,
               total_segments, processed_segments, bytes_processed, last_error,
               EXTRACT(EPOCH FROM (COALESCE(finished_at, NOW()) - started_at)) AS elapsed_s
        FROM rebalance_jobs
        ORDER BY started_at DESC
        LIMIT 5
        """,
        [],
        query_type="rebalance_status",
    )

    current = None
    history: list[dict] = []
    for r in rows:
        item = {
            "job_id": str(r["job_id"]),
            "started_at": r["started_at"].isoformat(),
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            "status": r["status"],
            "total_segments": int(r["total_segments"]) if r["total_segments"] is not None else None,
            "processed_segments": int(r["processed_segments"]),
            "bytes_processed": int(r["bytes_processed"]),
            "elapsed_s": float(r["elapsed_s"]) if r["elapsed_s"] is not None else 0.0,
            "last_error": r["last_error"],
        }
        if r["status"] in ("running", "paused") and current is None:
            current = item
        else:
            history.append(item)

    return {"current": current, "history": history}


@router.get("/usage")
async def get_usage(
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Current bytes used per tier (from video_segments)."""
    _require_admin(user)
    pool = request.app.state.db_pool

    rows = await fetch_rows(
        pool,
        """
        SELECT tier, COALESCE(SUM(bytes), 0) AS bytes, COUNT(*) AS segments,
               MIN(start_time) AS oldest, MAX(start_time) AS newest
        FROM video_segments
        GROUP BY tier
        """,
        [],
        query_type="tier_usage",
    )

    usage: dict[str, dict] = {
        "hot": {"bytes": 0, "segments": 0, "oldest": None, "newest": None},
        "warm": {"bytes": 0, "segments": 0, "oldest": None, "newest": None},
        "cold": {"bytes": 0, "segments": 0, "oldest": None, "newest": None},
    }
    for r in rows:
        usage[r["tier"]] = {
            "bytes": int(r["bytes"]),
            "segments": int(r["segments"]),
            "oldest": r["oldest"].isoformat() if r["oldest"] else None,
            "newest": r["newest"].isoformat() if r["newest"] else None,
        }
    return usage
