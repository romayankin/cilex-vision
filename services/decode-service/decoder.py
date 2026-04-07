"""Codec-agnostic frame decoder.

Decodes encoded frame bytes (H.264 NAL units, H.265 NAL units, MJPEG)
to RGB numpy arrays at the configured inference resolution.

GStreamer is used for H.264/H.265 decode via ``appsrc → decodebin →
videoconvert → videoscale → appsink``.  For JPEG, Pillow is used
directly — it's simpler, dependency-free at test time, and sufficient
for single-frame decode.

GStreamer imports are **lazy** so tests can run without system GI
packages (same pattern as ``edge-agent/rtsp_client.py``).
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
from PIL import Image

from color_space import ensure_rgb
from metrics import DECODE_ERRORS, DECODE_LATENCY, FRAMES_DECODED

logger = logging.getLogger(__name__)

# Lazily initialised GStreamer flag
_gst_initialised = False


def _ensure_gst_init() -> None:
    """Initialise GStreamer once (lazy, thread-safe enough for our use)."""
    global _gst_initialised  # noqa: PLW0603
    if _gst_initialised:
        return

    import gi  # noqa: PLC0415

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: PLC0415

    if not Gst.is_initialized():
        Gst.init(None)
    _gst_initialised = True


def decode_jpeg(
    data: bytes,
    codec: str,
    width: int,
    height: int,
    output_width: int,
    output_height: int,
    colorimetry: str | None = None,
    default_color_space: str = "bt601",
) -> np.ndarray:
    """Decode a JPEG frame using Pillow.

    Args:
        data: Raw JPEG bytes.
        codec: Codec identifier (for color space detection).
        width: Source frame width (for color space heuristic).
        height: Source frame height (for color space heuristic).
        output_width: Target width.
        output_height: Target height.
        colorimetry: GStreamer colorimetry hint.
        default_color_space: Fallback color space.

    Returns:
        (output_height, output_width, 3) uint8 RGB array.
    """
    import io  # noqa: PLC0415

    img = Image.open(io.BytesIO(data))

    # Pillow decodes JPEG directly to RGB — no YCbCr→RGB needed
    img = img.convert("RGB")

    if img.width != output_width or img.height != output_height:
        img = img.resize((output_width, output_height), Image.BILINEAR)

    return np.array(img, dtype=np.uint8)


def decode_gstreamer(
    data: bytes,
    codec: str,
    width: int,
    height: int,
    output_width: int,
    output_height: int,
    colorimetry: str | None = None,
    default_color_space: str = "bt601",
) -> np.ndarray:
    """Decode a frame using GStreamer (H.264, H.265).

    Builds a one-shot pipeline: appsrc → decodebin → videoconvert →
    videoscale → appsink, pushes the encoded data, and pulls the
    decoded RGB frame.

    Args:
        data: Raw encoded frame bytes (NAL units with start codes).
        codec: Codec identifier (``h264``, ``h265``).
        width: Source frame width (for caps hint).
        height: Source frame height (for caps hint).
        output_width: Target width after decode.
        output_height: Target height after decode.
        colorimetry: GStreamer colorimetry hint.
        default_color_space: Fallback color space.

    Returns:
        (output_height, output_width, 3) uint8 RGB array.

    Raises:
        RuntimeError: If GStreamer decode fails.
    """
    _ensure_gst_init()

    import gi  # noqa: PLC0415

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: PLC0415

    caps_str = _codec_to_caps(codec, width, height)

    pipeline_str = (
        f"appsrc name=src caps={caps_str} "
        "! decodebin "
        "! videoconvert "
        f"! videoscale ! video/x-raw,format=RGB,"
        f"width={output_width},height={output_height} "
        "! appsink name=sink emit-signals=false sync=false"
    )

    pipeline = Gst.parse_launch(pipeline_str)
    appsrc = pipeline.get_by_name("src")
    appsink = pipeline.get_by_name("sink")

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError(f"GStreamer pipeline failed to start for codec={codec}")

    # Push data into appsrc
    buf = Gst.Buffer.new_allocate(None, len(data), None)
    buf.fill(0, data)
    appsrc.emit("push-buffer", buf)
    appsrc.emit("end-of-stream")

    # Pull decoded frame from appsink (5 s timeout)
    sample = appsink.try_pull_sample(5 * Gst.SECOND)

    pipeline.set_state(Gst.State.NULL)

    if sample is None:
        raise RuntimeError(f"GStreamer decode produced no output for codec={codec}")

    buf = sample.get_buffer()
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        raise RuntimeError("Failed to map GStreamer output buffer")

    try:
        frame = (
            np.frombuffer(mapinfo.data, dtype=np.uint8)
            .reshape((output_height, output_width, 3))
            .copy()
        )
    finally:
        buf.unmap(mapinfo)

    # Color space conversion if the pipeline produced YCbCr
    # (videoconvert should handle this, but ensure_rgb is a safety net)
    frame = ensure_rgb(
        frame, codec, width, height, colorimetry, default_color_space
    )

    return frame


def _codec_to_caps(codec: str, width: int, height: int) -> str:
    """Map codec name to GStreamer caps string for appsrc."""
    codec_lower = codec.lower()
    if codec_lower in ("h264", "avc"):
        return (
            f'"video/x-h264,stream-format=byte-stream,'
            f'width={width},height={height},framerate=0/1"'
        )
    if codec_lower in ("h265", "hevc"):
        return (
            f'"video/x-h265,stream-format=byte-stream,'
            f'width={width},height={height},framerate=0/1"'
        )
    if codec_lower in ("jpeg", "mjpeg", "jpg"):
        return f'"image/jpeg,width={width},height={height}"'
    return f'"video/x-raw,width={width},height={height}"'


class FrameDecoder:
    """High-level frame decoder with codec dispatch and metrics.

    Routes to Pillow for JPEG or GStreamer for H.264/H.265.
    """

    def __init__(
        self,
        output_width: int = 1280,
        output_height: int = 720,
        default_color_space: str = "bt601",
    ) -> None:
        self._output_width = output_width
        self._output_height = output_height
        self._default_color_space = default_color_space

    async def decode(
        self,
        data: bytes,
        codec: str,
        width: int,
        height: int,
        colorimetry: str | None = None,
    ) -> np.ndarray:
        """Decode encoded frame bytes to RGB numpy array.

        Args:
            data: Raw encoded frame bytes.
            codec: Codec of the frame (``jpeg``, ``h264``, ``h265``).
            width: Original frame width.
            height: Original frame height.
            colorimetry: GStreamer colorimetry hint (optional).

        Returns:
            (output_height, output_width, 3) uint8 RGB array.
        """
        t0 = time.monotonic()
        codec_lower = codec.lower()

        try:
            if codec_lower in ("jpeg", "mjpeg", "jpg"):
                frame = await asyncio.to_thread(
                    decode_jpeg,
                    data,
                    codec,
                    width,
                    height,
                    self._output_width,
                    self._output_height,
                    colorimetry,
                    self._default_color_space,
                )
            elif codec_lower in ("rgb", "raw"):
                # Already decoded — just reshape and resize
                frame = (
                    np.frombuffer(data, dtype=np.uint8)
                    .reshape((height, width, 3))
                    .copy()
                )
                if width != self._output_width or height != self._output_height:
                    img = Image.fromarray(frame)
                    img = img.resize(
                        (self._output_width, self._output_height),
                        Image.BILINEAR,
                    )
                    frame = np.array(img, dtype=np.uint8)
            else:
                # H.264, H.265 — use GStreamer
                frame = await asyncio.to_thread(
                    decode_gstreamer,
                    data,
                    codec,
                    width,
                    height,
                    self._output_width,
                    self._output_height,
                    colorimetry,
                    self._default_color_space,
                )

            FRAMES_DECODED.inc()
            elapsed_ms = (time.monotonic() - t0) * 1000
            DECODE_LATENCY.observe(elapsed_ms)
            return frame

        except Exception:
            DECODE_ERRORS.labels(codec=codec_lower).inc()
            raise
