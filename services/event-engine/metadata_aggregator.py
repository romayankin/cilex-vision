"""Aggregate detections/tracks/attributes into events.metadata_jsonb.

Called when a motion duration event closes. Reads the DB for the
[start, end] window on a specific camera, builds the v1 metadata
payload (see `frontend/lib/event-metadata.ts`), writes it onto the
event row.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

METADATA_VERSION = 1
# TODO(phase4+): thread this in from inference config instead of a constant.
MODEL_VERSION_HINT = "yolov8s-v1+osnet-x0.25"

# Map DB attribute_type enum values to metadata buckets under objects.<class>.attributes.
# person_upper_color → attributes.upper_colors, etc.
ATTR_TYPE_TO_BUCKET = {
    "person_upper_color": "upper_colors",
    "person_lower_color": "lower_colors",
    "vehicle_color": "colors",
}

ATTR_MIN_CONFIDENCE = 0.5


class MetadataAggregator:
    """Build the events.metadata_jsonb payload for a motion window."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def aggregate(
        self,
        event_id: str,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, Any]:
        """Read the [start, end] window, build the payload, update the row.

        Returns the payload for logging/testing. Never raises — any
        failure is logged and an empty payload is written so the row
        still carries a version marker.
        """
        t0 = time.time()
        attributes_enabled = await self._read_attributes_enabled()

        try:
            detections = await self._load_detections(camera_id, start_time, end_time)
            local_tracks = await self._load_local_tracks(camera_id, start_time, end_time)
            if attributes_enabled and local_tracks:
                attributes = await self._load_attributes(
                    [t["local_track_id"] for t in local_tracks]
                )
            else:
                attributes = []

            zones = await self._find_zones_triggered(camera_id, detections)
            peak_at = self._find_peak_motion_moment(detections) or start_time

            duration_s = (end_time - start_time).total_seconds()
            frames_analyzed = len(detections)

            objects_by_class = self._build_objects(
                detections=detections,
                local_tracks=local_tracks,
                attributes=attributes,
            )

            processing_ms = int((time.time() - t0) * 1000)
            payload: dict[str, Any] = {
                "version": METADATA_VERSION,
                "motion_interval": {
                    "started_at": _iso(start_time),
                    "ended_at": _iso(end_time),
                    "duration_s": duration_s,
                    "peak_at": _iso(peak_at),
                },
                "objects": objects_by_class,
                "zones_triggered": zones,
                "model_version": MODEL_VERSION_HINT,
                "processing": {
                    "frames_analyzed": frames_analyzed,
                    "processing_ms": processing_ms,
                    "attributes_enabled": attributes_enabled,
                },
            }
        except Exception:
            logger.exception(
                "metadata aggregation failed for event %s — writing empty payload",
                event_id,
            )
            payload = {
                "version": METADATA_VERSION,
                "motion_interval": {
                    "started_at": _iso(start_time),
                    "ended_at": _iso(end_time),
                    "duration_s": (end_time - start_time).total_seconds(),
                    "peak_at": _iso(start_time),
                },
                "objects": {},
                "zones_triggered": [],
                "model_version": MODEL_VERSION_HINT,
                "processing": {
                    "frames_analyzed": 0,
                    "processing_ms": int((time.time() - t0) * 1000),
                    "attributes_enabled": attributes_enabled,
                    "error": True,
                },
            }

        await self._write_metadata(event_id, payload)
        total_objects = sum(obj["count"] for obj in payload["objects"].values())
        logger.info(
            "Aggregated metadata for event %s (%s): objects=%d frames=%d %dms",
            event_id, camera_id, total_objects,
            payload["processing"]["frames_analyzed"],
            payload["processing"]["processing_ms"],
        )
        return payload

    async def _read_attributes_enabled(self) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT enabled FROM service_toggles WHERE service_name = 'attribute-service'"
            )
        return bool(row["enabled"]) if row else True

    async def _load_detections(
        self,
        camera_id: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT time, object_class, confidence,
                       bbox_x, bbox_y, bbox_w, bbox_h, local_track_id
                FROM detections
                WHERE camera_id = $1 AND time BETWEEN $2 AND $3
                ORDER BY time
                """,
                camera_id, start, end,
            )
        return [dict(r) for r in rows]

    async def _load_local_tracks(
        self,
        camera_id: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT local_track_id, object_class, start_time, end_time
                FROM local_tracks
                WHERE camera_id = $1
                  AND start_time <= $3
                  AND COALESCE(end_time, NOW()) >= $2
                """,
                camera_id, start, end,
            )
        return [dict(r) for r in rows]

    async def _load_attributes(
        self,
        track_ids: list[Any],
    ) -> list[dict[str, Any]]:
        if not track_ids:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT local_track_id, attribute_type, color_value, confidence
                FROM track_attributes
                WHERE local_track_id = ANY($1::uuid[])
                  AND confidence >= $2
                """,
                track_ids, ATTR_MIN_CONFIDENCE,
            )
        return [dict(r) for r in rows]

    async def _find_zones_triggered(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
    ) -> list[str]:
        # Zone-polygon intersection is a later phase. Downstream consumers
        # expect a list; return empty until wired up.
        return []

    def _find_peak_motion_moment(
        self,
        detections: list[dict[str, Any]],
    ) -> datetime | None:
        if not detections:
            return None
        buckets: Counter[int] = Counter()
        for d in detections:
            t: datetime = d["time"]
            buckets[int(t.timestamp())] += 1
        if not buckets:
            return None
        peak_sec = buckets.most_common(1)[0][0]
        tzinfo = detections[0]["time"].tzinfo or timezone.utc
        return datetime.fromtimestamp(peak_sec, tz=tzinfo)

    def _build_objects(
        self,
        detections: list[dict[str, Any]],
        local_tracks: list[dict[str, Any]],
        attributes: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        tracks_by_class: dict[str, set[str]] = defaultdict(set)
        class_by_track: dict[str, str] = {}
        for t in local_tracks:
            cls = t["object_class"]
            tid = str(t["local_track_id"])
            tracks_by_class[cls].add(tid)
            class_by_track[tid] = cls

        frames_by_class: Counter[str] = Counter(d["object_class"] for d in detections)

        objects: dict[str, dict[str, Any]] = {}
        for cls, track_set in tracks_by_class.items():
            objects[cls] = {
                "count": len(track_set),
                "total_frames_seen": frames_by_class.get(cls, 0),
                "attributes": {},
                "track_ids": sorted(track_set),
            }

        for attr in attributes:
            track_id = str(attr["local_track_id"])
            cls = class_by_track.get(track_id)
            if cls is None or cls not in objects:
                continue
            bucket_name = ATTR_TYPE_TO_BUCKET.get(attr["attribute_type"])
            if bucket_name is None:
                continue
            bucket: list[str] = objects[cls]["attributes"].setdefault(bucket_name, [])
            color = attr["color_value"]
            if color not in bucket:
                bucket.append(color)

        return objects

    async def _write_metadata(
        self,
        event_id: str,
        payload: dict[str, Any],
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events
                SET metadata_jsonb = $1::jsonb,
                    updated_at = NOW()
                WHERE event_id = $2::uuid
                """,
                json.dumps(payload), event_id,
            )


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
