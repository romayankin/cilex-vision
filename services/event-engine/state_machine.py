"""Per-track finite state machine for event detection."""

from __future__ import annotations

import enum
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle", "bicycle"}

Point = tuple[float, float]
Polygon = tuple[Point, ...]


class TrackState(str, enum.Enum):
    """High-level per-track motion state."""

    MOVING = "moving"
    STOPPED = "stopped"
    LOITERING = "loitering"


class EventType(str, enum.Enum):
    """Taxonomy event types stored in PostgreSQL."""

    ENTERED_SCENE = "entered_scene"
    EXITED_SCENE = "exited_scene"
    STOPPED = "stopped"
    LOITERING = "loitering"
    MOTION_STARTED = "motion_started"
    MOTION_ENDED = "motion_ended"


class EventRecordState(str, enum.Enum):
    """Lifecycle state stored in PostgreSQL and mapped to the proto enum."""

    NEW = "new"
    ACTIVE = "active"
    STOPPED = "stopped"
    EXITED = "exited"
    CLOSED = "closed"


class EventOperation(str, enum.Enum):
    """Persistence operation required for the emitted event."""

    INSERT = "insert"
    UPDATE = "update"


@dataclass(frozen=True)
class EventTimestamps:
    """Canonical timestamp triple carried through the pipeline."""

    source_capture_ts: datetime | None = None
    edge_receive_ts: datetime | None = None
    core_ingest_ts: datetime | None = None
    clock_quality: int | None = None


@dataclass(frozen=True)
class EventTrigger:
    """A concrete event emission or event update."""

    event_id: str
    event_type: EventType
    camera_id: str
    track_id: str | None
    track_ids: tuple[str, ...]
    start_time: datetime
    end_time: datetime | None
    duration_ms: int | None
    state: EventRecordState
    operation: EventOperation
    clip_uri: str | None = None
    metadata: dict[str, Any] | None = None
    timestamps: EventTimestamps = field(default_factory=EventTimestamps)


@dataclass(frozen=True)
class LoiteringZone:
    """Loitering zone configuration for a camera."""

    zone_id: str
    polygon: Polygon
    duration_s: float


@dataclass(frozen=True)
class CameraZones:
    """Parsed ROI and loitering-zone configuration for a camera."""

    roi_polygon: Polygon | None = None
    loitering_zones: tuple[LoiteringZone, ...] = ()

    def in_roi(self, point: Point) -> bool:
        """Return True when the point is inside the ROI or ROI is unspecified."""
        if self.roi_polygon is None:
            return True
        return point_in_polygon(point, self.roi_polygon)

    def zones_containing(self, point: Point) -> tuple[LoiteringZone, ...]:
        """Return loitering zones containing the point."""
        return tuple(
            zone for zone in self.loitering_zones
            if point_in_polygon(point, zone.polygon)
        )

    @classmethod
    def from_camera_config(
        cls,
        raw_config: Any,
        default_loitering_duration_s: float,
    ) -> CameraZones:
        """Build camera zone config from a flexible JSONB payload.

        The repo defines *where* these settings live (`cameras.config_json`) but
        not a single canonical shape yet, so this parser accepts a few common
        polygon forms:

        - `{"roi": [[x, y], ...]}`
        - `{"roi_polygon": [{"x": 0.1, "y": 0.2}, ...]}`
        - `{"loitering_zones": [{"zone_id": "z1", "polygon": ...}]}`
        """
        config = _coerce_mapping(raw_config)
        if config is None:
            return cls()

        roi_polygon = None
        for key in ("roi", "roi_polygon", "roi_points"):
            roi_polygon = _normalise_polygon(config.get(key))
            if roi_polygon is not None:
                break

        zone_entries = config.get("loitering_zones")
        loitering_zones: list[LoiteringZone] = []
        if isinstance(zone_entries, dict):
            iterable = [
                {"zone_id": zone_id, **zone_cfg}
                for zone_id, zone_cfg in zone_entries.items()
                if isinstance(zone_cfg, dict)
            ]
        elif isinstance(zone_entries, list):
            iterable = zone_entries
        else:
            iterable = []

        for index, raw_zone in enumerate(iterable):
            if not isinstance(raw_zone, dict):
                continue
            polygon = _normalise_polygon(raw_zone)
            if polygon is None:
                continue
            zone_id = str(raw_zone.get("zone_id") or raw_zone.get("id") or f"zone-{index}")
            duration_s = float(
                raw_zone.get("duration_s")
                or raw_zone.get("loitering_duration_s")
                or default_loitering_duration_s
            )
            loitering_zones.append(
                LoiteringZone(
                    zone_id=zone_id,
                    polygon=polygon,
                    duration_s=duration_s,
                )
            )

        return cls(
            roi_polygon=roi_polygon,
            loitering_zones=tuple(loitering_zones),
        )


@dataclass
class ActiveEvent:
    """Open duration event tracked in memory until close."""

    event_id: str
    event_type: EventType
    start_time: datetime
    metadata: dict[str, Any] | None = None


def extract_event_timestamps(tracklet: Any) -> EventTimestamps:
    """Extract the pipeline timestamp triple from a tracklet-like object."""
    timestamps = getattr(tracklet, "timestamps", None)
    if timestamps is None:
        return EventTimestamps()
    return EventTimestamps(
        source_capture_ts=_coerce_datetime(
            getattr(timestamps, "source_capture_ts", None)
        ),
        edge_receive_ts=_coerce_datetime(
            getattr(timestamps, "edge_receive_ts", None)
        ),
        core_ingest_ts=_coerce_datetime(
            getattr(timestamps, "core_ingest_ts", None)
        ),
        clock_quality=_coerce_int(getattr(timestamps, "clock_quality", None)),
    )


def extract_tracklet_time(tracklet: Any) -> datetime:
    """Select the best available event time from the tracklet payload."""
    trajectory = getattr(tracklet, "trajectory", None) or []
    if trajectory:
        frame_ts = _coerce_datetime(getattr(trajectory[-1], "frame_ts", None))
        if frame_ts is not None:
            return frame_ts

    timestamps = extract_event_timestamps(tracklet)
    for candidate in (
        timestamps.source_capture_ts,
        timestamps.edge_receive_ts,
        timestamps.core_ingest_ts,
    ):
        if candidate is not None:
            return candidate
    return datetime.now(tz=timezone.utc)


def make_point_event(
    event_type: EventType,
    camera_id: str,
    event_time: datetime,
    timestamps: EventTimestamps,
    track_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EventTrigger:
    """Create an immediately-closed point-in-time event."""
    return EventTrigger(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        camera_id=camera_id,
        track_id=track_id,
        track_ids=(track_id,) if track_id else (),
        start_time=event_time,
        end_time=event_time,
        duration_ms=0,
        state=EventRecordState.CLOSED,
        operation=EventOperation.INSERT,
        metadata=metadata,
        timestamps=timestamps,
    )


class TrackStateMachine:
    """Per-track rule engine for track-derived events."""

    def __init__(
        self,
        track_id: str,
        camera_id: str,
        object_class: str,
        camera_zones: CameraZones,
        stopped_threshold: float,
        stopped_duration_s: float,
        stopped_resume_threshold: float,
        stopped_resume_duration_s: float,
    ) -> None:
        self.track_id = track_id
        self.camera_id = camera_id
        self.object_class = object_class
        self.camera_zones = camera_zones
        self.stopped_threshold = stopped_threshold
        self.stopped_duration_s = stopped_duration_s
        self.stopped_resume_threshold = stopped_resume_threshold
        self.stopped_resume_duration_s = stopped_resume_duration_s

        self.state = TrackState.MOVING
        self.last_centroid: Point | None = None
        self.last_update: float | None = None
        self.last_timestamps = EventTimestamps()
        self.stopped_since: float | None = None
        self.resume_candidate_since: float | None = None
        self.entered = False

        self._stopped_event: ActiveEvent | None = None
        self._loitering_candidates: dict[str, float] = {}
        self._loitering_events: dict[str, ActiveEvent] = {}
        self._current_loitering_zone_ids: set[str] = set()

    def update(self, tracklet: Any) -> list[EventTrigger]:
        """Process one tracklet update and return any triggered events."""
        event_time = extract_tracklet_time(tracklet)
        timestamps = extract_event_timestamps(tracklet)
        now = event_time.timestamp()
        centroid = _latest_centroid(tracklet)
        triggers: list[EventTrigger] = []

        self.last_timestamps = timestamps

        if centroid is not None:
            if not self.entered and self.camera_zones.in_roi(centroid):
                triggers.append(
                    make_point_event(
                        event_type=EventType.ENTERED_SCENE,
                        camera_id=self.camera_id,
                        track_id=self.track_id,
                        event_time=event_time,
                        timestamps=timestamps,
                    )
                )
                self.entered = True

            if self.last_centroid is not None:
                displacement = math.dist(self.last_centroid, centroid)
                if self.object_class in VEHICLE_CLASSES:
                    triggers.extend(
                        self._handle_vehicle_motion(
                            displacement=displacement,
                            centroid=centroid,
                            now=now,
                            event_time=event_time,
                        )
                    )
                else:
                    self.stopped_since = None
                    self.resume_candidate_since = None

            if self.object_class == "person":
                triggers.extend(
                    self._handle_loitering_update(
                        centroid=centroid,
                        now=now,
                        event_time=event_time,
                    )
                )
            else:
                self._current_loitering_zone_ids.clear()
                self._loitering_candidates.clear()

            self.last_centroid = centroid

        self.last_update = now

        if getattr(tracklet, "state", None) == 4:
            triggers.extend(self.close(event_time=event_time))

        self._sync_state()
        return triggers

    def check_timers(self, now: float) -> list[EventTrigger]:
        """Check time-based triggers that may fire without a fresh tracklet."""
        if self.last_update is None:
            return []

        triggers: list[EventTrigger] = []
        event_time = datetime.fromtimestamp(now, tz=timezone.utc)

        if (
            self.object_class in VEHICLE_CLASSES
            and self.stopped_since is not None
            and self._stopped_event is None
            and now - self.stopped_since >= self.stopped_duration_s
        ):
            triggers.append(
                self._open_stopped_event(
                    centroid=self.last_centroid,
                    start_time=datetime.fromtimestamp(
                        self.stopped_since, tz=timezone.utc
                    ),
                )
            )

        if (
            self._stopped_event is not None
            and self.resume_candidate_since is not None
            and now - self.resume_candidate_since >= self.stopped_resume_duration_s
        ):
            triggers.append(self._close_stopped_event(end_time=event_time))

        for zone in self.camera_zones.loitering_zones:
            candidate_since = self._loitering_candidates.get(zone.zone_id)
            if (
                zone.zone_id in self._current_loitering_zone_ids
                and zone.zone_id not in self._loitering_events
                and candidate_since is not None
                and now - candidate_since >= zone.duration_s
            ):
                triggers.append(
                    self._open_loitering_event(
                        zone=zone,
                        start_time=datetime.fromtimestamp(
                            candidate_since,
                            tz=timezone.utc,
                        ),
                    )
                )

        self._sync_state()
        return triggers

    def close(self, event_time: datetime | None = None) -> list[EventTrigger]:
        """Close any open duration events and emit exited_scene."""
        closed_at = event_time or datetime.now(tz=timezone.utc)
        triggers: list[EventTrigger] = []

        if self._stopped_event is not None:
            triggers.append(self._close_stopped_event(end_time=closed_at))

        for zone_id in list(self._loitering_events):
            triggers.append(
                self._close_loitering_event(
                    zone_id=zone_id,
                    end_time=closed_at,
                )
            )

        if self.entered:
            triggers.append(
                make_point_event(
                    event_type=EventType.EXITED_SCENE,
                    camera_id=self.camera_id,
                    track_id=self.track_id,
                    event_time=closed_at,
                    timestamps=self.last_timestamps,
                )
            )

        self.stopped_since = None
        self.resume_candidate_since = None
        self._loitering_candidates.clear()
        self._current_loitering_zone_ids.clear()
        self._sync_state()
        return triggers

    def _handle_vehicle_motion(
        self,
        displacement: float,
        centroid: Point,
        now: float,
        event_time: datetime,
    ) -> list[EventTrigger]:
        triggers: list[EventTrigger] = []

        if self._stopped_event is None:
            if displacement < self.stopped_threshold:
                if self.stopped_since is None:
                    self.stopped_since = now
            else:
                self.stopped_since = None

            if (
                self.stopped_since is not None
                and now - self.stopped_since >= self.stopped_duration_s
            ):
                triggers.append(
                    self._open_stopped_event(
                        centroid=centroid,
                        start_time=datetime.fromtimestamp(
                            self.stopped_since, tz=timezone.utc
                        ),
                    )
                )
            return triggers

        if displacement > self.stopped_resume_threshold:
            if self.resume_candidate_since is None:
                self.resume_candidate_since = now
            elif now - self.resume_candidate_since >= self.stopped_resume_duration_s:
                triggers.append(self._close_stopped_event(end_time=event_time))
        else:
            self.resume_candidate_since = None

        return triggers

    def _handle_loitering_update(
        self,
        centroid: Point,
        now: float,
        event_time: datetime,
    ) -> list[EventTrigger]:
        triggers: list[EventTrigger] = []
        containing_zones = self.camera_zones.zones_containing(centroid)
        zone_ids = {zone.zone_id for zone in containing_zones}

        for zone in containing_zones:
            if (
                zone.zone_id not in self._loitering_candidates
                and zone.zone_id not in self._loitering_events
            ):
                self._loitering_candidates[zone.zone_id] = now

            candidate_since = self._loitering_candidates.get(zone.zone_id)
            if (
                candidate_since is not None
                and zone.zone_id not in self._loitering_events
                and now - candidate_since >= zone.duration_s
            ):
                triggers.append(
                    self._open_loitering_event(
                        zone=zone,
                        start_time=datetime.fromtimestamp(
                            candidate_since,
                            tz=timezone.utc,
                        ),
                    )
                )

        exited_zones = self._current_loitering_zone_ids - zone_ids
        for zone_id in exited_zones:
            if zone_id in self._loitering_events:
                triggers.append(
                    self._close_loitering_event(
                        zone_id=zone_id,
                        end_time=event_time,
                    )
                )
            self._loitering_candidates.pop(zone_id, None)

        self._current_loitering_zone_ids = zone_ids
        return triggers

    def _open_stopped_event(
        self,
        centroid: Point | None,
        start_time: datetime,
    ) -> EventTrigger:
        metadata = None
        if centroid is not None:
            metadata = {
                "centroid_x": centroid[0],
                "centroid_y": centroid[1],
            }

        active_event = ActiveEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.STOPPED,
            start_time=start_time,
            metadata=metadata,
        )
        self._stopped_event = active_event
        self.resume_candidate_since = None

        return EventTrigger(
            event_id=active_event.event_id,
            event_type=EventType.STOPPED,
            camera_id=self.camera_id,
            track_id=self.track_id,
            track_ids=(self.track_id,),
            start_time=active_event.start_time,
            end_time=None,
            duration_ms=None,
            state=EventRecordState.ACTIVE,
            operation=EventOperation.INSERT,
            metadata=active_event.metadata,
            timestamps=self.last_timestamps,
        )

    def _close_stopped_event(self, end_time: datetime) -> EventTrigger:
        if self._stopped_event is None:
            raise RuntimeError("cannot close stopped event that is not open")

        active_event = self._stopped_event
        self._stopped_event = None
        self.stopped_since = None
        self.resume_candidate_since = None
        return _close_duration_event(
            active_event=active_event,
            camera_id=self.camera_id,
            track_id=self.track_id,
            end_time=end_time,
            timestamps=self.last_timestamps,
        )

    def _open_loitering_event(
        self,
        zone: LoiteringZone,
        start_time: datetime,
    ) -> EventTrigger:
        active_event = ActiveEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.LOITERING,
            start_time=start_time,
            metadata={"zone_id": zone.zone_id},
        )
        self._loitering_events[zone.zone_id] = active_event
        return EventTrigger(
            event_id=active_event.event_id,
            event_type=EventType.LOITERING,
            camera_id=self.camera_id,
            track_id=self.track_id,
            track_ids=(self.track_id,),
            start_time=active_event.start_time,
            end_time=None,
            duration_ms=None,
            state=EventRecordState.ACTIVE,
            operation=EventOperation.INSERT,
            metadata=active_event.metadata,
            timestamps=self.last_timestamps,
        )

    def _close_loitering_event(
        self,
        zone_id: str,
        end_time: datetime,
    ) -> EventTrigger:
        active_event = self._loitering_events.pop(zone_id)
        self._loitering_candidates.pop(zone_id, None)
        return _close_duration_event(
            active_event=active_event,
            camera_id=self.camera_id,
            track_id=self.track_id,
            end_time=end_time,
            timestamps=self.last_timestamps,
        )

    def _sync_state(self) -> None:
        if self._loitering_events:
            self.state = TrackState.LOITERING
        elif self._stopped_event is not None:
            self.state = TrackState.STOPPED
        else:
            self.state = TrackState.MOVING


def _close_duration_event(
    active_event: ActiveEvent,
    camera_id: str,
    track_id: str,
    end_time: datetime,
    timestamps: EventTimestamps,
) -> EventTrigger:
    duration_ms = max(
        0,
        int((end_time - active_event.start_time).total_seconds() * 1000),
    )
    return EventTrigger(
        event_id=active_event.event_id,
        event_type=active_event.event_type,
        camera_id=camera_id,
        track_id=track_id,
        track_ids=(track_id,),
        start_time=active_event.start_time,
        end_time=end_time,
        duration_ms=duration_ms,
        state=EventRecordState.CLOSED,
        operation=EventOperation.UPDATE,
        metadata=active_event.metadata,
        timestamps=timestamps,
    )


def _latest_centroid(tracklet: Any) -> Point | None:
    trajectory = getattr(tracklet, "trajectory", None) or []
    if not trajectory:
        return None
    point = trajectory[-1]
    x = float(getattr(point, "centroid_x"))
    y = float(getattr(point, "centroid_y"))
    return (x, y)


def _coerce_mapping(raw_config: Any) -> dict[str, Any] | None:
    if raw_config is None:
        return None
    if isinstance(raw_config, dict):
        return raw_config
    if isinstance(raw_config, str):
        try:
            parsed = json.loads(raw_config)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _normalise_polygon(raw_polygon: Any) -> Polygon | None:
    polygon = raw_polygon
    if isinstance(polygon, dict):
        for key in ("polygon", "points", "vertices", "coordinates"):
            polygon = polygon.get(key)
            if polygon is not None:
                break

    if not isinstance(polygon, list):
        return None

    points: list[Point] = []
    for raw_point in polygon:
        if isinstance(raw_point, (list, tuple)) and len(raw_point) >= 2:
            points.append((float(raw_point[0]), float(raw_point[1])))
            continue
        if isinstance(raw_point, dict):
            x_value = raw_point.get("x")
            y_value = raw_point.get("y")
            if x_value is not None and y_value is not None:
                points.append((float(x_value), float(y_value)))

    return tuple(points) if len(points) >= 3 else None


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Ray-casting point-in-polygon with boundary treated as inside."""
    x, y = point
    inside = False
    for index, first in enumerate(polygon):
        second = polygon[(index + 1) % len(polygon)]
        if _point_on_segment(point, first, second):
            return True
        x1, y1 = first
        x2, y2 = second
        intersects = ((y1 > y) != (y2 > y)) and (
            x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1
        )
        if intersects:
            inside = not inside
    return inside


def _point_on_segment(point: Point, start: Point, end: Point) -> bool:
    x, y = point
    x1, y1 = start
    x2, y2 = end
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > 1e-9:
        return False
    min_x = min(x1, x2) - 1e-9
    max_x = max(x1, x2) + 1e-9
    min_y = min(y1, y2) - 1e-9
    max_y = max(y1, y2) + 1e-9
    return min_x <= x <= max_x and min_y <= y <= max_y


def _coerce_datetime(raw_value: Any) -> datetime | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        if raw_value.tzinfo is None:
            return raw_value.replace(tzinfo=timezone.utc)
        return raw_value.astimezone(timezone.utc)

    seconds = getattr(raw_value, "seconds", None)
    nanos = getattr(raw_value, "nanos", None)
    if seconds is None or nanos is None:
        return None
    if seconds == 0 and nanos == 0:
        return None
    return datetime.fromtimestamp(
        float(seconds) + float(nanos) / 1_000_000_000,
        tz=timezone.utc,
    )


def _coerce_int(raw_value: Any) -> int | None:
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None
