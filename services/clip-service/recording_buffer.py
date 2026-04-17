"""Continuous RTSP recording buffer for full-FPS clip extraction.

Runs one ffmpeg subprocess per camera that records go2rtc's RTSP restream
into rolling MPEG-TS segments. When a clip is needed, the relevant segments
are concatenated and the time range is extracted.

Segments use `-c copy` (no re-encoding) so CPU cost is near zero.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class RecordingBuffer:
    """Manages per-camera ffmpeg recording subprocesses."""

    def __init__(
        self,
        go2rtc_rtsp_base: str = "rtsp://go2rtc:8554",
        buffer_dir: str = "/tmp/buffer",
        segment_duration_s: int = 30,
        max_segments: int = 20,
    ) -> None:
        self._go2rtc_base = go2rtc_rtsp_base
        self._buffer_dir = Path(buffer_dir)
        self._segment_duration = segment_duration_s
        self._max_segments = max_segments
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False

    async def start(self, camera_ids: list[str]) -> None:
        """Start recording for each camera."""
        self._running = True
        self._buffer_dir.mkdir(parents=True, exist_ok=True)

        for cam_id in camera_ids:
            cam_dir = self._buffer_dir / cam_id
            cam_dir.mkdir(parents=True, exist_ok=True)
            self._tasks[cam_id] = asyncio.create_task(self._record_camera(cam_id))
            logger.info("Recording buffer started for %s", cam_id)

    async def stop(self) -> None:
        """Stop all recording subprocesses."""
        self._running = False
        for cam_id, proc in list(self._processes.items()):
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            logger.info("Recording buffer stopped for %s", cam_id)
        self._processes.clear()

        for cam_id, task in list(self._tasks.items()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _record_camera(self, camera_id: str) -> None:
        """Run ffmpeg segment recording in a retry loop."""
        cam_dir = self._buffer_dir / camera_id
        rtsp_url = f"{self._go2rtc_base}/{camera_id}"

        while self._running:
            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                "-c", "copy",
                "-an",
                "-f", "segment",
                "-segment_time", str(self._segment_duration),
                "-segment_wrap", str(self._max_segments),
                "-segment_format", "mpegts",
                "-strftime", "1",
                "-reset_timestamps", "1",
                str(cam_dir / "%Y%m%d_%H%M%S.ts"),
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._processes[camera_id] = proc
                await proc.wait()

                if not self._running:
                    break

                stderr = await proc.stderr.read() if proc.stderr else b""
                logger.warning(
                    "ffmpeg recording for %s exited (code %s): %s — restarting in 5s",
                    camera_id,
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace")[:200],
                )
            except Exception:
                if not self._running:
                    break
                logger.exception("Recording buffer error for %s — restarting in 5s", camera_id)

            await asyncio.sleep(5)

    def get_segments(
        self,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Path]:
        """Find buffer segments that overlap the requested time range.

        Segment filenames are `YYYYMMDD_HHMMSS.ts` (strftime pattern).
        A segment is selected when its [seg_time, seg_time + segment_duration)
        window overlaps [start_time, end_time].
        """
        cam_dir = self._buffer_dir / camera_id
        if not cam_dir.exists():
            return []

        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)

        search_start = start_time - timedelta(seconds=self._segment_duration)

        segments: list[tuple[datetime, Path]] = []
        for f in cam_dir.glob("*.ts"):
            try:
                seg_time = datetime.strptime(f.stem, "%Y%m%d_%H%M%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

            seg_end = seg_time + timedelta(seconds=self._segment_duration)
            if seg_end >= search_start and seg_time <= end_time:
                segments.append((seg_time, f))

        segments.sort(key=lambda x: x[0])
        return [path for _, path in segments]

    def is_recording(self, camera_id: str) -> bool:
        """Check if we have an active recording for this camera."""
        proc = self._processes.get(camera_id)
        return proc is not None and proc.returncode is None

    @property
    def processes(self) -> dict[str, asyncio.subprocess.Process]:
        return self._processes
