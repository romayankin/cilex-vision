"""Segment-range URI helpers for continuous-mode motion events.

`range:<camera_id>:<start_iso>|<end_iso>` is resolved at play time by the
/clips/range backend endpoint (Phase 9) which concatenates whichever
video_segments overlap the window. The delimiter between timestamps is
`|` because ISO-8601 already uses `:` inside the timestamp.
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def build_segment_range_uri(
    camera_id: str,
    start: datetime,
    end: datetime,
) -> str:
    """Return a `range:` URI for the motion window.

    We do NOT verify segment existence at this point — recorder-service
    writes segments asynchronously in 30s chunks, so a short motion event
    often closes before its covering segment has been finalized and
    indexed. The URI is resolved at play time by /clips/range (Phase 9),
    which is already the authoritative check for segment availability
    (it also handles hot->warm tier migration).
    """
    return f"range:{camera_id}:{start.isoformat()}|{end.isoformat()}"
