"""Tests for debug_trace — sampling logic and trace construction."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from config import DebugConfig, MinioConfig
from debug_trace import DebugTracer, TraceCollector, TraceStage
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


# ---------------------------------------------------------------
# TraceCollector tests (P1-V07)
# ---------------------------------------------------------------


class TestTraceCollectorSampling:

    def test_manual_flag_always_collects(self) -> None:
        tc = TraceCollector(sample_rate=0.0)
        should, reason = tc.should_collect(manual_flag=True)
        assert should is True
        assert reason == "manual"

    def test_low_confidence_always_collects(self) -> None:
        tc = TraceCollector(sample_rate=0.0, low_confidence_threshold=0.3)
        dets = [_det(conf=0.25)]
        should, reason = tc.should_collect(detections=dets)
        assert should is True
        assert reason == "low_confidence"

    def test_high_confidence_not_forced(self) -> None:
        tc = TraceCollector(sample_rate=0.0, low_confidence_threshold=0.3)
        dets = [_det(conf=0.9)]
        should, _ = tc.should_collect(detections=dets)
        assert should is False

    def test_sampling_rate_one_always_collects(self) -> None:
        tc = TraceCollector(sample_rate=1.0)
        should, reason = tc.should_collect()
        assert should is True
        assert reason == "sampled"

    def test_sampling_rate_zero_skips(self) -> None:
        tc = TraceCollector(sample_rate=0.0)
        should, _ = tc.should_collect()
        assert should is False


class TestTraceCollectorEnrichment:

    def test_begin_with_metadata(self) -> None:
        tc = TraceCollector()
        trace = tc.begin(
            "f1", "cam-1", "s3://b/f.jpg",
            kafka_offset=42,
            source_capture_ts=1000.0,
            edge_receive_ts=1001.0,
            core_ingest_ts=1002.0,
        )
        assert trace.kafka_offset == 42
        assert trace.source_capture_ts == 1000.0
        assert trace.edge_receive_ts == 1001.0
        assert trace.core_ingest_ts == 1002.0

    def test_collect_raw_detections(self) -> None:
        tc = TraceCollector()
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg")
        raw_boxes = [
            {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "conf": 0.35, "class": 0},
            {"x": 0.5, "y": 0.6, "w": 0.1, "h": 0.1, "conf": 0.12, "class": 1},
        ]
        tc.collect_raw_detections(trace, raw_boxes)
        assert len(trace.raw_detections_pre_nms) == 2
        assert trace.raw_detections_pre_nms[0]["conf"] == 0.35

    def test_collect_post_nms_detections(self) -> None:
        tc = TraceCollector()
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg")
        tc.collect_post_nms_detections(trace, [_det(0.8), _det(0.6)])
        assert len(trace.detections) == 2
        assert trace.detections[0]["class"] == "person"

    def test_collect_tracker_delta(self) -> None:
        tc = TraceCollector()
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg")
        tc.collect_tracker_delta(
            trace,
            active_before=3,
            active_after=4,
            new_track_ids=["t-new-1"],
            closed_track_ids=["t-old-1"],
        )
        assert trace.tracker_state_delta["active_before"] == 3
        assert trace.tracker_state_delta["active_after"] == 4
        assert "t-new-1" in trace.tracker_state_delta["new_track_ids"]
        assert "t-new-1" in trace.track_ids

    def test_collect_attributes(self) -> None:
        tc = TraceCollector()
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg")
        attrs = [{"type": "vehicle_color", "value": "red", "confidence": 0.9}]
        tc.collect_attributes(trace, attrs)
        assert len(trace.attribute_outputs) == 1
        assert trace.attribute_outputs[0]["value"] == "red"

    def test_set_model_versions(self) -> None:
        tc = TraceCollector()
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg")
        tc.set_model_versions(trace, {"detector": "yolov8l-1", "tracker": "bytetrack-1.0"})
        assert trace.model_versions["detector"] == "yolov8l-1"

    def test_json_includes_enrichments(self) -> None:
        tc = TraceCollector()
        trace = tc.begin(
            "f1", "cam-1", "s3://b/f.jpg",
            kafka_offset=99,
            edge_receive_ts=1000.0,
        )
        tc.collect_raw_detections(trace, [{"box": [0, 0, 1, 1]}])
        tc.collect_tracker_delta(trace, 0, 1, ["t1"], [])
        tc.set_model_versions(trace, {"det": "v1"})
        trace.stages.append(TraceStage("detect", 1.0, 1.05))

        parsed = json.loads(trace.to_json())
        assert parsed["kafka_offset"] == 99
        assert parsed["edge_receive_ts"] == 1000.0
        assert len(parsed["raw_detections_pre_nms"]) == 1
        assert parsed["tracker_state_delta"]["active_after"] == 1
        assert parsed["model_versions"]["det"] == "v1"
        assert "t1" in parsed["track_ids"]
        assert parsed["stages"][0]["duration_us"] == 50000

    def test_date_str_from_source_ts(self) -> None:
        tc = TraceCollector()
        # 2026-04-07 in epoch
        ts = 1775606400.0  # approx 2026-04-07 UTC
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg", source_capture_ts=ts)
        # Should produce a valid YYYY-MM-DD string
        assert len(trace.date_str) == 10
        assert trace.date_str.count("-") == 2

    def test_date_str_fallback_to_edge_ts(self) -> None:
        tc = TraceCollector()
        ts = 1775606400.0
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg", edge_receive_ts=ts)
        assert len(trace.date_str) == 10

    def test_date_str_fallback_to_now(self) -> None:
        tc = TraceCollector()
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg")
        # No timestamps → falls back to today
        assert len(trace.date_str) == 10


class TestTraceCollectorStorage:

    @pytest.mark.asyncio
    async def test_store_uses_date_key_format(self) -> None:
        mock_minio = MagicMock()
        mock_minio.put_object = MagicMock()
        tc = TraceCollector(minio_client=mock_minio, bucket="debug-traces")

        trace = tc.begin(
            "f1", "cam-1", "s3://b/f.jpg",
            edge_receive_ts=1775606400.0,
        )
        await tc.store(trace)

        mock_minio.put_object.assert_called_once()
        call_args = mock_minio.put_object.call_args
        assert call_args[0][0] == "debug-traces"
        obj_name = call_args[0][1]
        # Key format: {camera_id}/{date}/{trace_id}.json
        parts = obj_name.split("/")
        assert len(parts) == 3
        assert parts[0] == "cam-1"
        assert len(parts[1]) == 10  # YYYY-MM-DD
        assert parts[2].endswith(".json")

    @pytest.mark.asyncio
    async def test_store_no_minio_does_not_raise(self) -> None:
        tc = TraceCollector(minio_client=None)
        trace = tc.begin("f1", "cam-1", "s3://b/f.jpg")
        await tc.store(trace)  # should not raise

    @pytest.mark.asyncio
    async def test_ensure_bucket_creates_bucket(self) -> None:
        mock_minio = MagicMock()
        mock_minio.bucket_exists = MagicMock(return_value=False)
        mock_minio.make_bucket = MagicMock()

        tc = TraceCollector(minio_client=mock_minio, bucket="debug-traces")
        await tc.ensure_bucket()

        mock_minio.make_bucket.assert_called_once_with("debug-traces")

    @pytest.mark.asyncio
    async def test_ensure_bucket_sets_lifecycle(self) -> None:
        mock_minio = MagicMock()
        mock_minio.bucket_exists = MagicMock(return_value=True)
        mock_minio.set_bucket_lifecycle = MagicMock()

        # Mock the minio lifecycle module imports
        mock_commonconfig = MagicMock()
        mock_commonconfig.ENABLED = "Enabled"
        mock_lifecycle = MagicMock()

        tc = TraceCollector(minio_client=mock_minio, bucket="debug-traces")
        with patch.dict(sys.modules, {
            "minio.commonconfig": mock_commonconfig,
            "minio.lifecycleconfig": mock_lifecycle,
        }):
            await tc.ensure_bucket()

        mock_minio.set_bucket_lifecycle.assert_called_once()
