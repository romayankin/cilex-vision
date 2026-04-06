"""COPY-only database writer for the Metadata Bulk Collector."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID


DETECTIONS_COLUMNS = [
    "time",
    "camera_id",
    "frame_seq",
    "object_class",
    "confidence",
    "bbox_x",
    "bbox_y",
    "bbox_w",
    "bbox_h",
    "local_track_id",
    "model_version",
]

TRACK_OBSERVATIONS_COLUMNS = [
    "time",
    "camera_id",
    "local_track_id",
    "centroid_x",
    "centroid_y",
    "bbox_area",
    "embedding_ref",
]


@dataclass(frozen=True)
class DetectionRow:
    """One detections hypertable row."""

    time: datetime
    camera_id: str
    frame_seq: int
    object_class: str
    confidence: float
    bbox_x: float
    bbox_y: float
    bbox_w: float
    bbox_h: float
    local_track_id: UUID | None
    model_version: str

    @property
    def dedupe_key(self) -> tuple[str, int, str]:
        return (self.camera_id, self.frame_seq, str(self.local_track_id or ""))

    def as_record(self) -> tuple[Any, ...]:
        return (
            self.time,
            self.camera_id,
            self.frame_seq,
            self.object_class,
            self.confidence,
            self.bbox_x,
            self.bbox_y,
            self.bbox_w,
            self.bbox_h,
            self.local_track_id,
            self.model_version,
        )


@dataclass(frozen=True)
class TrackObservationRow:
    """One track_observations hypertable row."""

    time: datetime
    camera_id: str
    frame_seq: int
    local_track_id: UUID
    centroid_x: float
    centroid_y: float
    bbox_area: float
    embedding_ref: str | None

    @property
    def dedupe_key(self) -> tuple[str, int, str]:
        return (self.camera_id, self.frame_seq, str(self.local_track_id))

    def as_record(self) -> tuple[Any, ...]:
        return (
            self.time,
            self.camera_id,
            self.local_track_id,
            self.centroid_x,
            self.centroid_y,
            self.bbox_area,
            self.embedding_ref,
        )


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one COPY invocation."""

    table_name: str
    rows_written: int
    duplicates_skipped: int
    elapsed_ms: float


class _DedupeCache:
    """Bounded TTL cache for idempotent re-delivery handling."""

    def __init__(
        self,
        *,
        ttl_s: int,
        max_keys: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_s = ttl_s
        self.max_keys = max_keys
        self.clock = clock or time.monotonic
        self._entries: OrderedDict[tuple[str, int, str], float] = OrderedDict()

    def add_if_new(self, key: tuple[str, int, str]) -> bool:
        now = float(self.clock())
        self._evict(now)
        if key in self._entries:
            self._entries.move_to_end(key)
            return False
        self._entries[key] = now
        self._entries.move_to_end(key)
        self._evict(now)
        return True

    def _evict(self, now: float) -> None:
        while self._entries:
            oldest_key = next(iter(self._entries))
            oldest_seen = self._entries[oldest_key]
            if now - oldest_seen <= self.ttl_s and len(self._entries) <= self.max_keys:
                break
            self._entries.popitem(last=False)


class AsyncpgBulkWriter:
    """COPY-only writer for TimescaleDB hypertables."""

    def __init__(
        self,
        *,
        dsn: str,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
        command_timeout_s: float = 30.0,
        dedup_ttl_s: int = 600,
        dedup_max_keys: int = 250_000,
        pool: Any | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.dsn = dsn
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self.command_timeout_s = command_timeout_s
        self.clock = clock or time.monotonic
        self._pool = pool
        self._create_pool_lock = asyncio.Lock()
        self._detection_cache = _DedupeCache(
            ttl_s=dedup_ttl_s,
            max_keys=dedup_max_keys,
            clock=self.clock,
        )
        self._track_cache = _DedupeCache(
            ttl_s=dedup_ttl_s,
            max_keys=dedup_max_keys,
            clock=self.clock,
        )

    async def connect(self) -> None:
        """Ensure the asyncpg pool exists."""
        if self._pool is not None:
            return
        async with self._create_pool_lock:
            if self._pool is not None:
                return
            try:
                import asyncpg  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - depends on local env
                raise RuntimeError("missing optional dependency 'asyncpg'; install requirements.txt") from exc
            self._pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=self.min_pool_size,
                max_size=self.max_pool_size,
                command_timeout=self.command_timeout_s,
                server_settings={"timezone": "UTC"},
            )

    async def close(self) -> None:
        """Close the underlying asyncpg pool."""
        if self._pool is not None and hasattr(self._pool, "close"):
            await self._pool.close()
        self._pool = None

    async def is_ready(self) -> bool:
        """Return True when the pool exists and answers a trivial query."""
        try:
            await self.connect()
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def write_detection_rows(self, rows: list[DetectionRow]) -> WriteResult:
        """COPY a batch of detections rows."""
        filtered, duplicates = self._filter_detection_rows(rows)
        if not filtered:
            return WriteResult("detections", 0, duplicates, 0.0)
        elapsed_ms = await self._copy_rows(
            "detections",
            DETECTIONS_COLUMNS,
            [row.as_record() for row in filtered],
        )
        return WriteResult("detections", len(filtered), duplicates, elapsed_ms)

    async def write_track_observation_rows(self, rows: list[TrackObservationRow]) -> WriteResult:
        """COPY a batch of track observation rows."""
        filtered, duplicates = self._filter_track_observation_rows(rows)
        if not filtered:
            return WriteResult("track_observations", 0, duplicates, 0.0)
        elapsed_ms = await self._copy_rows(
            "track_observations",
            TRACK_OBSERVATIONS_COLUMNS,
            [row.as_record() for row in filtered],
        )
        return WriteResult("track_observations", len(filtered), duplicates, elapsed_ms)

    async def _copy_rows(
        self,
        table_name: str,
        columns: list[str],
        records: list[tuple[Any, ...]],
    ) -> float:
        await self.connect()
        started = float(self.clock())
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.copy_records_to_table(
                    table_name,
                    records=records,
                    columns=columns,
                )
        return (float(self.clock()) - started) * 1000.0

    def _filter_detection_rows(self, rows: list[DetectionRow]) -> tuple[list[DetectionRow], int]:
        filtered: list[DetectionRow] = []
        seen_this_batch: set[tuple[str, int, str]] = set()
        duplicates = 0
        for row in rows:
            key = row.dedupe_key
            if key in seen_this_batch:
                duplicates += 1
                continue
            if not self._detection_cache.add_if_new(key):
                duplicates += 1
                continue
            seen_this_batch.add(key)
            filtered.append(self._normalise_detection_row(row))
        return filtered, duplicates

    def _filter_track_observation_rows(
        self,
        rows: list[TrackObservationRow],
    ) -> tuple[list[TrackObservationRow], int]:
        filtered: list[TrackObservationRow] = []
        seen_this_batch: set[tuple[str, int, str]] = set()
        duplicates = 0
        for row in rows:
            key = row.dedupe_key
            if key in seen_this_batch:
                duplicates += 1
                continue
            if not self._track_cache.add_if_new(key):
                duplicates += 1
                continue
            seen_this_batch.add(key)
            filtered.append(self._normalise_track_observation_row(row))
        return filtered, duplicates

    def _normalise_detection_row(self, row: DetectionRow) -> DetectionRow:
        timestamp = row.time.astimezone(timezone.utc)
        return DetectionRow(
            time=timestamp,
            camera_id=row.camera_id,
            frame_seq=int(row.frame_seq),
            object_class=str(row.object_class),
            confidence=float(row.confidence),
            bbox_x=float(row.bbox_x),
            bbox_y=float(row.bbox_y),
            bbox_w=float(row.bbox_w),
            bbox_h=float(row.bbox_h),
            local_track_id=row.local_track_id,
            model_version=str(row.model_version),
        )

    def _normalise_track_observation_row(self, row: TrackObservationRow) -> TrackObservationRow:
        timestamp = row.time.astimezone(timezone.utc)
        return TrackObservationRow(
            time=timestamp,
            camera_id=row.camera_id,
            frame_seq=int(row.frame_seq),
            local_track_id=row.local_track_id,
            centroid_x=float(row.centroid_x),
            centroid_y=float(row.centroid_y),
            bbox_area=float(row.bbox_area),
            embedding_ref=row.embedding_ref,
        )

