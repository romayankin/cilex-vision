"""Tests for debug_trace — sampling logic and trace construction."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from config import DebugConfig, MinioConfig
from debug_trace import DebugTracer, TraceStage
from detector_client import RawDetection


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

@pytest.fixture
def debug_cfg() -> DebugConfig:
    return DebugConfig(
        sample_rate_pct=100.0,
        low_confidence_threshold=0.45,
        enabled=True,
    )


@pytest.fixture
def minio_cfg() -> MinioConfig:
    return MinioConfig()


@pytest.fixture
def tracer(debug_cfg: DebugConfig, minio_cfg: MinioConfig) -> DebugTracer:
    return DebugTracer(debug_cfg, minio_cfg)


def _det(conf: float = 0.8) -> RawDetection:
    return RawDetection(
        x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.3,
        confidence=conf, class_index=0,
    )


# ---------------------------------------------------------------
# Sampling tests
# ---------------------------------------------------------------

class TestSampling:

    def test_sampling_enabled(self, tracer: DebugTracer) -> None:
        should, reason = tracer.should_sample()
        assert should is True
        assert reason == "sampled"

    def test_sampling_disabled(self, minio_cfg: MinioConfig) -> None:
        cfg = DebugConfig(enabled=False)
        tracer = DebugTracer(cfg, minio_cfg)
        should, _ = tracer.should_sample()
        assert should is False

    def test_low_confidence_always_traced(self, minio_cfg: MinioConfig) -> None:
        cfg = DebugConfig(sample_rate_pct=0.0, enabled=True)
        tracer = DebugTracer(cfg, minio_cfg)
        dets = [_det(conf=0.30)]  # below 0.45 threshold
        should, reason = tracer.should_sample(dets)
        assert should is True
        assert reason == "low_confidence"

    def test_high_confidence_not_forced(self, minio_cfg: MinioConfig) -> None:
        cfg = DebugConfig(sample_rate_pct=0.0, enabled=True)
        tracer = DebugTracer(cfg, minio_cfg)
        dets = [_det(conf=0.90)]
        should, _ = tracer.should_sample(dets)
        assert should is False


# ---------------------------------------------------------------
# Trace construction
# ---------------------------------------------------------------

class TestTraceConstruction:

    def test_begin_trace(self, tracer: DebugTracer) -> None:
        trace = tracer.begin_trace("frame-1", "cam-1", "s3://bucket/frame.jpg")
        assert trace.frame_id == "frame-1"
        assert trace.camera_id == "cam-1"
        assert trace.trace_id

    def test_add_detections(self, tracer: DebugTracer) -> None:
        trace = tracer.begin_trace("f1", "c1", "s3://b/f.jpg")
        dets = [_det(0.9), _det(0.7)]
        tracer.add_detection_info(trace, dets)
        assert len(trace.detections) == 2
        assert trace.detections[0]["class"] == "person"

    def test_trace_to_json(self, tracer: DebugTracer) -> None:
        trace = tracer.begin_trace("f1", "c1", "s3://b/f.jpg")
        trace.stages.append(TraceStage("detect", 1.0, 1.05))
        tracer.add_detection_info(trace, [_det(0.8)])

        json_str = trace.to_json()
        parsed = json.loads(json_str)
        assert parsed["frame_id"] == "f1"
        assert len(parsed["stages"]) == 1
        assert parsed["stages"][0]["duration_us"] == 50000
        assert len(parsed["detections"]) == 1


# ---------------------------------------------------------------
# Storage tests
# ---------------------------------------------------------------

class TestStorage:

    @pytest.mark.asyncio
    async def test_store_calls_minio(self, minio_cfg: MinioConfig) -> None:
        mock_minio = MagicMock()
        mock_minio.put_object = MagicMock()
        cfg = DebugConfig(enabled=True)
        tracer = DebugTracer(cfg, minio_cfg, minio_client=mock_minio)

        trace = tracer.begin_trace("f1", "c1", "s3://b/f.jpg")
        await tracer.store(trace)

        mock_minio.put_object.assert_called_once()
        call_args = mock_minio.put_object.call_args
        assert call_args[0][0] == "debug-traces"
        assert "traces/c1/" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_store_no_minio_does_not_raise(self, debug_cfg: DebugConfig, minio_cfg: MinioConfig) -> None:
        tracer = DebugTracer(debug_cfg, minio_cfg, minio_client=None)
        trace = tracer.begin_trace("f1", "c1", "s3://b/f.jpg")
        await tracer.store(trace)  # should not raise


# ---------------------------------------------------------------
# TraceStage tests
# ---------------------------------------------------------------

class TestTraceStage:

    def test_duration_us(self) -> None:
        stage = TraceStage("detect", 1.0, 1.025)
        assert abs(stage.duration_us - 25000) <= 1

    def test_zero_duration(self) -> None:
        stage = TraceStage("noop", 5.0, 5.0)
        assert stage.duration_us == 0
