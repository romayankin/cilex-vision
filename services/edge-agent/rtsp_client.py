"""RTSP client with GStreamer decode and exponential-backoff reconnection.

The client builds a GStreamer pipeline that connects to an RTSP camera,
decodes the video to raw RGB frames, and exposes them via an async
``read_frame()`` method.

On connection loss or decoder error the client waits with exponential
backoff (1 s → 60 s max) before attempting to reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import numpy as np

from metrics import CAMERA_UPTIME, DECODE_ERRORS

logger = logging.getLogger(__name__)


@dataclass
class DecodedFrame:
    """Container for a decoded RGB frame pulled from GStreamer."""

    data: np.ndarray  # shape (H, W, 3), dtype uint8
    pts_ns: int  # GStreamer PTS in nanoseconds (stream-relative)
    width: int
    height: int
    sequence: int  # monotonic per-camera, reset on reconnect


class RtspClient:
    """GStreamer-backed RTSP camera reader with automatic reconnection."""

    MIN_BACKOFF_S: float = 1.0
    MAX_BACKOFF_S: float = 60.0

    def __init__(self, camera_id: str, rtsp_url: str) -> None:
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url

        self._pipeline = None  # Gst.Pipeline
        self._appsink = None  # GstApp.AppSink
        self._connected: bool = False
        self._backoff: float = self.MIN_BACKOFF_S
        self._frame_seq: int = 0

        # Health tracking
        self._connect_time: float | None = None
        self._total_connected_s: float = 0.0
        self._session_start: float = time.monotonic()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build and start the GStreamer RTSP pipeline."""
        import gi  # noqa: PLC0415

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst, GstApp  # noqa: PLC0415, F401

        if not Gst.is_initialized():
            Gst.init(None)

        pipeline_str = (
            f'rtspsrc location="{self.rtsp_url}" latency=100 protocols=tcp '
            "! decodebin "
            "! videoconvert "
            "! video/x-raw,format=RGB "
            "! appsink name=sink emit-signals=false "
            "max-buffers=2 drop=true sync=false"
        )
        self._pipeline = Gst.parse_launch(pipeline_str)
        self._appsink = self._pipeline.get_by_name("sink")

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_gst_error)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise ConnectionError(
                f"GStreamer pipeline failed to start for {self.camera_id}"
            )

        self._connected = True
        self._connect_time = time.monotonic()
        self._frame_seq = 0
        logger.info("Connected to %s (%s)", self.camera_id, self.rtsp_url)

    async def stop(self) -> None:
        """Tear down the GStreamer pipeline."""
        if self._pipeline is not None:
            import gi  # noqa: PLC0415

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst  # noqa: PLC0415

            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._appsink = None

        if self._connected and self._connect_time is not None:
            self._total_connected_s += time.monotonic() - self._connect_time
        self._connected = False
        self._connect_time = None

    # ------------------------------------------------------------------
    # Frame reading
    # ------------------------------------------------------------------

    async def read_frame(self) -> DecodedFrame | None:
        """Pull the next decoded RGB frame (blocks up to 1 s).

        Returns ``None`` on EOS, timeout, or if the pipeline is down.
        """
        if not self._connected or self._appsink is None:
            return None

        import gi  # noqa: PLC0415

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst, GstApp  # noqa: PLC0415, F401

        sample = await asyncio.to_thread(
            self._appsink.emit, "try-pull-sample", Gst.SECOND  # 1-second timeout
        )
        if sample is None:
            return None

        buf = sample.get_buffer()
        caps = sample.get_caps()
        struct = caps.get_structure(0)
        width = struct.get_value("width")
        height = struct.get_value("height")

        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            DECODE_ERRORS.labels(camera_id=self.camera_id).inc()
            return None

        try:
            frame_data = (
                np.frombuffer(mapinfo.data, dtype=np.uint8)
                .reshape((height, width, 3))
                .copy()
            )
        finally:
            buf.unmap(mapinfo)

        self._frame_seq += 1
        return DecodedFrame(
            data=frame_data,
            pts_ns=buf.pts,
            width=width,
            height=height,
            sequence=self._frame_seq,
        )

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    def reset_backoff(self) -> None:
        """Reset backoff to minimum (call after a stable connection)."""
        self._backoff = self.MIN_BACKOFF_S

    async def reconnect_with_backoff(self) -> None:
        """Stop the pipeline, wait with exponential backoff, then restart."""
        await self.stop()
        delay = self._backoff
        logger.warning(
            "Reconnecting %s in %.1fs (backoff)", self.camera_id, delay
        )
        await asyncio.sleep(delay)
        self._backoff = min(self._backoff * 2, self.MAX_BACKOFF_S)
        await self.start()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @property
    def uptime_ratio(self) -> float:
        """Fraction of wall-clock time the camera has been connected."""
        now = time.monotonic()
        elapsed = now - self._session_start
        if elapsed <= 0:
            return 0.0
        connected = self._total_connected_s
        if self._connected and self._connect_time is not None:
            connected += now - self._connect_time
        ratio = min(connected / elapsed, 1.0)
        CAMERA_UPTIME.labels(camera_id=self.camera_id).set(ratio)
        return ratio

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_gst_error(self, _bus: object, msg: object) -> None:
        """GStreamer error callback (runs on GStreamer thread)."""
        err, debug = msg.parse_error()  # type: ignore[union-attr]
        logger.error(
            "GStreamer error on %s: %s (%s)", self.camera_id, err, debug
        )
        DECODE_ERRORS.labels(camera_id=self.camera_id).inc()
        self._connected = False
