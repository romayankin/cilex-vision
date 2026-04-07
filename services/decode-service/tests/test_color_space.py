"""Tests for color_space.py — detection heuristic and YCbCr→RGB conversion."""

from __future__ import annotations

import numpy as np

from color_space import (
    ColorSpace,
    detect_color_space,
    ensure_rgb,
    ycbcr_to_rgb,
)


# ---------------------------------------------------------------------------
# Detection heuristic
# ---------------------------------------------------------------------------


class TestDetectColorSpace:
    def test_explicit_bt709_metadata(self) -> None:
        cs = detect_color_space("h264", 1920, 1080, colorimetry="bt709")
        assert cs == ColorSpace.BT709

    def test_explicit_bt601_metadata(self) -> None:
        cs = detect_color_space("h264", 1920, 1080, colorimetry="bt601")
        assert cs == ColorSpace.BT601

    def test_gstreamer_colorimetry_format(self) -> None:
        # GStreamer uses "2:4:5:1" format for BT.709
        cs = detect_color_space("h264", 1920, 1080, colorimetry="2:4:709:1")
        assert cs == ColorSpace.BT709

    def test_rgb_codec_returns_srgb(self) -> None:
        cs = detect_color_space("rgb", 1920, 1080)
        assert cs == ColorSpace.SRGB

    def test_raw_codec_returns_srgb(self) -> None:
        cs = detect_color_space("raw", 1920, 1080)
        assert cs == ColorSpace.SRGB

    def test_jpeg_returns_bt601(self) -> None:
        cs = detect_color_space("jpeg", 1920, 1080)
        assert cs == ColorSpace.BT601

    def test_mjpeg_returns_bt601(self) -> None:
        cs = detect_color_space("mjpeg", 640, 480)
        assert cs == ColorSpace.BT601

    def test_hd_resolution_returns_bt709(self) -> None:
        cs = detect_color_space("h264", 1920, 1080)
        assert cs == ColorSpace.BT709

    def test_sd_resolution_returns_bt601(self) -> None:
        cs = detect_color_space("h264", 720, 480)
        assert cs == ColorSpace.BT601

    def test_sd_boundary_720_returns_bt601(self) -> None:
        cs = detect_color_space("h264", 720, 576)
        assert cs == ColorSpace.BT601

    def test_above_sd_returns_bt709(self) -> None:
        cs = detect_color_space("h264", 1280, 720)
        assert cs == ColorSpace.BT709

    def test_metadata_overrides_resolution(self) -> None:
        # 720p frame but metadata says BT.601
        cs = detect_color_space("h264", 1920, 1080, colorimetry="bt601")
        assert cs == ColorSpace.BT601


# ---------------------------------------------------------------------------
# YCbCr → RGB conversion
# ---------------------------------------------------------------------------


class TestYcbcrToRgb:
    def test_srgb_passthrough(self) -> None:
        frame = np.random.randint(0, 255, (4, 4, 3), dtype=np.uint8)
        result = ycbcr_to_rgb(frame, ColorSpace.SRGB)
        np.testing.assert_array_equal(result, frame)

    def test_bt601_pure_white(self) -> None:
        # YCbCr for white: Y=235, Cb=128, Cr=128 (full-range: Y=255)
        frame = np.full((2, 2, 3), [255, 128, 128], dtype=np.uint8)
        result = ycbcr_to_rgb(frame, ColorSpace.BT601)
        # White should map to approximately (255, 255, 255)
        assert result.dtype == np.uint8
        assert result[0, 0, 0] == 255  # R
        assert result[0, 0, 1] == 255  # G
        assert result[0, 0, 2] == 255  # B

    def test_bt601_pure_black(self) -> None:
        # YCbCr for black: Y=0, Cb=128, Cr=128
        frame = np.full((2, 2, 3), [0, 128, 128], dtype=np.uint8)
        result = ycbcr_to_rgb(frame, ColorSpace.BT601)
        assert result[0, 0, 0] == 0  # R
        assert result[0, 0, 1] == 0  # G
        assert result[0, 0, 2] == 0  # B

    def test_bt709_pure_white(self) -> None:
        frame = np.full((2, 2, 3), [255, 128, 128], dtype=np.uint8)
        result = ycbcr_to_rgb(frame, ColorSpace.BT709)
        assert result[0, 0, 0] == 255
        assert result[0, 0, 1] == 255
        assert result[0, 0, 2] == 255

    def test_bt601_vs_bt709_differ_on_color(self) -> None:
        # A coloured pixel should produce different RGB values with
        # different matrices — this is the core of the BT.601-vs-709 issue.
        frame = np.full((2, 2, 3), [180, 100, 200], dtype=np.uint8)
        rgb_601 = ycbcr_to_rgb(frame, ColorSpace.BT601)
        rgb_709 = ycbcr_to_rgb(frame, ColorSpace.BT709)
        # Results must differ (wrong matrix → wrong colours)
        assert not np.array_equal(rgb_601, rgb_709)

    def test_output_clipped_to_0_255(self) -> None:
        # Extreme values that would overflow without clipping
        frame = np.full((2, 2, 3), [255, 0, 255], dtype=np.uint8)
        result = ycbcr_to_rgb(frame, ColorSpace.BT601)
        assert result.min() >= 0
        assert result.max() <= 255

    def test_output_shape_preserved(self) -> None:
        frame = np.random.randint(0, 255, (10, 15, 3), dtype=np.uint8)
        result = ycbcr_to_rgb(frame, ColorSpace.BT601)
        assert result.shape == (10, 15, 3)
        assert result.dtype == np.uint8


# ---------------------------------------------------------------------------
# ensure_rgb (integration)
# ---------------------------------------------------------------------------


class TestEnsureRgb:
    def test_rgb_codec_no_conversion(self) -> None:
        frame = np.random.randint(0, 255, (4, 4, 3), dtype=np.uint8)
        result = ensure_rgb(frame, "rgb", 1920, 1080)
        np.testing.assert_array_equal(result, frame)

    def test_jpeg_uses_bt601(self) -> None:
        frame = np.full((2, 2, 3), [180, 100, 200], dtype=np.uint8)
        result_jpeg = ensure_rgb(frame, "jpeg", 1920, 1080)
        result_bt601 = ycbcr_to_rgb(frame.copy(), ColorSpace.BT601)
        np.testing.assert_array_equal(result_jpeg, result_bt601)

    def test_hd_h264_uses_bt709(self) -> None:
        frame = np.full((2, 2, 3), [180, 100, 200], dtype=np.uint8)
        result = ensure_rgb(frame, "h264", 1920, 1080)
        result_bt709 = ycbcr_to_rgb(frame.copy(), ColorSpace.BT709)
        np.testing.assert_array_equal(result, result_bt709)
