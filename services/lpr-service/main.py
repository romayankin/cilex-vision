"""LPR service — consume vehicle tracklets, detect plates, OCR, and store results."""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import asyncpg
import numpy as np
from PIL import Image

from config import LprSettings
from metrics import (
    PIPELINE_ERRORS_TOTAL,
    PLATES_DETECTED_TOTAL,
    PLATES_RECOGNIZED_TOTAL,
    QUALITY_REJECTED_TOTAL,
)
from ocr_client import OcrClient
from plate_detector import PlateDetectorClient
from quality_gate import check_quality

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"

PROTO_CLASS_TO_NAME: dict[int, str] = {
    0: "unspecified",
    1: "person",
    2: "car",
    3: "truck",
    4: "bus",
    5: "bicycle",
    6: "motorcycle",
    7: "animal",
}

VEHICLE_CLASSES = {"car", "truck", "bus"}
TRACKLET_STATE_LOST = 3
TRACKLET_STATE_TERMINATED = 4

LPR_RESULT_COLUMNS = [
    "local_track_id",
    "camera_id",
    "plate_text",
    "plate_confidence",
    "country_format",
    "plate_bbox_x",
    "plate_bbox_y",
    "plate_bbox_w",
    "plate_bbox_h",
    "model_version",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="YAML config path.")
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _load_tracklet_type() -> type[Any]:
    try:
        from vidanalytics.v1.tracklet import tracklet_pb2  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("generated protobufs not found; run `bash gen_proto.sh`") from exc
    return cast(type[Any], tracklet_pb2.Tracklet)


def _proto_ts_to_datetime(ts: Any) -> datetime | None:
    seconds = int(getattr(ts, "seconds", 0))
    nanos = int(getattr(ts, "nanos", 0))
    if seconds == 0 and nanos == 0:
        return None
    return datetime.fromtimestamp(seconds + (nanos / 1_000_000_000), tz=timezone.utc)


@dataclass(frozen=True)
class DetectionLookup:
    frame_seq: int
    detection_time: datetime
    bbox_x: float
    bbox_y: float
    bbox_w: float
    bbox_h: float
    confidence: float


@dataclass(frozen=True)
class PendingPlateResult:
    local_track_id: str
    camera_id: str
    plate_text: str
    plate_confidence: float
    country_format: str | None
    plate_bbox_x: float
    plate_bbox_y: float
    plate_bbox_w: float
    plate_bbox_h: float
    model_version: str
    score: float

    def as_record(self) -> tuple[Any, ...]:
        return (
            UUID(self.local_track_id),
            self.camera_id,
            self.plate_text,
            self.plate_confidence,
            self.country_format,
            self.plate_bbox_x,
            self.plate_bbox_y,
            self.plate_bbox_w,
            self.plate_bbox_h,
            self.model_version,
        )


@dataclass
class TrackState:
    best_result: PendingPlateResult | None = None
    samples_processed: int = 0
    last_frame_ts: datetime | None = None


class LprRepository:
    """Database access for detection lookup and buffered LPR writes."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._buffer: list[PendingPlateResult] = []

    def buffer_result(self, result: PendingPlateResult) -> None:
        self._buffer.append(result)

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    async def flush_buffer(self) -> None:
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.copy_records_to_table(
                    "lpr_results",
                    records=[item.as_record() for item in batch],
                    columns=LPR_RESULT_COLUMNS,
                )

    async def get_best_detection(
        self,
        camera_id: str,
        local_track_id: str,
    ) -> DetectionLookup | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT time, frame_seq, bbox_x, bbox_y, bbox_w, bbox_h, confidence
                FROM detections
                WHERE camera_id = $1
                  AND local_track_id = $2
                  AND object_class IN ('car', 'truck', 'bus')
                ORDER BY confidence DESC, time DESC
                LIMIT 1
                """,
                camera_id,
                local_track_id,
            )
        if row is None:
            return None
        return DetectionLookup(
            frame_seq=int(row["frame_seq"]),
            detection_time=row["time"],
            bbox_x=float(row["bbox_x"]),
            bbox_y=float(row["bbox_y"]),
            bbox_w=float(row["bbox_w"]),
            bbox_h=float(row["bbox_h"]),
            confidence=float(row["confidence"]),
        )


class LprService:
    """Kafka-driven LPR pipeline service."""

    def __init__(self, settings: LprSettings) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()
        self._pool: asyncpg.Pool | None = None
        self._repo: LprRepository | None = None
        self._minio: Any = None
        self._detector: PlateDetectorClient | None = None
        self._ocr: OcrClient | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._track_states: dict[str, TrackState] = {}

    async def start(self) -> None:
        from prometheus_client import start_http_server  # noqa: PLC0415

        start_http_server(self.settings.metrics_port)
        logger.info("Metrics server listening on %d", self.settings.metrics_port)

        if not self.settings.enabled:
            logger.warning("LPR service disabled via config; idling without consuming")
            await self._shutdown.wait()
            return

        self._pool = await asyncpg.create_pool(dsn=self.settings.db_dsn, min_size=1, max_size=5)
        self._repo = LprRepository(self._pool)
        self._minio = self._create_minio()
        self._detector = PlateDetectorClient(
            triton_url=self.settings.triton_url,
            model_name=self.settings.plate_detector_model,
            input_name=self.settings.plate_detector_input_name,
            output_name=self.settings.plate_detector_output_name,
            input_size=self.settings.plate_detector_input_size,
            confidence_threshold=self.settings.plate_detection_confidence_threshold,
            nms_iou_threshold=self.settings.plate_nms_iou_threshold,
        )
        self._ocr = OcrClient(
            triton_url=self.settings.triton_url,
            model_name=self.settings.ocr_model,
            input_name=self.settings.ocr_input_name,
            output_name=self.settings.ocr_output_name,
            input_width=self.settings.ocr_input_width,
            input_height=self.settings.ocr_input_height,
            alphabet=self.settings.ocr_alphabet,
            confidence_threshold=self.settings.ocr_confidence_threshold,
        )
        self._flush_task = asyncio.create_task(self._periodic_flush())
        await self._consume_loop()

    async def shutdown(self) -> None:
        self._shutdown.set()
        if self._flush_task is not None:
            self._flush_task.cancel()
        await self._flush_all_tracks()
        if self._repo is not None:
            await self._repo.flush_buffer()
        if self._pool is not None:
            await self._pool.close()
        logger.info("LPR service shut down")

    async def _consume_loop(self) -> None:
        from confluent_kafka import Consumer, KafkaError  # noqa: PLC0415

        consumer_config: dict[str, Any] = {
            "bootstrap.servers": self.settings.kafka_bootstrap,
            "group.id": self.settings.kafka_group_id,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
            "partition.assignment.strategy": "cooperative-sticky",
        }
        if self.settings.kafka_security_protocol != "PLAINTEXT":
            consumer_config["security.protocol"] = self.settings.kafka_security_protocol

        consumer = Consumer(consumer_config)
        consumer.subscribe([self.settings.kafka_topic])
        TrackletType = _load_tracklet_type()
        logger.info("Consuming vehicle tracklets from %s", self.settings.kafka_topic)

        try:
            while not self._shutdown.is_set():
                message = await asyncio.to_thread(consumer.poll, self.settings.kafka_poll_timeout_s)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("Kafka error: %s", message.error())
                    continue
                if message.value() is None:
                    await asyncio.to_thread(consumer.commit, asynchronous=False)
                    continue

                try:
                    tracklet = TrackletType()
                    tracklet.ParseFromString(message.value())
                    await self._process_tracklet(tracklet)
                except Exception:
                    PIPELINE_ERRORS_TOTAL.inc()
                    logger.exception("Failed to process tracklet at offset %s", message.offset())
                finally:
                    await asyncio.to_thread(consumer.commit, asynchronous=False)

                if self._repo is not None and self._repo.buffer_size >= self.settings.flush_batch_size:
                    await self._repo.flush_buffer()
        finally:
            consumer.close()

    async def _process_tracklet(self, tracklet: Any) -> None:
        track_id = str(tracklet.track_id)
        camera_id = str(tracklet.camera_id)
        object_class = PROTO_CLASS_TO_NAME.get(int(tracklet.object_class), "unspecified")
        state = int(tracklet.state)

        if object_class not in VEHICLE_CLASSES:
            if state == TRACKLET_STATE_TERMINATED:
                self._track_states.pop(track_id, None)
            return

        track_state = self._track_states.setdefault(track_id, TrackState())
        latest_point = tracklet.trajectory[-1] if tracklet.trajectory else None
        latest_ts = _proto_ts_to_datetime(latest_point.frame_ts) if latest_point is not None else None

        should_process_frame = state != TRACKLET_STATE_LOST
        if latest_ts is not None and track_state.last_frame_ts is not None and latest_ts <= track_state.last_frame_ts:
            should_process_frame = False

        if should_process_frame and track_state.samples_processed < self.settings.max_samples_per_track:
            track_state.last_frame_ts = latest_ts
            track_state.samples_processed += 1
            candidate = await self._run_inference(track_id, camera_id, object_class)
            if candidate is not None:
                current = track_state.best_result
                if current is None or candidate.score > current.score:
                    track_state.best_result = candidate

        if state == TRACKLET_STATE_TERMINATED:
            await self._flush_track(track_id)

    async def _run_inference(
        self,
        track_id: str,
        camera_id: str,
        object_class: str,
    ) -> PendingPlateResult | None:
        if self._repo is None or self._detector is None or self._ocr is None:
            return None

        detection = await self._repo.get_best_detection(camera_id, track_id)
        if detection is None:
            return None

        frame_rgb = await self._fetch_frame_rgb(camera_id, detection)
        if frame_rgb is None:
            return None

        vehicle_crop = _crop_normalized(frame_rgb, detection.bbox_x, detection.bbox_y, detection.bbox_w, detection.bbox_h)
        if vehicle_crop is None:
            return None

        detections = await self._detector.detect(vehicle_crop)
        if not detections:
            return None

        plate = detections[0]
        PLATES_DETECTED_TOTAL.labels(object_class=object_class).inc()

        plate_crop = _crop_normalized(vehicle_crop, plate.x, plate.y, plate.w, plate.h)
        if plate_crop is None:
            QUALITY_REJECTED_TOTAL.labels(reason="empty_crop").inc()
            return None

        quality = check_quality(
            plate_crop,
            min_plate_height=self.settings.min_plate_height,
            min_plate_width=self.settings.min_plate_width,
            sharpness_threshold=self.settings.sharpness_threshold,
            min_aspect_ratio=self.settings.min_aspect_ratio,
            max_aspect_ratio=self.settings.max_aspect_ratio,
        )
        if not quality.passed:
            QUALITY_REJECTED_TOTAL.labels(reason=quality.reason or "unknown").inc()
            return None

        ocr_result = await self._ocr.recognize(plate_crop)
        if not ocr_result.text:
            return None

        country_format = ocr_result.country_format or "unknown"
        PLATES_RECOGNIZED_TOTAL.labels(country_format=country_format).inc()

        return PendingPlateResult(
            local_track_id=track_id,
            camera_id=camera_id,
            plate_text=ocr_result.text,
            plate_confidence=ocr_result.confidence,
            country_format=ocr_result.country_format,
            plate_bbox_x=plate.x,
            plate_bbox_y=plate.y,
            plate_bbox_w=plate.w,
            plate_bbox_h=plate.h,
            model_version=self.settings.model_version,
            score=float(plate.confidence * max(ocr_result.confidence, 1e-6)),
        )

    async def _flush_track(self, track_id: str) -> None:
        if self._repo is None:
            self._track_states.pop(track_id, None)
            return
        track_state = self._track_states.pop(track_id, None)
        if track_state is None or track_state.best_result is None:
            return
        self._repo.buffer_result(track_state.best_result)
        if self._repo.buffer_size >= self.settings.flush_batch_size:
            await self._repo.flush_buffer()

    async def _flush_all_tracks(self) -> None:
        track_ids = list(self._track_states.keys())
        for track_id in track_ids:
            await self._flush_track(track_id)

    async def _periodic_flush(self) -> None:
        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(self.settings.flush_interval_s)
                if self._repo is not None:
                    await self._repo.flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception:
                PIPELINE_ERRORS_TOTAL.inc()
                logger.exception("Periodic LPR buffer flush failed")

    async def _fetch_frame_rgb(self, camera_id: str, detection: DetectionLookup) -> np.ndarray | None:
        if self._minio is None:
            return None
        bucket = self.settings.minio_frame_bucket
        key = await self._resolve_frame_key(camera_id, detection)
        if key is None:
            return None
        try:
            response = await asyncio.to_thread(self._minio.get_object, bucket, key)
            data = response.read()
            response.close()
            response.release_conn()
        except Exception:
            logger.debug("Failed to download frame %s/%s", bucket, key, exc_info=True)
            return None
        try:
            image = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None
        return np.asarray(image)

    async def _resolve_frame_key(self, camera_id: str, detection: DetectionLookup) -> str | None:
        listed_key = await self._resolve_key_by_listing(camera_id, detection.detection_time)
        if listed_key is not None:
            return listed_key

        date_str = detection.detection_time.astimezone(timezone.utc).strftime("%Y-%m-%d")
        for template in self.settings.frame_key_templates:
            candidate = template.format(
                camera_id=camera_id,
                frame_seq=detection.frame_seq,
                date=date_str,
            )
            try:
                await asyncio.to_thread(self._minio.stat_object, self.settings.minio_frame_bucket, candidate)
                return candidate
            except Exception:
                continue
        return None

    async def _resolve_key_by_listing(self, camera_id: str, detection_time: datetime) -> str | None:
        prefix = f"{camera_id}/{detection_time.astimezone(timezone.utc).strftime('%Y-%m-%d')}/"
        try:
            objects = await asyncio.to_thread(
                lambda: list(
                    self._minio.list_objects(
                        self.settings.minio_frame_bucket,
                        prefix=prefix,
                        recursive=True,
                    )
                )
            )
        except Exception:
            logger.debug("Failed to list frame objects for prefix %s", prefix, exc_info=True)
            return None

        closest_key: str | None = None
        closest_delta = float("inf")
        for obj in objects:
            modified = getattr(obj, "last_modified", None)
            if modified is None:
                continue
            delta = abs((modified.astimezone(timezone.utc) - detection_time).total_seconds())
            if delta < closest_delta:
                closest_delta = delta
                closest_key = str(obj.object_name)
        if closest_key is None or closest_delta > self.settings.frame_lookup_tolerance_s:
            return None
        return closest_key

    def _create_minio(self) -> Any:
        try:
            from minio import Minio  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("missing optional dependency 'minio'; install requirements.txt") from exc

        return Minio(
            self.settings.minio_url,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key,
            secure=self.settings.minio_secure,
        )


def _crop_normalized(image_rgb: np.ndarray, x: float, y: float, w: float, h: float) -> np.ndarray | None:
    height, width = image_rgb.shape[:2]
    left = max(0, int(round(x * width)))
    top = max(0, int(round(y * height)))
    box_width = max(0, int(round(w * width)))
    box_height = max(0, int(round(h * height)))
    right = min(width, left + box_width)
    bottom = min(height, top + box_height)
    if right <= left or bottom <= top:
        return None
    crop = image_rgb[top:bottom, left:right]
    if crop.size == 0:
        return None
    return crop


async def run(settings: LprSettings) -> None:
    service = LprService(settings)
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        asyncio.create_task(service.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await service.start()
    except asyncio.CancelledError:
        pass
    finally:
        await service.shutdown()


def main() -> None:
    args = parse_args()
    settings = LprSettings.from_yaml(args.config)
    setup_logging(settings.log_level)
    logger.info("Starting LPR service")
    asyncio.run(run(settings))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
