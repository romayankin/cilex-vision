"""Tests for sampler.py — FPS-based frame sampling."""

from __future__ import annotations

import pytest

from sampler import FrameSampler


class TestFrameSampler:
    def test_first_frame_always_accepted(self) -> None:
        sampler = FrameSampler(target_fps=5.0)
        assert sampler.should_sample("cam-1", 1000.0) is True

    def test_frame_within_interval_skipped(self) -> None:
        sampler = FrameSampler(target_fps=5.0)  # 200ms interval
        sampler.should_sample("cam-1", 1000.0)
        # 100ms later — too soon
        assert sampler.should_sample("cam-1", 1000.1) is False

    def test_frame_at_interval_accepted(self) -> None:
        sampler = FrameSampler(target_fps=5.0)  # 200ms interval
        sampler.should_sample("cam-1", 1000.0)
        # Exactly 200ms later
        assert sampler.should_sample("cam-1", 1000.2) is True

    def test_frame_after_interval_accepted(self) -> None:
        sampler = FrameSampler(target_fps=5.0)
        sampler.should_sample("cam-1", 1000.0)
        # 500ms later
        assert sampler.should_sample("cam-1", 1000.5) is True

    def test_cameras_independent(self) -> None:
        sampler = FrameSampler(target_fps=5.0)
        sampler.should_sample("cam-1", 1000.0)
        # cam-2 first frame — should be accepted independently
        assert sampler.should_sample("cam-2", 1000.05) is True
        # cam-1 too soon
        assert sampler.should_sample("cam-1", 1000.05) is False

    def test_high_fps_target(self) -> None:
        sampler = FrameSampler(target_fps=30.0)  # ~33ms interval
        sampler.should_sample("cam-1", 1000.0)
        # 40ms later — accepted
        assert sampler.should_sample("cam-1", 1000.04) is True
        # 10ms after that — too soon
        assert sampler.should_sample("cam-1", 1000.05) is False

    def test_1_fps(self) -> None:
        sampler = FrameSampler(target_fps=1.0)
        sampler.should_sample("cam-1", 1000.0)
        assert sampler.should_sample("cam-1", 1000.5) is False
        assert sampler.should_sample("cam-1", 1001.0) is True

    def test_reset_clears_camera_state(self) -> None:
        sampler = FrameSampler(target_fps=5.0)
        sampler.should_sample("cam-1", 1000.0)
        sampler.reset("cam-1")
        # After reset, next frame should be accepted (first frame logic)
        assert sampler.should_sample("cam-1", 1000.05) is True

    def test_reset_only_affects_target_camera(self) -> None:
        sampler = FrameSampler(target_fps=5.0)
        sampler.should_sample("cam-1", 1000.0)
        sampler.should_sample("cam-2", 1000.0)
        sampler.reset("cam-1")
        # cam-2 should still have its state
        assert sampler.should_sample("cam-2", 1000.05) is False

    def test_invalid_fps_raises(self) -> None:
        with pytest.raises(ValueError):
            FrameSampler(target_fps=0)

    def test_negative_fps_raises(self) -> None:
        with pytest.raises(ValueError):
            FrameSampler(target_fps=-1.0)

    def test_target_fps_property(self) -> None:
        sampler = FrameSampler(target_fps=10.0)
        assert sampler.target_fps == 10.0

    def test_steady_state_sequence(self) -> None:
        """Simulate a 30fps camera sampled at 5fps — should keep ~1/6."""
        sampler = FrameSampler(target_fps=5.0)
        accepted = 0
        for i in range(180):  # 6 seconds at 30fps
            ts = 1000.0 + i / 30.0
            if sampler.should_sample("cam-1", ts):
                accepted += 1
        # Should accept ~30 frames (5fps * 6s), allow some tolerance
        assert 25 <= accepted <= 35
