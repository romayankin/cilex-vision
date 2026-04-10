#!/usr/bin/env python3
"""Collect Re-ID training triplets from MTMC associations and debug traces.

Usage:
    python collect_reid_training_data.py \
        --db-dsn postgresql://localhost:5432/vidanalytics \
        --min-confidence 0.9 --output-dir data/reid-training/raw
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import hashlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any
from uuid import UUID

from hard_example_miner import (
    build_minio_client,
    bucket_object_last_modified,
    load_trace_payload,
    parse_trace_timestamp,
    sanitize_fragment,
)


LOG = logging.getLogger("collect_reid_training_data")

DEFAULT_MINIO_URL = "http://localhost:9000"
DEFAULT_MINIO_ACCESS_KEY = "minioadmin"
DEFAULT_MINIO_SECRET_KEY = "minioadmin123"
DEFAULT_DEBUG_BUCKET = "debug-traces"
OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)


@dataclass(frozen=True)
class TrackLinkRecord:
    global_track_id: str
    local_track_id: str
    camera_id: str
    object_class: str
    link_confidence: float
    linked_at: datetime
    start_time: datetime
    end_time: datetime | None
    mean_confidence: float | None

    @property
    def reference_time(self) -> datetime:
        end_time = self.end_time or self.start_time
        midpoint = self.start_time + (end_time - self.start_time) / 2
        return midpoint.astimezone(UTC)


@dataclass(frozen=True)
class LocalTrackRecord:
    local_track_id: str
    camera_id: str
    object_class: str
    start_time: datetime
    end_time: datetime | None
    mean_confidence: float | None
    global_track_id: str | None
    link_confidence: float | None

    @property
    def reference_time(self) -> datetime:
        end_time = self.end_time or self.start_time
        midpoint = self.start_time + (end_time - self.start_time) / 2
        return midpoint.astimezone(UTC)


@dataclass(frozen=True)
class DetectionSnapshot:
    local_track_id: str
    time: datetime
    bbox_xywh: tuple[float, float, float, float]
    confidence: float
    model_version: str

    @property
    def bbox_xyxy(self) -> tuple[float, float, float, float]:
        x, y, w, h = self.bbox_xywh
        return (x, y, x + w, y + h)


@dataclass(frozen=True)
class TrackFrameRef:
    trace_id: str
    frame_uri: str
    timestamp: datetime


@dataclass(frozen=True)
class DirectedPositivePair:
    anchor: TrackLinkRecord
    positive: TrackLinkRecord

    @property
    def object_class(self) -> str:
        return self.anchor.object_class

    @property
    def positive_link_confidence(self) -> float:
        return min(self.anchor.link_confidence, self.positive.link_confidence)


@dataclass(frozen=True)
class HardNegativeChoice:
    track: LocalTrackRecord
    time_distance_s: float
    duration_distance_s: float
    confidence_distance: float

    @property
    def ranking_tuple(self) -> tuple[float, float, float, str]:
        return (
            self.time_distance_s,
            self.duration_distance_s,
            self.confidence_distance,
            self.track.local_track_id,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-dsn",
        default=os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN"),
        help="PostgreSQL DSN.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.9,
        help="Minimum MTMC link confidence used for positive pairs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/reid-training/raw"),
        help="Directory receiving raw frames, crops, and triplet-manifest.json.",
    )
    parser.add_argument(
        "--max-triplets",
        type=int,
        default=10000,
        help="Maximum number of triplets to emit.",
    )
    parser.add_argument(
        "--negative-window-hours",
        type=float,
        default=1.0,
        help="Hard negative search window around the anchor time.",
    )
    parser.add_argument(
        "--negatives-per-positive",
        type=int,
        default=3,
        help="Maximum negatives to mine per directed anchor-positive pair.",
    )
    parser.add_argument(
        "--minio-url",
        default=os.environ.get("MINIO_URL", DEFAULT_MINIO_URL),
        help="MinIO base URL, e.g. http://localhost:9000.",
    )
    parser.add_argument(
        "--minio-access-key",
        default=os.environ.get("MINIO_ACCESS_KEY", DEFAULT_MINIO_ACCESS_KEY),
        help="MinIO access key.",
    )
    parser.add_argument(
        "--minio-secret-key",
        default=os.environ.get("MINIO_SECRET_KEY", DEFAULT_MINIO_SECRET_KEY),
        help="MinIO secret key.",
    )
    parser.add_argument(
        "--debug-bucket",
        default=os.environ.get("DEBUG_TRACE_BUCKET", DEFAULT_DEBUG_BUCKET),
        help="MinIO bucket holding debug traces.",
    )
    parser.add_argument(
        "--manifest-name",
        default="triplet-manifest.json",
        help="Output manifest filename inside --output-dir.",
    )
    return parser.parse_args()


def require_pillow_image() -> Any:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError("missing optional dependency 'Pillow'; install with: pip install Pillow") from exc
    return Image


def uuid_list(values: set[str]) -> list[UUID]:
    return [UUID(value) for value in sorted(values)]


def time_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def track_duration_s(track: TrackLinkRecord | LocalTrackRecord) -> float:
    end_time = track.end_time or track.start_time
    return max(0.0, (end_time - track.start_time).total_seconds())


def output_path(root: Path, *parts: str) -> Path:
    return root.joinpath(*parts)


async def query_positive_tracks(
    dsn: str,
    *,
    min_confidence: float,
) -> list[TrackLinkRecord]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT gt.global_track_id::text AS global_track_id,
                   gt.object_class,
                   gtl.local_track_id::text AS local_track_id,
                   gtl.camera_id,
                   gtl.confidence,
                   gtl.linked_at,
                   lt.start_time,
                   lt.end_time,
                   lt.mean_confidence
            FROM global_tracks gt
            JOIN global_track_links gtl
              ON gtl.global_track_id = gt.global_track_id
            JOIN local_tracks lt
              ON lt.local_track_id = gtl.local_track_id
            WHERE gtl.confidence >= $1
            ORDER BY gt.global_track_id, gtl.linked_at, gtl.local_track_id
            """,
            min_confidence,
        )
    finally:
        await conn.close()

    return [
        TrackLinkRecord(
            global_track_id=str(row["global_track_id"]),
            local_track_id=str(row["local_track_id"]),
            camera_id=str(row["camera_id"]),
            object_class=str(row["object_class"]),
            link_confidence=float(row["confidence"]),
            linked_at=row["linked_at"].astimezone(UTC),
            start_time=row["start_time"].astimezone(UTC),
            end_time=row["end_time"].astimezone(UTC) if row["end_time"] is not None else None,
            mean_confidence=float(row["mean_confidence"]) if row["mean_confidence"] is not None else None,
        )
        for row in rows
        if str(row["object_class"]) in OBJECT_CLASSES
    ]


async def query_negative_candidates(
    dsn: str,
    *,
    camera_ids: set[str],
    object_classes: set[str],
    window_start: datetime,
    window_end: datetime,
) -> list[LocalTrackRecord]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT lt.local_track_id::text AS local_track_id,
                   lt.camera_id,
                   lt.object_class,
                   lt.start_time,
                   lt.end_time,
                   lt.mean_confidence,
                   link.global_track_id::text AS global_track_id,
                   link.confidence AS link_confidence
            FROM local_tracks lt
            LEFT JOIN LATERAL (
                SELECT gtl.global_track_id, gtl.confidence
                FROM global_track_links gtl
                WHERE gtl.local_track_id = lt.local_track_id
                ORDER BY gtl.confidence DESC, gtl.linked_at DESC
                LIMIT 1
            ) AS link ON TRUE
            WHERE lt.camera_id = ANY($1::text[])
              AND lt.object_class = ANY($2::text[])
              AND COALESCE(lt.end_time, lt.start_time) >= $3
              AND lt.start_time <= $4
            ORDER BY lt.camera_id, lt.object_class, lt.start_time
            """,
            sorted(camera_ids),
            sorted(object_classes),
            window_start,
            window_end,
        )
    finally:
        await conn.close()

    return [
        LocalTrackRecord(
            local_track_id=str(row["local_track_id"]),
            camera_id=str(row["camera_id"]),
            object_class=str(row["object_class"]),
            start_time=row["start_time"].astimezone(UTC),
            end_time=row["end_time"].astimezone(UTC) if row["end_time"] is not None else None,
            mean_confidence=float(row["mean_confidence"]) if row["mean_confidence"] is not None else None,
            global_track_id=str(row["global_track_id"]) if row["global_track_id"] is not None else None,
            link_confidence=float(row["link_confidence"]) if row["link_confidence"] is not None else None,
        )
        for row in rows
        if str(row["object_class"]) in OBJECT_CLASSES
    ]


async def query_representative_detections(
    dsn: str,
    *,
    track_ids: set[str],
) -> dict[str, DetectionSnapshot]:
    if not track_ids:
        return {}

    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (local_track_id)
                   local_track_id::text AS local_track_id,
                   time,
                   bbox_x,
                   bbox_y,
                   bbox_w,
                   bbox_h,
                   confidence,
                   model_version
            FROM detections
            WHERE local_track_id = ANY($1::uuid[])
            ORDER BY local_track_id, confidence DESC, time DESC
            """,
            uuid_list(track_ids),
        )
    finally:
        await conn.close()

    return {
        str(row["local_track_id"]): DetectionSnapshot(
            local_track_id=str(row["local_track_id"]),
            time=row["time"].astimezone(UTC),
            bbox_xywh=(
                float(row["bbox_x"]),
                float(row["bbox_y"]),
                float(row["bbox_w"]),
                float(row["bbox_h"]),
            ),
            confidence=float(row["confidence"]),
            model_version=str(row["model_version"]),
        )
        for row in rows
    }


def build_directed_pairs(tracks: list[TrackLinkRecord]) -> list[DirectedPositivePair]:
    by_global_track: dict[str, list[TrackLinkRecord]] = defaultdict(list)
    for track in tracks:
        by_global_track[track.global_track_id].append(track)

    directed_pairs: list[DirectedPositivePair] = []
    for group in by_global_track.values():
        unique_cameras = {track.camera_id for track in group}
        if len(unique_cameras) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda item: (
                item.reference_time,
                item.camera_id,
                item.local_track_id,
            ),
        )
        for left, right in combinations(ordered, 2):
            if left.camera_id == right.camera_id:
                continue
            directed_pairs.append(DirectedPositivePair(anchor=left, positive=right))
            directed_pairs.append(DirectedPositivePair(anchor=right, positive=left))

    directed_pairs.sort(
        key=lambda item: (
            -item.positive_link_confidence,
            item.anchor.reference_time,
            item.anchor.local_track_id,
            item.positive.local_track_id,
        )
    )
    return directed_pairs


def index_negative_candidates(
    tracks: list[LocalTrackRecord],
) -> dict[tuple[str, str], tuple[list[float], list[LocalTrackRecord]]]:
    grouped: dict[tuple[str, str], list[LocalTrackRecord]] = defaultdict(list)
    for track in tracks:
        grouped[(track.camera_id, track.object_class)].append(track)

    indexed: dict[tuple[str, str], tuple[list[float], list[LocalTrackRecord]]] = {}
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda item: (item.reference_time, item.local_track_id))
        indexed[key] = ([item.reference_time.timestamp() for item in ordered], ordered)
    return indexed


def mine_negatives_for_anchor(
    pair: DirectedPositivePair,
    *,
    negative_index: dict[tuple[str, str], tuple[list[float], list[LocalTrackRecord]]],
    negative_window: timedelta,
    negatives_per_positive: int,
) -> list[HardNegativeChoice]:
    index_key = (pair.anchor.camera_id, pair.anchor.object_class)
    indexed_group = negative_index.get(index_key)
    if indexed_group is None:
        return []

    times, candidates = indexed_group
    anchor_time = pair.anchor.reference_time
    window_start = (anchor_time - negative_window).timestamp()
    window_end = (anchor_time + negative_window).timestamp()
    left = bisect.bisect_left(times, window_start)
    right = bisect.bisect_right(times, window_end)

    hard_negatives: list[HardNegativeChoice] = []
    for candidate in candidates[left:right]:
        if candidate.local_track_id == pair.anchor.local_track_id:
            continue
        if candidate.global_track_id and candidate.global_track_id == pair.anchor.global_track_id:
            continue

        time_distance = abs((candidate.reference_time - anchor_time).total_seconds())
        duration_distance = abs(track_duration_s(candidate) - track_duration_s(pair.anchor))
        anchor_confidence = pair.anchor.mean_confidence or 0.0
        confidence_distance = abs((candidate.mean_confidence or 0.0) - anchor_confidence)
        hard_negatives.append(
            HardNegativeChoice(
                track=candidate,
                time_distance_s=time_distance,
                duration_distance_s=duration_distance,
                confidence_distance=confidence_distance,
            )
        )

    hard_negatives.sort(key=lambda item: item.ranking_tuple)
    return hard_negatives[:negatives_per_positive]


def collect_track_frame_refs(
    *,
    minio_client: Any,
    bucket: str,
    track_times: dict[str, datetime],
) -> dict[str, TrackFrameRef]:
    if not track_times:
        return {}

    earliest = min(track_times.values()) - timedelta(hours=24)
    latest = max(track_times.values()) + timedelta(hours=24)
    track_ids = set(track_times)
    frame_refs: dict[str, TrackFrameRef] = {}

    for obj in minio_client.list_objects(bucket, recursive=True):
        object_name = str(getattr(obj, "object_name", ""))
        if not object_name.endswith(".json"):
            continue

        last_modified = bucket_object_last_modified(obj)
        if last_modified is not None and last_modified < earliest:
            continue
        if last_modified is not None and last_modified > latest:
            continue

        try:
            payload = load_trace_payload(minio_client, bucket, object_name)
        except Exception as exc:
            LOG.warning("skipping unreadable debug trace %s: %s", object_name, exc)
            continue

        frame_uri = str(payload.get("frame_uri", "")).strip()
        if not frame_uri:
            continue
        timestamp = parse_trace_timestamp(payload) or last_modified
        if timestamp is None:
            continue
        timestamp = timestamp.astimezone(UTC)
        if timestamp < earliest or timestamp > latest:
            continue

        trace_id = str(payload.get("trace_id", "")).strip()
        trace_track_ids = {
            str(track_id)
            for track_id in payload.get("track_ids", [])
            if track_id not in (None, "")
        }
        relevant_track_ids = track_ids & trace_track_ids
        if not relevant_track_ids:
            continue

        for track_id in relevant_track_ids:
            current = frame_refs.get(track_id)
            distance = abs((timestamp - track_times[track_id]).total_seconds())
            if current is None:
                frame_refs[track_id] = TrackFrameRef(
                    trace_id=trace_id or object_name,
                    frame_uri=frame_uri,
                    timestamp=timestamp,
                )
                continue
            current_distance = abs((current.timestamp - track_times[track_id]).total_seconds())
            if distance < current_distance:
                frame_refs[track_id] = TrackFrameRef(
                    trace_id=trace_id or object_name,
                    frame_uri=frame_uri,
                    timestamp=timestamp,
                )

    return frame_refs


def download_frame_bytes(minio_client: Any, frame_uri: str) -> bytes:
    parsed = frame_uri.removeprefix("s3://")
    if "/" not in parsed:
        raise RuntimeError(f"unsupported frame URI {frame_uri!r}; expected s3://bucket/key")
    bucket, object_name = parsed.split("/", 1)
    response = minio_client.get_object(bucket, object_name)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def build_track_assets(
    *,
    output_dir: Path,
    tracks_by_id: dict[str, TrackLinkRecord | LocalTrackRecord],
    detections_by_track: dict[str, DetectionSnapshot],
    frame_refs_by_track: dict[str, TrackFrameRef],
    minio_client: Any,
) -> dict[str, dict[str, Any]]:
    image_module = require_pillow_image()
    frames_dir = output_path(output_dir, "frames")
    crops_dir = output_path(output_dir, "crops")
    frame_cache: dict[str, Path] = {}
    track_assets: dict[str, dict[str, Any]] = {}

    for track_id, track in sorted(tracks_by_id.items()):
        detection = detections_by_track.get(track_id)
        frame_ref = frame_refs_by_track.get(track_id)
        asset: dict[str, Any] = {
            "representative_time": None,
            "representative_bbox_xywh": None,
            "representative_bbox_xyxy": None,
            "representative_detection_confidence": None,
            "model_version": None,
            "frame_uri": None,
            "frame_path": None,
            "crop_uri": None,
            "crop_path": None,
            "trace_id": None,
            "trace_timestamp": None,
        }
        if detection is not None:
            asset["representative_time"] = detection.time.isoformat()
            asset["representative_bbox_xywh"] = [
                round(value, 4) for value in detection.bbox_xywh
            ]
            asset["representative_bbox_xyxy"] = [
                round(value, 4) for value in detection.bbox_xyxy
            ]
            asset["representative_detection_confidence"] = round(detection.confidence, 6)
            asset["model_version"] = detection.model_version
        if frame_ref is not None:
            asset["frame_uri"] = frame_ref.frame_uri
            asset["trace_id"] = frame_ref.trace_id
            asset["trace_timestamp"] = frame_ref.timestamp.isoformat()

        if detection is None or frame_ref is None:
            track_assets[track_id] = asset
            continue

        saved_frame = frame_cache.get(frame_ref.frame_uri)
        if saved_frame is None:
            suffix = Path(frame_ref.frame_uri).suffix or ".jpg"
            safe_name = (
                f"{sanitize_fragment(track.camera_id)}-"
                f"{sanitize_fragment(track_id)}-{sanitize_fragment(frame_ref.trace_id)}{suffix}"
            )
            frame_path = output_path(
                frames_dir,
                sanitize_fragment(track.camera_id),
                track.reference_time.strftime("%Y-%m-%d"),
                safe_name,
            )
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                frame_path.write_bytes(download_frame_bytes(minio_client, frame_ref.frame_uri))
            except Exception as exc:
                LOG.warning("unable to download frame for track %s: %s", track_id, exc)
                track_assets[track_id] = asset
                continue
            frame_cache[frame_ref.frame_uri] = frame_path
            saved_frame = frame_path

        asset["frame_path"] = str(saved_frame.resolve())
        try:
            with image_module.open(saved_frame) as image:
                x1, y1, x2, y2 = detection.bbox_xyxy
                width, height = image.size
                left = max(0, min(int(round(x1)), width))
                top = max(0, min(int(round(y1)), height))
                right = max(left + 1, min(int(round(x2)), width))
                bottom = max(top + 1, min(int(round(y2)), height))
                crop = image.crop((left, top, right, bottom))
                crop_path = output_path(
                    crops_dir,
                    sanitize_fragment(track.object_class),
                    f"{sanitize_fragment(track_id)}.jpg",
                )
                crop_path.parent.mkdir(parents=True, exist_ok=True)
                crop.save(crop_path, format="JPEG", quality=95)
                asset["crop_path"] = str(crop_path.resolve())
                asset["crop_uri"] = crop_path.resolve().as_uri()
        except Exception as exc:
            LOG.warning("unable to crop track %s from %s: %s", track_id, saved_frame, exc)

        track_assets[track_id] = asset

    return track_assets


def endpoint_payload(
    track: TrackLinkRecord | LocalTrackRecord,
    *,
    asset: dict[str, Any] | None,
    negative_choice: HardNegativeChoice | None = None,
) -> dict[str, Any]:
    payload = {
        "local_track_id": track.local_track_id,
        "global_track_id": track.global_track_id,
        "camera_id": track.camera_id,
        "object_class": track.object_class,
        "start_time": track.start_time.isoformat(),
        "end_time": time_or_none(track.end_time),
        "reference_time": track.reference_time.isoformat(),
        "mean_confidence": round(track.mean_confidence, 6) if track.mean_confidence is not None else None,
        "link_confidence": round(track.link_confidence, 6) if getattr(track, "link_confidence", None) is not None else None,
    }
    if asset:
        payload.update(asset)
    if negative_choice is not None:
        payload["hard_negative_time_distance_s"] = round(negative_choice.time_distance_s, 4)
        payload["hard_negative_duration_distance_s"] = round(negative_choice.duration_distance_s, 4)
        payload["hard_negative_confidence_distance"] = round(negative_choice.confidence_distance, 6)
    return payload


def build_triplet_payloads(
    *,
    directed_pairs: list[DirectedPositivePair],
    negative_index: dict[tuple[str, str], tuple[list[float], list[LocalTrackRecord]]],
    negative_window: timedelta,
    negatives_per_positive: int,
    max_triplets: int,
    assets_by_track: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    triplets: list[dict[str, Any]] = []
    counters = {
        "directed_pairs_scanned": len(directed_pairs),
        "directed_pairs_with_negatives": 0,
        "skipped_missing_assets": 0,
        "negative_candidates_used": 0,
    }

    for pair in directed_pairs:
        negatives = mine_negatives_for_anchor(
            pair,
            negative_index=negative_index,
            negative_window=negative_window,
            negatives_per_positive=negatives_per_positive,
        )
        if not negatives:
            continue
        counters["directed_pairs_with_negatives"] += 1

        for rank, negative in enumerate(negatives, start=1):
            anchor_asset = assets_by_track.get(pair.anchor.local_track_id)
            positive_asset = assets_by_track.get(pair.positive.local_track_id)
            negative_asset = assets_by_track.get(negative.track.local_track_id)
            if not anchor_asset or not positive_asset or not negative_asset:
                counters["skipped_missing_assets"] += 1
                continue

            triplet_id = hashlib.sha1(
                (
                    f"{pair.anchor.local_track_id}:{pair.positive.local_track_id}:"
                    f"{negative.track.local_track_id}:{rank}"
                ).encode("utf-8")
            ).hexdigest()[:16]
            positive_pair_id = f"{triplet_id}-positive"
            negative_pair_id = f"{triplet_id}-negative"
            triplets.append(
                {
                    "triplet_id": triplet_id,
                    "object_class": pair.object_class,
                    "positive_link_confidence": round(pair.positive_link_confidence, 6),
                    "negative_rank": rank,
                    "validation_pair_ids": {
                        "positive": positive_pair_id,
                        "negative": negative_pair_id,
                    },
                    "anchor": endpoint_payload(pair.anchor, asset=anchor_asset),
                    "positive": endpoint_payload(pair.positive, asset=positive_asset),
                    "negative": endpoint_payload(
                        negative.track,
                        asset=negative_asset,
                        negative_choice=negative,
                    ),
                }
            )
            counters["negative_candidates_used"] += 1
            if len(triplets) >= max_triplets:
                return triplets, counters

    return triplets, counters


def summarize_triplets_by_class(triplets: list[dict[str, Any]]) -> dict[str, int]:
    counts = {object_class: 0 for object_class in OBJECT_CLASSES}
    for triplet in triplets:
        counts[str(triplet["object_class"])] += 1
    return counts


def summarize_asset_coverage(assets_by_track: dict[str, dict[str, Any]]) -> dict[str, int]:
    summary = {
        "tracks_total": len(assets_by_track),
        "tracks_with_frame_path": 0,
        "tracks_with_crop_path": 0,
        "tracks_missing_frame_uri": 0,
    }
    for asset in assets_by_track.values():
        if asset.get("frame_path"):
            summary["tracks_with_frame_path"] += 1
        if asset.get("crop_path"):
            summary["tracks_with_crop_path"] += 1
        if not asset.get("frame_uri"):
            summary["tracks_missing_frame_uri"] += 1
    return summary


async def main_async(args: argparse.Namespace) -> None:
    if not args.db_dsn:
        raise RuntimeError("database DSN is required via --db-dsn or DATABASE_URL")
    if args.min_confidence <= 0.0 or args.min_confidence > 1.0:
        raise RuntimeError("--min-confidence must be in the interval (0, 1]")
    if args.negatives_per_positive <= 0:
        raise RuntimeError("--negatives-per-positive must be > 0")
    if args.max_triplets <= 0:
        raise RuntimeError("--max-triplets must be > 0")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    positive_tracks = await query_positive_tracks(args.db_dsn, min_confidence=args.min_confidence)
    if not positive_tracks:
        raise RuntimeError("no MTMC links met the requested confidence threshold")

    directed_pairs = build_directed_pairs(positive_tracks)
    if not directed_pairs:
        raise RuntimeError("no cross-camera MTMC associations satisfied the positive-pair criteria")

    negative_window = timedelta(hours=args.negative_window_hours)
    camera_ids = {pair.anchor.camera_id for pair in directed_pairs}
    object_classes = {pair.anchor.object_class for pair in directed_pairs}
    earliest = min(pair.anchor.reference_time for pair in directed_pairs) - negative_window
    latest = max(pair.anchor.reference_time for pair in directed_pairs) + negative_window
    candidate_negatives = await query_negative_candidates(
        args.db_dsn,
        camera_ids=camera_ids,
        object_classes=object_classes,
        window_start=earliest,
        window_end=latest,
    )
    negative_index = index_negative_candidates(candidate_negatives)

    track_ids_for_assets: set[str] = set()
    for pair in directed_pairs:
        track_ids_for_assets.add(pair.anchor.local_track_id)
        track_ids_for_assets.add(pair.positive.local_track_id)
        negatives = mine_negatives_for_anchor(
            pair,
            negative_index=negative_index,
            negative_window=negative_window,
            negatives_per_positive=args.negatives_per_positive,
        )
        for negative in negatives:
            track_ids_for_assets.add(negative.track.local_track_id)

    representative_detections = await query_representative_detections(
        args.db_dsn,
        track_ids=track_ids_for_assets,
    )

    tracks_by_id: dict[str, TrackLinkRecord | LocalTrackRecord] = {}
    for pair in directed_pairs:
        tracks_by_id[pair.anchor.local_track_id] = pair.anchor
        tracks_by_id[pair.positive.local_track_id] = pair.positive
    for track in candidate_negatives:
        if track.local_track_id in track_ids_for_assets:
            tracks_by_id[track.local_track_id] = track

    minio_client = build_minio_client(args)
    track_frame_refs = collect_track_frame_refs(
        minio_client=minio_client,
        bucket=args.debug_bucket,
        track_times={track_id: track.reference_time for track_id, track in tracks_by_id.items()},
    )
    assets_by_track = build_track_assets(
        output_dir=output_dir,
        tracks_by_id=tracks_by_id,
        detections_by_track=representative_detections,
        frame_refs_by_track=track_frame_refs,
        minio_client=minio_client,
    )
    triplets, mining_counters = build_triplet_payloads(
        directed_pairs=directed_pairs,
        negative_index=negative_index,
        negative_window=negative_window,
        negatives_per_positive=args.negatives_per_positive,
        max_triplets=args.max_triplets,
        assets_by_track=assets_by_track,
    )
    if not triplets:
        raise RuntimeError(
            "no triplets could be materialized; representative debug-trace-backed frame assets are missing"
        )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "mtmc-high-confidence-links",
        "filters": {
            "min_confidence": args.min_confidence,
            "negative_window_hours": args.negative_window_hours,
            "negatives_per_positive": args.negatives_per_positive,
            "max_triplets": args.max_triplets,
            "debug_bucket": args.debug_bucket,
        },
        "source_summary": {
            "positive_tracks": len(positive_tracks),
            "directed_pairs": len(directed_pairs),
            "negative_candidates": len(candidate_negatives),
            "representative_detections": len(representative_detections),
            "debug_trace_frame_refs": len(track_frame_refs),
            **mining_counters,
            **summarize_asset_coverage(assets_by_track),
        },
        "triplet_counts_by_class": summarize_triplets_by_class(triplets),
        "triplets": triplets,
    }
    manifest_path = output_path(output_dir, args.manifest_name)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "triplet_count": len(triplets),
                "triplet_counts_by_class": payload["triplet_counts_by_class"],
            },
            indent=2,
        )
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
