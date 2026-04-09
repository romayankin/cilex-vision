#!/usr/bin/env python3
"""Shadow inference worker.

Consumes the same decoded frames as production but publishes only to shadow
topics. The worker targets an explicit Triton model version and never writes to
production Kafka topics.

Usage:
    python shadow_inference_worker.py --triton-url localhost:8001 \
        --model-name yolov8l --model-version 2 \
        --kafka-bootstrap localhost:9092
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import signal
import ssl
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np
from prometheus_client import Counter, Histogram, start_http_server
from scipy.optimize import linear_sum_assignment


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTO_SEARCH_PATHS = (
    REPO_ROOT / "services" / "inference-worker" / "proto_gen",
    REPO_ROOT / "services" / "clip-service" / "proto_gen",
    REPO_ROOT / "services" / "decode-service" / "proto_gen",
    REPO_ROOT / "services" / "event-engine" / "proto_gen",
)
_PROTO_TEMP_DIR: tempfile.TemporaryDirectory[str] | None = None

SHADOW_DETECTIONS_TOTAL = Counter(
    "shadow_detections_total",
    "Total detections produced by the shadow inference worker.",
    ["object_class"],
)

SHADOW_INFERENCE_LATENCY = Histogram(
    "shadow_inference_latency_ms",
    "Shadow detector inference latency in milliseconds.",
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2000],
)

SHADOW_FRAMES_CONSUMED_TOTAL = Counter(
    "shadow_frames_consumed_total",
    "Total decoded frames consumed by the shadow inference worker.",
)

SHADOW_PUBLISH_ERRORS_TOTAL = Counter(
    "shadow_publish_errors_total",
    "Total Kafka publish failures in the shadow inference worker.",
)

SHADOW_DETECTION_CONFIDENCE = Histogram(
    "shadow_detection_confidence",
    "Shadow detection confidence distribution.",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)

logger = logging.getLogger(__name__)

CLASS_INDEX_TO_NAME: dict[int, str] = {
    0: "person",
    1: "car",
    2: "truck",
    3: "bus",
    4: "bicycle",
    5: "motorcycle",
    6: "animal",
}

CLASS_INDEX_TO_PROTO: dict[int, int] = {
    0: 1,
    1: 2,
    2: 3,
    3: 4,
    4: 5,
    5: 6,
    6: 7,
}


@dataclass(frozen=True)
class RawDetection:
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    confidence: float
    class_index: int

    @property
    def class_name(self) -> str:
        return CLASS_INDEX_TO_NAME[self.class_index]

    @property
    def proto_class(self) -> int:
        return CLASS_INDEX_TO_PROTO[self.class_index]


@dataclass(frozen=True)
class LetterboxInfo:
    scale: float
    pad_w: float
    pad_h: float
    orig_w: int
    orig_h: int


class TrackState(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    LOST = "lost"
    TERMINATED = "terminated"

    @property
    def proto_value(self) -> int:
        return {
            TrackState.NEW: 1,
            TrackState.ACTIVE: 2,
            TrackState.LOST: 3,
            TrackState.TERMINATED: 4,
        }[self]


@dataclass
class TrajectoryPoint:
    detection_id: str
    centroid_x: float
    centroid_y: float
    frame_ts: float


@dataclass
class STrack:
    track_id: str
    camera_id: str
    bbox: np.ndarray
    confidence: float
    class_index: int
    state: TrackState
    trajectory: list[TrajectoryPoint] = field(default_factory=list)
    hits: int = 1
    lost_frames: int = 0
    class_votes: dict[int, int] = field(default_factory=dict)

    @property
    def majority_class(self) -> int:
        if not self.class_votes:
            return self.class_index
        return max(self.class_votes, key=self.class_votes.get)

    def predict(self) -> None:
        if len(self.trajectory) < 2:
            return
        prev = self.trajectory[-2]
        curr = self.trajectory[-1]
        dx = curr.centroid_x - prev.centroid_x
        dy = curr.centroid_y - prev.centroid_y
        width = self.bbox[2] - self.bbox[0]
        height = self.bbox[3] - self.bbox[1]
        center_x = (self.bbox[0] + self.bbox[2]) / 2 + dx
        center_y = (self.bbox[1] + self.bbox[3]) / 2 + dy
        self.bbox = np.array(
            [
                center_x - width / 2,
                center_y - height / 2,
                center_x + width / 2,
                center_y + height / 2,
            ],
            dtype=np.float64,
        )

    def update(self, detection: RawDetection, detection_id: str, frame_ts: float) -> None:
        self.bbox = np.array(
            [detection.x_min, detection.y_min, detection.x_max, detection.y_max],
            dtype=np.float64,
        )
        self.confidence = detection.confidence
        self.class_votes[detection.class_index] = (
            self.class_votes.get(detection.class_index, 0) + 1
        )
        self.hits += 1
        self.lost_frames = 0
        self.trajectory.append(
            TrajectoryPoint(
                detection_id=detection_id,
                centroid_x=(detection.x_min + detection.x_max) / 2,
                centroid_y=(detection.y_min + detection.y_max) / 2,
                frame_ts=frame_ts,
            ),
        )
        if self.state == TrackState.NEW and self.hits >= 3:
            self.state = TrackState.ACTIVE
        elif self.state == TrackState.LOST:
            self.state = TrackState.ACTIVE


class ByteTracker:
    def __init__(
        self,
        camera_id: str,
        *,
        track_thresh: float,
        match_thresh: float,
        second_match_thresh: float,
        max_lost_frames: int,
    ) -> None:
        self.camera_id = camera_id
        self.track_thresh = track_thresh
        self.match_thresh = match_thresh
        self.second_match_thresh = second_match_thresh
        self.max_lost_frames = max_lost_frames
        self._active_tracks: list[STrack] = []
        self._lost_tracks: list[STrack] = []

    def update(
        self,
        detections: list[RawDetection],
        frame_ts: float,
    ) -> tuple[list[STrack], list[STrack]]:
        for track in self._active_tracks:
            track.predict()

        high_conf = [det for det in detections if det.confidence >= self.track_thresh]
        low_conf = [det for det in detections if det.confidence < self.track_thresh]

        matched_t, matched_d, unmatched_tracks, unmatched_high = self._match(
            self._active_tracks,
            high_conf,
            self.match_thresh,
        )

        updated_tracks: list[STrack] = []
        for track_idx, det_idx in zip(matched_t, matched_d):
            detection = high_conf[det_idx]
            track = self._active_tracks[track_idx]
            track.update(detection, str(uuid.uuid4()), frame_ts)
            updated_tracks.append(track)

        remaining_tracks = [self._active_tracks[index] for index in unmatched_tracks]
        if remaining_tracks and low_conf:
            second_t, second_d, still_unmatched, _ = self._match(
                remaining_tracks,
                low_conf,
                self.second_match_thresh,
            )
            for track_idx, det_idx in zip(second_t, second_d):
                detection = low_conf[det_idx]
                track = remaining_tracks[track_idx]
                track.update(detection, str(uuid.uuid4()), frame_ts)
                updated_tracks.append(track)
            remaining_tracks = [remaining_tracks[index] for index in still_unmatched]

        unmatched_high_detections = [high_conf[index] for index in unmatched_high]
        if self._lost_tracks and unmatched_high_detections:
            lost_t, lost_d, still_lost, still_high = self._match(
                self._lost_tracks,
                unmatched_high_detections,
                self.second_match_thresh,
            )
            for track_idx, det_idx in zip(lost_t, lost_d):
                detection = unmatched_high_detections[det_idx]
                track = self._lost_tracks[track_idx]
                track.update(detection, str(uuid.uuid4()), frame_ts)
                updated_tracks.append(track)
            self._lost_tracks = [self._lost_tracks[index] for index in still_lost]
            unmatched_high_detections = [
                unmatched_high_detections[index] for index in still_high
            ]

        for track in remaining_tracks:
            track.lost_frames += 1
            if track.state is not TrackState.LOST:
                track.state = TrackState.LOST
            self._lost_tracks.append(track)

        terminated_tracks: list[STrack] = []
        surviving_lost: list[STrack] = []
        for track in self._lost_tracks:
            track.lost_frames += 1
            if track.lost_frames > self.max_lost_frames:
                track.state = TrackState.TERMINATED
                terminated_tracks.append(track)
            else:
                surviving_lost.append(track)
        self._lost_tracks = surviving_lost

        for detection in unmatched_high_detections:
            track = STrack(
                track_id=str(uuid.uuid4()),
                camera_id=self.camera_id,
                bbox=np.array(
                    [detection.x_min, detection.y_min, detection.x_max, detection.y_max],
                    dtype=np.float64,
                ),
                confidence=detection.confidence,
                class_index=detection.class_index,
                state=TrackState.NEW,
                class_votes={detection.class_index: 1},
            )
            track.trajectory.append(
                TrajectoryPoint(
                    detection_id=str(uuid.uuid4()),
                    centroid_x=(detection.x_min + detection.x_max) / 2,
                    centroid_y=(detection.y_min + detection.y_max) / 2,
                    frame_ts=frame_ts,
                ),
            )
            updated_tracks.append(track)

        self._active_tracks = [
            track for track in updated_tracks if track.state is not TrackState.TERMINATED
        ]
        return updated_tracks, terminated_tracks

    @staticmethod
    def _match(
        tracks: list[STrack],
        detections: list[RawDetection],
        threshold: float,
    ) -> tuple[list[int], list[int], list[int], list[int]]:
        if not tracks or not detections:
            return [], [], list(range(len(tracks))), list(range(len(detections)))

        cost_matrix = np.ones((len(tracks), len(detections)), dtype=np.float64)
        for track_idx, track in enumerate(tracks):
            for det_idx, detection in enumerate(detections):
                iou = _bbox_iou(
                    track.bbox,
                    np.array(
                        [detection.x_min, detection.y_min, detection.x_max, detection.y_max],
                        dtype=np.float64,
                    ),
                )
                cost_matrix[track_idx, det_idx] = 1.0 - iou

        row_ind, col_ind = linear_sum_assignment(cost_matrix)
        matched_tracks: list[int] = []
        matched_detections: list[int] = []
        for row, col in zip(row_ind.tolist(), col_ind.tolist()):
            if 1.0 - cost_matrix[row, col] >= threshold:
                matched_tracks.append(row)
                matched_detections.append(col)

        unmatched_tracks = [
            index for index in range(len(tracks)) if index not in matched_tracks
        ]
        unmatched_detections = [
            index for index in range(len(detections)) if index not in matched_detections
        ]
        return matched_tracks, matched_detections, unmatched_tracks, unmatched_detections


class ShadowDetectorClient:
    def __init__(
        self,
        *,
        triton_url: str,
        model_name: str,
        model_version: str,
        input_name: str,
        output_name: str,
        confidence_threshold: float,
        nms_iou_threshold: float,
        input_size: int,
    ) -> None:
        self._triton_url = triton_url
        self._model_name = model_name
        self._model_version = model_version
        self._input_name = input_name
        self._output_name = output_name
        self._confidence_threshold = confidence_threshold
        self._nms_iou_threshold = nms_iou_threshold
        self._input_size = input_size
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import tritonclient.grpc as grpcclient  # noqa: PLC0415

            self._client = grpcclient.InferenceServerClient(url=self._triton_url)
        return self._client

    async def detect(self, frame: np.ndarray) -> list[RawDetection]:
        input_tensor, letterbox = self._preprocess(frame)
        started = time.monotonic()
        raw_output = await asyncio.to_thread(self._infer, input_tensor)
        SHADOW_INFERENCE_LATENCY.observe((time.monotonic() - started) * 1000.0)
        detections = self._postprocess(raw_output, letterbox)
        for detection in detections:
            SHADOW_DETECTIONS_TOTAL.labels(object_class=detection.class_name).inc()
            SHADOW_DETECTION_CONFIDENCE.observe(detection.confidence)
        return detections

    def _preprocess(self, frame: np.ndarray) -> tuple[np.ndarray, LetterboxInfo]:
        orig_h, orig_w = frame.shape[:2]
        scale = min(self._input_size / orig_w, self._input_size / orig_h)
        new_w = int(round(orig_w * scale))
        new_h = int(round(orig_h * scale))
        pad_w = (self._input_size - new_w) / 2.0
        pad_h = (self._input_size - new_h) / 2.0

        resized = _resize_bilinear(frame, new_w, new_h)
        padded = np.full((self._input_size, self._input_size, 3), 114, dtype=np.uint8)
        top = int(round(pad_h))
        left = int(round(pad_w))
        padded[top : top + new_h, left : left + new_w] = resized

        tensor = padded.transpose(2, 0, 1).astype(np.float32) / 255.0
        return (
            np.expand_dims(tensor, axis=0),
            LetterboxInfo(
                scale=scale,
                pad_w=pad_w,
                pad_h=pad_h,
                orig_w=orig_w,
                orig_h=orig_h,
            ),
        )

    def _infer(self, input_tensor: np.ndarray) -> np.ndarray:
        import tritonclient.grpc as grpcclient  # noqa: PLC0415

        inputs = [
            grpcclient.InferInput(self._input_name, list(input_tensor.shape), "FP32"),
        ]
        inputs[0].set_data_from_numpy(input_tensor)
        outputs = [grpcclient.InferRequestedOutput(self._output_name)]
        result = self._get_client().infer(
            model_name=self._model_name,
            model_version=self._model_version,
            inputs=inputs,
            outputs=outputs,
        )
        return result.as_numpy(self._output_name)

    def _postprocess(
        self,
        raw: np.ndarray,
        letterbox: LetterboxInfo,
    ) -> list[RawDetection]:
        predictions = raw[0].T
        center_x = predictions[:, 0]
        center_y = predictions[:, 1]
        width = predictions[:, 2]
        height = predictions[:, 3]
        class_scores = predictions[:, 4:]

        class_ids = np.argmax(class_scores, axis=1)
        confidences = class_scores[np.arange(len(class_ids)), class_ids]
        mask = confidences >= self._confidence_threshold
        if not np.any(mask):
            return []

        center_x = center_x[mask]
        center_y = center_y[mask]
        width = width[mask]
        height = height[mask]
        confidences = confidences[mask]
        class_ids = class_ids[mask]

        x1 = center_x - width / 2
        y1 = center_y - height / 2
        x2 = center_x + width / 2
        y2 = center_y + height / 2

        keep = _per_class_nms(
            x1,
            y1,
            x2,
            y2,
            confidences,
            class_ids,
            self._nms_iou_threshold,
        )
        if not keep:
            return []

        detections: list[RawDetection] = []
        for index in keep:
            norm_x1 = (float(x1[index]) - letterbox.pad_w) / letterbox.scale / letterbox.orig_w
            norm_y1 = (float(y1[index]) - letterbox.pad_h) / letterbox.scale / letterbox.orig_h
            norm_x2 = (float(x2[index]) - letterbox.pad_w) / letterbox.scale / letterbox.orig_w
            norm_y2 = (float(y2[index]) - letterbox.pad_h) / letterbox.scale / letterbox.orig_h
            detections.append(
                RawDetection(
                    x_min=max(0.0, min(1.0, norm_x1)),
                    y_min=max(0.0, min(1.0, norm_y1)),
                    x_max=max(0.0, min(1.0, norm_x2)),
                    y_max=max(0.0, min(1.0, norm_y2)),
                    confidence=float(confidences[index]),
                    class_index=int(class_ids[index]),
                ),
            )
        return detections


class MinioFrameStore:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool,
    ) -> None:
        from minio import Minio  # noqa: PLC0415

        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

    async def download(self, frame_uri: str) -> np.ndarray | None:
        bucket, object_name = _parse_s3_uri(frame_uri)
        if bucket is None or object_name is None:
            logger.warning("invalid frame URI: %s", frame_uri)
            return None

        try:
            response = await asyncio.to_thread(self._client.get_object, bucket, object_name)
            data = response.read()
            response.close()
            response.release_conn()
        except Exception:
            logger.warning("failed to download %s", frame_uri, exc_info=True)
            return None

        from PIL import Image  # noqa: PLC0415

        image = Image.open(io.BytesIO(data)).convert("RGB")
        return np.array(image)


class ShadowInferenceWorker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._shutdown = asyncio.Event()
        self._consumer: Any | None = None
        self._producer: Any | None = None
        self._trackers: dict[str, ByteTracker] = {}
        self._frame_store = MinioFrameStore(
            endpoint=args.minio_endpoint,
            access_key=args.minio_access_key,
            secret_key=args.minio_secret_key,
            secure=args.minio_secure,
        )
        self._detector = ShadowDetectorClient(
            triton_url=args.triton_url,
            model_name=args.model_name,
            model_version=args.model_version,
            input_name=args.triton_input_name,
            output_name=args.triton_output_name,
            confidence_threshold=args.confidence_threshold,
            nms_iou_threshold=args.nms_iou_threshold,
            input_size=args.input_size,
        )

    async def start(self) -> None:
        try:
            from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - runtime path
            raise RuntimeError(
                "missing optional dependency 'aiokafka'; install inference-worker "
                "requirements or run inside the service image",
            ) from exc

        start_http_server(self.args.metrics_port)
        logger.info("shadow metrics exposed on port %d", self.args.metrics_port)

        ssl_context = _build_ssl_context(self.args)
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.args.kafka_bootstrap,
            security_protocol=self.args.kafka_security_protocol,
            sasl_mechanism=self.args.kafka_sasl_mechanism,
            sasl_plain_username=self.args.kafka_sasl_username,
            sasl_plain_password=self.args.kafka_sasl_password,
            ssl_context=ssl_context,
            acks="all",
            enable_idempotence=True,
            compression_type="zstd",
        )
        await self._producer.start()

        self._consumer = AIOKafkaConsumer(
            self.args.kafka_input_topic,
            bootstrap_servers=self.args.kafka_bootstrap,
            group_id=self.args.kafka_group_id,
            security_protocol=self.args.kafka_security_protocol,
            sasl_mechanism=self.args.kafka_sasl_mechanism,
            sasl_plain_username=self.args.kafka_sasl_username,
            sasl_plain_password=self.args.kafka_sasl_password,
            ssl_context=ssl_context,
            enable_auto_commit=False,
            auto_offset_reset=self.args.auto_offset_reset,
        )
        await self._consumer.start()
        logger.info(
            "consuming %s with group=%s",
            self.args.kafka_input_topic,
            self.args.kafka_group_id,
        )

        try:
            while not self._shutdown.is_set():
                batches = await self._consumer.getmany(
                    timeout_ms=self.args.poll_timeout_ms,
                    max_records=self.args.max_poll_records,
                )
                if not batches:
                    continue
                for messages in batches.values():
                    for message in messages:
                        await self._process_message(message)
                await self._consumer.commit()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._shutdown.set()
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def _process_message(self, message: Any) -> None:
        if message.value is None:
            return

        SHADOW_FRAMES_CONSUMED_TOTAL.inc()

        frame_ref = _load_frame_ref_type()()
        frame_ref.ParseFromString(message.value)

        edge_ts = _timestamp_to_epoch(frame_ref.timestamps.edge_receive_ts)
        if edge_ts <= 0:
            edge_ts = time.time()

        frame = await self._frame_store.download(frame_ref.frame_uri)
        if frame is None:
            return

        detections = await self._detector.detect(frame)
        tracker = self._trackers.setdefault(
            frame_ref.camera_id,
            ByteTracker(
                frame_ref.camera_id,
                track_thresh=self.args.track_thresh,
                match_thresh=self.args.match_thresh,
                second_match_thresh=self.args.second_match_thresh,
                max_lost_frames=self.args.max_lost_frames,
            ),
        )
        updated_tracks, terminated_tracks = tracker.update(detections, edge_ts)
        assignments = _build_track_assignments(detections, updated_tracks)

        await self._publish_detections(
            detections=detections,
            frame_id=frame_ref.frame_id,
            camera_id=frame_ref.camera_id,
            frame_sequence=int(frame_ref.frame_sequence),
            timestamps=frame_ref.timestamps,
            track_assignments=assignments,
        )
        for track in updated_tracks:
            await self._publish_tracklet(track, frame_ref.timestamps)
        for track in terminated_tracks:
            await self._publish_tracklet(track, frame_ref.timestamps)

    async def _publish_detections(
        self,
        *,
        detections: list[RawDetection],
        frame_id: str,
        camera_id: str,
        frame_sequence: int,
        timestamps: Any,
        track_assignments: dict[int, str],
    ) -> None:
        for index, detection in enumerate(detections):
            payload = build_detection_proto(
                detection,
                frame_id=frame_id,
                camera_id=camera_id,
                model_name=self.args.model_name,
                model_version=self.args.model_version,
                timestamps=timestamps,
            )
            headers = [
                ("x-proto-schema", b"vidanalytics.v1.detection.Detection"),
                ("x-frame-seq", str(frame_sequence).encode("ascii")),
            ]
            track_id = track_assignments.get(index)
            if track_id:
                headers.append(("x-local-track-id", track_id.encode("ascii")))
            await self._send(
                self.args.kafka_output_detections_topic,
                key=camera_id.encode("utf-8"),
                value=payload,
                headers=headers,
            )

    async def _publish_tracklet(self, track: STrack, timestamps: Any) -> None:
        await self._send(
            self.args.kafka_output_tracklets_topic,
            key=track.camera_id.encode("utf-8"),
            value=build_tracklet_proto(
                track,
                timestamps=timestamps,
                tracker_version=self.args.tracker_version,
            ),
            headers=[("x-proto-schema", b"vidanalytics.v1.tracklet.Tracklet")],
        )

    async def _send(
        self,
        topic: str,
        *,
        key: bytes,
        value: bytes,
        headers: list[tuple[str, bytes]],
    ) -> None:
        if self._producer is None:
            SHADOW_PUBLISH_ERRORS_TOTAL.inc()
            return
        try:
            await self._producer.send_and_wait(topic, key=key, value=value, headers=headers)
        except Exception:
            SHADOW_PUBLISH_ERRORS_TOTAL.inc()
            logger.warning("failed to publish to %s", topic, exc_info=True)


def build_detection_proto(
    detection: RawDetection,
    *,
    frame_id: str,
    camera_id: str,
    model_name: str,
    model_version: str,
    timestamps: Any,
) -> bytes:
    detection_type = _load_detection_type()
    bbox_type = _load_bbox_type()
    message = detection_type()
    message.detection_id = str(uuid.uuid4())
    message.frame_id = frame_id
    message.camera_id = camera_id
    message.object_class = detection.proto_class
    message.confidence = detection.confidence
    message.bbox.CopyFrom(
        bbox_type(
            x_min=detection.x_min,
            y_min=detection.y_min,
            x_max=detection.x_max,
            y_max=detection.y_max,
        ),
    )
    message.model_name = model_name
    message.model_version = model_version
    message.timestamps.CopyFrom(timestamps)
    return message.SerializeToString()


def build_tracklet_proto(
    track: STrack,
    *,
    timestamps: Any,
    tracker_version: str,
) -> bytes:
    tracklet_type = _load_tracklet_type()
    trajectory_point_type = _load_trajectory_point_type()
    message = tracklet_type()
    message.track_id = track.track_id
    message.camera_id = track.camera_id
    message.object_class = CLASS_INDEX_TO_PROTO[track.majority_class]
    message.state = track.state.proto_value
    message.mean_confidence = track.confidence
    message.tracker_version = tracker_version

    for point in track.trajectory[-10:]:
        proto_point = trajectory_point_type()
        proto_point.detection_id = point.detection_id
        proto_point.centroid_x = point.centroid_x
        proto_point.centroid_y = point.centroid_y
        _set_proto_timestamp(proto_point.frame_ts, point.frame_ts)
        message.trajectory.append(proto_point)

    message.timestamps.CopyFrom(timestamps)
    return message.SerializeToString()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triton-url", default="localhost:8001")
    parser.add_argument("--model-name", default="yolov8l")
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--triton-input-name", default="images")
    parser.add_argument("--triton-output-name", default="output0")
    parser.add_argument("--kafka-bootstrap", default="localhost:9092")
    parser.add_argument("--kafka-group-id", default="shadow-detector-worker")
    parser.add_argument("--kafka-input-topic", default="frames.decoded.refs")
    parser.add_argument("--kafka-output-detections-topic", default="detections.shadow")
    parser.add_argument("--kafka-output-tracklets-topic", default="tracklets.shadow")
    parser.add_argument("--kafka-security-protocol", default="PLAINTEXT")
    parser.add_argument("--kafka-sasl-mechanism")
    parser.add_argument("--kafka-sasl-username")
    parser.add_argument("--kafka-sasl-password")
    parser.add_argument("--kafka-ssl-ca-file")
    parser.add_argument("--kafka-ssl-cert-file")
    parser.add_argument("--kafka-ssl-key-file")
    parser.add_argument("--auto-offset-reset", default="latest")
    parser.add_argument("--poll-timeout-ms", type=int, default=1000)
    parser.add_argument("--max-poll-records", type=int, default=10)
    parser.add_argument("--metrics-port", type=int, default=9108)
    parser.add_argument("--minio-endpoint", default="localhost:9000")
    parser.add_argument("--minio-access-key", default="minioadmin")
    parser.add_argument("--minio-secret-key", default="minioadmin")
    parser.add_argument("--minio-secure", action="store_true")
    parser.add_argument("--confidence-threshold", type=float, default=0.40)
    parser.add_argument("--nms-iou-threshold", type=float, default=0.45)
    parser.add_argument("--input-size", type=int, default=640)
    parser.add_argument("--track-thresh", type=float, default=0.5)
    parser.add_argument("--match-thresh", type=float, default=0.8)
    parser.add_argument("--second-match-thresh", type=float, default=0.5)
    parser.add_argument("--max-lost-frames", type=int, default=50)
    parser.add_argument("--tracker-version", default="bytetrack-shadow-1.0")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _ensure_proto_search_path() -> None:
    for candidate in PROTO_SEARCH_PATHS:
        if candidate.exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return

    generated_dir = _generate_protos()
    if str(generated_dir) not in sys.path:
        sys.path.insert(0, str(generated_dir))


def _generate_protos() -> Path:
    global _PROTO_TEMP_DIR
    if _PROTO_TEMP_DIR is None:
        _PROTO_TEMP_DIR = tempfile.TemporaryDirectory(prefix="cilex-shadow-proto-")
    output_dir = Path(_PROTO_TEMP_DIR.name)

    try:
        from grpc_tools import protoc  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - runtime path
        raise RuntimeError(
            "generated protobufs are unavailable; run a service gen_proto.sh or "
            "install grpcio-tools",
        ) from exc

    proto_root = REPO_ROOT / "proto"
    result = protoc.main(
        (
            "grpc_tools.protoc",
            f"-I{proto_root}",
            f"-I{REPO_ROOT}",
            f"--python_out={output_dir}",
            str(proto_root / "vidanalytics" / "v1" / "common" / "common.proto"),
            str(proto_root / "vidanalytics" / "v1" / "frame" / "frame.proto"),
            str(proto_root / "vidanalytics" / "v1" / "detection" / "detection.proto"),
            str(proto_root / "vidanalytics" / "v1" / "tracklet" / "tracklet.proto"),
        ),
    )
    if result != 0:  # pragma: no cover - runtime path
        raise RuntimeError("failed to generate shadow worker protobuf bindings")
    return output_dir


def _load_frame_ref_type() -> type[Any]:
    _ensure_proto_search_path()
    return import_module("vidanalytics.v1.frame.frame_pb2").FrameRef


def _load_detection_type() -> type[Any]:
    _ensure_proto_search_path()
    return import_module("vidanalytics.v1.detection.detection_pb2").Detection


def _load_bbox_type() -> type[Any]:
    _ensure_proto_search_path()
    return import_module("vidanalytics.v1.detection.detection_pb2").BoundingBox


def _load_tracklet_type() -> type[Any]:
    _ensure_proto_search_path()
    return import_module("vidanalytics.v1.tracklet.tracklet_pb2").Tracklet


def _load_trajectory_point_type() -> type[Any]:
    _ensure_proto_search_path()
    return import_module("vidanalytics.v1.tracklet.tracklet_pb2").TrajectoryPoint


def _set_proto_timestamp(field: Any, epoch_s: float) -> None:
    whole = int(epoch_s)
    field.seconds = whole
    field.nanos = int((epoch_s - whole) * 1_000_000_000)


def _timestamp_to_epoch(timestamp: Any) -> float:
    return float(timestamp.seconds) + float(timestamp.nanos) / 1_000_000_000.0


def _build_track_assignments(
    detections: list[RawDetection],
    updated_tracks: list[STrack],
) -> dict[int, str]:
    assignments: dict[int, str] = {}
    for index, detection in enumerate(detections):
        centroid_x = (detection.x_min + detection.x_max) / 2
        centroid_y = (detection.y_min + detection.y_max) / 2
        for track in updated_tracks:
            if not track.trajectory:
                continue
            point = track.trajectory[-1]
            if abs(point.centroid_x - centroid_x) < 0.001 and abs(
                point.centroid_y - centroid_y,
            ) < 0.001:
                assignments[index] = track.track_id
                break
    return assignments


def _parse_s3_uri(frame_uri: str) -> tuple[str | None, str | None]:
    uri = frame_uri[5:] if frame_uri.startswith("s3://") else frame_uri
    parts = uri.split("/", 1)
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def _resize_bilinear(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    src_h, src_w = frame.shape[:2]
    if src_h == height and src_w == width:
        return frame.copy()

    x_coords = np.linspace(0, src_w - 1, width)
    y_coords = np.linspace(0, src_h - 1, height)
    x0 = np.floor(x_coords).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, src_w - 1)
    y0 = np.floor(y_coords).astype(np.int32)
    y1 = np.clip(y0 + 1, 0, src_h - 1)

    x_weight = (x_coords - x0).astype(np.float32)
    y_weight = (y_coords - y0).astype(np.float32)

    top_left = frame[y0[:, None], x0[None, :]].astype(np.float32)
    top_right = frame[y0[:, None], x1[None, :]].astype(np.float32)
    bottom_left = frame[y1[:, None], x0[None, :]].astype(np.float32)
    bottom_right = frame[y1[:, None], x1[None, :]].astype(np.float32)

    top = top_left * (1 - x_weight)[None, :, None] + top_right * x_weight[None, :, None]
    bottom = (
        bottom_left * (1 - x_weight)[None, :, None]
        + bottom_right * x_weight[None, :, None]
    )
    resized = top * (1 - y_weight)[:, None, None] + bottom * y_weight[:, None, None]
    return np.clip(resized, 0, 255).astype(np.uint8)


def _per_class_nms(
    x1: np.ndarray,
    y1: np.ndarray,
    x2: np.ndarray,
    y2: np.ndarray,
    scores: np.ndarray,
    class_ids: np.ndarray,
    threshold: float,
) -> list[int]:
    kept: list[int] = []
    for class_id in np.unique(class_ids):
        indices = np.where(class_ids == class_id)[0]
        order = indices[np.argsort(scores[indices])[::-1]]
        while order.size > 0:
            current = int(order[0])
            kept.append(current)
            if order.size == 1:
                break
            remainder = order[1:]
            ious = _array_iou(
                x1[current],
                y1[current],
                x2[current],
                y2[current],
                x1[remainder],
                y1[remainder],
                x2[remainder],
                y2[remainder],
            )
            order = remainder[ious < threshold]
    return kept


def _array_iou(
    box_x1: float,
    box_y1: float,
    box_x2: float,
    box_y2: float,
    other_x1: np.ndarray,
    other_y1: np.ndarray,
    other_x2: np.ndarray,
    other_y2: np.ndarray,
) -> np.ndarray:
    inter_x1 = np.maximum(box_x1, other_x1)
    inter_y1 = np.maximum(box_y1, other_y1)
    inter_x2 = np.minimum(box_x2, other_x2)
    inter_y2 = np.minimum(box_y2, other_y2)

    inter_w = np.maximum(0.0, inter_x2 - inter_x1)
    inter_h = np.maximum(0.0, inter_y2 - inter_y1)
    intersection = inter_w * inter_h

    box_area = max(0.0, box_x2 - box_x1) * max(0.0, box_y2 - box_y1)
    other_area = np.maximum(0.0, other_x2 - other_x1) * np.maximum(
        0.0,
        other_y2 - other_y1,
    )
    union = box_area + other_area - intersection
    return np.divide(intersection, union, out=np.zeros_like(intersection), where=union > 0)


def _bbox_iou(box_a: np.ndarray, box_b: np.ndarray) -> float:
    inter_x1 = max(float(box_a[0]), float(box_b[0]))
    inter_y1 = max(float(box_a[1]), float(box_b[1]))
    inter_x2 = min(float(box_a[2]), float(box_b[2]))
    inter_y2 = min(float(box_a[3]), float(box_b[3]))
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    intersection = inter_w * inter_h
    area_a = max(0.0, float(box_a[2] - box_a[0])) * max(0.0, float(box_a[3] - box_a[1]))
    area_b = max(0.0, float(box_b[2] - box_b[0])) * max(0.0, float(box_b[3] - box_b[1]))
    union = area_a + area_b - intersection
    if union <= 0:
        return 0.0
    return intersection / union


def _build_ssl_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    if not any([args.kafka_ssl_ca_file, args.kafka_ssl_cert_file, args.kafka_ssl_key_file]):
        return None
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if args.kafka_ssl_ca_file:
        context.load_verify_locations(args.kafka_ssl_ca_file)
    if args.kafka_ssl_cert_file and args.kafka_ssl_key_file:
        context.load_cert_chain(args.kafka_ssl_cert_file, args.kafka_ssl_key_file)
    return context


async def run(args: argparse.Namespace) -> None:
    worker = ShadowInferenceWorker(args)
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        logger.info("shutdown requested")
        asyncio.create_task(worker.shutdown())

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, _request_shutdown)

    await worker.start()


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    logger.info(
        "starting shadow inference worker model=%s version=%s topic=%s",
        args.model_name,
        args.model_version,
        args.kafka_input_topic,
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
