"""Quality gate for attribute extraction crops.

Checks bbox size, sharpness, brightness, IR/night mode, and occlusion
before allowing a crop to proceed to color classification.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class QualityResult:
    """Result of the quality gate check."""

    passed: bool
    reason: str | None  # rejection reason for metrics
    is_ir: bool  # IR/night mode detected
    quality_score: float  # [0.0, 1.0] composite


def check_quality(
    bbox_height_px: int,
    frame_height_px: int,
    frame_width_px: int,
    bbox_x: float,
    bbox_y: float,
    bbox_w: float,
    bbox_h: float,
    crop_bgr: np.ndarray,
    min_bbox_height: int = 40,
    min_sharpness: float = 50.0,
    brightness_range: tuple[int, int] = (30, 220),
    ir_saturation_threshold: int = 15,
    max_occlusion_ratio: float = 0.4,
) -> QualityResult:
    """Run all quality checks on a detection crop.

    Parameters
    ----------
    bbox_height_px : int
        Height of the bounding box in pixels.
    frame_height_px, frame_width_px : int
        Source frame dimensions (for occlusion check).
    bbox_x, bbox_y, bbox_w, bbox_h : float
        Normalized bounding box coordinates [0, 1].
    crop_bgr : np.ndarray
        Cropped region in BGR color space (H, W, 3).
    """
    # 1. Height check
    if bbox_height_px < min_bbox_height:
        return QualityResult(passed=False, reason="too_small", is_ir=False, quality_score=0.0)

    # 2. Sharpness (Laplacian variance)
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if laplacian_var < min_sharpness:
        return QualityResult(passed=False, reason="blurry", is_ir=False, quality_score=0.0)

    # 3. Brightness
    mean_brightness = float(np.mean(gray))
    if mean_brightness < brightness_range[0] or mean_brightness > brightness_range[1]:
        return QualityResult(passed=False, reason="exposure", is_ir=False, quality_score=0.0)

    # 4. IR/night mode detection (low saturation in HSV)
    hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
    mean_saturation = float(np.mean(hsv[:, :, 1]))
    is_ir = mean_saturation < ir_saturation_threshold
    if is_ir:
        return QualityResult(passed=False, reason="ir_mode", is_ir=True, quality_score=0.0)

    # 5. Occlusion — bbox clipped at frame edge
    x_min = bbox_x
    y_min = bbox_y
    x_max = bbox_x + bbox_w
    y_max = bbox_y + bbox_h

    visible_w = max(0.0, min(x_max, 1.0) - max(x_min, 0.0))
    visible_h = max(0.0, min(y_max, 1.0) - max(y_min, 0.0))
    bbox_area = bbox_w * bbox_h
    visible_area = visible_w * visible_h
    clipped_ratio = 1.0 - (visible_area / bbox_area) if bbox_area > 0 else 0.0
    if clipped_ratio > max_occlusion_ratio:
        return QualityResult(passed=False, reason="occluded", is_ir=False, quality_score=0.0)

    # Composite quality score
    sharpness_norm = min(laplacian_var / 500.0, 1.0)
    brightness_norm = 1.0 - abs(mean_brightness - 127.5) / 127.5
    size_norm = min(bbox_height_px / 200.0, 1.0)
    quality_score = 0.4 * sharpness_norm + 0.3 * brightness_norm + 0.3 * size_norm

    return QualityResult(
        passed=True,
        reason=None,
        is_ir=False,
        quality_score=max(0.0, min(1.0, quality_score)),
    )
