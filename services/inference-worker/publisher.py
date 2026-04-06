"""Kafka publisher for inference worker outputs.

Publishes to three topics:

- ``tracklets.local`` — Tracklet proto, key = camera_id
- ``mtmc.active_embeddings`` — Embedding proto, key = local_track_id
  (compacted topic; tombstone on track termination)
- ``bulk.detections`` — Detection proto for the bulk collector
  (headers: x-proto-schema, x-frame-seq, x-local-track-id)
"""

from __future__ import annotations

import logging
import ssl
import sys
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from config import KafkaConfig
from detector_client import CLASS_INDEX_TO_PROTO, RawDetection
from metrics import PUBLISH_ERRORS
from tracker import STrack

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))


def _load_detection_type() -> type[Any]:
    from vidanalytics.v1.detection import detection_pb2  # noqa: PLC0415
    return detection_pb2.Detection


def _load_tracklet_type() -> type[Any]:
    from vidanalytics.v1.tracklet import tracklet_pb2  # noqa: PLC0415
    return tracklet_pb2.Tracklet


def _load_trajectory_point_type() -> type[Any]:
    from vidanalytics.v1.tracklet import tracklet_pb2  # noqa: PLC0415
    return tracklet_pb2.TrajectoryPoint


def _load_embedding_type() -> type[Any]:
    from vidanalytics.v1.embedding import embedding_pb2  # noqa: PLC0415
    return embedding_pb2.Embedding


def _load_bbox_type() -> type[Any]:
    from vidanalytics.v1.detection import detection_pb2  # noqa: PLC0415
    return detection_pb2.BoundingBox


def _set_timestamp(ts_field: Any, epoch_s: float) -> None:
    """Set a google.protobuf.Timestamp from epoch seconds."""
    ts_field.seconds = int(epoch_s)
    ts_field.nanos = int((epoch_s - int(epoch_s)) * 1_000_000_000)


def build_detection_proto(
    det: RawDetection,
    frame_id: str,
    camera_id: str,
    model_name: str,
    model_version: str,
    timestamps: Any,
) -> bytes:
    """Serialise a Detection protobuf from a RawDetection."""
    Detection = _load_detection_type()
    BoundingBox = _load_bbox_type()

    msg = Detection()
    msg.detection_id = str(uuid.uuid4())
    msg.frame_id = frame_id
    msg.camera_id = camera_id
    msg.object_class = det.proto_class
    msg.confidence = det.confidence
    msg.bbox.CopyFrom(
        BoundingBox(
            x_min=det.x_min,
            y_min=det.y_min,
            x_max=det.x_max,
            y_max=det.y_max,
        )
    )
    msg.model_name = model_name
    msg.model_version = model_version
    if timestamps is not None:
        msg.timestamps.CopyFrom(timestamps)
    return msg.SerializeToString()


def build_tracklet_proto(
    track: STrack,
    timestamps: Any,
) -> bytes:
    """Serialise a Tracklet protobuf from a STrack."""
    Tracklet = _load_tracklet_type()
    TrajPoint = _load_trajectory_point_type()

    msg = Tracklet()
    msg.track_id = track.track_id
    msg.camera_id = track.camera_id
    msg.object_class = CLASS_INDEX_TO_PROTO[track.majority_class]
    msg.state = track.state.proto_value
    msg.mean_confidence = track.confidence
    msg.tracker_version = "bytetrack-1.0"

    for pt in track.trajectory[-10:]:  # last 10 points to limit message size
        tp = TrajPoint()
        tp.detection_id = pt.detection_id
        tp.centroid_x = pt.centroid_x
        tp.centroid_y = pt.centroid_y
        _set_timestamp(tp.frame_ts, pt.frame_ts)
        msg.trajectory.append(tp)

    if timestamps is not None:
        msg.timestamps.CopyFrom(timestamps)
    return msg.SerializeToString()


def build_embedding_proto(
    track: STrack,
    embedding: np.ndarray,
    model_name: str,
    model_version: str,
) -> bytes:
    """Serialise an Embedding protobuf."""
    Embedding = _load_embedding_type()

    msg = Embedding()
    msg.embedding_id = str(uuid.uuid4())
    msg.source_id = track.track_id
    msg.source_type = 2  # EMBEDDING_SOURCE_TYPE_TRACKLET
    msg.vector.extend(embedding.tolist())
    msg.dimension = len(embedding)
    msg.model_name = model_name
    msg.model_version = model_version
    msg.quality_score = track.best_confidence
    return msg.SerializeToString()


class KafkaPublisher:
    """Async Kafka producer for inference worker outputs."""

    def __init__(self, cfg: KafkaConfig) -> None:
        self._cfg = cfg
        self._producer = None  # lazy init
        self._connected = False

    async def connect(self) -> None:
        """Create and start the aiokafka producer."""
        try:
            from aiokafka import AIOKafkaProducer  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'aiokafka'; install requirements.txt"
            ) from exc

        ssl_context = self._build_ssl_context()
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._cfg.bootstrap_servers,
            security_protocol=self._cfg.security_protocol,
            sasl_mechanism=self._cfg.sasl_mechanism,
            sasl_plain_username=self._cfg.sasl_username,
            sasl_plain_password=self._cfg.sasl_password,
            ssl_context=ssl_context,
            acks="all",
            enable_idempotence=True,
            compression_type="zstd",
        )
        await self._producer.start()
        self._connected = True

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def publish_detections(
        self,
        detections: list[RawDetection],
        frame_id: str,
        camera_id: str,
        frame_sequence: int,
        timestamps: Any,
        track_assignments: dict[int, str],
        model_name: str = "yolov8l",
        model_version: str = "1",
    ) -> None:
        """Publish Detection protos to bulk.detections topic."""
        for i, det in enumerate(detections):
            payload = build_detection_proto(
                det, frame_id, camera_id, model_name, model_version, timestamps,
            )
            track_id = track_assignments.get(i)
            headers = [
                ("x-proto-schema", b"vidanalytics.v1.detection.Detection"),
                ("x-frame-seq", str(frame_sequence).encode()),
            ]
            if track_id:
                headers.append(("x-local-track-id", track_id.encode()))

            await self._send(
                self._cfg.detection_topic,
                key=camera_id.encode(),
                value=payload,
                headers=headers,
            )

    async def publish_tracklet(
        self,
        track: STrack,
        timestamps: Any,
    ) -> None:
        """Publish a Tracklet proto to tracklets.local."""
        payload = build_tracklet_proto(track, timestamps)
        headers = [
            ("x-proto-schema", b"vidanalytics.v1.tracklet.Tracklet"),
        ]
        await self._send(
            self._cfg.tracklet_topic,
            key=track.camera_id.encode(),
            value=payload,
            headers=headers,
        )

    async def publish_embedding(
        self,
        track: STrack,
        embedding: np.ndarray,
        model_name: str = "osnet",
        model_version: str = "1",
    ) -> None:
        """Publish an Embedding proto to mtmc.active_embeddings."""
        payload = build_embedding_proto(
            track, embedding, model_name, model_version
        )
        headers = [
            ("x-proto-schema", b"vidanalytics.v1.embedding.Embedding"),
        ]
        await self._send(
            self._cfg.embedding_topic,
            key=track.track_id.encode(),
            value=payload,
            headers=headers,
        )

    async def publish_tombstone(self, track_id: str) -> None:
        """Publish a tombstone (null value) to compact terminated tracks."""
        await self._send(
            self._cfg.embedding_topic,
            key=track_id.encode(),
            value=None,
            headers=[],
        )

    async def _send(
        self,
        topic: str,
        key: bytes,
        value: bytes | None,
        headers: list[tuple[str, bytes]],
    ) -> None:
        if self._producer is None:
            PUBLISH_ERRORS.labels(topic=topic).inc()
            return
        try:
            await self._producer.send(
                topic,
                key=key,
                value=value,
                headers=headers,
            )
        except Exception:
            PUBLISH_ERRORS.labels(topic=topic).inc()
            logger.warning("Failed to publish to %s", topic, exc_info=True)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        if not any([
            self._cfg.ssl_ca_file,
            self._cfg.ssl_cert_file,
            self._cfg.ssl_key_file,
        ]):
            return None
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if self._cfg.ssl_ca_file:
            ctx.load_verify_locations(self._cfg.ssl_ca_file)
        if self._cfg.ssl_cert_file and self._cfg.ssl_key_file:
            ctx.load_cert_chain(self._cfg.ssl_cert_file, self._cfg.ssl_key_file)
        return ctx
