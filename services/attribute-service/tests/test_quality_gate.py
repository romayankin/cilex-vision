"""Tests for the attribute quality gate.

Verifies each rejection path: too_small, blurry, exposure, ir_mode,
occluded, and a passing bright daytime case.
"""

from __future__ import annotations

import numpy as np

from quality_gate import check_quality


def test_bright_daytime_passes(bright_daytime_crop: np.ndarray) -> None:
    """A good quality daytime crop should pass all checks."""
    result = check_quality(
        bbox_height_px=100,
        frame_height_px=720,
        frame_width_px=1280,
        bbox_x=0.2,
        bbox_y=0.1,
        bbox_w=0.1,
        bbox_h=0.14,
        crop_bgr=bright_daytime_crop,
    )
    assert result.passed is True
    assert result.reason is None
    assert result.is_ir is False
    assert 0.0 < result.quality_score <= 1.0


def test_tiny_bbox_rejected() -> None:
    """A bbox shorter than min_bbox_height should be rejected."""
    crop = np.full((20, 30, 3), 128, dtype=np.uint8)
    result = check_quality(
        bbox_height_px=20,
        frame_height_px=720,
        frame_width_px=1280,
        bbox_x=0.3,
        bbox_y=0.3,
        bbox_w=0.05,
        bbox_h=0.03,
        crop_bgr=crop,
        min_bbox_height=40,
    )
    assert result.passed is False
    assert result.reason == "too_small"


def test_blurry_rejected(blurry_crop: np.ndarray) -> None:
    """A heavily blurred crop should be rejected."""
    result = check_quality(
        bbox_height_px=100,
        frame_height_px=720,
        frame_width_px=1280,
        bbox_x=0.2,
        bbox_y=0.1,
        bbox_w=0.1,
        bbox_h=0.14,
        crop_bgr=blurry_crop,
        min_sharpness=50.0,
    )
    assert result.passed is False
    assert result.reason == "blurry"


def test_dark_exposure_rejected(dark_crop: np.ndarray) -> None:
    """A very dark crop should be rejected for exposure."""
    result = check_quality(
        bbox_height_px=100,
        frame_height_px=720,
        frame_width_px=1280,
        bbox_x=0.2,
        bbox_y=0.1,
        bbox_w=0.1,
        bbox_h=0.14,
        crop_bgr=dark_crop,
        brightness_range=(30, 220),
    )
    assert result.passed is False
    assert result.reason == "exposure"


def test_bright_exposure_rejected() -> None:
    """An overexposed crop (mean > 220) should be rejected."""
    # High brightness but with enough structure to pass sharpness check
    crop = np.full((100, 80, 3), 240, dtype=np.uint8)
    # Checkerboard pattern for sharpness
    for i in range(0, 100, 4):
        for j in range(0, 80, 4):
            if (i // 4 + j // 4) % 2 == 0:
                crop[i:i + 4, j:j + 4, :] = 230
    result = check_quality(
        bbox_height_px=100,
        frame_height_px=720,
        frame_width_px=1280,
        bbox_x=0.2,
        bbox_y=0.1,
        bbox_w=0.1,
        bbox_h=0.14,
        crop_bgr=crop,
        brightness_range=(30, 220),
    )
    assert result.passed is False
    assert result.reason == "exposure"


def test_ir_mode_detected(ir_crop: np.ndarray) -> None:
    """An IR/night-mode crop (low saturation) should be flagged."""
    result = check_quality(
        bbox_height_px=100,
        frame_height_px=720,
        frame_width_px=1280,
        bbox_x=0.2,
        bbox_y=0.1,
        bbox_w=0.1,
        bbox_h=0.14,
        crop_bgr=ir_crop,
        ir_saturation_threshold=15,
    )
    assert result.passed is False
    assert result.reason == "ir_mode"
    assert result.is_ir is True


def test_edge_clipped_occluded() -> None:
    """A bbox significantly clipped at the frame edge should be rejected."""
    # Create a good quality crop
    rng = np.random.default_rng(42)
    crop = rng.integers(80, 200, size=(100, 80, 3), dtype=np.uint8)
    crop[30:70, 20:60, :] = rng.integers(40, 120, size=(40, 40, 3), dtype=np.uint8)

    result = check_quality(
        bbox_height_px=100,
        frame_height_px=720,
        frame_width_px=1280,
        # bbox extends well beyond right edge (>50% clipped)
        bbox_x=0.7,
        bbox_y=0.1,
        bbox_w=0.6,
        bbox_h=0.14,
        crop_bgr=crop,
        max_occlusion_ratio=0.4,
    )
    assert result.passed is False
    assert result.reason == "occluded"


def test_quality_score_range(bright_daytime_crop: np.ndarray) -> None:
    """Quality score must be in [0, 1] for passing crops."""
    result = check_quality(
        bbox_height_px=150,
        frame_height_px=720,
        frame_width_px=1280,
        bbox_x=0.2,
        bbox_y=0.1,
        bbox_w=0.1,
        bbox_h=0.21,
        crop_bgr=bright_daytime_crop,
    )
    assert result.passed is True
    assert 0.0 <= result.quality_score <= 1.0
