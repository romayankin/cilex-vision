"""Database writer for MTMC global track results.

Uses asyncpg connection pool for all writes.  Supports both single-row
parameterised queries and batch writes via COPY protocol.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


class DBWriter:
    """Async DB writer for global tracks and track links."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_global_track(
        self,
        object_class: str,
        first_seen: datetime,
        last_seen: datetime,
    ) -> UUID:
        """Insert a new global track and return its ID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO global_tracks (object_class, first_seen, last_seen) "
                "VALUES ($1, $2, $3) "
                "RETURNING global_track_id",
                object_class,
                first_seen,
                last_seen,
            )
            gid: UUID = row["global_track_id"]  # type: ignore[index]
            logger.debug("Created global track %s (%s)", gid, object_class)
            return gid

    async def create_global_track_link(
        self,
        global_track_id: UUID,
        local_track_id: UUID,
        camera_id: str,
        confidence: float,
        linked_at: datetime,
    ) -> UUID:
        """Insert a global-to-local track link."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO global_track_links "
                "(global_track_id, local_track_id, camera_id, confidence, linked_at) "
                "VALUES ($1, $2, $3, $4, $5) "
                "RETURNING link_id",
                global_track_id,
                local_track_id,
                camera_id,
                confidence,
                linked_at,
            )
            lid: UUID = row["link_id"]  # type: ignore[index]
            logger.debug(
                "Linked local_track %s -> global_track %s (conf=%.3f)",
                local_track_id,
                global_track_id,
                confidence,
            )
            return lid

    async def update_global_track_last_seen(
        self,
        global_track_id: UUID,
        last_seen: datetime,
    ) -> None:
        """Update the last_seen timestamp of a global track."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE global_tracks SET last_seen = $1 WHERE global_track_id = $2",
                last_seen,
                global_track_id,
            )

    async def batch_create_links(
        self,
        records: list[tuple[UUID, UUID, str, float, datetime]],
    ) -> None:
        """Batch insert global track links via COPY protocol.

        Each record: (global_track_id, local_track_id, camera_id, confidence, linked_at)
        """
        if not records:
            return
        async with self._pool.acquire() as conn:
            await conn.copy_records_to_table(
                "global_track_links",
                records=records,
                columns=[
                    "global_track_id",
                    "local_track_id",
                    "camera_id",
                    "confidence",
                    "linked_at",
                ],
            )
        logger.info("Batch inserted %d global track links", len(records))

    async def get_local_track_info(
        self,
        local_track_id: str,
    ) -> Optional[tuple[str, str]]:
        """Look up camera_id and object_class for a local track.

        Returns (camera_id, object_class) or None if not found.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT camera_id, object_class FROM local_tracks "
                "WHERE local_track_id = $1",
                local_track_id,
            )
        if row is None:
            return None
        return (row["camera_id"], row["object_class"])

    async def find_existing_global_track(
        self,
        local_track_id: str,
    ) -> Optional[UUID]:
        """Check if a local track is already linked to a global track."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT global_track_id FROM global_track_links "
                "WHERE local_track_id = $1 "
                "ORDER BY linked_at DESC LIMIT 1",
                local_track_id,
            )
        if row is None:
            return None
        return row["global_track_id"]

    async def get_track_colors(
        self,
        local_track_id: str,
    ) -> list[str]:
        """Get color attribute values for a local track."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT color_value FROM track_attributes "
                "WHERE local_track_id = $1 AND attribute_type IN "
                "('vehicle_color', 'person_upper_color', 'person_lower_color') "
                "ORDER BY confidence DESC LIMIT 3",
                local_track_id,
            )
        return [row["color_value"] for row in rows]
