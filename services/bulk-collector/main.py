"""Metadata Bulk Collector service.

Consumes Kafka metadata streams, batches messages in memory, and writes
high-volume rows to TimescaleDB via asyncpg COPY.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import ssl
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from collector import BatchCollector, BufferedMessage, FlushBatch, KafkaOffsetCommit
from config import KafkaTopicBinding, Settings
from metrics import (
    BATCH_SIZE,
    CONSUMER_LAG,
    DUPLICATES_SKIPPED,
    MESSAGES_CONSUMED,
    MESSAGES_REJECTED,
    ROWS_WRITTEN,
    WRITE_ERRORS,
    WRITE_LATENCY,
)
from writer import AsyncpgBulkWriter, DetectionRow, TrackObservationRow

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
FQCN_DETECTION = "vidanalytics.v1.detection.Detection"
OBJECT_CLASS_MAP = {
    1: "person",
    2: "car",
    3: "truck",
    4: "bus",
    5: "bicycle",
    6: "motorcycle",
    7: "animal",
}
CLASS_MIN_CONFIDENCE = {name: 0.40 for name in OBJECT_CLASS_MAP.values()}


class DecodeError(RuntimeError):
    """Raised when a Kafka message cannot be mapped into DB rows."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedDetectionMessage:
    """Rows derived from a single Detection payload."""

    detection_rows: list[DetectionRow]
    track_observation_rows: list[TrackObservationRow]


def parse_args() -> argparse.Namespace:
    """CLI options for standalone service startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="YAML config path.",
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    """Configure stdlib logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def timestamp_to_datetime(ts: Any) -> datetime | None:
    """Convert a protobuf-like Timestamp into an aware UTC datetime."""
    if ts is None:
        return None
    seconds = int(getattr(ts, "seconds", 0))
    nanos = int(getattr(ts, "nanos", 0))
    if seconds == 0 and nanos == 0:
        return None
    return datetime.fromtimestamp(
        seconds + (nanos / 1_000_000_000.0),
        tz=timezone.utc,
    )


def select_message_time(timestamps: Any) -> datetime:
    """Use edge_receive_ts as the primary storage coordinate."""
    if timestamps is None:
        raise DecodeError("missing_timestamps")
    for field_name in ("edge_receive_ts", "core_ingest_ts", "source_capture_ts"):
        value = timestamp_to_datetime(getattr(timestamps, field_name, None))
        if value is not None:
            return value
    raise DecodeError("missing_all_timestamps")


def load_detection_type() -> type[Any]:
    """Import the generated Detection type lazily."""
    try:
        from vidanalytics.v1.detection import detection_pb2  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(
            "generated protobufs not found; run `bash services/bulk-collector/gen_proto.sh`"
        ) from exc
    return detection_pb2.Detection


def normalise_headers(headers: Any) -> dict[str, str]:
    """Return a lower-cased string header mapping."""
    if not headers:
        return {}
    output: dict[str, str] = {}
    for key, value in headers:
        key_text = str(key).lower()
        if isinstance(value, bytes):
            output[key_text] = value.decode("utf-8")
        else:
            output[key_text] = str(value)
    return output


def build_kafka_ssl_context(settings: Settings) -> ssl.SSLContext | None:
    """Create an SSL context for Kafka when cert paths are configured."""
    if not any(
        [
            settings.kafka.ssl_ca_file,
            settings.kafka.ssl_cert_file,
            settings.kafka.ssl_key_file,
        ]
    ):
        return None
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if settings.kafka.ssl_ca_file:
        context.load_verify_locations(settings.kafka.ssl_ca_file)
    if settings.kafka.ssl_cert_file and settings.kafka.ssl_key_file:
        context.load_cert_chain(
            settings.kafka.ssl_cert_file,
            settings.kafka.ssl_key_file,
        )
    return context


def object_class_to_db(value: Any) -> str:
    """Map a protobuf enum value into the canonical DB enum string."""
    if isinstance(value, str):
        normalised = value.lower()
        if normalised.startswith("object_class_"):
            normalised = normalised.removeprefix("object_class_")
        if normalised in CLASS_MIN_CONFIDENCE:
            return normalised
    numeric = int(getattr(value, "value", value))
    if numeric not in OBJECT_CLASS_MAP:
        raise DecodeError("unknown_object_class")
    return OBJECT_CLASS_MAP[numeric]


def bbox_to_xywh(bbox: Any) -> tuple[float, float, float, float]:
    """Convert proto bbox corners into DB x/y/w/h format."""
    x_min = float(getattr(bbox, "x_min", 0.0))
    y_min = float(getattr(bbox, "y_min", 0.0))
    x_max = float(getattr(bbox, "x_max", 0.0))
    y_max = float(getattr(bbox, "y_max", 0.0))
    width = x_max - x_min
    height = y_max - y_min
    if width <= 0 or height <= 0:
        raise DecodeError("invalid_bbox")
    return x_min, y_min, width, height


def parse_frame_seq(headers: dict[str, str]) -> int:
    """Parse the required frame sequence header."""
    raw = headers.get("x-frame-seq")
    if raw is None:
        raise DecodeError("missing_frame_seq_header")
    try:
        value = int(raw)
    except ValueError as exc:
        raise DecodeError("invalid_frame_seq_header") from exc
    if value < 0:
        raise DecodeError("invalid_frame_seq_header")
    return value


def parse_local_track_id(headers: dict[str, str]) -> UUID | None:
    """Parse the optional local track UUID header."""
    raw = headers.get("x-local-track-id")
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise DecodeError("invalid_local_track_id_header") from exc


class ProtoDecoder:
    """Schema-aware protobuf decoder with raw-protobuf fallback."""

    def __init__(self, schema_registry_url: str | None = None) -> None:
        self.schema_registry_url = schema_registry_url
        self._detection_parser: Callable[[bytes, str], Any] | None = None

    def decode_detection(self, payload: bytes, *, topic: str) -> Any:
        if self._detection_parser is None:
            self._detection_parser = self._build_detection_parser() or self._decode_detection_raw
        try:
            return self._detection_parser(payload, topic)
        except Exception as exc:
            if self._detection_parser is not self._decode_detection_raw:
                logger.debug(
                    "Schema Registry decode failed; falling back to raw protobuf parse: %s",
                    exc,
                )
                return self._decode_detection_raw(payload, topic)
            raise DecodeError("protobuf_deserialize_failed") from exc

    def _build_detection_parser(self) -> Callable[[bytes, str], Any] | None:
        if not self.schema_registry_url:
            return None
        try:
            from confluent_kafka.schema_registry import SchemaRegistryClient  # noqa: PLC0415
            from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer  # noqa: PLC0415
            from confluent_kafka.serialization import MessageField, SerializationContext  # noqa: PLC0415
        except ImportError:
            return None

        detection_type = load_detection_type()
        client = SchemaRegistryClient({"url": self.schema_registry_url})
        try:
            deserializer = ProtobufDeserializer(
                detection_type,
                schema_registry_client=client,
            )
        except TypeError:  # pragma: no cover - depends on confluent-kafka version
            deserializer = ProtobufDeserializer(detection_type, {}, client)

        def _decode(payload: bytes, topic: str) -> Any:
            context = SerializationContext(topic, MessageField.VALUE)
            return deserializer(payload, context)

        return _decode

    def _decode_detection_raw(self, payload: bytes, _topic: str) -> Any:
        detection_type = load_detection_type()
        message = detection_type()
        try:
            message.ParseFromString(payload)
        except Exception as exc:  # pragma: no cover - protobuf implementation detail
            raise DecodeError("protobuf_deserialize_failed") from exc
        return message


class BulkCollectorService:
    """Kafka -> BatchCollector -> asyncpg COPY service."""

    def __init__(
        self,
        settings: Settings,
        *,
        writer: AsyncpgBulkWriter | None = None,
        collector: BatchCollector | None = None,
        decoder: ProtoDecoder | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or time.monotonic
        self.collector = collector or BatchCollector(
            batch_size=settings.collector.batch_size,
            max_age_ms=settings.collector.max_age_ms,
            clock=self.clock,
        )
        self.writer = writer or AsyncpgBulkWriter(
            dsn=settings.database.dsn,
            min_pool_size=settings.database.min_pool_size,
            max_pool_size=settings.database.max_pool_size,
            command_timeout_s=settings.database.command_timeout_s,
            dedup_ttl_s=settings.collector.dedup_ttl_s,
            dedup_max_keys=settings.collector.dedup_max_keys,
            clock=self.clock,
        )
        self.decoder = decoder or ProtoDecoder(settings.schema_registry.url)
        self._buffer_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None
        self._consumer_tasks: list[asyncio.Task[None]] = []
        self._consumers: dict[str, Any] = {}
        self._consumer_connected: set[str] = set()

    async def start_background_tasks(self) -> None:
        await self.writer.connect()
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self.flush_loop(), name="bulk-flush")
        if not self._consumer_tasks:
            for binding in self.enabled_bindings():
                task = asyncio.create_task(
                    self.consumer_loop(binding),
                    name=f"consume-{binding.group_id}",
                )
                self._consumer_tasks.append(task)

    async def shutdown(self) -> None:
        self._shutdown.set()
        for task in self._consumer_tasks:
            task.cancel()
        for task in self._consumer_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._consumer_tasks = []
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        self._flush_task = None
        await self.flush(force=True)
        for consumer in list(self._consumers.values()):
            with contextlib.suppress(Exception):
                await consumer.stop()
        self._consumers.clear()
        self._consumer_connected.clear()
        await self.writer.close()

    def enabled_bindings(self) -> list[KafkaTopicBinding]:
        """All configured topic bindings that are enabled."""
        return [binding for binding in self.settings.kafka.topic_bindings if binding.enabled]

    async def is_ready(self) -> bool:
        if not self.enabled_bindings():
            return False
        return await self.writer.is_ready() and set(self._consumer_connected) == {
            binding.group_id for binding in self.enabled_bindings()
        }

    async def flush(self, *, force: bool = False) -> None:
        async with self._buffer_lock:
            batch = self.collector.flush_all() if force else self.collector.flush_due()
            if batch is None:
                return
            await self._flush_batch_locked(batch)

    async def flush_loop(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(self.settings.collector.flush_interval_ms / 1000.0)
            with contextlib.suppress(Exception):
                await self.flush(force=False)

    async def stage_message(self, message: BufferedMessage) -> None:
        async with self._buffer_lock:
            ready = self.collector.add(message)
            if ready is not None:
                await self._flush_batch_locked(ready)

    async def _flush_batch_locked(self, batch: FlushBatch) -> None:
        detection_rows = batch.detection_rows
        track_rows = batch.track_observation_rows
        commit_tokens = [message.commit for message in batch.messages]
        try:
            detection_result = await self.writer.write_detection_rows(detection_rows)
            track_result = await self.writer.write_track_observation_rows(track_rows)
        except Exception:
            self.collector.requeue(batch)
            if detection_rows:
                WRITE_ERRORS.labels(table="detections").inc()
            if track_rows:
                WRITE_ERRORS.labels(table="track_observations").inc()
            raise

        self._apply_write_metrics(detection_result)
        self._apply_write_metrics(track_result)
        await self.commit_offsets(commit_tokens)

    def _apply_write_metrics(self, result: Any) -> None:
        if result.rows_written > 0:
            ROWS_WRITTEN.labels(table=result.table_name).inc(result.rows_written)
            BATCH_SIZE.labels(table=result.table_name).observe(result.rows_written)
            WRITE_LATENCY.labels(table=result.table_name).observe(result.elapsed_ms)
        if result.duplicates_skipped > 0:
            DUPLICATES_SKIPPED.labels(table=result.table_name).inc(result.duplicates_skipped)

    async def commit_offsets(self, commits: list[KafkaOffsetCommit]) -> None:
        if not commits:
            return
        try:
            from aiokafka.structs import OffsetAndMetadata, TopicPartition  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on local env
            raise RuntimeError("missing optional dependency 'aiokafka'; install requirements.txt") from exc

        grouped: dict[str, dict[tuple[str, int], int]] = defaultdict(dict)
        for commit in commits:
            key = (commit.topic, commit.partition)
            grouped[commit.group_id][key] = max(
                grouped[commit.group_id].get(key, -1),
                commit.offset + 1,
            )

        for group_id, offsets in grouped.items():
            consumer = self._consumers.get(group_id)
            if consumer is None:
                continue
            payload = {
                TopicPartition(topic=topic, partition=partition): OffsetAndMetadata(offset, "")
                for (topic, partition), offset in offsets.items()
            }
            await consumer.commit(payload)
            await self.update_consumer_lag(group_id, consumer)

    async def consumer_loop(self, binding: KafkaTopicBinding) -> None:
        delay_s = 1.0
        while not self._shutdown.is_set():
            consumer = None
            try:
                consumer = await self._create_consumer(binding)
                self._consumers[binding.group_id] = consumer
                self._consumer_connected.add(binding.group_id)
                delay_s = 1.0
                while not self._shutdown.is_set():
                    batches = await consumer.getmany(
                        timeout_ms=self.settings.kafka.poll_timeout_ms,
                        max_records=self.settings.kafka.max_poll_records,
                    )
                    if not batches:
                        await self.update_consumer_lag(binding.group_id, consumer)
                        continue
                    for partition, messages in batches.items():
                        for message in messages:
                            commit_token = KafkaOffsetCommit(
                                group_id=binding.group_id,
                                topic=message.topic,
                                partition=message.partition,
                                offset=message.offset,
                            )
                            try:
                                buffered = self.parse_message(binding, message, commit_token)
                            except DecodeError as exc:
                                MESSAGES_REJECTED.labels(topic=message.topic, reason=exc.reason).inc()
                                await self.commit_offsets([commit_token])
                                continue
                            await self.stage_message(buffered)
                            await self.update_partition_lag(
                                group_id=binding.group_id,
                                topic=partition.topic,
                                partition=partition.partition,
                                consumer=consumer,
                            )
            except Exception as exc:
                logger.warning(
                    "Kafka consumer loop failed for group=%s: %s",
                    binding.group_id,
                    exc,
                    exc_info=True,
                )
                self._consumer_connected.discard(binding.group_id)
                await asyncio.sleep(delay_s)
                delay_s = min(delay_s * 2.0, 30.0)
            finally:
                self._consumers.pop(binding.group_id, None)
                if consumer is not None:
                    with contextlib.suppress(Exception):
                        await consumer.stop()

    def parse_message(
        self,
        binding: KafkaTopicBinding,
        message: Any,
        commit_token: KafkaOffsetCommit,
    ) -> BufferedMessage:
        headers = normalise_headers(getattr(message, "headers", None))
        schema_name = headers.get("x-proto-schema") or binding.expected_schema or ""
        if schema_name != FQCN_DETECTION:
            raise DecodeError("unsupported_schema")
        parsed = self.parse_detection_message(message.topic, message.value, headers)
        MESSAGES_CONSUMED.labels(topic=message.topic, schema=schema_name).inc()
        return BufferedMessage(
            commit=commit_token,
            detection_rows=parsed.detection_rows,
            track_observation_rows=parsed.track_observation_rows,
        )

    def parse_detection_message(
        self,
        topic: str,
        payload: bytes,
        headers: dict[str, str],
    ) -> ParsedDetectionMessage:
        detection = self.decoder.decode_detection(payload, topic=topic)
        camera_id = str(getattr(detection, "camera_id", "")).strip()
        if not camera_id:
            raise DecodeError("missing_camera_id")
        frame_seq = parse_frame_seq(headers)
        local_track_id = parse_local_track_id(headers)
        bbox = getattr(detection, "bbox", None)
        if bbox is None:
            raise DecodeError("missing_bbox")
        bbox_x, bbox_y, bbox_w, bbox_h = bbox_to_xywh(bbox)
        object_class = object_class_to_db(getattr(detection, "object_class", 0))
        confidence = float(getattr(detection, "confidence", 0.0))
        if confidence < CLASS_MIN_CONFIDENCE[object_class]:
            raise DecodeError("below_threshold")
        message_time = select_message_time(getattr(detection, "timestamps", None))
        model_version = str(getattr(detection, "model_version", "")).strip()
        if not model_version:
            raise DecodeError("missing_model_version")

        detection_row = DetectionRow(
            time=message_time,
            camera_id=camera_id,
            frame_seq=frame_seq,
            object_class=object_class,
            confidence=confidence,
            bbox_x=bbox_x,
            bbox_y=bbox_y,
            bbox_w=bbox_w,
            bbox_h=bbox_h,
            local_track_id=local_track_id,
            model_version=model_version,
        )

        track_rows: list[TrackObservationRow] = []
        if local_track_id is not None:
            track_rows.append(
                TrackObservationRow(
                    time=message_time,
                    camera_id=camera_id,
                    frame_seq=frame_seq,
                    local_track_id=local_track_id,
                    centroid_x=bbox_x + (bbox_w / 2.0),
                    centroid_y=bbox_y + (bbox_h / 2.0),
                    bbox_area=bbox_w * bbox_h,
                    embedding_ref=headers.get("x-embedding-ref"),
                )
            )

        return ParsedDetectionMessage(
            detection_rows=[detection_row],
            track_observation_rows=track_rows,
        )

    async def _create_consumer(self, binding: KafkaTopicBinding) -> Any:
        try:
            from aiokafka import AIOKafkaConsumer  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on local env
            raise RuntimeError("missing optional dependency 'aiokafka'; install requirements.txt") from exc
        consumer = AIOKafkaConsumer(
            binding.topic,
            bootstrap_servers=self.settings.kafka.bootstrap_servers,
            group_id=binding.group_id,
            client_id=f"{self.settings.kafka.client_id}-{binding.group_id}",
            security_protocol=self.settings.kafka.security_protocol,
            sasl_mechanism=self.settings.kafka.sasl_mechanism,
            sasl_plain_username=self.settings.kafka.sasl_username,
            sasl_plain_password=self.settings.kafka.sasl_password,
            ssl_context=build_kafka_ssl_context(self.settings),
            enable_auto_commit=False,
            auto_offset_reset=self.settings.kafka.auto_offset_reset,
        )
        await consumer.start()
        return consumer

    async def update_consumer_lag(self, group_id: str, consumer: Any) -> None:
        partitions = list(consumer.assignment())
        if not partitions:
            return
        end_offsets = await consumer.end_offsets(partitions)
        for partition in partitions:
            position = await consumer.position(partition)
            lag = max(int(end_offsets.get(partition, 0)) - int(position), 0)
            CONSUMER_LAG.labels(
                group=group_id,
                topic=partition.topic,
                partition=str(partition.partition),
            ).set(lag)

    async def update_partition_lag(
        self,
        *,
        group_id: str,
        topic: str,
        partition: int,
        consumer: Any,
    ) -> None:
        try:
            from aiokafka.structs import TopicPartition  # noqa: PLC0415
        except ImportError:
            return
        tp = TopicPartition(topic=topic, partition=partition)
        end_offsets = await consumer.end_offsets([tp])
        position = await consumer.position(tp)
        lag = max(int(end_offsets.get(tp, 0)) - int(position), 0)
        CONSUMER_LAG.labels(
            group=group_id,
            topic=topic,
            partition=str(partition),
        ).set(lag)


def create_app(service: BulkCollectorService) -> Any:
    """Build the FastAPI app lazily so tests don't require FastAPI."""
    try:
        from fastapi import FastAPI, Response  # noqa: PLC0415
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError("missing optional dependency 'fastapi'; install requirements.txt") from exc

    app = FastAPI(title="Metadata Bulk Collector", version="1.0.0")

    @app.on_event("startup")
    async def _startup() -> None:
        await service.start_background_tasks()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await service.shutdown()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> Response:
        if await service.is_ready():
            return Response(
                content=json.dumps({"status": "ready"}),
                media_type="application/json",
                status_code=200,
            )
        return Response(
            content=json.dumps({"status": "not-ready"}),
            media_type="application/json",
            status_code=503,
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


async def run_http(service: BulkCollectorService, settings: Settings) -> None:
    """Run the FastAPI app with uvicorn."""
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError("missing optional dependency 'uvicorn'; install requirements.txt") from exc
    app = create_app(service)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.health_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


async def async_main() -> None:
    """Entrypoint used by __main__."""
    args = parse_args()
    settings = Settings.from_yaml(args.config)
    setup_logging(settings.log_level)
    service = BulkCollectorService(settings)
    await run_http(service, settings)


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc
