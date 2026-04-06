"""Tests for rtsp_client.RtspClient — reconnection and health scoring.

GStreamer is mocked so these tests run without a real RTSP camera or
the ``gi`` system package.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out ``gi`` before importing the module under test so we don't need
# the system GObject-Introspection packages.
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

from rtsp_client import RtspClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_pipeline(state_ret=_StateChangeReturn.SUCCESS):
    """Return a mock GStreamer pipeline + appsink."""
    pipeline = MagicMock()
    pipeline.set_state.return_value = state_ret
    appsink = MagicMock()
    pipeline.get_by_name.return_value = appsink
    bus = MagicMock()
    pipeline.get_bus.return_value = bus

    _Gst.parse_launch = MagicMock(return_value=pipeline)  # type: ignore[attr-defined]
    return pipeline, appsink


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBackoff:
    """Exponential backoff on reconnection."""

    @pytest.mark.asyncio
    async def test_backoff_doubles(self) -> None:
        client = RtspClient("cam-1", "rtsp://fake")
        _mock_pipeline()

        delays: list[float] = []
        _orig_sleep = asyncio.sleep

        async def _capture_sleep(d: float) -> None:
            delays.append(d)

        with patch("rtsp_client.asyncio.sleep", side_effect=_capture_sleep):
            # Three reconnect cycles: 1s, 2s, 4s
            for _ in range(3):
                await client.reconnect_with_backoff()

        assert delays == pytest.approx([1.0, 2.0, 4.0])

    @pytest.mark.asyncio
    async def test_backoff_caps_at_max(self) -> None:
        client = RtspClient("cam-2", "rtsp://fake")
        _mock_pipeline()

        delays: list[float] = []

        async def _capture_sleep(d: float) -> None:
            delays.append(d)

        with patch("rtsp_client.asyncio.sleep", side_effect=_capture_sleep):
            for _ in range(8):
                await client.reconnect_with_backoff()

        # 1, 2, 4, 8, 16, 32, 60, 60
        assert delays[-1] == 60.0
        assert delays[-2] == 60.0

    @pytest.mark.asyncio
    async def test_backoff_resets_via_reset_backoff(self) -> None:
        client = RtspClient("cam-3", "rtsp://fake")
        _mock_pipeline()

        # Reconnect twice to bump backoff to 4s.
        with patch("rtsp_client.asyncio.sleep", new_callable=AsyncMock):
            await client.reconnect_with_backoff()
            await client.reconnect_with_backoff()

        assert client._backoff == 4.0

        # Explicit reset (called by CameraPipeline after stable start).
        client.reset_backoff()
        assert client._backoff == RtspClient.MIN_BACKOFF_S


class TestHealth:
    """Uptime ratio calculation."""

    @pytest.mark.asyncio
    async def test_uptime_starts_zero(self) -> None:
        client = RtspClient("cam-4", "rtsp://fake")
        assert client.uptime_ratio == pytest.approx(0.0, abs=0.05)

    @pytest.mark.asyncio
    async def test_uptime_after_connect(self) -> None:
        client = RtspClient("cam-5", "rtsp://fake")
        _mock_pipeline()
        await client.start()
        await asyncio.sleep(0.05)
        assert client.uptime_ratio > 0.0

    @pytest.mark.asyncio
    async def test_start_failure_raises(self) -> None:
        client = RtspClient("cam-6", "rtsp://fake")
        _mock_pipeline(state_ret=_StateChangeReturn.FAILURE)
        with pytest.raises(ConnectionError):
            await client.start()
