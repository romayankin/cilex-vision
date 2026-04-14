"""Storage quota watchdog.

Background task that polls disk usage and MinIO bucket sizes every minute,
and auto-purges the oldest objects from ``frame-blobs`` when the video
buckets exceed the configured quota. Hysteresis: purge stops once usage
drops to ``PURGE_TARGET`` of the quota, so the next check doesn't
immediately re-trigger.

Quota semantics: only the portion of the disk not held by system/other
data is "assignable". The quota is a percentage of assignable space. This
way the watchdog can't delete video data to make room for logs it isn't
responsible for, and the UI slider maps cleanly to "what share of the
disk can video use".
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

MONITORED_BUCKETS = ["frame-blobs", "decoded-frames"]
CHECK_INTERVAL = 60
PURGE_TARGET = 0.80
BATCH_SIZE = 1000


def _human(n: float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


class StorageWatchdog:
    """Periodically enforces a video-bucket quota against disk capacity."""

    def __init__(self, minio_client: Any, quota_percent: int = 50) -> None:
        self.minio = minio_client
        self.quota_percent = max(10, min(90, int(quota_percent)))
        self._task: asyncio.Task[None] | None = None
        self._stats: dict[str, Any] = {}
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="storage-watchdog")
        logger.info("Storage watchdog started (quota=%d%%)", self.quota_percent)

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def set_quota_percent(self, percent: int) -> int:
        self.quota_percent = max(10, min(90, int(percent)))
        return self.quota_percent

    @property
    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    async def _loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await asyncio.to_thread(self._check)
            except Exception:
                logger.exception("Watchdog iteration failed")
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=CHECK_INTERVAL
                )
            except asyncio.TimeoutError:
                continue

    def _check(self) -> None:
        disk = shutil.disk_usage("/")
        bucket_sizes = {b: self._bucket_size(b) for b in MONITORED_BUCKETS}
        video_bytes = sum(bucket_sizes.values())
        non_video = max(0, disk.used - video_bytes)
        assignable = max(0, disk.total - non_video)
        quota_bytes = int(assignable * self.quota_percent / 100)
        over = video_bytes > quota_bytes

        self._stats = {
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_free": disk.free,
            "non_video_used": non_video,
            "assignable": assignable,
            "video_bytes": video_bytes,
            "quota_percent": self.quota_percent,
            "quota_bytes": quota_bytes,
            "over_quota": over,
            "bucket_sizes": bucket_sizes,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        if over:
            target = int(quota_bytes * PURGE_TARGET)
            to_free = video_bytes - target
            logger.warning(
                "Over quota: %s > %s, purging %s from frame-blobs",
                _human(video_bytes), _human(quota_bytes), _human(to_free),
            )
            self._purge_oldest("frame-blobs", to_free)

    def _bucket_size(self, bucket: str) -> int:
        if self.minio is None:
            return 0
        try:
            total = 0
            for obj in self.minio.list_objects(bucket, recursive=True):
                total += obj.size or 0
            return total
        except Exception:
            logger.debug("Bucket size scan failed for %s", bucket, exc_info=True)
            return 0

    def _purge_oldest(self, bucket: str, bytes_to_free: int) -> None:
        from minio.deleteobjects import DeleteObject  # noqa: PLC0415

        if self.minio is None or bytes_to_free <= 0:
            return

        # Collect (last_modified, size, name) so we can delete oldest-first.
        entries: list[tuple[datetime, int, str]] = []
        try:
            for obj in self.minio.list_objects(bucket, recursive=True):
                if obj.last_modified is None:
                    continue
                entries.append(
                    (obj.last_modified, obj.size or 0, obj.object_name)
                )
        except Exception:
            logger.warning("purge list failed for %s", bucket, exc_info=True)
            return
        entries.sort(key=lambda e: e[0])

        freed = 0
        batch: list[Any] = []
        for _, size, name in entries:
            if freed >= bytes_to_free:
                break
            batch.append(DeleteObject(name))
            freed += size
            if len(batch) >= BATCH_SIZE:
                self._flush(bucket, batch)
                batch = []
        if batch:
            self._flush(bucket, batch)
        logger.info("Watchdog purged %s from %s", _human(freed), bucket)

    def _flush(self, bucket: str, batch: list[Any]) -> None:
        try:
            errors = list(self.minio.remove_objects(bucket, batch))
            for err in errors:
                logger.warning("watchdog delete error in %s: %s", bucket, err)
        except Exception:
            logger.warning("watchdog remove_objects failed", exc_info=True)
