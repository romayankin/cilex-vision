"""Tests for the event-engine track state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from state_machine import (
    CameraZones,
    EventOperation,
    EventRecordState,
    EventType,
    LoiteringZone,
    TrackStateMachine,
)


@dataclass
class FakeTimestamps:
    source_capture_ts: datetime
    edge_receive_ts: datetime
    core_ingest_ts: datetime
    clock_quality: int = 2


@dataclass
class FakeTrajectoryPoint:
    centroid_x: float
    centroid_y: float
    frame_ts: datetime


@dataclass
class FakeTracklet:
    state: int
    trajectory: list[FakeTrajectoryPoint]
    timestamps: FakeTimestamps
    track_id: str = "track-1"
    camera_id: str = "cam-1"


@pytest.fixture
def full_frame_zone() -> CameraZones:
    polygon = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    return CameraZones(roi_polygon=polygon)


def _tracklet(
    when: datetime,
    centroid_x: float,
    centroid_y: float,
    state: int = 2,
) -> FakeTracklet:
    timestamps = FakeTimestamps(
        source_capture_ts=when,
        edge_receive_ts=when + timedelta(milliseconds=25),
        core_ingest_ts=when + timedelta(milliseconds=50),
    )
    return FakeTracklet(
        state=state,
        trajectory=[
            FakeTrajectoryPoint(
                centroid_x=centroid_x,
                centroid_y=centroid_y,
                frame_ts=when,
            )
        ],
        timestamps=timestamps,
    )


def test_track_appears_emits_entered_scene(full_frame_zone: CameraZones) -> None:
    machine = TrackStateMachine(
        track_id="track-1",
        camera_id="cam-1",
        object_class="car",
        camera_zones=full_frame_zone,
        stopped_threshold=0.005,
        stopped_duration_s=3.0,
        stopped_resume_threshold=0.01,
        stopped_resume_duration_s=1.0,
    )

    now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    triggers = machine.update(_tracklet(now, 0.2, 0.4))

    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.event_type is EventType.ENTERED_SCENE
    assert trigger.operation is EventOperation.INSERT
    assert trigger.state is EventRecordState.CLOSED
    assert trigger.end_time == trigger.start_time
    assert trigger.duration_ms == 0


def test_track_with_movement_does_not_emit_stopped(full_frame_zone: CameraZones) -> None:
    machine = TrackStateMachine(
        track_id="track-1",
        camera_id="cam-1",
        object_class="car",
        camera_zones=full_frame_zone,
        stopped_threshold=0.005,
        stopped_duration_s=3.0,
        stopped_resume_threshold=0.01,
        stopped_resume_duration_s=1.0,
    )

    start = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    machine.update(_tracklet(start, 0.1, 0.1))
    triggers = machine.update(_tracklet(start + timedelta(seconds=1), 0.4, 0.4))

    assert triggers == []
    assert machine.check_timers((start + timedelta(seconds=5)).timestamp()) == []


def test_track_stops_for_three_seconds_emits_stopped(
    full_frame_zone: CameraZones,
) -> None:
    machine = TrackStateMachine(
        track_id="track-1",
        camera_id="cam-1",
        object_class="car",
        camera_zones=full_frame_zone,
        stopped_threshold=0.005,
        stopped_duration_s=3.0,
        stopped_resume_threshold=0.01,
        stopped_resume_duration_s=1.0,
    )

    start = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    machine.update(_tracklet(start, 0.2, 0.2))
    machine.update(_tracklet(start + timedelta(seconds=1), 0.2, 0.2))

    triggers = machine.check_timers((start + timedelta(seconds=4, milliseconds=100)).timestamp())

    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.event_type is EventType.STOPPED
    assert trigger.operation is EventOperation.INSERT
    assert trigger.state is EventRecordState.ACTIVE
    assert trigger.end_time is None
    assert trigger.start_time == start + timedelta(seconds=1)


def test_person_in_zone_for_thirty_seconds_emits_loitering() -> None:
    polygon = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    camera_zones = CameraZones(
        roi_polygon=polygon,
        loitering_zones=(LoiteringZone("zone-a", polygon, 30.0),),
    )
    machine = TrackStateMachine(
        track_id="track-1",
        camera_id="cam-1",
        object_class="person",
        camera_zones=camera_zones,
        stopped_threshold=0.005,
        stopped_duration_s=3.0,
        stopped_resume_threshold=0.01,
        stopped_resume_duration_s=1.0,
    )

    start = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    triggers = machine.update(_tracklet(start, 0.3, 0.3))
    assert triggers[0].event_type is EventType.ENTERED_SCENE

    loitering = machine.check_timers((start + timedelta(seconds=30, milliseconds=100)).timestamp())

    assert len(loitering) == 1
    trigger = loitering[0]
    assert trigger.event_type is EventType.LOITERING
    assert trigger.state is EventRecordState.ACTIVE
    assert trigger.metadata == {"zone_id": "zone-a"}


def test_stopped_track_resumes_and_closes_event(full_frame_zone: CameraZones) -> None:
    machine = TrackStateMachine(
        track_id="track-1",
        camera_id="cam-1",
        object_class="car",
        camera_zones=full_frame_zone,
        stopped_threshold=0.005,
        stopped_duration_s=3.0,
        stopped_resume_threshold=0.01,
        stopped_resume_duration_s=1.0,
    )

    start = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    machine.update(_tracklet(start, 0.2, 0.2))
    machine.update(_tracklet(start + timedelta(seconds=1), 0.2, 0.2))
    opened = machine.check_timers((start + timedelta(seconds=4, milliseconds=100)).timestamp())
    assert opened[0].event_type is EventType.STOPPED

    moving_update = machine.update(_tracklet(start + timedelta(seconds=5), 0.5, 0.5))
    assert moving_update == []

    closed = machine.check_timers((start + timedelta(seconds=6, milliseconds=200)).timestamp())

    assert len(closed) == 1
    trigger = closed[0]
    assert trigger.event_type is EventType.STOPPED
    assert trigger.operation is EventOperation.UPDATE
    assert trigger.state is EventRecordState.CLOSED
    assert trigger.end_time == start + timedelta(seconds=6, milliseconds=200)
    assert trigger.duration_ms == 5200


def test_track_terminated_emits_exited_scene_and_closes_open_events(
    full_frame_zone: CameraZones,
) -> None:
    machine = TrackStateMachine(
        track_id="track-1",
        camera_id="cam-1",
        object_class="car",
        camera_zones=full_frame_zone,
        stopped_threshold=0.005,
        stopped_duration_s=3.0,
        stopped_resume_threshold=0.01,
        stopped_resume_duration_s=1.0,
    )

    start = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    machine.update(_tracklet(start, 0.2, 0.2))
    machine.update(_tracklet(start + timedelta(seconds=1), 0.2, 0.2))
    machine.check_timers((start + timedelta(seconds=4, milliseconds=100)).timestamp())

    terminated = _tracklet(
        start + timedelta(seconds=5),
        0.2,
        0.2,
        state=4,
    )
    triggers = machine.update(terminated)
    event_types = {trigger.event_type for trigger in triggers}

    assert EventType.STOPPED in event_types
    assert EventType.EXITED_SCENE in event_types
    stopped_close = next(
        trigger for trigger in triggers if trigger.event_type is EventType.STOPPED
    )
    assert stopped_close.operation is EventOperation.UPDATE
    assert stopped_close.state is EventRecordState.CLOSED


@pytest.mark.parametrize(
    ("object_class", "expect_stopped"),
    [
        ("car", True),
        ("person", False),
        ("bicycle", True),
    ],
)
def test_stopped_applies_only_to_vehicle_classes(
    full_frame_zone: CameraZones,
    object_class: str,
    expect_stopped: bool,
) -> None:
    machine = TrackStateMachine(
        track_id="track-1",
        camera_id="cam-1",
        object_class=object_class,
        camera_zones=full_frame_zone,
        stopped_threshold=0.005,
        stopped_duration_s=3.0,
        stopped_resume_threshold=0.01,
        stopped_resume_duration_s=1.0,
    )

    start = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    machine.update(_tracklet(start, 0.25, 0.25))
    machine.update(_tracklet(start + timedelta(seconds=1), 0.25, 0.25))
    triggers = machine.check_timers((start + timedelta(seconds=4)).timestamp())

    if expect_stopped:
        assert len(triggers) == 1
        assert triggers[0].event_type is EventType.STOPPED
    else:
        assert triggers == []
