"""Background tier rebalance daemon.

Periodically checks for idle conditions (low CPU, no recent motion events)
and migrates segments down the tier chain (hot → warm → cold), re-encoding
with each tier's quality settings. When cold exceeds its budget, oldest
segments are evicted. Runs with nice -n 19 ionice -c 3 so it never steals
resources from core services.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import psutil
from minio import Minio

logger = logging.getLogger(__name__)

DEFAULT_SITE = UUID("00000000-0000-0000-0000-000000000001")

BUCKET_HOT = "raw-video-hot"
BUCKET_WARM = "raw-video-warm"
BUCKET_COLD = "raw-video-cold"


class TierRebalancer:
    def __init__(
        self,
        pool: asyncpg.Pool,
        minio: Minio,
        idle_check_interval_s: int = 60,
        cpu_threshold_pct: float = 30.0,
        no_motion_window_min: int = 5,
        disk_full_threshold_pct: float = 90.0,
        segments_per_cycle: int = 1000,
    ):
        self.pool = pool
        self.minio = minio
        self.idle_check_interval_s = idle_check_interval_s
        self.cpu_threshold_pct = cpu_threshold_pct
        self.no_motion_window_min = no_motion_window_min
        self.disk_full_threshold_pct = disk_full_threshold_pct
        self.segments_per_cycle = segments_per_cycle
        self._current_job: UUID | None = None

    async def run(self) -> None:
        logger.info(
            "Rebalance daemon started (idle check every %ds, cpu<%.0f%%, motion-free %d min)",
            self.idle_check_interval_s,
            self.cpu_threshold_pct,
            self.no_motion_window_min,
        )
        await self._ensure_buckets()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Rebalance tick failed")
            await asyncio.sleep(self.idle_check_interval_s)

    async def _ensure_buckets(self) -> None:
        for bucket in (BUCKET_HOT, BUCKET_WARM, BUCKET_COLD):
            try:
                exists = await asyncio.to_thread(self.minio.bucket_exists, bucket)
                if not exists:
                    await asyncio.to_thread(self.minio.make_bucket, bucket)
                    logger.info("Created MinIO bucket %s", bucket)
            except Exception:
                logger.exception("Failed to verify bucket %s", bucket)

    async def _tick(self) -> None:
        if await self._disk_full():
            logger.warning(
                "Disk >=%.0f%% full — skipping rebalance (admin must purge)",
                self.disk_full_threshold_pct,
            )
            return

        if not await self._is_idle():
            return

        async with self.pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT job_id FROM rebalance_jobs WHERE status = 'running' LIMIT 1"
            )
        if existing:
            logger.debug("Rebalance job %s already running, skipping tick", existing)
            return

        config = await self._load_config()
        if config is None:
            return

        await self._run_rebalance_cycle(config)

    async def _is_idle(self) -> bool:
        cpu = await asyncio.to_thread(psutil.cpu_percent, 5)
        if cpu > self.cpu_threshold_pct:
            logger.debug("CPU busy: %.1f%% > %.0f%%", cpu, self.cpu_threshold_pct)
            return False

        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM events
                WHERE event_type IN ('entered_scene','motion_started')
                  AND start_time > NOW() - ($1 * INTERVAL '1 minute')
                """,
                self.no_motion_window_min,
            )

        if count and int(count) > 0:
            logger.debug(
                "%d motion events in last %d min — not idle",
                count, self.no_motion_window_min,
            )
            return False

        return True

    async def _disk_full(self) -> bool:
        try:
            du = await asyncio.to_thread(shutil.disk_usage, "/")
            pct = (du.used / du.total) * 100
            return pct >= self.disk_full_threshold_pct
        except Exception:
            return False

    async def _load_config(self) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT total_budget_gb, hot_fraction, warm_fraction, cold_fraction,
                       hot_width, hot_height, hot_fps, hot_bitrate_kbps,
                       warm_width, warm_height, warm_fps, warm_bitrate_kbps,
                       cold_width, cold_height, cold_fps, cold_bitrate_kbps
                FROM storage_tier_config
                WHERE site_id = $1
                """,
                DEFAULT_SITE,
            )
        if row is None:
            logger.debug("No storage_tier_config row — skipping rebalance")
            return None
        return dict(row)

    async def _num_active_cameras(self) -> int:
        async with self.pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM cameras WHERE status != 'disabled'"
            )
        return max(int(n or 0), 1)

    async def _run_rebalance_cycle(self, config: dict) -> None:
        num_cams = await self._num_active_cameras()

        hot_gb = float(config["total_budget_gb"]) * float(config["hot_fraction"])
        warm_gb = float(config["total_budget_gb"]) * float(config["warm_fraction"])
        hot_retention_s = self._retention_seconds(hot_gb, int(config["hot_bitrate_kbps"]), num_cams)
        warm_retention_s = self._retention_seconds(warm_gb, int(config["warm_bitrate_kbps"]), num_cams)

        now = datetime.now(timezone.utc)
        hot_cutoff = now - timedelta(seconds=hot_retention_s)
        warm_cutoff = now - timedelta(seconds=warm_retention_s)

        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                """
                SELECT COUNT(*) FROM video_segments
                WHERE (tier = 'hot' AND start_time < $1)
                   OR (tier = 'warm' AND start_time < $2)
                """,
                hot_cutoff, warm_cutoff,
            )
            job_id = await conn.fetchval(
                """
                INSERT INTO rebalance_jobs (site_id, total_segments)
                VALUES ($1, $2) RETURNING job_id
                """,
                DEFAULT_SITE, int(total or 0),
            )

        self._current_job = job_id
        logger.info(
            "Rebalance job %s started (%d eligible, hot→warm cutoff=%s, warm→cold cutoff=%s)",
            job_id, int(total or 0), hot_cutoff.isoformat(), warm_cutoff.isoformat(),
        )

        try:
            paused = await self._process_tier_transition(
                job_id, from_tier="hot", to_tier="warm", cutoff=hot_cutoff,
                target_width=int(config["warm_width"]),
                target_height=int(config["warm_height"]),
                target_fps=int(config["warm_fps"]),
                target_bitrate_kbps=int(config["warm_bitrate_kbps"]),
            )
            if paused:
                return

            paused = await self._process_tier_transition(
                job_id, from_tier="warm", to_tier="cold", cutoff=warm_cutoff,
                target_width=int(config["cold_width"]),
                target_height=int(config["cold_height"]),
                target_fps=int(config["cold_fps"]),
                target_bitrate_kbps=int(config["cold_bitrate_kbps"]),
            )
            if paused:
                return

            cold_budget_gb = float(config["total_budget_gb"]) * float(config["cold_fraction"])
            await self._evict_cold_if_needed(cold_budget_gb)

            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE rebalance_jobs
                    SET status = 'completed', finished_at = NOW()
                    WHERE job_id = $1
                    """,
                    job_id,
                )
            logger.info("Rebalance job %s completed", job_id)
        except Exception as exc:
            logger.exception("Rebalance job %s failed", job_id)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE rebalance_jobs
                    SET status = 'failed', finished_at = NOW(), last_error = $2
                    WHERE job_id = $1
                    """,
                    job_id, str(exc)[:2000],
                )
        finally:
            self._current_job = None

    @staticmethod
    def _retention_seconds(gb: float, bitrate_kbps: int, num_cameras: int) -> float:
        bytes_per_sec = (bitrate_kbps * 1000 / 8) * max(num_cameras, 1)
        if bytes_per_sec <= 0:
            return 0.0
        return gb * (1024 ** 3) / bytes_per_sec

    async def _process_tier_transition(
        self, job_id: UUID, from_tier: str, to_tier: str, cutoff: datetime,
        target_width: int, target_height: int,
        target_fps: int, target_bitrate_kbps: int,
    ) -> bool:
        """Move all segments older than cutoff from from_tier to to_tier.

        Returns True if the job was paused (no longer idle)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT segment_id, camera_id, start_time, storage_uri, bytes
                FROM video_segments
                WHERE tier = $1 AND start_time < $2
                ORDER BY start_time ASC
                LIMIT $3
                """,
                from_tier, cutoff, self.segments_per_cycle,
            )

        if not rows:
            return False

        logger.info("Moving %d segments from %s to %s", len(rows), from_tier, to_tier)

        from_bucket = f"raw-video-{from_tier}"
        to_bucket = f"raw-video-{to_tier}"

        for row in rows:
            if not await self._is_idle():
                logger.info("No longer idle — pausing rebalance job %s", job_id)
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE rebalance_jobs SET status = 'paused', finished_at = NOW() WHERE job_id = $1",
                        job_id,
                    )
                return True

            try:
                await self._migrate_segment(
                    row, from_bucket, to_bucket, to_tier,
                    target_width, target_height, target_fps, target_bitrate_kbps,
                )
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE rebalance_jobs
                        SET processed_segments = processed_segments + 1,
                            bytes_processed = bytes_processed + $2
                        WHERE job_id = $1
                        """,
                        job_id, int(row["bytes"]),
                    )
            except Exception:
                logger.exception("Failed to migrate segment %s", row["segment_id"])

        return False

    async def _migrate_segment(
        self, row: Any, from_bucket: str, to_bucket: str, to_tier: str,
        width: int, height: int, fps: int, bitrate_kbps: int,
    ) -> None:
        segment_id = row["segment_id"]
        camera_id = row["camera_id"]
        start_time: datetime = row["start_time"]
        storage_uri: str = row["storage_uri"]
        prefix = f"s3://{from_bucket}/"
        if not storage_uri.startswith(prefix):
            raise RuntimeError(f"segment {segment_id} has unexpected storage_uri {storage_uri!r}")
        from_key = storage_uri[len(prefix):]

        with tempfile.TemporaryDirectory(prefix="rebalance-") as tmpdir:
            src = Path(tmpdir) / "src.ts"
            dst = Path(tmpdir) / "dst.ts"

            await asyncio.to_thread(self.minio.fget_object, from_bucket, from_key, str(src))

            cmd = [
                "nice", "-n", "19", "ionice", "-c", "3",
                "ffmpeg", "-y", "-loglevel", "error",
                "-threads", "1",
                "-i", str(src),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-threads", "1",
                "-vf", f"scale={width}:{height},fps={fps}",
                "-b:v", f"{bitrate_kbps}k",
                "-maxrate", f"{bitrate_kbps}k",
                "-bufsize", f"{bitrate_kbps * 2}k",
                "-an",
                "-f", "mpegts",
                str(dst),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')[:500]}")

            to_key = f"{camera_id}/{start_time.strftime('%Y-%m-%d')}/{segment_id}.ts"
            new_size = dst.stat().st_size
            await asyncio.to_thread(
                self.minio.fput_object, to_bucket, to_key, str(dst),
                content_type="video/mp2t",
            )

            new_uri = f"s3://{to_bucket}/{to_key}"
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE video_segments
                    SET tier = $2, storage_uri = $3, bytes = $4,
                        width = $5, height = $6, fps = $7, bitrate_kbps = $8
                    WHERE segment_id = $1
                    """,
                    segment_id, to_tier, new_uri, new_size,
                    width, height, fps, bitrate_kbps,
                )

            try:
                await asyncio.to_thread(self.minio.remove_object, from_bucket, from_key)
            except Exception:
                logger.warning("Could not delete %s from %s", from_key, from_bucket)

        logger.debug(
            "Migrated %s %s→%s (%.1f → %.1f MB)",
            segment_id, from_bucket, to_bucket,
            int(row["bytes"]) / 1024 / 1024, new_size / 1024 / 1024,
        )

    async def _evict_cold_if_needed(self, budget_gb: float) -> None:
        budget_bytes = int(budget_gb * (1024 ** 3))
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COALESCE(SUM(bytes), 0) FROM video_segments WHERE tier = 'cold'"
            )
            if int(total) <= budget_bytes:
                return

            overflow = int(total) - budget_bytes
            logger.info(
                "Cold tier over budget by %.1f GB — evicting oldest",
                overflow / (1024 ** 3),
            )

            rows = await conn.fetch(
                """
                SELECT segment_id, storage_uri, bytes FROM video_segments
                WHERE tier = 'cold'
                ORDER BY start_time ASC
                """
            )

        freed = 0
        to_delete = []
        for r in rows:
            to_delete.append(r)
            freed += int(r["bytes"])
            if freed >= overflow:
                break

        for r in to_delete:
            uri: str = r["storage_uri"]
            prefix = f"s3://{BUCKET_COLD}/"
            if uri.startswith(prefix):
                key = uri[len(prefix):]
                try:
                    await asyncio.to_thread(self.minio.remove_object, BUCKET_COLD, key)
                except Exception:
                    logger.warning("Failed to delete %s from MinIO", key)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM video_segments WHERE segment_id = $1", r["segment_id"]
                )

        logger.info(
            "Evicted %d cold segments (%.1f GB)",
            len(to_delete), freed / (1024 ** 3),
        )
