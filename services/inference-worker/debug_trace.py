"""Debug trace sampling for inference pipeline diagnostics.

Samples 1–5% of inference paths and stores timing + detection data
as JSON in MinIO.  Always traces frames with low-confidence detections.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import uuid
from dataclasses import dataclass, field
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
