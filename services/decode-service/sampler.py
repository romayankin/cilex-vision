"""FPS-based frame sampler.

Decides whether to forward a decoded frame based on the configured
target FPS per camera.  Frames that arrive faster than the target rate
are skipped.

Each camera maintains its own timestamp so cameras at different source
FPS rates are sampled independently.
"""

from __future__ import annotations

import logging

from metrics import FRAMES_SAMPLED, FRAMES_SKIPPED

logger = logging.getLogger(__name__)


class FrameSampler:
    """Per-camera FPS-based frame sampler.

    Args:
        target_fps: Maximum frames per second to forward (per camera).
    """

    def __init__(self, target_fps: float = 5.0) -> None:
        if target_fps <= 0:
            raise ValueError(f"target_fps must be positive, got {target_fps}")
        self._target_fps = target_fps
        self._min_interval_s = 1.0 / target_fps
        # camera_id → last accepted timestamp (epoch seconds)
        self._last_accepted: dict[str, float] = {}

    @property
    def target_fps(self) -> float:
        return self._target_fps

    def should_sample(self, camera_id: str, frame_ts: float) -> bool:
        """Decide whether to forward this frame.

        Args:
            camera_id: Camera identifier.
            frame_ts: Frame timestamp in epoch seconds (edge_receive_ts).

        Returns:
            True if the frame should be forwarded.
        """
        last = self._last_accepted.get(camera_id)

        if last is None:
            # First frame from this camera — always accept
            self._last_accepted[camera_id] = frame_ts
            FRAMES_SAMPLED.inc()
            return True

        elapsed = frame_ts - last
        if elapsed >= self._min_interval_s:
            self._last_accepted[camera_id] = frame_ts
            FRAMES_SAMPLED.inc()
            return True

        FRAMES_SKIPPED.inc()
        return False

    def reset(self, camera_id: str) -> None:
        """Reset sampling state for a camera (e.g., on reconnect)."""
        self._last_accepted.pop(camera_id, None)
