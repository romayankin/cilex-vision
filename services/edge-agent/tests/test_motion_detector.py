"""Tests for motion_detector.MotionDetector."""

from __future__ import annotations

import numpy as np
import pytest

from motion_detector import MotionDetector, _to_grayscale


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _solid_frame(value: int, h: int = 100, w: int = 100) -> np.ndarray:
    """Return an (H, W, 3) RGB frame with a uniform intensity."""
    return np.full((h, w, 3), value, dtype=np.uint8)


def _noisy_frame(
    base: int, changed_fraction: float, delta: int = 50,
    h: int = 100, w: int = 100,
) -> np.ndarray:
    """Return a frame where *changed_fraction* of pixels differ by *delta*."""
    frame = np.full((h, w, 3), base, dtype=np.uint8)
    n_pixels = h * w
    n_changed = int(n_pixels * changed_fraction)
    # Change the first n_changed pixels (row-major).
    flat = frame.reshape(-1, 3)
    flat[:n_changed] = np.clip(base + delta, 0, 255)
    return frame


# ---------------------------------------------------------------
# Tests
# ---------------------------------------------------------------

class TestMotionDetector:
    """Core motion-detection logic."""

    def test_first_frame_always_motion(self) -> None:
        md = MotionDetector()
        has_motion, is_scene = md.detect(_solid_frame(128))
        assert has_motion is True
        assert is_scene is True

    def test_identical_frames_no_motion(self) -> None:
        md = MotionDetector()
        frame = _solid_frame(128)
        md.detect(frame)  # first (reference)
        has_motion, is_scene = md.detect(frame.copy())
        assert has_motion is False
        assert is_scene is False

    def test_small_change_no_motion(self) -> None:
        """Change 1% of pixels — below default 2% threshold."""
        md = MotionDetector(motion_threshold=0.02)
        md.detect(_solid_frame(128))
        has_motion, _ = md.detect(_noisy_frame(128, 0.01, delta=50))
        assert has_motion is False

    def test_moderate_change_triggers_motion(self) -> None:
        """Change 5% of pixels — above 2% threshold."""
        md = MotionDetector(motion_threshold=0.02)
        md.detect(_solid_frame(128))
        has_motion, is_scene = md.detect(_noisy_frame(128, 0.05, delta=50))
        assert has_motion is True
        assert is_scene is False

    def test_large_change_triggers_scene_change(self) -> None:
        """Change 85% of pixels — above 80% scene-change threshold."""
        md = MotionDetector(scene_change_threshold=0.80)
        md.detect(_solid_frame(128))
        has_motion, is_scene = md.detect(_noisy_frame(128, 0.85, delta=80))
        assert has_motion is True
        assert is_scene is True

    def test_scene_change_resets_reference(self) -> None:
        """After a scene change, the *new* scene becomes the reference."""
        md = MotionDetector(scene_change_threshold=0.80)
        md.detect(_solid_frame(100))
        new_scene = _solid_frame(200)
        md.detect(new_scene)  # scene change → reference updated
        # Same frame again should show no motion.
        has_motion, _ = md.detect(new_scene.copy())
        assert has_motion is False

    def test_configurable_pixel_threshold(self) -> None:
        """Pixel differences below pixel_threshold are ignored."""
        md = MotionDetector(pixel_threshold=60, motion_threshold=0.02)
        md.detect(_solid_frame(128))
        # delta=30 is below pixel_threshold=60, so even if 100% of pixels
        # differ they won't count as changed.
        frame = _noisy_frame(128, 1.0, delta=30)
        has_motion, _ = md.detect(frame)
        assert has_motion is False

    def test_update_reference_manually(self) -> None:
        md = MotionDetector()
        md.detect(_solid_frame(100))
        md.update_reference(_solid_frame(200))
        has_motion, _ = md.detect(_solid_frame(200))
        assert has_motion is False


class TestGrayscaleConversion:
    """Edge cases for _to_grayscale."""

    def test_rgb_to_gray(self) -> None:
        rgb = np.zeros((2, 2, 3), dtype=np.uint8)
        rgb[:, :, 0] = 255  # pure red
        gray = _to_grayscale(rgb)
        assert gray.shape == (2, 2)
        assert gray.dtype == np.uint8
        # BT.601: 0.299 * 255 ≈ 76
        assert gray[0, 0] == pytest.approx(76, abs=1)

    def test_already_grayscale(self) -> None:
        gray_in = np.full((3, 3), 42, dtype=np.uint8)
        gray_out = _to_grayscale(gray_in)
        np.testing.assert_array_equal(gray_in, gray_out)
