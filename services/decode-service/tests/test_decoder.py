"""Tests for decoder.py — JPEG decode and GStreamer pipeline.

GStreamer is mocked so these tests run without system GI packages.
JPEG decode tests use real Pillow decoding with synthetic images.
"""

from __future__ import annotations

import io
import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Stub out ``gi`` before importing the module under test.
# Same pattern as edge-agent/tests/test_rtsp_client.py.
# ---------------------------------------------------------------------------

_gst_stub = types.ModuleType("gi")
_gst_stub.require_version = MagicMock()  # type: ignore[attr-defined]

_gst_repo = types.ModuleType("gi.repository")

_Gst = types.ModuleType("gi.repository.Gst")
_Gst.is_initialized = MagicMock(return_value=True)  # type: ignore[attr-defined]
_Gst.init = MagicMock()  # type: ignore[attr-defined]
_Gst.SECOND = 1_000_000_000  # type: ignore[attr-defined]


class _StateChangeReturn:
    FAILURE = 0
    SUCCESS = 1


class _State:
    NULL = 0
    PLAYING = 4


class _MapFlags:
    READ = 1


_Gst.StateChangeReturn = _StateChangeReturn  # type: ignore[attr-defined]
_Gst.State = _State  # type: ignore[attr-defined]
_Gst.MapFlags = _MapFlags  # type: ignore[attr-defined]

_GstApp = types.ModuleType("gi.repository.GstApp")

_gst_repo.Gst = _Gst  # type: ignore[attr-defined]
_gst_repo.GstApp = _GstApp  # type: ignore[attr-defined]

sys.modules["gi"] = _gst_stub
sys.modules["gi.repository"] = _gst_repo
sys.modules["gi.repository.Gst"] = _Gst
sys.modules["gi.repository.GstApp"] = _GstApp

from decoder import FrameDecoder, decode_jpeg  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg(width: int = 64, height: int = 48, color: tuple = (128, 64, 200)) -> bytes:
    """Create a synthetic JPEG image in memory."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _make_raw_rgb(width: int = 64, height: int = 48) -> bytes:
    """Create raw RGB bytes for testing."""
    return np.random.randint(0, 255, (height, width, 3), dtype=np.uint8).tobytes()


# ---------------------------------------------------------------------------
# JPEG decode tests (real Pillow, no GStreamer)
# ---------------------------------------------------------------------------


class TestDecodeJpeg:
    def test_basic_decode(self) -> None:
        data = _make_jpeg(64, 48)
        frame = decode_jpeg(data, "jpeg", 64, 48, 64, 48)
        assert frame.shape == (48, 64, 3)
        assert frame.dtype == np.uint8

    def test_resize_on_decode(self) -> None:
        data = _make_jpeg(640, 480)
        frame = decode_jpeg(data, "jpeg", 640, 480, 320, 240)
        assert frame.shape == (240, 320, 3)

    def test_resize_to_inference_resolution(self) -> None:
        data = _make_jpeg(1920, 1080)
        frame = decode_jpeg(data, "jpeg", 1920, 1080, 1280, 720)
        assert frame.shape == (720, 1280, 3)

    def test_color_preserved_approximately(self) -> None:
        # Solid red image
        data = _make_jpeg(32, 32, color=(255, 0, 0))
        frame = decode_jpeg(data, "jpeg", 32, 32, 32, 32)
        # JPEG is lossy, so check approximately
        assert frame[:, :, 0].mean() > 200  # R channel high
        assert frame[:, :, 2].mean() < 50  # B channel low

    def test_no_resize_when_matching(self) -> None:
        data = _make_jpeg(1280, 720)
        frame = decode_jpeg(data, "jpeg", 1280, 720, 1280, 720)
        assert frame.shape == (720, 1280, 3)


# ---------------------------------------------------------------------------
# FrameDecoder (high-level)
# ---------------------------------------------------------------------------


class TestFrameDecoder:
    @pytest.mark.asyncio
    async def test_jpeg_dispatch(self) -> None:
        decoder = FrameDecoder(output_width=320, output_height=240)
        data = _make_jpeg(640, 480)
        frame = await decoder.decode(data, "jpeg", 640, 480)
        assert frame.shape == (240, 320, 3)

    @pytest.mark.asyncio
    async def test_mjpeg_dispatch(self) -> None:
        decoder = FrameDecoder(output_width=320, output_height=240)
        data = _make_jpeg(640, 480)
        frame = await decoder.decode(data, "mjpeg", 640, 480)
        assert frame.shape == (240, 320, 3)

    @pytest.mark.asyncio
    async def test_raw_rgb_dispatch(self) -> None:
        decoder = FrameDecoder(output_width=32, output_height=24)
        raw = _make_raw_rgb(64, 48)
        frame = await decoder.decode(raw, "rgb", 64, 48)
        assert frame.shape == (24, 32, 3)

    @pytest.mark.asyncio
    async def test_raw_rgb_no_resize_when_matching(self) -> None:
        decoder = FrameDecoder(output_width=64, output_height=48)
        raw = _make_raw_rgb(64, 48)
        frame = await decoder.decode(raw, "raw", 64, 48)
        assert frame.shape == (48, 64, 3)

    @pytest.mark.asyncio
    async def test_h264_calls_gstreamer(self) -> None:
        """Verify H.264 dispatches to decode_gstreamer (mocked)."""
        decoder = FrameDecoder(output_width=320, output_height=240)
        fake_frame = np.zeros((240, 320, 3), dtype=np.uint8)

        with patch("decoder.decode_gstreamer", return_value=fake_frame) as mock_gst:
            frame = await decoder.decode(b"\x00\x00\x00\x01", "h264", 1920, 1080)
            mock_gst.assert_called_once()
            assert frame.shape == (240, 320, 3)

    @pytest.mark.asyncio
    async def test_h265_calls_gstreamer(self) -> None:
        decoder = FrameDecoder(output_width=320, output_height=240)
        fake_frame = np.zeros((240, 320, 3), dtype=np.uint8)

        with patch("decoder.decode_gstreamer", return_value=fake_frame) as mock_gst:
            frame = await decoder.decode(b"\x00\x00\x00\x01", "h265", 1920, 1080)
            mock_gst.assert_called_once()
            assert frame.shape == (240, 320, 3)

    @pytest.mark.asyncio
    async def test_decode_error_increments_metric(self) -> None:
        decoder = FrameDecoder(output_width=320, output_height=240)

        with pytest.raises(Exception):
            await decoder.decode(b"not-valid-jpeg", "jpeg", 640, 480)


# ---------------------------------------------------------------------------
# GStreamer pipeline (mocked)
# ---------------------------------------------------------------------------


class TestDecodeGstreamer:
    def test_codec_to_caps_h264(self) -> None:
        from decoder import _codec_to_caps

        caps = _codec_to_caps("h264", 1920, 1080)
        assert "video/x-h264" in caps
        assert "byte-stream" in caps

    def test_codec_to_caps_h265(self) -> None:
        from decoder import _codec_to_caps

        caps = _codec_to_caps("h265", 1920, 1080)
        assert "video/x-h265" in caps

    def test_codec_to_caps_jpeg(self) -> None:
        from decoder import _codec_to_caps

        caps = _codec_to_caps("jpeg", 640, 480)
        assert "image/jpeg" in caps

    def test_codec_to_caps_unknown(self) -> None:
        from decoder import _codec_to_caps

        caps = _codec_to_caps("vp9", 1280, 720)
        assert "video/x-raw" in caps
