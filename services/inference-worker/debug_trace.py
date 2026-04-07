"""Debug trace sampling for inference pipeline diagnostics.

Samples 1–5% of inference paths and stores timing + detection data
as JSON in MinIO.  Always traces frames with low-confidence detections.

TraceCollector (P1-V07) extends this with pre-NMS raw outputs, tracker
state deltas, attribute outputs, model versions, and Kafka offsets.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config import DebugConfig, MinioConfig
from detector_client import RawDetection

logger = logging.getLogger(__name__)


@dataclass
class TraceStage:
    """Timing for one pipeline stage."""

    name: str
    entry_epoch: float
    exit_epoch: float

    @property
    def duration_us(self) -> int:
        return int((self.exit_epoch - self.entry_epoch) * 1_000_000)


@dataclass
class DebugTrace:
    """Collected trace data for one frame."""

    trace_id: str
    frame_id: str
    camera_id: str
    frame_uri: str
    stages: list[TraceStage] = field(default_factory=list)
    detections: list[dict[str, Any]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    reason: str = "sampled"
    # --- P1-V07 enrichments ---
    raw_detections_pre_nms: list[dict[str, Any]] = field(default_factory=list)
    tracker_state_delta: dict[str, Any] = field(default_factory=dict)
    attribute_outputs: list[dict[str, Any]] = field(default_factory=list)
    model_versions: dict[str, str] = field(default_factory=dict)
    kafka_offset: int | None = None
    source_capture_ts: float | None = None
    edge_receive_ts: float | None = None
    core_ingest_ts: float | None = None
    track_ids: list[str] = field(default_factory=list)

    @property
    def date_str(self) -> str:
        """ISO date string for storage key partitioning."""
        ts = self.source_capture_ts or self.edge_receive_ts
        if ts and ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def to_json(self) -> str:
        return json.dumps(
            {
                "trace_id": self.trace_id,
                "frame_id": self.frame_id,
                "camera_id": self.camera_id,
                "frame_uri": self.frame_uri,
                "reason": self.reason,
                "stages": [
                    {
                        "name": s.name,
                        "entry_epoch": s.entry_epoch,
                        "exit_epoch": s.exit_epoch,
                        "duration_us": s.duration_us,
                    }
                    for s in self.stages
                ],
                "detections": self.detections,
                "labels": self.labels,
                "raw_detections_pre_nms": self.raw_detections_pre_nms,
                "tracker_state_delta": self.tracker_state_delta,
                "attribute_outputs": self.attribute_outputs,
                "model_versions": self.model_versions,
                "kafka_offset": self.kafka_offset,
                "source_capture_ts": self.source_capture_ts,
                "edge_receive_ts": self.edge_receive_ts,
                "core_ingest_ts": self.core_ingest_ts,
                "track_ids": self.track_ids,
            },
            indent=2,
        )


class DebugTracer:
    """Manages debug trace sampling and storage."""

    def __init__(
        self,
        debug_cfg: DebugConfig,
        minio_cfg: MinioConfig,
        minio_client: Any = None,
    ) -> None:
        self._cfg = debug_cfg
        self._minio_cfg = minio_cfg
        self._minio = minio_client

    def should_sample(
        self, detections: list[RawDetection] | None = None
    ) -> tuple[bool, str]:
        """Decide whether to trace this frame.

        Returns (should_trace, reason).
        """
        if not self._cfg.enabled:
            return False, ""

        # Always trace low-confidence detections
        if detections:
            for det in detections:
                if det.confidence < self._cfg.low_confidence_threshold:
                    return True, "low_confidence"

        # Random sampling at configured rate
        if random.random() * 100 < self._cfg.sample_rate_pct:
            return True, "sampled"

        return False, ""

    def begin_trace(
        self,
        frame_id: str,
        camera_id: str,
        frame_uri: str,
        reason: str = "sampled",
    ) -> DebugTrace:
        """Create a new trace for a frame."""
        return DebugTrace(
            trace_id=str(uuid.uuid4()),
            frame_id=frame_id,
            camera_id=camera_id,
            frame_uri=frame_uri,
            reason=reason,
        )

    def add_detection_info(
        self,
        trace: DebugTrace,
        detections: list[RawDetection],
    ) -> None:
        """Attach detection summaries to the trace."""
        for det in detections:
            trace.detections.append(
                {
                    "class": det.class_name,
                    "confidence": round(det.confidence, 4),
                    "bbox": [
                        round(det.x_min, 4),
                        round(det.y_min, 4),
                        round(det.x_max, 4),
                        round(det.y_max, 4),
                    ],
                }
            )

    async def store(self, trace: DebugTrace) -> None:
        """Upload trace JSON to MinIO."""
        if self._minio is None:
            logger.debug("MinIO not configured — skipping trace store")
            return

        json_bytes = trace.to_json().encode("utf-8")
        buf = io.BytesIO(json_bytes)
        object_name = f"traces/{trace.camera_id}/{trace.trace_id}.json"

        try:
            await asyncio.to_thread(
                self._minio.put_object,
                self._minio_cfg.debug_bucket,
                object_name,
                buf,
                len(json_bytes),
                "application/json",
            )
        except Exception:
            logger.warning(
                "Failed to store debug trace %s", trace.trace_id,
                exc_info=True,
            )


class TraceCollector:
    """Enhanced trace collector with full pipeline data capture.

    Extends the basic DebugTracer with:
    - Pre-NMS raw detector output (all boxes before filtering)
    - Tracker state delta (tracks added/removed)
    - Attribute classification outputs
    - Model versions used in the pipeline
    - Kafka offset for message provenance
    - Date-partitioned storage keys: ``{camera_id}/{date}/{trace_id}.json``
    - 30-day MinIO lifecycle policy
    """

    def __init__(
        self,
        sample_rate: float = 0.02,
        low_confidence_threshold: float = 0.3,
        minio_client: Any = None,
        bucket: str = "debug-traces",
    ) -> None:
        self._sample_rate = sample_rate
        self._low_conf_threshold = low_confidence_threshold
        self._minio = minio_client
        self._bucket = bucket

    def should_collect(
        self,
        detections: list[RawDetection] | None = None,
        manual_flag: bool = False,
    ) -> tuple[bool, str]:
        """Decide whether to collect a full trace for this frame.

        Returns ``(should_collect, reason)``.
        """
        if manual_flag:
            return True, "manual"

        if detections:
            for det in detections:
                if det.confidence < self._low_conf_threshold:
                    return True, "low_confidence"

        if random.random() < self._sample_rate:
            return True, "sampled"

        return False, ""

    def begin(
        self,
        frame_id: str,
        camera_id: str,
        frame_uri: str,
        reason: str = "sampled",
        kafka_offset: int | None = None,
        source_capture_ts: float | None = None,
        edge_receive_ts: float | None = None,
        core_ingest_ts: float | None = None,
    ) -> DebugTrace:
        """Create a new enriched trace for a frame."""
        return DebugTrace(
            trace_id=str(uuid.uuid4()),
            frame_id=frame_id,
            camera_id=camera_id,
            frame_uri=frame_uri,
            reason=reason,
            kafka_offset=kafka_offset,
            source_capture_ts=source_capture_ts,
            edge_receive_ts=edge_receive_ts,
            core_ingest_ts=core_ingest_ts,
        )

    def collect_raw_detections(
        self,
        trace: DebugTrace,
        raw_boxes: list[dict[str, Any]],
    ) -> None:
        """Attach all pre-NMS detector output boxes to the trace."""
        trace.raw_detections_pre_nms = raw_boxes

    def collect_post_nms_detections(
        self,
        trace: DebugTrace,
        detections: list[RawDetection],
    ) -> None:
        """Attach post-NMS detection summaries to the trace."""
        for det in detections:
            trace.detections.append(
                {
                    "class": det.class_name,
                    "confidence": round(det.confidence, 4),
                    "bbox": [
                        round(det.x_min, 4),
                        round(det.y_min, 4),
                        round(det.x_max, 4),
                        round(det.y_max, 4),
                    ],
                }
            )

    def collect_tracker_delta(
        self,
        trace: DebugTrace,
        active_before: int,
        active_after: int,
        new_track_ids: list[str],
        closed_track_ids: list[str],
    ) -> None:
        """Record tracker state change for this frame."""
        trace.tracker_state_delta = {
            "active_before": active_before,
            "active_after": active_after,
            "new_track_ids": new_track_ids,
            "closed_track_ids": closed_track_ids,
        }
        trace.track_ids.extend(new_track_ids)

    def collect_attributes(
        self,
        trace: DebugTrace,
        attributes: list[dict[str, Any]],
    ) -> None:
        """Attach attribute classification outputs to the trace."""
        trace.attribute_outputs = attributes

    def set_model_versions(
        self,
        trace: DebugTrace,
        versions: dict[str, str],
    ) -> None:
        """Record model versions used in this pipeline run."""
        trace.model_versions = versions

    async def ensure_bucket(self) -> None:
        """Create the debug-traces bucket with 30-day lifecycle if needed."""
        if self._minio is None:
            return

        try:
            exists = await asyncio.to_thread(
                self._minio.bucket_exists, self._bucket
            )
            if not exists:
                await asyncio.to_thread(self._minio.make_bucket, self._bucket)
        except Exception:
            logger.warning("Cannot ensure bucket: %s", self._bucket)
            return

        try:
            from minio.commonconfig import ENABLED  # noqa: PLC0415
            from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule  # noqa: PLC0415

            rule = Rule(
                ENABLED,
                rule_id="auto-expire-30d",
                expiration=Expiration(days=30),
            )
            config = LifecycleConfig([rule])
            await asyncio.to_thread(
                self._minio.set_bucket_lifecycle, self._bucket, config
            )
        except Exception:
            logger.warning(
                "Failed to set 30-day lifecycle on %s", self._bucket,
                exc_info=True,
            )

    async def store(self, trace: DebugTrace) -> None:
        """Upload trace JSON to MinIO with date-partitioned key."""
        if self._minio is None:
            logger.debug("MinIO not configured — skipping trace store")
            return

        json_bytes = trace.to_json().encode("utf-8")
        buf = io.BytesIO(json_bytes)
        object_name = f"{trace.camera_id}/{trace.date_str}/{trace.trace_id}.json"

        try:
            await asyncio.to_thread(
                self._minio.put_object,
                self._bucket,
                object_name,
                buf,
                len(json_bytes),
                "application/json",
            )
        except Exception:
            logger.warning(
                "Failed to store debug trace %s", trace.trace_id,
                exc_info=True,
            )
