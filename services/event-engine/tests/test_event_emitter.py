"""Tests for event protobuf construction and event persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from event_emitter import EventEmitter, build_event_message
from state_machine import (
    EventOperation,
    EventRecordState,
    EventTimestamps,
    EventTrigger,
    EventType,
    make_point_event,
)


class FakeProtoTimestamp:
    def __init__(self) -> None:
        self.value: datetime | None = None

    def FromDatetime(self, value: datetime) -> None:
        self.value = value


class FakeVideoTimestamp:
    def __init__(self) -> None:
        self.source_capture_ts = FakeProtoTimestamp()
        self.edge_receive_ts = FakeProtoTimestamp()
        self.core_ingest_ts = FakeProtoTimestamp()
        self.clock_quality = 0


class FakeEventMessage:
    def __init__(self) -> None:
        self.event_id = ""
        self.event_type = 0
        self.track_id = ""
        self.camera_id = ""
        self.start_time = FakeProtoTimestamp()
        self.end_time = FakeProtoTimestamp()
        self.duration_ms = 0
        self.clip_uri = ""
        self.state = 0
        self.track_ids: list[str] = []
        self.timestamps = FakeVideoTimestamp()

    def SerializeToString(self) -> bytes:
        return b"fake-event"


class FakeEventModule:
    Event = FakeEventMessage
    EVENT_TYPE_ENTERED_SCENE = 1
    EVENT_TYPE_EXITED_SCENE = 2
    EVENT_TYPE_STOPPED = 3
    EVENT_TYPE_LOITERING = 4
    EVENT_TYPE_MOTION_STARTED = 5
    EVENT_TYPE_MOTION_ENDED = 6
    EVENT_STATE_NEW = 1
    EVENT_STATE_ACTIVE = 2
    EVENT_STATE_STOPPED_SUBSTATE = 3
    EVENT_STATE_EXITED = 4
    EVENT_STATE_CLOSED = 5


class FakeProducer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def produce(
        self,
        topic: str,
        key: bytes,
        value: bytes,
        headers: list[tuple[str, bytes]],
    ) -> None:
        self.calls.append(
            {
                "topic": topic,
                "key": key,
                "value": value,
                "headers": headers,
            }
        )

    def flush(self, timeout: float | None = None) -> int:
        return 0


class FakeConnection:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "OK"


class FakeAcquireContext:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, *args: Any) -> bool:
        return False


class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._connection = FakeConnection(self.calls)

    def acquire(self) -> FakeAcquireContext:
        return FakeAcquireContext(self._connection)


@pytest.fixture
def timestamps() -> EventTimestamps:
    now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    return EventTimestamps(
        source_capture_ts=now,
        edge_receive_ts=now + timedelta(milliseconds=20),
        core_ingest_ts=now + timedelta(milliseconds=40),
        clock_quality=2,
    )


def test_point_in_time_event_has_closed_fields(timestamps: EventTimestamps) -> None:
    now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    trigger = make_point_event(
        event_type=EventType.ENTERED_SCENE,
        camera_id="cam-1",
        event_time=now,
        timestamps=timestamps,
        track_id="track-1",
    )

    message = build_event_message(trigger, event_pb2=FakeEventModule)

    assert message.end_time.value == message.start_time.value
    assert message.duration_ms == 0
    assert message.state == FakeEventModule.EVENT_STATE_CLOSED


def test_duration_event_initially_active_with_null_end_time(
    timestamps: EventTimestamps,
) -> None:
    now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    trigger = EventTrigger(
        event_id="11111111-1111-1111-1111-111111111111",
        event_type=EventType.STOPPED,
        camera_id="cam-1",
        track_id="track-1",
        track_ids=("track-1",),
        start_time=now,
        end_time=None,
        duration_ms=None,
        state=EventRecordState.ACTIVE,
        operation=EventOperation.INSERT,
        metadata={"centroid_x": 0.2, "centroid_y": 0.3},
        timestamps=timestamps,
    )

    message = build_event_message(trigger, event_pb2=FakeEventModule)

    assert message.end_time.value is None
    assert message.duration_ms == 0
    assert message.state == FakeEventModule.EVENT_STATE_ACTIVE


def test_duration_event_close_sets_end_time_duration_and_state(
    timestamps: EventTimestamps,
) -> None:
    start = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=5)
    trigger = EventTrigger(
        event_id="11111111-1111-1111-1111-111111111111",
        event_type=EventType.STOPPED,
        camera_id="cam-1",
        track_id="track-1",
        track_ids=("track-1",),
        start_time=start,
        end_time=end,
        duration_ms=5000,
        state=EventRecordState.CLOSED,
        operation=EventOperation.UPDATE,
        metadata={"centroid_x": 0.2, "centroid_y": 0.3},
        timestamps=timestamps,
    )

    message = build_event_message(trigger, event_pb2=FakeEventModule)

    assert message.end_time.value == end
    assert message.duration_ms == 5000
    assert message.state == FakeEventModule.EVENT_STATE_CLOSED


def test_event_proto_maps_fields_correctly(timestamps: EventTimestamps) -> None:
    now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    trigger = EventTrigger(
        event_id="11111111-1111-1111-1111-111111111111",
        event_type=EventType.LOITERING,
        camera_id="cam-9",
        track_id="track-9",
        track_ids=("track-9",),
        start_time=now,
        end_time=None,
        duration_ms=None,
        state=EventRecordState.ACTIVE,
        operation=EventOperation.INSERT,
        metadata={"zone_id": "zone-a"},
        timestamps=timestamps,
    )

    message = build_event_message(trigger, event_pb2=FakeEventModule)

    assert message.event_id == trigger.event_id
    assert message.event_type == FakeEventModule.EVENT_TYPE_LOITERING
    assert message.track_id == "track-9"
    assert message.camera_id == "cam-9"
    assert message.track_ids == ["track-9"]
    assert message.timestamps.source_capture_ts.value == timestamps.source_capture_ts
    assert message.timestamps.edge_receive_ts.value == timestamps.edge_receive_ts
    assert message.timestamps.core_ingest_ts.value == timestamps.core_ingest_ts
    assert message.timestamps.clock_quality == 2


@pytest.mark.asyncio
async def test_db_write_parameters_match_events_table_columns(
    monkeypatch: pytest.MonkeyPatch,
    timestamps: EventTimestamps,
) -> None:
    fake_pool = FakePool()
    fake_producer = FakeProducer()
    emitter = EventEmitter(fake_pool, fake_producer, "events.raw")

    monkeypatch.setattr("event_emitter._load_event_module", lambda: FakeEventModule)

    start = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    open_trigger = EventTrigger(
        event_id="11111111-1111-1111-1111-111111111111",
        event_type=EventType.STOPPED,
        camera_id="cam-1",
        track_id="22222222-2222-2222-2222-222222222222",
        track_ids=("22222222-2222-2222-2222-222222222222",),
        start_time=start,
        end_time=None,
        duration_ms=None,
        state=EventRecordState.ACTIVE,
        operation=EventOperation.INSERT,
        metadata={"centroid_x": 0.2, "centroid_y": 0.3},
        timestamps=timestamps,
    )
    close_trigger = EventTrigger(
        event_id=open_trigger.event_id,
        event_type=EventType.STOPPED,
        camera_id="cam-1",
        track_id=open_trigger.track_id,
        track_ids=open_trigger.track_ids,
        start_time=start,
        end_time=start + timedelta(seconds=3),
        duration_ms=3000,
        state=EventRecordState.CLOSED,
        operation=EventOperation.UPDATE,
        metadata={"centroid_x": 0.2, "centroid_y": 0.3},
        timestamps=timestamps,
    )

    await emitter.emit(open_trigger)
    await emitter.emit(close_trigger)

    assert len(fake_pool.calls) == 2
    insert_sql, insert_args = fake_pool.calls[0]
    update_sql, update_args = fake_pool.calls[1]

    assert "INSERT INTO events" in insert_sql
    assert insert_args[1] == "stopped"
    assert insert_args[3] == "cam-1"
    assert insert_args[4] == start
    assert insert_args[8] == "active"
    assert insert_args[9] == '{"centroid_x": 0.2, "centroid_y": 0.3}'

    assert "UPDATE events" in update_sql
    assert update_args[1] == start + timedelta(seconds=3)
    assert update_args[2] == 3000
    assert update_args[4] == "closed"
    assert update_args[5] == '{"centroid_x": 0.2, "centroid_y": 0.3}'

    assert len(fake_producer.calls) == 2
    assert fake_producer.calls[0]["topic"] == "events.raw"
    assert fake_producer.calls[0]["key"] == open_trigger.event_id.encode("utf-8")
    assert fake_producer.calls[0]["headers"] == [
        ("x-proto-schema", b"vidanalytics.v1.event.Event")
    ]
