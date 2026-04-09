"""Database helpers for event clip asset persistence."""

from __future__ import annotations

import json
from typing import cast

import asyncpg


class ClipDBUpdater:
    """Read and update event asset metadata in PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._site_cache: dict[str, str] = {}

    async def get_existing_clip_uri(self, event_id: str) -> str | None:
        """Return the current clip URI for the event if already populated."""
        sql = "SELECT clip_uri FROM events WHERE event_id = $1::uuid"
        async with self._pool.acquire() as conn:
            return cast(str | None, await conn.fetchval(sql, event_id))

    async def get_camera_site_id(self, camera_id: str) -> str | None:
        """Return the site's UUID string for the camera, cached in-process."""
        cached = self._site_cache.get(camera_id)
        if cached is not None:
            return cached

        sql = "SELECT site_id::text FROM cameras WHERE camera_id = $1"
        async with self._pool.acquire() as conn:
            site_id = await conn.fetchval(sql, camera_id)

        if site_id is not None:
            site_id_text = str(site_id)
            self._site_cache[camera_id] = site_id_text
            return site_id_text
        return None

    async def update_event_assets(
        self,
        event_id: str,
        clip_uri: str,
        thumbnail_uri: str,
    ) -> None:
        """Persist clip_uri and thumbnail_uri metadata for the event."""
        metadata_payload = json.dumps({"thumbnail_uri": thumbnail_uri})
        sql = """
            UPDATE events
            SET clip_uri = $2,
                metadata_jsonb = COALESCE(metadata_jsonb, '{}'::jsonb) || $3::jsonb
            WHERE event_id = $1::uuid
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                sql,
                event_id,
                clip_uri,
                metadata_payload,
            )
