"""Quality gate for LPR crops."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class QualityResult:
    """Outcome of the LPR quality checks."""

    passed: bool
    reason: str | None
    sharpness: float
    aspect_ratio: float


def _grayscale(crop_rgb: np.ndarray) -> np.ndarray:
    return np.asarray(
        np.dot(crop_rgb[..., :3], np.array([0.299, 0.587, 0.114], dtype=np.float32)),
        dtype=np.float32,
    )


def _laplacian_variance(gray: np.ndarray) -> float:
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    center = gray[1:-1, 1:-1]
    lap = (
        gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
        - (4.0 * center)
    )
    return float(lap.var())


def check_quality(
    plate_crop_rgb: np.ndarray,
    *,
    min_plate_height: int = 20,
    min_plate_width: int = 60,
    sharpness_threshold: float = 40.0,
    min_aspect_ratio: float = 2.0,
    max_aspect_ratio: float = 5.0,
) -> QualityResult:
    """Validate a plate crop before OCR."""
    if plate_crop_rgb.size == 0:
        return QualityResult(False, "empty_crop", 0.0, 0.0)

    height, width = plate_crop_rgb.shape[:2]
    if height < min_plate_height or width < min_plate_width:
        return QualityResult(False, "too_small", 0.0, 0.0)

    aspect_ratio = float(width) / float(height)
    if aspect_ratio < min_aspect_ratio or aspect_ratio > max_aspect_ratio:
        return QualityResult(False, "bad_aspect_ratio", 0.0, aspect_ratio)

    gray = _grayscale(plate_crop_rgb.astype(np.float32))
    sharpness = _laplacian_variance(gray)
    if sharpness < sharpness_threshold:
        return QualityResult(False, "blurry", sharpness, aspect_ratio)

    return QualityResult(True, None, sharpness, aspect_ratio)
