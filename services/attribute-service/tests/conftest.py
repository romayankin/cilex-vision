"""Shared test fixtures for attribute-service tests."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# Add service root to sys.path for plain imports.
SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


@pytest.fixture
def bright_daytime_crop() -> np.ndarray:
    """Synthetic 100x80 BGR image simulating a bright daytime scene."""
    rng = np.random.default_rng(42)
    # Moderately bright, colorful image
    img = rng.integers(80, 200, size=(100, 80, 3), dtype=np.uint8)
    # Add some structure (edges) for sharpness
    img[30:70, 20:60, :] = rng.integers(40, 120, size=(40, 40, 3), dtype=np.uint8)
    return img


@pytest.fixture
def tiny_crop() -> np.ndarray:
    """Synthetic 15x20 BGR crop (too small)."""
    return np.full((15, 20, 3), 128, dtype=np.uint8)


@pytest.fixture
def blurry_crop() -> np.ndarray:
    """Synthetic 100x80 BGR crop with heavy gaussian blur."""
    import cv2
    rng = np.random.default_rng(42)
    img = rng.integers(80, 200, size=(100, 80, 3), dtype=np.uint8)
    return cv2.GaussianBlur(img, (31, 31), 15)


@pytest.fixture
def dark_crop() -> np.ndarray:
    """Synthetic 100x80 BGR crop — very dark (mean < 30)."""
    rng = np.random.default_rng(42)
    return rng.integers(0, 25, size=(100, 80, 3), dtype=np.uint8)


@pytest.fixture
def ir_crop() -> np.ndarray:
    """Synthetic 100x80 BGR crop simulating IR/night mode (grayscale, low saturation)."""
    gray_val = np.random.default_rng(42).integers(60, 180, size=(100, 80), dtype=np.uint8)
    # Stack to 3-channel but keep nearly identical across channels (low saturation)
    return np.stack([gray_val, gray_val, gray_val + 1], axis=-1).astype(np.uint8)
