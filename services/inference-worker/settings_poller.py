"""Polls the settings table for runtime-adjustable config.

Runs as a background asyncio task. Updates in-memory config objects so
changes take effect without a container restart. Poll interval: 30s.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 30

# Current effective values (read by /health endpoint)
_effective_values: dict[str, Any] = {}


def get_effective_values() -> dict[str, Any]:
    return dict(_effective_values)


async def start_polling(db_url: str, thumbnail_writer: Any) -> asyncio.Task:
    """Start the background polling task. Returns the task for cleanup."""
    # Seed effective value from the current writer so /health is populated
    # immediately, before the first DB poll completes.
    if thumbnail_writer is not None:
        _effective_values["thumbnail_max_per_track"] = (
            thumbnail_writer._cfg.max_per_track
        )
    return asyncio.create_task(_poll_loop(db_url, thumbnail_writer))


async def _poll_loop(db_url: str, thumbnail_writer: Any) -> None:
    import asyncpg  # noqa: PLC0415 — late import; only needed when polling runs

    pool: Optional[asyncpg.Pool] = None

    while True:
        try:
            if pool is None:
                pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)

            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT value FROM settings WHERE key = 'thumbnail_max_per_track'"
                )

            if row is not None:
                try:
                    new_val: int | None = int(row["value"])
                except (ValueError, TypeError):
                    new_val = None

                if new_val is not None and thumbnail_writer is not None:
                    old_val = thumbnail_writer._cfg.max_per_track
                    if new_val != old_val:
                        thumbnail_writer._cfg.max_per_track = new_val
                        logger.info(
                            "thumbnail_max_per_track updated: %d -> %d (from settings table)",
                            old_val,
                            new_val,
                        )
                    _effective_values["thumbnail_max_per_track"] = new_val
                elif thumbnail_writer is not None:
                    _effective_values["thumbnail_max_per_track"] = (
                        thumbnail_writer._cfg.max_per_track
                    )
            elif thumbnail_writer is not None:
                # No DB override — reflect the in-memory (env-var) value.
                _effective_values["thumbnail_max_per_track"] = (
                    thumbnail_writer._cfg.max_per_track
                )

        except Exception:
            logger.warning("Settings poll failed", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL_S)
