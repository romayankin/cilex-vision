"""Jetson edge pipeline: GStreamer RTSP -> TensorRT inference -> NATS publish detections.

Unlike the central-inference pipeline, the Jetson variant runs detection on-device
and publishes Detection protobufs directly, skipping frame upload to MinIO entirely.

Pipeline flow:
1. RTSP decode via GStreamer (reuses rtsp_client patterns)
2. Stamp edge_receive_ts immediately
3. Motion filter (reuses motion_detector)
4. On motion: run TensorRT inference via JetsonDetector
5. NMS + confidence filter (threshold 0.40)
6. Publish Detection protobufs to NATS detections.edge.{site_id}.{camera_id}
7. Local buffer on NATS failure
8. Expose Prometheus metrics
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from jetson_config import CameraConfig, JetsonSettings
from jetson_detector import JetsonDetector

# --- Reuse base edge agent modules ---
# Add parent directory to path for importing base modules.
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from local_buffer import LocalBuffer  # noqa: E402
from motion_detector import MotionDetector  # noqa: E402
from nats_publisher import NatsPublisher  # noqa: E402

# Allow proto_gen imports at runtime.
_proto_gen = os.path.join(_parent, "proto_gen")
if _proto_gen not in sys.path:
    sys.path.insert(0, _proto_gen)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Prometheus metrics (jetson_ prefix)
# ------------------------------------------------------------------

JETSON_CAMERA_UPTIME = Gauge(
    "jetson_camera_uptime_ratio",
    "Camera connection uptime ratio (0.0-1.0)",
    ["camera_id"],
)
JETSON_MOTION_FRAMES = Counter(
    "jetson_motion_frames_total",
    "Frames that passed the motion filter",
    ["camera_id"],
)
JETSON_STATIC_FILTERED = Counter(
    "jetson_static_frames_filtered_total",
    "Frames filtered by motion detector (no motion)",
    ["camera_id"],
)
JETSON_DETECTIONS = Counter(
    "jetson_detections_total",
    "Total detections produced",
    ["camera_id", "class_name"],
)
JETSON_INFERENCE_LATENCY = Histogram(
    "jetson_inference_latency_ms",
    "TensorRT inference latency in milliseconds",
    ["camera_id"],
    buckets=[5, 10, 20, 30, 50, 75, 100, 150, 200, 500],
)
JETSON_NATS_LATENCY = Histogram(
    "jetson_nats_publish_latency_ms",
    "NATS JetStream publish latency in milliseconds",
    ["camera_id"],
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)
JETSON_BUFFER_FILL = Gauge(
    "jetson_buffer_fill_bytes",
    "Current local ring-buffer usage in bytes",
)


# ------------------------------------------------------------------
# Protobuf serialization
# ------------------------------------------------------------------


def serialize_detection(
    *,
    detection_id: str,
    frame_id: str,
    camera_id: str,
    object_class: int,
    confidence: float,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    model_name: str,
    model_version: str,
    source_capture_ts: float,
    edge_receive_ts: float,
) -> bytes:
    """Build a Detection protobuf and return wire-format bytes.

    core_ingest_ts is intentionally left unset -- stamped by ingress bridge.
    """
    from google.protobuf.timestamp_pb2 import Timestamp  # noqa: PLC0415

    from vidanalytics.v1.common import common_pb2  # noqa: PLC0415
    from vidanalytics.v1.detection import detection_pb2  # noqa: PLC0415

    def _ts(epoch: float) -> Timestamp:
        t = Timestamp()
        t.FromDatetime(datetime.fromtimestamp(epoch, tz=timezone.utc))
        return t

    ts = common_pb2.VideoTimestamp(
        source_capture_ts=_ts(source_capture_ts),
        edge_receive_ts=_ts(edge_receive_ts),
    )
    bbox = detection_pb2.BoundingBox(
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
    )
    det = detection_pb2.Detection(
        detection_id=detection_id,
        frame_id=frame_id,
        camera_id=camera_id,
        object_class=object_class,
        confidence=confidence,
        bbox=bbox,
        model_name=model_name,
        model_version=model_version,
        timestamps=ts,
    )
    return det.SerializeToString()


# ------------------------------------------------------------------
# Per-camera pipeline
# ------------------------------------------------------------------

# Proto ObjectClass enum values (1-indexed, matching detection.proto)
_CLASS_NAME_TO_PROTO = {
    "person": 1,
    "car": 2,
    "truck": 3,
    "bus": 4,
    "bicycle": 5,
    "motorcycle": 6,
    "animal": 7,
}


class JetsonCameraPipeline:
    """Runs RTSP decode -> motion -> TensorRT inference -> NATS publish for one camera."""

    def __init__(
        self,
        camera: CameraConfig,
        site_id: str,
        nats_pub: NatsPublisher,
        detector: JetsonDetector,
        buffer: LocalBuffer,
        model_name: str,
        model_version: str,
        motion_cfg: dict[str, object] | None = None,
    ) -> None:
        self._camera = camera
        self._site_id = site_id
        self._nats = nats_pub
        self._detector = detector
        self._buffer = buffer
        self._model_name = model_name
        self._model_version = model_version
        self._shutdown = False

        # Lazy import — RtspClient needs GStreamer
        from rtsp_client import RtspClient  # noqa: PLC0415

        self._rtsp = RtspClient(camera.camera_id, camera.rtsp_url)

        mc = motion_cfg or {}
        self._motion = MotionDetector(
            pixel_threshold=int(mc.get("pixel_threshold", 25)),
            motion_threshold=float(mc.get("motion_threshold", 0.02)),
            scene_change_threshold=float(mc.get("scene_change_threshold", 0.80)),
            reference_update_interval_s=int(mc.get("reference_update_interval_s", 300)),
        )

    def _detection_subject(self, camera_id: str) -> str:
        """NATS subject for edge detections."""
        return f"detections.edge.{self._site_id}.{camera_id}"

    async def run(self) -> None:
        """Main loop -- runs until shutdown() is called."""
        cam_id = self._camera.camera_id
        while not self._shutdown:
            try:
                await self._rtsp.start()
                self._rtsp.reset_backoff()
                await self._drain_buffer()
                await self._capture_loop()
            except Exception:
                logger.exception("Pipeline error for %s", cam_id)
            finally:
                JETSON_CAMERA_UPTIME.labels(camera_id=cam_id).set(
                    self._rtsp.uptime_ratio
                )

            if not self._shutdown:
                try:
                    await self._rtsp.reconnect_with_backoff()
                except Exception:
                    logger.exception("Reconnect failed for %s", cam_id)
                    await asyncio.sleep(self._rtsp.MAX_BACKOFF_S)

    def shutdown(self) -> None:
        self._shutdown = True

    async def _capture_loop(self) -> None:
        cam_id = self._camera.camera_id

        while not self._shutdown:
            frame = await self._rtsp.read_frame()
            if frame is None:
                logger.warning("No frame from %s -- stream may have ended", cam_id)
                return

            # edge_receive_ts stamped IMMEDIATELY on arrival (trust anchor)
            edge_receive_ts = time.time()

            has_motion, _is_scene_change = self._motion.detect(frame.data)
            if not has_motion:
                JETSON_STATIC_FILTERED.labels(camera_id=cam_id).inc()
                continue

            JETSON_MOTION_FRAMES.labels(camera_id=cam_id).inc()

            # Run on-device TensorRT inference
            detections = await asyncio.to_thread(self._detector.detect, frame.data)
            JETSON_INFERENCE_LATENCY.labels(camera_id=cam_id).observe(
                self._detector.stats.last_latency_ms
            )

            if not detections:
                continue

            # source_capture_ts: best-effort from PTS (advisory / untrusted)
            source_capture_ts = _pts_to_epoch(frame.pts_ns, edge_receive_ts)
            frame_id = str(uuid.uuid4())

            # Publish each detection as a separate protobuf message
            subject = self._detection_subject(cam_id)
            for det in detections:
                proto_class = _CLASS_NAME_TO_PROTO.get(det.class_name, 0)
                payload = serialize_detection(
                    detection_id=str(uuid.uuid4()),
                    frame_id=frame_id,
                    camera_id=cam_id,
                    object_class=proto_class,
                    confidence=det.confidence,
                    x_min=det.x_min,
                    y_min=det.y_min,
                    x_max=det.x_max,
                    y_max=det.y_max,
                    model_name=self._model_name,
                    model_version=self._model_version,
                    source_capture_ts=source_capture_ts,
                    edge_receive_ts=edge_receive_ts,
                )

                t0 = time.monotonic()
                ok = await self._nats.publish(subject, payload, camera_id=cam_id)
                elapsed_ms = (time.monotonic() - t0) * 1000
                JETSON_NATS_LATENCY.labels(camera_id=cam_id).observe(elapsed_ms)

                if not ok:
                    await self._buffer.enqueue(subject, payload)

                JETSON_DETECTIONS.labels(
                    camera_id=cam_id, class_name=det.class_name
                ).inc()

            JETSON_CAMERA_UPTIME.labels(camera_id=cam_id).set(
                self._rtsp.uptime_ratio
            )

    async def _drain_buffer(self) -> None:
        """Drain buffered messages from a prior NATS outage."""
        if self._buffer.is_empty or not self._nats.is_connected:
            return

        async def _pub(subject: str, payload: bytes) -> bool:
            return await self._nats.publish(subject, payload)

        await self._buffer.drain(_pub)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


async def run(settings: JetsonSettings) -> None:
    """Async entry point -- sets up components and runs pipelines."""

    start_http_server(settings.metrics_port)
    logger.info("Prometheus metrics at :%d/metrics", settings.metrics_port)

    # Load TensorRT detector (shared across cameras)
    detector = JetsonDetector(
        engine_path=settings.detector.engine_path,
        input_size=settings.detector.model_input_size,
        confidence_threshold=settings.detector.confidence_threshold,
        nms_iou_threshold=settings.detector.nms_iou_threshold,
        max_detections=settings.detector.max_detections,
        thermal_throttle_warn_ms=settings.detector.thermal_throttle_warn_ms,
    )
    detector.load()

    # NATS publisher
    nats_pub = NatsPublisher(
        url=settings.nats.url,
        site_id=settings.site_id,
        cert_file=settings.nats.tls.cert_file if settings.nats.tls else None,
        key_file=settings.nats.tls.key_file if settings.nats.tls else None,
        ca_file=settings.nats.tls.ca_file if settings.nats.tls else None,
    )
    await nats_pub.connect()

    # Local buffer
    buffer = LocalBuffer(
        path=settings.buffer.path,
        max_bytes=settings.buffer.max_bytes,
        replay_rate_limit=settings.buffer.replay_rate_limit,
    )

    # Motion config as dict for pipeline
    motion_cfg = {
        "pixel_threshold": settings.motion.pixel_threshold,
        "motion_threshold": settings.motion.motion_threshold,
        "scene_change_threshold": settings.motion.scene_change_threshold,
        "reference_update_interval_s": settings.motion.reference_update_interval_s,
    }

    # Camera pipelines
    pipelines: list[JetsonCameraPipeline] = []
    tasks: list[asyncio.Task[None]] = []

    for cam in settings.cameras:
        if not cam.enabled:
            continue
        pipeline = JetsonCameraPipeline(
            camera=cam,
            site_id=settings.site_id,
            nats_pub=nats_pub,
            detector=detector,
            buffer=buffer,
            model_name=settings.model_name,
            model_version=settings.model_version,
            motion_cfg=motion_cfg,
        )
        pipelines.append(pipeline)
        tasks.append(
            asyncio.create_task(pipeline.run(), name=f"jetson-cam-{cam.camera_id}")
        )

    logger.info("Started %d Jetson camera pipeline(s)", len(tasks))

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        for p in pipelines:
            p.shutdown()
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await shutdown_event.wait()
    await asyncio.gather(*tasks, return_exceptions=True)
    await nats_pub.close()
    logger.info("Jetson edge agent stopped")


def main() -> None:
    config_path = os.environ.get("JETSON_CONFIG", "config.yaml")
    settings = JetsonSettings.from_yaml(config_path)

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=(
            '{"time":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","message":"%(message)s"}'
        ),
    )
    logger.info(
        "Jetson Edge Agent starting -- site=%s cameras=%d engine=%s",
        settings.site_id,
        len(settings.cameras),
        settings.detector.engine_path,
    )

    asyncio.run(run(settings))


def _pts_to_epoch(pts_ns: int, fallback_epoch: float) -> float:
    """Best-effort PTS-to-epoch conversion (see camera_pipeline.py)."""
    if pts_ns <= 0 or pts_ns >= (1 << 63):
        return fallback_epoch
    return fallback_epoch


if __name__ == "__main__":
    main()
