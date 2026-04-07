"""Color space detection and conversion.

Handles BT.601 (SD) and BT.709 (HD) YCbCr → RGB conversion.

**Why this matters**: applying the wrong YCbCr→RGB matrix shifts hues
by 3–8% in the red/blue channels, which silently degrades detection
and attribute model accuracy. BT.601-vs-709 misdetection is the
single most common source of color-based model regression.

Detection heuristic (in priority order):

1. Explicit metadata from GStreamer caps ``colorimetry`` field.
2. Codec hint: JPEG (JFIF) → BT.601 by convention.
3. Resolution heuristic: width ≤ 720 → BT.601, otherwise BT.709.
4. Configurable default fallback (default: BT.601 for safety).
"""

from __future__ import annotations

import enum
import logging

import numpy as np

from metrics import COLOR_SPACE_CONVERSIONS

logger = logging.getLogger(__name__)


class ColorSpace(str, enum.Enum):
    """Supported YCbCr color spaces."""

    BT601 = "bt601"
    BT709 = "bt709"
    SRGB = "srgb"  # already RGB, no conversion needed


# ---------------------------------------------------------------
# YCbCr → RGB conversion matrices (full-range, 0-255)
# ---------------------------------------------------------------
#
# These matrices convert from YCbCr (Y=luma, Cb/Cr=chroma centered
# at 128) to RGB.  They are the exact inverses of the encoding
# matrices specified in ITU-R BT.601 and ITU-R BT.709.

# BT.601 (used by SD video, JPEG/JFIF default)
BT601_MATRIX = np.array(
    [
        [1.0, 0.0, 1.402],
        [1.0, -0.344136, -0.714136],
        [1.0, 1.772, 0.0],
    ],
    dtype=np.float64,
)

# BT.709 (used by HD video, ≥720p)
BT709_MATRIX = np.array(
    [
        [1.0, 0.0, 1.5748],
        [1.0, -0.1873, -0.4681],
        [1.0, 1.8556, 0.0],
    ],
    dtype=np.float64,
)


def detect_color_space(
    codec: str,
    width: int,
    height: int,
    colorimetry: str | None = None,
    default: str = "bt601",
) -> ColorSpace:
    """Detect the source color space from available metadata.

    Args:
        codec: Frame codec (``jpeg``, ``h264``, ``h265``, ``rgb``).
        width: Frame width in pixels.
        height: Frame height in pixels.
        colorimetry: GStreamer colorimetry string if available
                     (e.g. ``bt601``, ``bt709``, ``2:4:5:1``).
        default: Fallback color space.

    Returns:
        Detected :class:`ColorSpace`.
    """
    # 1. Explicit metadata
    if colorimetry:
        lower = colorimetry.lower()
        if "709" in lower:
            return ColorSpace.BT709
        if "601" in lower:
            return ColorSpace.BT601

    # 2. Already RGB (e.g., pre-decoded by edge agent)
    if codec.lower() in ("rgb", "raw"):
        return ColorSpace.SRGB

    # 3. JPEG → BT.601 by JFIF convention
    if codec.lower() in ("jpeg", "mjpeg", "jpg"):
        return ColorSpace.BT601

    # 4. Resolution heuristic for H.264/H.265
    #    SD (≤720 width) → BT.601, HD → BT.709
    if width <= 720:
        return ColorSpace.BT601
    return ColorSpace.BT709


def ycbcr_to_rgb(
    frame: np.ndarray,
    source: ColorSpace,
) -> np.ndarray:
    """Convert a YCbCr frame to RGB using the correct matrix.

    Args:
        frame: (H, W, 3) uint8 array in YCbCr order.
        source: Color space of the input.

    Returns:
        (H, W, 3) uint8 array in RGB order.
    """
    if source == ColorSpace.SRGB:
        return frame

    matrix = BT601_MATRIX if source == ColorSpace.BT601 else BT709_MATRIX

    ycbcr = frame.astype(np.float64)
    y = ycbcr[:, :, 0]
    cb = ycbcr[:, :, 1] - 128.0
    cr = ycbcr[:, :, 2] - 128.0

    r = y + matrix[0, 2] * cr
    g = y + matrix[1, 1] * cb + matrix[1, 2] * cr
    b = y + matrix[2, 1] * cb

    rgb = np.stack([r, g, b], axis=-1)
    np.clip(rgb, 0, 255, out=rgb)

    COLOR_SPACE_CONVERSIONS.labels(
        from_space=source.value, to_space="srgb"
    ).inc()

    return rgb.astype(np.uint8)


def ensure_rgb(
    frame: np.ndarray,
    codec: str,
    width: int,
    height: int,
    colorimetry: str | None = None,
    default: str = "bt601",
) -> np.ndarray:
    """Detect color space and convert to RGB if needed.

    This is the main entry point for color space handling.  For
    frames already in RGB (e.g., decoded by GStreamer with
    ``video/x-raw,format=RGB`` caps), this is a no-op.

    Args:
        frame: (H, W, 3) uint8 array.
        codec: Codec of the source frame.
        width: Source width (for heuristic).
        height: Source height (for heuristic).
        colorimetry: GStreamer colorimetry hint.
        default: Fallback color space.

    Returns:
        (H, W, 3) uint8 array guaranteed to be in sRGB.
    """
    cs = detect_color_space(codec, width, height, colorimetry, default)
    if cs == ColorSpace.SRGB:
        return frame
    return ycbcr_to_rgb(frame, cs)
