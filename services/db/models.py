"""
Database models for the Cilex Vision Multi-Camera Video Analytics Platform.

SQLAlchemy 2.0 async models for all tables:
- TimescaleDB hypertables: detections, track_observations
- PostgreSQL relational: sites, cameras, topology_edges, local_tracks,
  global_tracks, global_track_links, track_attributes, events, users, audit_logs

ER diagram: docs/diagrams/schema.mermaid
ADR: docs/adr/ADR-003-database-schema.md
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID as PG_UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)
from sqlalchemy.sql import func


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ObjectClass(str, enum.Enum):
    PERSON = "person"
    CAR = "car"
    TRUCK = "truck"
    BUS = "bus"
    BICYCLE = "bicycle"
    MOTORCYCLE = "motorcycle"
    ANIMAL = "animal"


class EventType(str, enum.Enum):
    ENTERED_SCENE = "entered_scene"
    EXITED_SCENE = "exited_scene"
    STOPPED = "stopped"
    LOITERING = "loitering"
    MOTION_STARTED = "motion_started"
    MOTION_ENDED = "motion_ended"


class EventState(str, enum.Enum):
    NEW = "new"
    ACTIVE = "active"
    STOPPED = "stopped"
    EXITED = "exited"
    CLOSED = "closed"


class TrackletState(str, enum.Enum):
    NEW = "new"
    ACTIVE = "active"
    LOST = "lost"
    TERMINATED = "terminated"


class ColorValue(str, enum.Enum):
    RED = "red"
    BLUE = "blue"
    WHITE = "white"
    BLACK = "black"
    SILVER = "silver"
    GREEN = "green"
    YELLOW = "yellow"
    BROWN = "brown"
    ORANGE = "orange"
    UNKNOWN = "unknown"


class AttributeType(str, enum.Enum):
    VEHICLE_COLOR = "vehicle_color"
    PERSON_UPPER_COLOR = "person_upper_color"
    PERSON_LOWER_COLOR = "person_lower_color"


class CameraStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Base & mixins
# ---------------------------------------------------------------------------

TSTZ = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TSTZ, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        TSTZ, onupdate=func.now(), nullable=True
    )


# ---------------------------------------------------------------------------
# Helper: enum CHECK constraint values
# ---------------------------------------------------------------------------

_OBJECT_CLASSES = ", ".join(f"'{c.value}'" for c in ObjectClass)
_EVENT_TYPES = ", ".join(f"'{t.value}'" for t in EventType)
_EVENT_STATES = ", ".join(f"'{s.value}'" for s in EventState)
_TRACKLET_STATES = ", ".join(f"'{s.value}'" for s in TrackletState)
_COLORS = ", ".join(f"'{c.value}'" for c in ColorValue)
_ATTR_TYPES = ", ".join(f"'{a.value}'" for a in AttributeType)
_CAMERA_STATUSES = ", ".join(f"'{s.value}'" for s in CameraStatus)


# ---------------------------------------------------------------------------
# TimescaleDB hypertables (high-volume, append-only)
# ---------------------------------------------------------------------------


class Detection(Base):
    """Raw object detections. TimescaleDB hypertable, chunk 1h, retain 30d, compress 2d."""

    __tablename__ = "detections"

    time: Mapped[datetime] = mapped_column(TSTZ, primary_key=True)
    camera_id: Mapped[str] = mapped_column(Text, primary_key=True)
    frame_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_class: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_w: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_h: Mapped[float] = mapped_column(Float, nullable=False)
    local_track_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False)
    thumbnail_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(f"object_class IN ({_OBJECT_CLASSES})", name="ck_detections_class"),
    )


class TrackObservation(Base):
    """Per-frame track centroid observations. TimescaleDB hypertable, chunk 1h, retain 30d."""

    __tablename__ = "track_observations"

    time: Mapped[datetime] = mapped_column(TSTZ, primary_key=True)
    camera_id: Mapped[str] = mapped_column(Text, primary_key=True)
    local_track_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True
    )
    centroid_x: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_y: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_area: Mapped[float] = mapped_column(Float, nullable=False)
    embedding_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# PostgreSQL relational tables
# ---------------------------------------------------------------------------


class Site(TimestampMixin, Base):
    __tablename__ = "sites"

    site_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), server_default="UTC", nullable=False)

    cameras: Mapped[list[Camera]] = relationship(back_populates="site")


class Camera(TimestampMixin, Base):
    __tablename__ = "cameras"

    camera_id: Mapped[str] = mapped_column(Text, primary_key=True)
    site_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sites.site_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rtsp_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), server_default="offline", nullable=False
    )
    config_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    site: Mapped[Site] = relationship(back_populates="cameras")
    local_tracks: Mapped[list[LocalTrack]] = relationship(back_populates="camera")
    events: Mapped[list[Event]] = relationship(back_populates="camera")

    __table_args__ = (
        CheckConstraint(f"status IN ({_CAMERA_STATUSES})", name="ck_cameras_status"),
    )


class TopologyEdge(Base):
    __tablename__ = "topology_edges"

    edge_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    camera_a_id: Mapped[str] = mapped_column(
        Text, ForeignKey("cameras.camera_id"), nullable=False
    )
    camera_b_id: Mapped[str] = mapped_column(
        Text, ForeignKey("cameras.camera_id"), nullable=False
    )
    transition_time_s: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TSTZ, server_default=func.now(), nullable=False
    )

    camera_a: Mapped[Camera] = relationship(foreign_keys=[camera_a_id])
    camera_b: Mapped[Camera] = relationship(foreign_keys=[camera_b_id])


class LocalTrack(Base):
    __tablename__ = "local_tracks"

    local_track_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    camera_id: Mapped[str] = mapped_column(
        Text, ForeignKey("cameras.camera_id"), nullable=False
    )
    object_class: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    mean_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    start_time: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    end_time: Mapped[Optional[datetime]] = mapped_column(TSTZ, nullable=True)
    tracker_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TSTZ, server_default=func.now(), nullable=False
    )

    camera: Mapped[Camera] = relationship(back_populates="local_tracks")
    global_track_links: Mapped[list[GlobalTrackLink]] = relationship(back_populates="local_track")
    track_attributes: Mapped[list[TrackAttribute]] = relationship(back_populates="local_track")

    __table_args__ = (
        CheckConstraint(f"object_class IN ({_OBJECT_CLASSES})", name="ck_local_tracks_class"),
        CheckConstraint(f"state IN ({_TRACKLET_STATES})", name="ck_local_tracks_state"),
    )


class GlobalTrack(Base):
    __tablename__ = "global_tracks"

    global_track_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    object_class: Mapped[str] = mapped_column(String(20), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TSTZ, server_default=func.now(), nullable=False
    )

    links: Mapped[list[GlobalTrackLink]] = relationship(back_populates="global_track")

    __table_args__ = (
        CheckConstraint(f"object_class IN ({_OBJECT_CLASSES})", name="ck_global_tracks_class"),
    )


class GlobalTrackLink(Base):
    __tablename__ = "global_track_links"

    link_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    global_track_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("global_tracks.global_track_id"),
        nullable=False,
    )
    local_track_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("local_tracks.local_track_id"),
        nullable=False,
    )
    camera_id: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(TSTZ, nullable=False)

    global_track: Mapped[GlobalTrack] = relationship(back_populates="links")
    local_track: Mapped[LocalTrack] = relationship(back_populates="global_track_links")


class TrackAttribute(Base):
    __tablename__ = "track_attributes"

    attribute_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    local_track_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("local_tracks.local_track_id"),
        nullable=False,
    )
    attribute_type: Mapped[str] = mapped_column(String(30), nullable=False)
    color_value: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    model_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(TSTZ, nullable=False)

    local_track: Mapped[LocalTrack] = relationship(back_populates="track_attributes")

    __table_args__ = (
        CheckConstraint(f"attribute_type IN ({_ATTR_TYPES})", name="ck_track_attrs_type"),
        CheckConstraint(f"color_value IN ({_COLORS})", name="ck_track_attrs_color"),
    )


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    track_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("local_tracks.local_track_id"),
        nullable=True,
    )
    camera_id: Mapped[str] = mapped_column(
        Text, ForeignKey("cameras.camera_id"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    end_time: Mapped[Optional[datetime]] = mapped_column(TSTZ, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    clip_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    metadata_jsonb: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    source_capture_ts: Mapped[Optional[datetime]] = mapped_column(TSTZ, nullable=True)
    edge_receive_ts: Mapped[Optional[datetime]] = mapped_column(TSTZ, nullable=True)
    core_ingest_ts: Mapped[Optional[datetime]] = mapped_column(TSTZ, nullable=True)

    track: Mapped[Optional[LocalTrack]] = relationship()
    camera: Mapped[Camera] = relationship(back_populates="events")

    __table_args__ = (
        CheckConstraint(f"event_type IN ({_EVENT_TYPES})", name="ck_events_type"),
        CheckConstraint(f"state IN ({_EVENT_STATES})", name="ck_events_state"),
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)

    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="user")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    log_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.user_id"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details_jsonb: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TSTZ, server_default=func.now(), nullable=False
    )

    user: Mapped[Optional[User]] = relationship(back_populates="audit_logs")


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
    "Base",
    "ObjectClass",
    "EventType",
    "EventState",
    "TrackletState",
    "ColorValue",
    "AttributeType",
    "CameraStatus",
    "Detection",
    "TrackObservation",
    "Site",
    "Camera",
    "TopologyEdge",
    "LocalTrack",
    "GlobalTrack",
    "GlobalTrackLink",
    "TrackAttribute",
    "Event",
    "User",
    "AuditLog",
]
