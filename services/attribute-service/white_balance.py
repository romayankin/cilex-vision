"""White balance correction for attribute crops.

Applies OpenCV's SimpleWB algorithm to correct color cast before
classification.  Skipped for IR/night mode images.
"""

from __future__ import annotations

import cv2
import numpy as np


def apply_white_balance(crop_bgr: np.ndarray, is_ir: bool = False) -> np.ndarray:
    """Apply simple white balance correction to a BGR crop.

    Parameters
    ----------
    crop_bgr : np.ndarray
        Input crop in BGR color space (H, W, 3) uint8.
    is_ir : bool
        If True, skip correction (would distort grayscale IR images).

    Returns
    -------
    np.ndarray
        White-balanced BGR image, same shape and dtype as input.
    """
    if is_ir:
        return crop_bgr

    wb = cv2.xphoto.createSimpleWB()
    return wb.balanceWhite(crop_bgr)
