"""Pydantic response models for the Query API.

These align with the database schema in services/db/models.py and the
protobuf schemas in proto/vidanalytics/v1/.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Detections
# ---------------------------------------------------------------------------


class BoundingBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class DetectionResponse(BaseModel):
    time: datetime
    camera_id: str
    frame_seq: int
    object_class: str
    confidence: float
    bbox: BoundingBox
    local_track_id: Optional[str] = None
    model_version: str


class DetectionListResponse(BaseModel):
    detections: list[DetectionResponse]
    total: int
    offset: int
    limit: int


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------


class TrackAttributeResponse(BaseModel):
    attribute_id: str
    attribute_type: str
    color_value: str
    confidence: float
    model_version: Optional[str] = None
    observed_at: datetime


class TrackSummaryResponse(BaseModel):
    local_track_id: str
    camera_id: str
    object_class: str
    state: str
    mean_confidence: Optional[float] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    tracker_version: Optional[str] = None
    created_at: datetime


class TrackDetailResponse(TrackSummaryResponse):
    attributes: list[TrackAttributeResponse] = Field(default_factory=list)
    thumbnail_url: Optional[str] = None


class TrackListResponse(BaseModel):
    tracks: list[TrackSummaryResponse]
    total: int
    offset: int
    limit: int


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class EventResponse(BaseModel):
    event_id: str
    event_type: str
    track_id: Optional[str] = None
    camera_id: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: Optional[int] = None
    clip_url: Optional[str] = None  # signed MinIO URL
    state: str
    metadata: Optional[dict] = None
    source_capture_ts: Optional[datetime] = None
    edge_receive_ts: Optional[datetime] = None
    core_ingest_ts: Optional[datetime] = None


class EventListResponse(BaseModel):
    events: list[EventResponse]
    total: int
    offset: int
    limit: int


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class UserClaims(BaseModel):
    """JWT token claims extracted from the httpOnly cookie."""

    user_id: str
    username: str
    role: str
    camera_scope: list[str] = Field(default_factory=list)
