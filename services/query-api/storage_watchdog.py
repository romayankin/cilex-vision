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

    def __init__(
        self,
        minio_client: Any,
        quota_percent: int = 50,
        db_pool: Any = None,
    ) -> None:
        self.minio = minio_client
        self.quota_percent = max(10, min(90, int(quota_percent)))
        self.db_pool = db_pool
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
                purge_summary = await asyncio.to_thread(self._check)
                if purge_summary is not None:
                    await self._audit_auto_purge(purge_summary)
            except Exception:
                logger.exception("Watchdog iteration failed")
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=CHECK_INTERVAL
                )
            except asyncio.TimeoutError:
                continue

    async def _audit_auto_purge(self, summary: dict[str, Any]) -> None:
        if self.db_pool is None:
            return
        try:
            from auth.audit import _write_audit_log  # noqa: PLC0415

            await _write_audit_log(
                pool=self.db_pool,
                user_id=None,
                action="AUTO_PURGE",
                resource_type="storage",
                resource_id=summary["bucket"],
                details={
                    "description": (
                        f"Watchdog auto-purge: video usage "
                        f"{_human(summary['video_bytes'])} exceeded quota "
                        f"{_human(summary['quota_bytes'])} "
                        f"({summary['quota_percent']}%)"
                    ),
                    "username": "system/watchdog",
                    "bucket": summary["bucket"],
                    "deleted_objects": summary["deleted"],
                    "freed_bytes": summary["freed"],
                    "freed_human": _human(summary["freed"]),
                    "quota_percent": summary["quota_percent"],
                    "video_bytes": summary["video_bytes"],
                    "quota_bytes": summary["quota_bytes"],
                },
                ip_address="127.0.0.1",
                hostname="localhost",
            )
        except Exception:
            logger.warning("Watchdog audit write failed", exc_info=True)

    def _check(self) -> dict[str, Any] | None:
        disk = shutil.disk_usage("/")
        bucket_sizes = {b: self._bucket_size(b) for b in MONITORED_BUCKETS}
        video_bytes = sum(bucket_sizes.values())
        non_video = max(0, disk.used - video_bytes)
        assignable = max(0, disk.total - non_video)
        quota_bytes = int(assignable * self.quota_percent / 100)
        over = video_bytes > quota_bytes

        # Preserve purge progress / last_purge across iterations so the UI
        # can keep showing them while the next check runs.
        prev = self._stats
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
            "purging": False,
            "purge_deleted": 0,
            "purge_freed": 0,
            "purge_freed_human": "",
            "purge_target": 0,
            "purge_target_human": "",
            "last_purge": prev.get("last_purge") if prev else None,
        }

        if over:
            target = int(quota_bytes * PURGE_TARGET)
            to_free = video_bytes - target
            logger.warning(
                "Over quota: %s > %s, purging %s from frame-blobs",
                _human(video_bytes), _human(quota_bytes), _human(to_free),
            )
            self._stats["purging"] = True
            self._stats["purge_target"] = to_free
            self._stats["purge_target_human"] = _human(to_free)
            try:
                self._purge_oldest("frame-blobs", to_free)
            finally:
                self._stats["purging"] = False
                self._stats["last_purge"] = {
                    "deleted": self._stats.get("purge_deleted", 0),
                    "freed_human": self._stats.get("purge_freed_human", ""),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            return {
                "bucket": "frame-blobs",
                "deleted": int(self._stats.get("purge_deleted", 0)),
                "freed": int(self._stats.get("purge_freed", 0)),
                "video_bytes": video_bytes,
                "quota_bytes": quota_bytes,
                "quota_percent": self.quota_percent,
            }
        return None

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
        deleted = 0
        batch: list[Any] = []
        for _, size, name in entries:
            if freed >= bytes_to_free:
                break
            batch.append(DeleteObject(name))
            freed += size
            deleted += 1
            if len(batch) >= BATCH_SIZE:
                self._flush(bucket, batch)
                batch = []
                self._stats["purge_deleted"] = deleted
                self._stats["purge_freed"] = freed
                self._stats["purge_freed_human"] = _human(freed)
        if batch:
            self._flush(bucket, batch)
        self._stats["purge_deleted"] = deleted
        self._stats["purge_freed"] = freed
        self._stats["purge_freed_human"] = _human(freed)
        logger.info("Watchdog purged %s from %s", _human(freed), bucket)

    def _flush(self, bucket: str, batch: list[Any]) -> None:
        try:
            errors = list(self.minio.remove_objects(bucket, batch))
            for err in errors:
                logger.warning("watchdog delete error in %s: %s", bucket, err)
        except Exception:
            logger.warning("watchdog remove_objects failed", exc_info=True)
