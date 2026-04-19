"""Segment-range URI helpers for continuous-mode motion events.

`range:<camera_id>:<start_iso>|<end_iso>` is resolved at play time by the
/clips/range backend endpoint (Phase 9) which concatenates whichever
video_segments overlap the window. The delimiter between timestamps is
`|` because ISO-8601 already uses `:` inside the timestamp.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


async def build_segment_range_uri(
    pool: asyncpg.Pool,
    camera_id: str,
    start: datetime,
    end: datetime,
) -> Optional[str]:
    """Return a playable `range:` URI when hot segments cover the window.

    Returns None when no hot video_segments overlap the window — typically
    because the recorder was down. Callers should persist None rather than
    a URI that would 404 at play time.
    """
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM video_segments
                WHERE camera_id = $1 AND tier = 'hot'
                  AND start_time < $3 AND end_time > $2
            )
            """,
            camera_id, start, end,
        )

    if not exists:
        logger.warning(
            "No segments overlap motion window on %s (%s to %s); clip_uri will be null",
            camera_id, start.isoformat(), end.isoformat(),
        )
        return None

    return f"range:{camera_id}:{start.isoformat()}|{end.isoformat()}"
