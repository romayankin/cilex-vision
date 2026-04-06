"""Frame-differencing motion detector with scene-change detection.

The detector compares each incoming frame against a stored reference frame.
A frame is forwarded when the fraction of changed pixels exceeds
``motion_threshold``.  A sudden large change (above ``scene_change_threshold``)
is treated as a scene change — the reference is replaced and the frame is
forwarded, but downstream can distinguish the two cases.

The reference frame is also refreshed periodically
(``reference_update_interval_s``) to adapt to gradual lighting changes.
"""

from __future__ import annotations

import logging
import time

import numpy as np

logger = logging.getLogger(__name__)


class MotionDetector:
    """Configurable motion detector using pixel-level frame differencing."""

    def __init__(
        self,
        pixel_threshold: int = 25,
        motion_threshold: float = 0.02,
        scene_change_threshold: float = 0.80,
        reference_update_interval_s: int = 300,
    ) -> None:
        self.pixel_threshold = pixel_threshold
        self.motion_threshold = motion_threshold
        self.scene_change_threshold = scene_change_threshold
        self.reference_update_interval_s = reference_update_interval_s

        self._ref_frame: np.ndarray | None = None
        self._ref_updated_at: float = 0.0

    def detect(self, frame: np.ndarray) -> tuple[bool, bool]:
        """Evaluate a decoded frame for motion.

        Parameters
        ----------
        frame:
            RGB (H, W, 3) or grayscale (H, W) uint8 array.

        Returns
        -------
        (has_motion, is_scene_change)
            *has_motion* is ``True`` when the frame should be forwarded.
            *is_scene_change* is ``True`` when the reference was reset.
        """
        gray = _to_grayscale(frame)
        now = time.monotonic()

        # First frame is always forwarded and becomes the reference.
        if self._ref_frame is None:
            self._ref_frame = gray
            self._ref_updated_at = now
            return True, True

        # Periodic reference refresh for gradual lighting drift.
        if now - self._ref_updated_at >= self.reference_update_interval_s:
            self._ref_frame = gray
            self._ref_updated_at = now

        diff = np.abs(gray.astype(np.int16) - self._ref_frame.astype(np.int16))
        changed = int(np.count_nonzero(diff > self.pixel_threshold))
        total = gray.size
        ratio = changed / total if total else 0.0

        is_scene_change = ratio >= self.scene_change_threshold
        has_motion = ratio >= self.motion_threshold

        if is_scene_change:
            self._ref_frame = gray
            self._ref_updated_at = now
            logger.debug("scene change detected (ratio=%.3f)", ratio)

        return has_motion, is_scene_change

    def update_reference(self, frame: np.ndarray) -> None:
        """Force-replace the reference frame."""
        self._ref_frame = _to_grayscale(frame)
        self._ref_updated_at = time.monotonic()


def _to_grayscale(frame: np.ndarray) -> np.ndarray:
    """Convert an RGB frame to uint8 grayscale using BT.601 luma."""
    if frame.ndim == 3 and frame.shape[2] >= 3:
        return (
            0.299 * frame[:, :, 0]
            + 0.587 * frame[:, :, 1]
            + 0.114 * frame[:, :, 2]
        ).astype(np.uint8)
    return frame.astype(np.uint8) if frame.dtype != np.uint8 else frame
