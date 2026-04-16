"""Attribute Extraction Service.

Kafka consumer pipeline that extracts color attributes from tracklets:

1. Consume Tracklet protos from ``tracklets.local``
2. Skip animal/bicycle (no color attributes)
3. Fetch best frame from MinIO, crop detection bbox
4. Quality gate: size, sharpness, brightness, IR detection, occlusion
5. White balance correction
6. Color classification via Triton (ResNet-18)
7. Confidence-weighted aggregation per track
8. Flush to DB on track TERMINATED or observation threshold
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import cv2
import numpy as np

from aggregator import TrackAggregator
from classifier_client import ClassifierClient
from config import AttributeSettings
from metrics import (
    CLASSIFIED_TOTAL,
    IR_SKIPPED_TOTAL,
    QUALITY_REJECTED_TOTAL,
)
from publisher import DBWriter
from quality_gate import check_quality
from white_balance import apply_white_balance

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"

# Proto object_class enum → taxonomy lowercase name
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

# Object classes that get color attributes
VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}
PERSON_CLASS = "person"
SKIP_CLASSES = {"animal", "bicycle", "unspecified"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="YAML config path."
    )
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
        raise RuntimeError(
            "generated protobufs not found; run `bash gen_proto.sh`"
        ) from exc
    return tracklet_pb2.Tracklet


class AttributeService:
    """Main attribute extraction service orchestrator."""

    def __init__(self, settings: AttributeSettings) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()
        self._pool: asyncpg.Pool | None = None
        self._db: DBWriter | None = None
        self._classifier: ClassifierClient | None = None
        self._aggregator = TrackAggregator()
        self._minio: Any = None
        self._flush_task: asyncio.Task[None] | None = None
        self._started_at: float = time.time()
        self._consumer_subscribed: bool = False
        self._health_runner: Any = None

    async def start(self) -> None:
        """Initialise subsystems and start consuming."""
        self._pool = await asyncpg.create_pool(
            dsn=self.settings.db_dsn, min_size=2, max_size=10,
        )
        self._db = DBWriter(self._pool, self.settings.model_version)

        self._classifier = ClassifierClient(
            triton_url=self.settings.triton_url,
            model_name=self.settings.triton_model,
            input_name=self.settings.triton_input_name,
            output_name=self.settings.triton_output_name,
            confidence_threshold=self.settings.color_confidence_threshold,
        )

        self._minio = self._create_minio()

        from prometheus_client import start_http_server  # noqa: PLC0415
        start_http_server(self.settings.metrics_port)
        logger.info("Metrics server on port %d", self.settings.metrics_port)

        await self._start_health_server()

        self._flush_task = asyncio.create_task(self._periodic_flush())

        await self._consume_loop()

    async def _start_health_server(self) -> None:
        try:
            from aiohttp import web  # noqa: PLC0415
        except ImportError:
            logger.warning("aiohttp not installed — /health endpoint disabled")
            return

        async def health_handler(_request: Any) -> Any:
            now = time.time()
            uptime = now - self._started_at
            checks: dict[str, str] = {}
            healthy = True

            if self._consumer_subscribed:
                checks["consumer"] = "connected"
            else:
                checks["consumer"] = "disconnected"
                healthy = False

            body = {
                "status": "ok" if healthy else "unhealthy",
                "uptime_seconds": int(uptime),
                "checks": checks,
            }
            return web.json_response(body, status=200 if healthy else 503)

        app = web.Application()
        app.router.add_get("/health", health_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.settings.health_port)
        await site.start()
        self._health_runner = runner
        logger.info("Health server on port %d", self.settings.health_port)

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self._shutdown.set()
        if self._flush_task is not None:
            self._flush_task.cancel()
        if self._db is not None:
            await self._db.flush_buffer()
        if self._health_runner is not None:
            try:
                await self._health_runner.cleanup()
            except Exception:
                pass
        if self._pool is not None:
            await self._pool.close()
        logger.info("Attribute service shut down")

    # ------------------------------------------------------------------
    # Kafka consumer
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        from confluent_kafka import Consumer, KafkaError  # noqa: PLC0415

        cfg = self.settings
        consumer_config: dict[str, Any] = {
            "bootstrap.servers": cfg.kafka_bootstrap,
            "group.id": cfg.kafka_group_id,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
        }
        if cfg.kafka_security_protocol != "PLAINTEXT":
            consumer_config["security.protocol"] = cfg.kafka_security_protocol

        consumer = Consumer(consumer_config)
        consumer.subscribe([cfg.kafka_topic])
        self._consumer_subscribed = True

        TrackletType = _load_tracklet_type()
        logger.info(
            "Consuming from %s (group=%s)", cfg.kafka_topic, cfg.kafka_group_id,
        )

        try:
            while not self._shutdown.is_set():
                msg = await asyncio.to_thread(
                    consumer.poll, cfg.kafka_poll_timeout_s,
                )

                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("Kafka error: %s", msg.error())
                    continue

                if msg.value() is None:
                    await asyncio.to_thread(consumer.commit, asynchronous=False)
                    continue

                try:
                    tracklet = TrackletType()
                    tracklet.ParseFromString(msg.value())
                    await self._process_tracklet(tracklet)
                except Exception:
                    logger.exception(
                        "Error processing tracklet at offset %d", msg.offset(),
                    )

                await asyncio.to_thread(consumer.commit, asynchronous=False)

                # Flush buffer if large enough
                if self._db is not None and self._db.buffer_size >= cfg.flush_batch_size:
                    await self._db.flush_buffer()
        finally:
            self._consumer_subscribed = False
            consumer.close()

    # ------------------------------------------------------------------
    # Per-tracklet pipeline
    # ------------------------------------------------------------------

    async def _process_tracklet(self, tracklet: Any) -> None:
        """Process a single Tracklet through the attribute pipeline."""
        track_id = tracklet.track_id
        camera_id = tracklet.camera_id
        obj_class = PROTO_CLASS_TO_NAME.get(tracklet.object_class, "unspecified")
        state = tracklet.state  # enum int

        # Skip classes without color attributes
        if obj_class in SKIP_CLASSES:
            return

        # Determine attribute types
        attr_types: list[str] = []
        if obj_class in VEHICLE_CLASSES:
            attr_types = ["vehicle_color"]
        elif obj_class == PERSON_CLASS:
            attr_types = ["person_upper_color", "person_lower_color"]

        if not attr_types:
            return

        # Check if track is TERMINATED (state=4) — flush aggregated
        is_terminated = state == 4

        # If terminated, flush what we have
        if is_terminated:
            await self._flush_track(track_id)
            return

        # Skip if we've hit observation limit
        if self._aggregator.observation_count(track_id) >= self.settings.max_observations_per_track:
            return

        # Fetch frame and bbox from DB + MinIO
        frame_bgr, bbox = await self._fetch_frame_and_bbox(camera_id, track_id)
        if frame_bgr is None or bbox is None:
            return

        bbox_x, bbox_y, bbox_w, bbox_h = bbox
        frame_h, frame_w = frame_bgr.shape[:2]

        # Compute pixel-space bbox
        px_x = int(bbox_x * frame_w)
        px_y = int(bbox_y * frame_h)
        px_w = int(bbox_w * frame_w)
        px_h = int(bbox_h * frame_h)

        # Clamp to frame boundaries
        px_x = max(0, px_x)
        px_y = max(0, px_y)
        px_w = min(px_w, frame_w - px_x)
        px_h = min(px_h, frame_h - px_y)

        if px_w <= 0 or px_h <= 0:
            return

        # Crop the detection region
        crop_bgr = frame_bgr[px_y:px_y + px_h, px_x:px_x + px_w]
        if crop_bgr.size == 0:
            return

        # Quality gate
        qr = check_quality(
            bbox_height_px=px_h,
            frame_height_px=frame_h,
            frame_width_px=frame_w,
            bbox_x=bbox_x,
            bbox_y=bbox_y,
            bbox_w=bbox_w,
            bbox_h=bbox_h,
            crop_bgr=crop_bgr,
            min_bbox_height=self.settings.min_bbox_height,
            min_sharpness=self.settings.min_sharpness,
            brightness_range=self.settings.brightness_range,
            ir_saturation_threshold=self.settings.ir_saturation_threshold,
            max_occlusion_ratio=self.settings.max_occlusion_ratio,
        )

        if qr.is_ir:
            IR_SKIPPED_TOTAL.inc()
            return

        if not qr.passed:
            QUALITY_REJECTED_TOTAL.labels(reason=qr.reason or "unknown").inc()
            return

        # White balance
        corrected = apply_white_balance(crop_bgr, is_ir=qr.is_ir)

        # Classify per attribute type
        now = datetime.now(tz=timezone.utc)
        for attr_type in attr_types:
            # For person: split crop into upper/lower halves
            classify_crop = corrected
            if attr_type == "person_upper_color":
                mid = corrected.shape[0] // 2
                classify_crop = corrected[:mid, :, :]
            elif attr_type == "person_lower_color":
                mid = corrected.shape[0] // 2
                classify_crop = corrected[mid:, :, :]

            if classify_crop.size == 0:
                continue

            try:
                color, confidence = await self._classifier.classify(classify_crop)
            except Exception:
                logger.debug(
                    "Classification failed for track %s", track_id, exc_info=True,
                )
                continue

            CLASSIFIED_TOTAL.labels(
                attribute_type=attr_type, color_value=color,
            ).inc()

            self._aggregator.add_observation(
                track_id=track_id,
                attr_type=attr_type,
                color=color,
                confidence=confidence,
                quality=qr.quality_score,
                observed_at=now,
            )

        # Check observation threshold for early flush
        if self._aggregator.observation_count(track_id) >= self.settings.max_observations_per_track:
            await self._flush_track(track_id)

    async def _flush_track(self, track_id: str) -> None:
        """Flush aggregated attributes to DB for a track."""
        if self._db is None:
            return
        attrs = self._aggregator.flush_track(track_id)
        for attr in attrs:
            self._db.buffer_attribute(attr)

    # ------------------------------------------------------------------
    # Frame fetching
    # ------------------------------------------------------------------

    async def _fetch_frame_and_bbox(
        self,
        camera_id: str,
        track_id: str,
    ) -> tuple[np.ndarray | None, tuple[float, float, float, float] | None]:
        """Fetch the latest frame for a track from MinIO and return bbox."""
        if self._db is None or self._minio is None:
            return None, None

        # Get detection bbox from DB
        det = await self._db.get_detection_bbox(camera_id, track_id)
        if det is None:
            return None, None

        frame_seq, bbox_x, bbox_y, bbox_w, bbox_h = det

        # Download frame from MinIO
        bucket = self.settings.minio_frame_bucket
        key = f"{camera_id}/{frame_seq}.jpg"

        try:
            response = await asyncio.to_thread(
                self._minio.get_object, bucket, key,
            )
            data = response.read()
            response.close()
            response.release_conn()
        except Exception:
            logger.debug("Failed to download frame %s/%s", bucket, key)
            return None, None

        # Decode JPEG to BGR
        arr = np.frombuffer(data, dtype=np.uint8)
        frame_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame_bgr is None:
            return None, None

        return frame_bgr, (bbox_x, bbox_y, bbox_w, bbox_h)

    # ------------------------------------------------------------------
    # Periodic flush
    # ------------------------------------------------------------------

    async def _periodic_flush(self) -> None:
        """Periodically flush the write buffer."""
        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(self.settings.flush_interval_s)
                if self._db is not None:
                    await self._db.flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in periodic flush")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_minio(self) -> Any:
        try:
            from minio import Minio  # noqa: PLC0415
        except ImportError:
            logger.warning("minio package not installed — frame download disabled")
            return None

        return Minio(
            self.settings.minio_url,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key,
            secure=self.settings.minio_secure,
        )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


async def run(settings: AttributeSettings) -> None:
    service = AttributeService(settings)
    loop = asyncio.get_event_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        asyncio.ensure_future(service.shutdown())

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
    settings = AttributeSettings.from_yaml(args.config)
    setup_logging(settings.log_level)
    logger.info("Starting attribute extraction service")
    asyncio.run(run(settings))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
