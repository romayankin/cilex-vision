"""Database writer for track attributes.

Uses asyncpg connection pool for all writes.  Supports single-row
parameterised queries and batch writes via executemany.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

import asyncpg

from aggregator import AggregatedAttribute
from metrics import DB_WRITE_LATENCY, TRACKS_FLUSHED_TOTAL

logger = logging.getLogger(__name__)


class DBWriter:
    """Async DB writer for track attributes."""

    def __init__(self, pool: asyncpg.Pool, model_version: str = "1.0.0") -> None:
        self._pool = pool
        self._model_version = model_version
        self._buffer: list[AggregatedAttribute] = []

    async def write_attribute(
        self,
        local_track_id: str,
        attribute_type: str,
        color_value: str,
        confidence: float,
        model_version: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Write a single attribute to the database."""
        t0 = time.monotonic()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO track_attributes "
                "(local_track_id, attribute_type, color_value, confidence, "
                "model_version, observed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                local_track_id,
                attribute_type,
                color_value,
                confidence,
                model_version or self._model_version,
                observed_at or datetime.utcnow(),
            )
        elapsed_ms = (time.monotonic() - t0) * 1000
        DB_WRITE_LATENCY.observe(elapsed_ms)

    async def write_aggregated(self, attrs: list[AggregatedAttribute]) -> None:
        """Write a batch of aggregated attributes."""
        if not attrs:
            return

        records = [
            (
                a.track_id,
                a.attribute_type,
                a.color_value,
                a.confidence,
                self._model_version,
                a.observed_at,
            )
            for a in attrs
        ]

        t0 = time.monotonic()
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO track_attributes "
                "(local_track_id, attribute_type, color_value, confidence, "
                "model_version, observed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                records,
            )
        elapsed_ms = (time.monotonic() - t0) * 1000
        DB_WRITE_LATENCY.observe(elapsed_ms)
        TRACKS_FLUSHED_TOTAL.inc()
        logger.info("Wrote %d attributes to DB", len(attrs))

    def buffer_attribute(self, attr: AggregatedAttribute) -> None:
        """Add an attribute to the write buffer."""
        self._buffer.append(attr)

    async def flush_buffer(self) -> None:
        """Flush the write buffer to the database."""
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        await self.write_aggregated(batch)

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    async def get_detection_bbox(
        self,
        camera_id: str,
        local_track_id: str,
    ) -> Optional[tuple[int, float, float, float, float]]:
        """Look up the latest detection bbox for a track.

        Returns (frame_seq, bbox_x, bbox_y, bbox_w, bbox_h) or None.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT frame_seq, bbox_x, bbox_y, bbox_w, bbox_h "
                "FROM detections "
                "WHERE camera_id = $1 AND local_track_id = $2 "
                "ORDER BY time DESC LIMIT 1",
                camera_id,
                local_track_id,
            )
        if row is None:
            return None
        return (
            int(row["frame_seq"]),
            float(row["bbox_x"]),
            float(row["bbox_y"]),
            float(row["bbox_w"]),
            float(row["bbox_h"]),
        )
