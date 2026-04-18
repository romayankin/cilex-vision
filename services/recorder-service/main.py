"""Continuous video recorder.

For each camera, runs a persistent ffmpeg process reading from go2rtc RTSP
and writing MPEG-TS segments every N seconds. Completed segments are
uploaded to MinIO and indexed in video_segments.

Always records 24/7 for now. Camera profiles will add mode-switching later.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
from aiohttp import web
from minio import Minio

from config import RecorderSettings
from rebalancer import TierRebalancer

logger = logging.getLogger(__name__)


class CameraRecorder:
    """Runs one ffmpeg process per camera, manages segment lifecycle."""

    def __init__(
        self,
        settings: RecorderSettings,
        camera_id: str,
        pool: asyncpg.Pool,
        minio: Minio,
        recording_mode: str = "continuous",
        business_hours_start=None,
        business_hours_end=None,
        business_days: list[int] | None = None,
        timezone: str = "UTC",
    ):
        self.settings = settings
        self.camera_id = camera_id
        self.pool = pool
        self.minio = minio
        self.recording_mode = recording_mode or "continuous"
        self.business_hours_start = business_hours_start
        self.business_hours_end = business_hours_end
        self.business_days = business_days or [1, 2, 3, 4, 5]
        self.tz_name = timezone or "UTC"
        self.proc: subprocess.Popen | None = None
        self._shutdown = False
        self._cam_dir = Path(settings.work_dir) / camera_id
        self._cam_dir.mkdir(parents=True, exist_ok=True)

        if self.recording_mode != "continuous":
            logger.warning(
                "Camera %s profile requests '%s' mode — not yet implemented, "
                "falling back to continuous 24/7 recording",
                self.camera_id, self.recording_mode,
            )

    def _start_ffmpeg(self) -> subprocess.Popen:
        rtsp_url = f"{self.settings.go2rtc_base}/{self.camera_id}"
        pattern = str(self._cam_dir / "seg-%Y%m%d-%H%M%S.ts")

        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.settings.segment_duration_s),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            "-strftime", "1",
            pattern,
        ]
        logger.info("Starting ffmpeg for %s: %s", self.camera_id, " ".join(cmd))
        return subprocess.Popen(cmd)

    async def run(self) -> None:
        self.proc = self._start_ffmpeg()

        while not self._shutdown:
            if self.proc.poll() is not None:
                logger.warning(
                    "ffmpeg for %s exited (code=%s), restarting in 5s",
                    self.camera_id, self.proc.returncode,
                )
                await asyncio.sleep(5)
                if self._shutdown:
                    break
                self.proc = self._start_ffmpeg()

            await asyncio.sleep(10)
            await self._upload_completed_segments()

        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        await self._upload_completed_segments()

    async def _upload_completed_segments(self) -> None:
        now = time.time()
        threshold = self.settings.segment_duration_s + 5

        for path in sorted(self._cam_dir.glob("seg-*.ts")):
            try:
                mtime = path.stat().st_mtime
                if (now - mtime) < threshold:
                    continue
            except FileNotFoundError:
                continue

            try:
                await self._upload_and_index(path)
                path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to upload %s", path)

    async def _upload_and_index(self, path: Path) -> None:
        name = path.stem
        try:
            parts = name.split("-")
            date_str, time_str = parts[1], parts[2]
            start_time = datetime(
                int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
                int(time_str[:2]), int(time_str[2:4]), int(time_str[4:6]),
                tzinfo=timezone.utc,
            )
        except Exception:
            logger.warning("Can't parse timestamp from %s", path.name)
            start_time = datetime.now(timezone.utc)

        duration_s = float(self.settings.segment_duration_s)
        end_time = start_time + timedelta(seconds=duration_s)

        size_bytes = path.stat().st_size
        if size_bytes == 0:
            logger.warning("Skipping empty segment %s", path.name)
            return

        segment_id = uuid4()
        object_key = f"{self.camera_id}/{start_time.strftime('%Y-%m-%d')}/{segment_id}.ts"
        bucket = self.settings.bucket_hot

        def _upload():
            self.minio.fput_object(bucket, object_key, str(path),
                                   content_type="video/mp2t")

        await asyncio.to_thread(_upload)

        storage_uri = f"s3://{bucket}/{object_key}"

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO video_segments
                    (segment_id, camera_id, start_time, end_time, duration_s, tier,
                     storage_uri, bytes, codec)
                VALUES ($1, $2, $3, $4, $5, 'hot', $6, $7, 'h264')
                """,
                segment_id, self.camera_id, start_time, end_time, duration_s,
                storage_uri, size_bytes,
            )

        logger.info(
            "Uploaded segment %s (%s, %.1f MB)",
            object_key, self.camera_id, size_bytes / 1024 / 1024,
        )

    def stop(self) -> None:
        self._shutdown = True


class RecorderService:
    def __init__(self, settings: RecorderSettings):
        self.settings = settings
        self._pool: asyncpg.Pool | None = None
        self._minio: Minio | None = None
        self._recorders: dict[str, CameraRecorder] = {}
        self._tasks: list[asyncio.Task] = []
        self._rebalancer: TierRebalancer | None = None
        self._rebalancer_task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            dsn=self.settings.db_dsn, min_size=1, max_size=4,
        )

        self._minio = Minio(
            self.settings.minio_url,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key,
            secure=self.settings.minio_secure,
        )

        if not self._minio.bucket_exists(self.settings.bucket_hot):
            self._minio.make_bucket(self.settings.bucket_hot)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.camera_id,
                       COALESCE(p.recording_mode, 'continuous') AS recording_mode,
                       p.business_hours_start, p.business_hours_end,
                       p.business_days, COALESCE(p.timezone, 'UTC') AS timezone
                FROM cameras c
                LEFT JOIN camera_profiles p ON p.profile_id = c.profile_id
                WHERE c.status != 'disabled'
                """
            )
        logger.info("Found %d cameras", len(rows))

        for r in rows:
            cam_id = r["camera_id"]
            business_days_raw = r["business_days"]
            if isinstance(business_days_raw, str):
                try:
                    import json as _json
                    business_days = _json.loads(business_days_raw)
                except Exception:
                    business_days = [1, 2, 3, 4, 5]
            elif isinstance(business_days_raw, list):
                business_days = business_days_raw
            else:
                business_days = [1, 2, 3, 4, 5]

            recorder = CameraRecorder(
                self.settings, cam_id, self._pool, self._minio,
                recording_mode=r["recording_mode"],
                business_hours_start=r["business_hours_start"],
                business_hours_end=r["business_hours_end"],
                business_days=business_days,
                timezone=r["timezone"],
            )
            self._recorders[cam_id] = recorder
            self._tasks.append(asyncio.create_task(recorder.run()))
            logger.info(
                "Registered recorder for %s (mode=%s tz=%s)",
                cam_id, recorder.recording_mode, recorder.tz_name,
            )

        self._rebalancer = TierRebalancer(self._pool, self._minio)
        self._rebalancer_task = asyncio.create_task(self._rebalancer.run())

        await self._start_health_server()

        await self._shutdown.wait()

    async def _start_health_server(self) -> None:
        async def health(_req):
            alive_count = sum(
                1 for r in self._recorders.values()
                if r.proc is not None and r.proc.poll() is None
            )
            body = {
                "status": "ok" if alive_count == len(self._recorders) else "degraded",
                "recorders_alive": alive_count,
                "recorders_total": len(self._recorders),
            }
            return web.json_response(body)

        app = web.Application()
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.settings.health_port)
        await site.start()
        logger.info("Health server on :%d", self.settings.health_port)

    async def shutdown(self) -> None:
        logger.info("Shutting down %d recorders", len(self._recorders))
        if self._rebalancer_task:
            self._rebalancer_task.cancel()
            try:
                await self._rebalancer_task
            except (asyncio.CancelledError, Exception):
                pass
        for r in self._recorders.values():
            r.stop()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._pool:
            await self._pool.close()
        self._shutdown.set()


async def main():
    settings = RecorderSettings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    service = RecorderService(settings)
    loop = asyncio.get_running_loop()

    def _sigterm():
        loop.create_task(service.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _sigterm)

    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
