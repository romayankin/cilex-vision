"""Event protobuf construction, Kafka publishing, and PostgreSQL writes."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import asyncpg

from metrics import EVENT_DB_WRITE_LATENCY_MS, EVENT_EMITTED_TOTAL
from state_machine import EventOperation, EventRecordState, EventTrigger, EventType

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))

EVENT_TYPE_PROTO_NAMES = {
    EventType.ENTERED_SCENE: "EVENT_TYPE_ENTERED_SCENE",
    EventType.EXITED_SCENE: "EVENT_TYPE_EXITED_SCENE",
    EventType.STOPPED: "EVENT_TYPE_STOPPED",
    EventType.LOITERING: "EVENT_TYPE_LOITERING",
    EventType.MOTION_STARTED: "EVENT_TYPE_MOTION_STARTED",
    EventType.MOTION_ENDED: "EVENT_TYPE_MOTION_ENDED",
}

EVENT_STATE_PROTO_NAMES = {
    EventRecordState.NEW: "EVENT_STATE_NEW",
    EventRecordState.ACTIVE: "EVENT_STATE_ACTIVE",
    EventRecordState.STOPPED: "EVENT_STATE_STOPPED_SUBSTATE",
    EventRecordState.EXITED: "EVENT_STATE_EXITED",
    EventRecordState.CLOSED: "EVENT_STATE_CLOSED",
}


class ProducerProtocol(Protocol):
    """Small protocol for the synchronous confluent-kafka producer API."""

    def produce(
        self,
        topic: str,
        key: bytes,
        value: bytes,
        headers: list[tuple[str, bytes]],
    ) -> None:
        """Queue a Kafka message for delivery."""

    def flush(self, timeout: float | None = None) -> int:
        """Block until queued messages are delivered or timed out."""


def _load_event_module() -> Any:
    try:
        from vidanalytics.v1.event import event_pb2  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "generated protobufs not found; run `bash gen_proto.sh`"
        ) from exc
    return event_pb2


def build_event_message(trigger: EventTrigger, event_pb2: Any | None = None) -> Any:
    """Build an Event protobuf message from an EventTrigger."""
    module = event_pb2 or _load_event_module()
    message = module.Event()

    message.event_id = trigger.event_id
    message.event_type = getattr(module, EVENT_TYPE_PROTO_NAMES[trigger.event_type])
    if trigger.track_id is not None:
        message.track_id = trigger.track_id
    message.camera_id = trigger.camera_id
    _set_timestamp(message.start_time, trigger.start_time)

    if trigger.end_time is not None:
        _set_timestamp(message.end_time, trigger.end_time)
    if trigger.duration_ms is not None:
        message.duration_ms = trigger.duration_ms
    if trigger.clip_uri is not None:
        message.clip_uri = trigger.clip_uri

    message.state = getattr(module, EVENT_STATE_PROTO_NAMES[trigger.state])
    message.track_ids.extend(trigger.track_ids)
    _set_video_timestamps(message.timestamps, trigger)
    return message


class EventEmitter:
    """Persist events and publish them to Kafka."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        producer: ProducerProtocol,
        output_topic: str,
    ) -> None:
        self._pool = pool
        self._producer = producer
        self._output_topic = output_topic

    async def emit(self, trigger: EventTrigger) -> None:
        """Write the event update to PostgreSQL and publish to Kafka."""
        message = build_event_message(trigger)
        payload = message.SerializeToString()

        write_started = time.perf_counter()
        if trigger.operation == EventOperation.INSERT:
            await self._insert_event(trigger)
        else:
            await self._update_event(trigger)
        EVENT_DB_WRITE_LATENCY_MS.observe(
            (time.perf_counter() - write_started) * 1000.0
        )

        await asyncio.to_thread(self._publish, trigger.event_id, payload)
        EVENT_EMITTED_TOTAL.labels(event_type=trigger.event_type.value).inc()

    async def emit_many(self, triggers: list[EventTrigger]) -> None:
        """Emit a batch of events sequentially to preserve causal ordering."""
        for trigger in triggers:
            await self.emit(trigger)

    async def flush(self) -> None:
        """Flush any producer buffers during shutdown."""
        await asyncio.to_thread(self._producer.flush, 5.0)

    async def _insert_event(self, trigger: EventTrigger) -> None:
        metadata = json.dumps(trigger.metadata) if trigger.metadata is not None else None
        track_id = uuid.UUID(trigger.track_id) if trigger.track_id is not None else None

        sql = """
            INSERT INTO events (
                event_id,
                event_type,
                track_id,
                camera_id,
                start_time,
                end_time,
                duration_ms,
                clip_uri,
                state,
                metadata_jsonb,
                source_capture_ts,
                edge_receive_ts,
                core_ingest_ts
            ) VALUES (
                $1::uuid,
                $2,
                $3::uuid,
                $4,
                $5,
                $6,
                $7,
                $8,
                $9,
                $10::jsonb,
                $11,
                $12,
                $13
            )
        """

        async with self._pool.acquire() as conn:
            await conn.execute(
                sql,
                uuid.UUID(trigger.event_id),
                trigger.event_type.value,
                track_id,
                trigger.camera_id,
                trigger.start_time,
                trigger.end_time,
                trigger.duration_ms,
                trigger.clip_uri,
                trigger.state.value,
                metadata,
                trigger.timestamps.source_capture_ts,
                trigger.timestamps.edge_receive_ts,
                trigger.timestamps.core_ingest_ts,
            )

    async def _update_event(self, trigger: EventTrigger) -> None:
        metadata = json.dumps(trigger.metadata) if trigger.metadata is not None else None

        sql = """
            UPDATE events
            SET end_time = $2,
                duration_ms = $3,
                clip_uri = $4,
                state = $5,
                metadata_jsonb = $6::jsonb,
                source_capture_ts = $7,
                edge_receive_ts = $8,
                core_ingest_ts = $9
            WHERE event_id = $1::uuid
        """

        async with self._pool.acquire() as conn:
            await conn.execute(
                sql,
                uuid.UUID(trigger.event_id),
                trigger.end_time,
                trigger.duration_ms,
                trigger.clip_uri,
                trigger.state.value,
                metadata,
                trigger.timestamps.source_capture_ts,
                trigger.timestamps.edge_receive_ts,
                trigger.timestamps.core_ingest_ts,
            )

    def _publish(self, event_id: str, payload: bytes) -> None:
        headers = [("x-proto-schema", b"vidanalytics.v1.event.Event")]
        self._producer.produce(
            self._output_topic,
            key=event_id.encode("utf-8"),
            value=payload,
            headers=headers,
        )
        pending = self._producer.flush(5.0)
        if pending != 0:
            raise RuntimeError(f"Kafka producer flush timed out with {pending} messages")


def _set_timestamp(field: Any, value: datetime) -> None:
    if value.tzinfo is None:
        field.FromDatetime(value.replace(tzinfo=timezone.utc))
        return
    field.FromDatetime(value.astimezone(timezone.utc))


def _set_video_timestamps(field: Any, trigger: EventTrigger) -> None:
    timestamps = trigger.timestamps
    if timestamps.source_capture_ts is not None:
        _set_timestamp(field.source_capture_ts, timestamps.source_capture_ts)
    if timestamps.edge_receive_ts is not None:
        _set_timestamp(field.edge_receive_ts, timestamps.edge_receive_ts)
    if timestamps.core_ingest_ts is not None:
        _set_timestamp(field.core_ingest_ts, timestamps.core_ingest_ts)
    if timestamps.clock_quality is not None:
        field.clock_quality = timestamps.clock_quality
