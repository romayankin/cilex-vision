"""Clip pipeline service.

Consumes closed event records, builds event clips from decoded frames in MinIO,
generates thumbnails, uploads both assets, updates PostgreSQL, and publishes a
completion message to Kafka.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import asyncpg

from clip_extractor import extract_clip
from config import ClipServiceSettings
from db_updater import ClipDBUpdater
from metrics import (
    CLIP_EVENTS_CONSUMED_TOTAL,
    CLIP_EVENTS_SKIPPED_TOTAL,
    CLIP_EXTRACTED_TOTAL,
    CLIP_EXTRACTION_ERRORS_TOTAL,
    CLIP_EXTRACTION_LATENCY_MS,
    CLIP_SIZE_BYTES,
    CLIP_THUMBNAILS_GENERATED_TOTAL,
)
from minio_client import ClipMinioClient
from thumbnail_gen import generate_thumbnail

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"
EVENT_STATE_CLOSED = 5
FRAME_PROTO_SCHEMA = b"vidanalytics.v1.frame.FrameRef"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="YAML config path.",
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _load_event_type() -> type[Any]:
    try:
        from vidanalytics.v1.event import event_pb2  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "generated protobufs not found; run `bash gen_proto.sh`"
        ) from exc
    return cast(type[Any], event_pb2.Event)


def _load_frame_module() -> Any:
    try:
        from vidanalytics.v1.frame import frame_pb2  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "generated protobufs not found; run `bash gen_proto.sh`"
        ) from exc
    return frame_pb2


class ClipService:
    """Main clip service orchestrator."""

    def __init__(self, settings: ClipServiceSettings) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()
        self._pool: asyncpg.Pool | None = None
        self._db: ClipDBUpdater | None = None
        self._minio: ClipMinioClient | None = None
        self._consumer: Any = None
        self._producer: Any = None

    async def start(self) -> None:
        """Initialise dependencies and start the Kafka loop."""
        self._pool = await asyncpg.create_pool(
            dsn=self.settings.db_dsn,
            min_size=2,
            max_size=10,
        )
        self._db = ClipDBUpdater(self._pool)
        self._minio = ClipMinioClient(
            url=self.settings.minio_url,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key,
            secure=self.settings.minio_secure,
            source_bucket=self.settings.source_bucket,
            clip_bucket=self.settings.clip_bucket,
            thumbnail_bucket=self.settings.thumbnail_bucket,
        )
        await self._minio.ensure_buckets()
        Path(self.settings.temp_dir).mkdir(parents=True, exist_ok=True)

        from confluent_kafka import Consumer, Producer  # noqa: PLC0415
        from prometheus_client import start_http_server  # noqa: PLC0415

        consumer_config: dict[str, Any] = {
            "bootstrap.servers": self.settings.kafka_bootstrap,
            "group.id": self.settings.kafka_group_id,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
        }
        producer_config: dict[str, Any] = {
            "bootstrap.servers": self.settings.kafka_bootstrap,
            "acks": "all",
            "compression.type": "zstd",
            "enable.idempotence": True,
        }
        if self.settings.kafka_security_protocol != "PLAINTEXT":
            consumer_config["security.protocol"] = self.settings.kafka_security_protocol
            producer_config["security.protocol"] = self.settings.kafka_security_protocol

        self._consumer = Consumer(consumer_config)
        self._consumer.subscribe([self.settings.kafka_input_topic])
        self._producer = Producer(producer_config)

        start_http_server(self.settings.metrics_port)
        logger.info("Metrics server listening on port %d", self.settings.metrics_port)
        await self._consume_loop()

    async def shutdown(self) -> None:
        """Flush and close all external resources."""
        self._shutdown.set()
        if self._producer is not None:
            await asyncio.to_thread(self._producer.flush, 5.0)
        if self._consumer is not None:
            self._consumer.close()
        if self._pool is not None:
            await self._pool.close()
        logger.info("Clip service shut down")

    async def _consume_loop(self) -> None:
        EventType = _load_event_type()

        logger.info(
            "Consuming from %s (group=%s)",
            self.settings.kafka_input_topic,
            self.settings.kafka_group_id,
        )

        while not self._shutdown.is_set():
            msg = await asyncio.to_thread(
                self._consumer.poll,
                self.settings.kafka_poll_timeout_s,
            )

            if msg is None:
                continue
            if msg.error():
                logger.error("Kafka error: %s", msg.error())
                continue
            if msg.value() is None:
                await asyncio.to_thread(self._consumer.commit, asynchronous=False)
                continue

            CLIP_EVENTS_CONSUMED_TOTAL.inc()

            try:
                event = EventType()
                event.ParseFromString(msg.value())
                await self._process_event(event)
            except Exception:
                CLIP_EXTRACTION_ERRORS_TOTAL.labels(reason="message_processing_failed").inc()
                logger.exception("Error processing event at offset %d", msg.offset())

            await asyncio.to_thread(self._consumer.commit, asynchronous=False)

    async def _process_event(self, event: Any) -> None:
        if self._db is None or self._minio is None or self._producer is None:
            raise RuntimeError("service not started")

        if int(event.state) != EVENT_STATE_CLOSED:
            CLIP_EVENTS_SKIPPED_TOTAL.labels(reason="event_not_closed").inc()
            return
        if event.clip_uri:
            CLIP_EVENTS_SKIPPED_TOTAL.labels(reason="clip_uri_already_set").inc()
            return

        event_id = str(event.event_id)
        existing_clip_uri = await self._db.get_existing_clip_uri(event_id)
        if existing_clip_uri:
            CLIP_EVENTS_SKIPPED_TOTAL.labels(reason="clip_already_persisted").inc()
            return

        start_time = timestamp_to_datetime(event.start_time)
        if start_time is None:
            CLIP_EVENTS_SKIPPED_TOTAL.labels(reason="missing_start_time").inc()
            return

        end_time = timestamp_to_datetime(event.end_time) or start_time
        if end_time < start_time:
            end_time = start_time

        minimum_duration = timedelta(milliseconds=self.settings.min_clip_duration_ms)
        if (end_time - start_time) < minimum_duration:
            end_time = start_time + minimum_duration

        clip_start = start_time - timedelta(seconds=self.settings.pre_roll_s)
        clip_end = end_time + timedelta(seconds=self.settings.post_roll_s)

        frames = await self._minio.list_source_frames(
            camera_id=str(event.camera_id),
            start_time=clip_start,
            end_time=clip_end,
        )
        if not frames:
            CLIP_EVENTS_SKIPPED_TOTAL.labels(reason="no_source_frames").inc()
            return

        site_id = await self._db.get_camera_site_id(str(event.camera_id))
        temp_root = Path(self.settings.temp_dir) / event_id
        frames_dir = temp_root / "frames"
        clip_path = temp_root / f"{event_id}.mp4"
        thumbnail_path = temp_root / f"{event_id}_thumb.jpg"
        started_at = time.perf_counter()
        clip_size = 0

        try:
            frame_paths = await self._minio.download_frames(frames, frames_dir)
            if not frame_paths:
                logger.warning(
                    "No frames available for event %s (all source frames missing) — skipping clip",
                    event_id,
                )
                CLIP_EVENTS_SKIPPED_TOTAL.labels(reason="frames_unavailable").inc()
                return
            await extract_clip(
                frame_paths=frame_paths,
                output_path=clip_path,
                target_bitrate=self.settings.target_bitrate,
                fps=self.settings.target_fps,
            )
            await generate_thumbnail(
                frame_paths=frame_paths,
                output_path=thumbnail_path,
                width=self.settings.thumbnail_width,
                height=self.settings.thumbnail_height,
            )

            asset_date = start_time.date()
            clip_uri = await self._minio.upload_clip(
                local_path=clip_path,
                site_id=site_id,
                camera_id=str(event.camera_id),
                event_id=event_id,
                asset_date=asset_date,
            )
            thumbnail_uri = await self._minio.upload_thumbnail(
                local_path=thumbnail_path,
                site_id=site_id,
                camera_id=str(event.camera_id),
                event_id=event_id,
                asset_date=asset_date,
            )

            await self._db.update_event_assets(
                event_id=event_id,
                clip_uri=clip_uri,
                thumbnail_uri=thumbnail_uri,
            )
            await asyncio.to_thread(self._publish_completion, event, clip_uri)
            clip_size = clip_path.stat().st_size
        except FileNotFoundError:
            CLIP_EXTRACTION_ERRORS_TOTAL.labels(reason="ffmpeg_not_found").inc()
            logger.exception("FFmpeg is not installed or not on PATH")
            return
        except Exception:
            CLIP_EXTRACTION_ERRORS_TOTAL.labels(reason="clip_pipeline_failed").inc()
            logger.exception("Clip extraction failed for event %s", event_id)
            return
        finally:
            await asyncio.to_thread(shutil.rmtree, temp_root, True)

        CLIP_EXTRACTED_TOTAL.inc()
        CLIP_THUMBNAILS_GENERATED_TOTAL.inc()
        CLIP_SIZE_BYTES.observe(clip_size)
        CLIP_EXTRACTION_LATENCY_MS.observe((time.perf_counter() - started_at) * 1000.0)

    def _publish_completion(self, event: Any, clip_uri: str) -> None:
        frame_payload = build_completion_frame_ref(event, clip_uri)
        self._producer.produce(
            self.settings.kafka_output_topic,
            key=str(event.camera_id).encode("utf-8"),
            value=frame_payload,
            headers=[("x-proto-schema", FRAME_PROTO_SCHEMA)],
        )
        pending = self._producer.flush(5.0)
        if pending != 0:
            raise RuntimeError(
                f"Kafka producer flush timed out with {pending} pending completion messages"
            )


def build_completion_frame_ref(event: Any, clip_uri: str) -> bytes:
    """Serialize a FrameRef completion record pointing to the generated clip."""
    frame_pb2 = _load_frame_module()
    message = frame_pb2.FrameRef()
    message.frame_id = str(event.event_id)
    message.camera_id = str(event.camera_id)
    message.frame_uri = clip_uri
    message.frame_sequence = 0
    message.width_px = 0
    message.height_px = 0
    message.codec = "h264-baseline-mp4"
    _copy_video_timestamps(message.timestamps, event.timestamps)
    return cast(bytes, message.SerializeToString())


def timestamp_to_datetime(raw_timestamp: Any) -> datetime | None:
    """Convert a protobuf Timestamp-like object into an aware UTC datetime."""
    seconds = int(getattr(raw_timestamp, "seconds", 0))
    nanos = int(getattr(raw_timestamp, "nanos", 0))
    if seconds == 0 and nanos == 0:
        return None
    return datetime.fromtimestamp(
        seconds + (nanos / 1_000_000_000.0),
        tz=timezone.utc,
    )


def _copy_video_timestamps(target: Any, source: Any) -> None:
    for field_name in ("source_capture_ts", "edge_receive_ts", "core_ingest_ts"):
        source_ts = getattr(source, field_name)
        if int(getattr(source_ts, "seconds", 0)) == 0 and int(getattr(source_ts, "nanos", 0)) == 0:
            continue
        getattr(target, field_name).seconds = int(source_ts.seconds)
        getattr(target, field_name).nanos = int(source_ts.nanos)
    if hasattr(source, "clock_quality"):
        target.clock_quality = int(source.clock_quality)


async def _run() -> None:
    args = parse_args()
    settings = ClipServiceSettings.from_yaml(args.config)
    setup_logging(settings.log_level)

    service = ClipService(settings)
    loop = asyncio.get_running_loop()
    shutdown_requested = False

    async def _shutdown_once() -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        await service.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown_once()))

    try:
        await service.start()
    finally:
        await _shutdown_once()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
