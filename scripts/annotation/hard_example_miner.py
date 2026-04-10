#!/usr/bin/env python3
"""Mine hard examples from TimescaleDB detections and MinIO debug traces.

Usage:
    python hard_example_miner.py --db-dsn postgresql://localhost:5432/vidanalytics \
        --minio-url http://localhost:9000 --output-dir data/hard-examples \
        --samples-per-class 50

The script queries recent low-confidence detections from TimescaleDB and
recent debug traces from MinIO, then exports a capped, stratified manifest of
examples that can be sent to CVAT for review.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import hashlib
import importlib
import json
import logging
import os
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


LOG = logging.getLogger("hard_example_miner")

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
class DetectionRecord:
    time: datetime
    camera_id: str
    object_class: str
    confidence: float
    bbox_xywh: tuple[float, float, float, float]
    local_track_id: str | None
    model_version: str


@dataclass
class CandidateExample:
    example_id: str
    trace_id: str
    frame_id: str
    camera_id: str
    timestamp: datetime
    object_class: str
    confidence: float
    prediction_bbox_xyxy: tuple[float, float, float, float]
    prediction_bbox_xywh: tuple[float, float, float, float]
    selection_reason: str
    frame_uri: str
    model_version: str | None
    local_track_id: str | None
    trace_labels: dict[str, str]
    trace_reason: str
    shadow_score: float | None = None
    frame_path: str | None = None
    crop_path: str | None = None

    @property
    def date_str(self) -> str:
        return self.timestamp.astimezone(UTC).strftime("%Y-%m-%d")

    @property
    def selection_score(self) -> float:
        if self.selection_reason == "shadow_disagreement" and self.shadow_score is not None:
            return self.shadow_score
        return 1.0 - self.confidence

    def to_json(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "trace_id": self.trace_id,
            "frame_id": self.frame_id,
            "camera_id": self.camera_id,
            "timestamp": self.timestamp.isoformat(),
            "date": self.date_str,
            "object_class": self.object_class,
            "confidence": round(self.confidence, 6),
            "prediction_bbox_xyxy": [round(value, 4) for value in self.prediction_bbox_xyxy],
            "prediction_bbox_xywh": [round(value, 4) for value in self.prediction_bbox_xywh],
            "selection_reason": self.selection_reason,
            "frame_uri": self.frame_uri,
            "frame_path": self.frame_path,
            "crop_path": self.crop_path,
            "model_version": self.model_version,
            "local_track_id": self.local_track_id,
            "trace_reason": self.trace_reason,
            "trace_labels": self.trace_labels,
            "shadow_score": self.shadow_score,
        }


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(
            f"missing optional dependency '{module_name}'; install {install_hint}"
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-dsn",
        default=os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN"),
        help="PostgreSQL DSN.",
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
        "--output-dir",
        type=Path,
        default=Path("data/hard-examples"),
        help="Directory receiving frames, crops, and manifest.json.",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.50,
        help="Mine detections below this confidence threshold.",
    )
    parser.add_argument(
        "--samples-per-class",
        type=int,
        default=50,
        help="Maximum examples to keep per class per day.",
    )
    parser.add_argument(
        "--daily-limit",
        type=int,
        default=500,
        help="Hard cap on the total number of exported examples.",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=24,
        help="Look-back window for detections and traces.",
    )
    parser.add_argument(
        "--include-shadow-disagreements",
        action="store_true",
        help="Include traces flagged with shadow disagreement hints when present.",
    )
    parser.add_argument(
        "--manifest-name",
        default="manifest.json",
        help="Output manifest filename inside --output-dir.",
    )
    return parser.parse_args()


def parse_minio_endpoint(raw_url: str) -> tuple[str, bool]:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            f"invalid --minio-url {raw_url!r}; expected http://host:port or https://host:port"
        )
    return parsed.netloc, parsed.scheme == "https"


def build_minio_client(args: argparse.Namespace) -> Any:
    minio_module = require_module("minio", "pip install minio")
    endpoint, secure = parse_minio_endpoint(args.minio_url)
    return minio_module.Minio(
        endpoint,
        access_key=args.minio_access_key,
        secret_key=args.minio_secret_key,
        secure=secure,
    )


def parse_trace_timestamp(payload: dict[str, Any]) -> datetime | None:
    for key in ("source_capture_ts", "edge_receive_ts", "core_ingest_ts"):
        raw_value = payload.get(key)
        if raw_value in (None, "", 0, 0.0):
            continue
        try:
            return datetime.fromtimestamp(float(raw_value), tz=UTC)
        except (TypeError, ValueError, OSError):
            continue
    return None


def xyxy_to_xywh(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = box
    return (x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1))


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise RuntimeError(f"unsupported frame URI {uri!r}; expected s3://bucket/key")
    return parsed.netloc, parsed.path.lstrip("/")


async def query_low_confidence_detections(
    dsn: str,
    *,
    window_start: datetime,
    confidence_threshold: float,
) -> list[DetectionRecord]:
    asyncpg = require_module("asyncpg", "pip install asyncpg")
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT time,
                   camera_id,
                   object_class,
                   confidence,
                   bbox_x,
                   bbox_y,
                   bbox_w,
                   bbox_h,
                   local_track_id,
                   model_version
            FROM detections
            WHERE time >= $1
              AND confidence < $2
            ORDER BY time DESC
            """,
            window_start,
            confidence_threshold,
        )
    finally:
        await conn.close()

    return [
        DetectionRecord(
            time=row["time"],
            camera_id=str(row["camera_id"]),
            object_class=str(row["object_class"]),
            confidence=float(row["confidence"]),
            bbox_xywh=(
                float(row["bbox_x"]),
                float(row["bbox_y"]),
                float(row["bbox_w"]),
                float(row["bbox_h"]),
            ),
            local_track_id=(
                str(row["local_track_id"]) if row["local_track_id"] is not None else None
            ),
            model_version=str(row["model_version"]),
        )
        for row in rows
    ]


def bucket_object_last_modified(obj: Any) -> datetime | None:
    raw_value = getattr(obj, "last_modified", None)
    if isinstance(raw_value, datetime):
        if raw_value.tzinfo is None:
            return raw_value.replace(tzinfo=UTC)
        return raw_value.astimezone(UTC)
    return None


def load_trace_payload(minio_client: Any, bucket: str, object_name: str) -> dict[str, Any]:
    response = minio_client.get_object(bucket, object_name)
    try:
        raw_bytes = response.read()
    finally:
        response.close()
        response.release_conn()
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"trace object {object_name!r} is not a JSON object")
    return payload


def extract_shadow_score(payload: dict[str, Any]) -> float | None:
    candidates = [
        payload.get("shadow_disagreement_score"),
        payload.get("disagreement_score"),
        (payload.get("labels") or {}).get("shadow_disagreement_score"),
        (payload.get("labels") or {}).get("disagreement_score"),
    ]
    for raw_value in candidates:
        try:
            if raw_value is None or raw_value == "":
                continue
            return float(raw_value)
        except (TypeError, ValueError):
            continue
    return None


def has_shadow_disagreement(payload: dict[str, Any]) -> bool:
    labels = payload.get("labels")
    label_dict = labels if isinstance(labels, dict) else {}
    for raw_value in (
        payload.get("shadow_disagreement"),
        label_dict.get("shadow_disagreement"),
    ):
        if isinstance(raw_value, bool) and raw_value:
            return True
        if isinstance(raw_value, str) and raw_value.strip().lower() in {"1", "true", "yes"}:
            return True
    score = extract_shadow_score(payload)
    return score is not None and score >= 0.15


def collect_trace_candidates(
    *,
    minio_client: Any,
    bucket: str,
    window_start: datetime,
    confidence_threshold: float,
    include_shadow_disagreements: bool,
) -> tuple[list[CandidateExample], int]:
    candidates: list[CandidateExample] = []
    traces_scanned = 0
    seen_keys: set[tuple[str, str, str, str, float, float, float, float]] = set()

    for obj in minio_client.list_objects(bucket, recursive=True):
        object_name = str(getattr(obj, "object_name", ""))
        if not object_name.endswith(".json"):
            continue
        last_modified = bucket_object_last_modified(obj)
        if last_modified is not None and last_modified < window_start:
            continue

        try:
            payload = load_trace_payload(minio_client, bucket, object_name)
        except Exception as exc:
            LOG.warning("skipping unreadable trace %s: %s", object_name, exc)
            continue

        timestamp = parse_trace_timestamp(payload)
        if timestamp is not None and timestamp < window_start:
            continue

        traces_scanned += 1
        trace_id = str(payload.get("trace_id", "")).strip()
        camera_id = str(payload.get("camera_id", "")).strip()
        frame_id = str(payload.get("frame_id", "")).strip()
        frame_uri = str(payload.get("frame_uri", "")).strip()
        if not trace_id or not camera_id or not frame_uri:
            continue

        detections = payload.get("detections", [])
        if not isinstance(detections, list):
            continue

        labels = payload.get("labels")
        trace_labels = {
            str(key): str(value)
            for key, value in (labels.items() if isinstance(labels, dict) else [])
        }
        model_versions = payload.get("model_versions")
        model_version = None
        if isinstance(model_versions, dict):
            detector_version = model_versions.get("detector")
            if detector_version is not None:
                model_version = str(detector_version)

        disagreement = include_shadow_disagreements and has_shadow_disagreement(payload)
        shadow_score = extract_shadow_score(payload) if disagreement else None
        candidate_timestamp = timestamp or bucket_object_last_modified(obj) or datetime.now(UTC)

        for index, detection in enumerate(detections):
            if not isinstance(detection, dict):
                continue
            object_class = str(detection.get("class", "")).strip()
            if object_class not in OBJECT_CLASSES:
                continue
            try:
                confidence = float(detection.get("confidence"))
            except (TypeError, ValueError):
                continue
            raw_bbox = detection.get("bbox")
            if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
                continue
            try:
                bbox_xyxy = tuple(float(value) for value in raw_bbox)
            except (TypeError, ValueError):
                continue

            reason = None
            if confidence < confidence_threshold:
                reason = "low_confidence"
            elif disagreement:
                reason = "shadow_disagreement"
            if reason is None:
                continue

            dedupe_key = (
                trace_id,
                camera_id,
                object_class,
                frame_uri,
                round(confidence, 6),
                round(bbox_xyxy[0], 2),
                round(bbox_xyxy[1], 2),
                round(bbox_xyxy[2], 2),
                round(bbox_xyxy[3], 2),
            )
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            example_id = hashlib.sha1(
                f"{trace_id}:{index}:{object_class}:{confidence:.6f}".encode("utf-8")
            ).hexdigest()[:16]
            candidates.append(
                CandidateExample(
                    example_id=example_id,
                    trace_id=trace_id,
                    frame_id=frame_id or trace_id,
                    camera_id=camera_id,
                    timestamp=candidate_timestamp,
                    object_class=object_class,
                    confidence=confidence,
                    prediction_bbox_xyxy=bbox_xyxy,
                    prediction_bbox_xywh=xyxy_to_xywh(bbox_xyxy),
                    selection_reason=reason,
                    frame_uri=frame_uri,
                    model_version=model_version,
                    local_track_id=None,
                    trace_labels=trace_labels,
                    trace_reason=str(payload.get("reason", "")),
                    shadow_score=shadow_score,
                )
            )

    return candidates, traces_scanned


def build_detection_index(
    detections: list[DetectionRecord],
) -> dict[tuple[str, str], tuple[list[float], list[DetectionRecord]]]:
    grouped: dict[tuple[str, str], list[DetectionRecord]] = defaultdict(list)
    for detection in detections:
        grouped[(detection.camera_id, detection.object_class)].append(detection)

    indexed: dict[tuple[str, str], tuple[list[float], list[DetectionRecord]]] = {}
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda item: item.time)
        indexed[key] = ([item.time.timestamp() for item in ordered], ordered)
    return indexed


def attach_db_matches(
    candidates: list[CandidateExample],
    indexed_detections: dict[tuple[str, str], tuple[list[float], list[DetectionRecord]]],
) -> int:
    matches = 0
    for candidate in candidates:
        index_key = (candidate.camera_id, candidate.object_class)
        detection_index = indexed_detections.get(index_key)
        if detection_index is None:
            continue
        times, rows = detection_index
        position = bisect.bisect_left(times, candidate.timestamp.timestamp())
        best_match: DetectionRecord | None = None
        best_distance = 10.0
        for neighbor in range(max(0, position - 3), min(len(rows), position + 4)):
            row = rows[neighbor]
            distance = abs((row.time - candidate.timestamp).total_seconds())
            confidence_delta = abs(row.confidence - candidate.confidence)
            if distance <= 10.0 and confidence_delta <= 0.20 and distance < best_distance:
                best_match = row
                best_distance = distance
        if best_match is None:
            continue
        candidate.local_track_id = best_match.local_track_id
        if not candidate.model_version:
            candidate.model_version = best_match.model_version
        matches += 1
    return matches


def select_examples(
    candidates: list[CandidateExample],
    *,
    samples_per_class: int,
    daily_limit: int,
) -> list[CandidateExample]:
    grouped: dict[tuple[str, str], list[CandidateExample]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.date_str, candidate.object_class)].append(candidate)

    selected: list[CandidateExample] = []
    for key in sorted(grouped):
        bucket = sorted(
            grouped[key],
            key=lambda item: (-item.selection_score, item.timestamp, item.example_id),
        )
        selected.extend(bucket[:samples_per_class])

    selected.sort(key=lambda item: (-item.selection_score, item.timestamp, item.example_id))
    return selected[:daily_limit]


def download_frame_bytes(minio_client: Any, frame_uri: str) -> bytes:
    bucket, object_name = parse_s3_uri(frame_uri)
    response = minio_client.get_object(bucket, object_name)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def sanitize_fragment(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in value)


def save_assets(
    candidates: list[CandidateExample],
    *,
    minio_client: Any,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"
    crops_dir = output_dir / "crops"

    pil_image = None
    try:
        pil_image = require_module("PIL.Image", "pip install Pillow")
    except RuntimeError:
        LOG.warning("Pillow not installed; crop export will be skipped")

    frame_cache: dict[str, Path] = {}
    for candidate in candidates:
        if candidate.frame_uri not in frame_cache:
            try:
                frame_bytes = download_frame_bytes(minio_client, candidate.frame_uri)
            except Exception as exc:
                LOG.warning("unable to download frame %s: %s", candidate.frame_uri, exc)
                continue

            parsed = urllib.parse.urlparse(candidate.frame_uri)
            suffix = Path(parsed.path).suffix or ".jpg"
            frame_name = f"{sanitize_fragment(candidate.frame_id or candidate.trace_id)}-{candidate.example_id}{suffix}"
            frame_path = frames_dir / sanitize_fragment(candidate.camera_id) / candidate.date_str / frame_name
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(frame_bytes)
            frame_cache[candidate.frame_uri] = frame_path

            if pil_image is not None:
                try:
                    with pil_image.open(frame_path) as image:
                        _ = image.size
                except Exception as exc:
                    LOG.warning("unable to inspect frame %s: %s", frame_path, exc)

        saved_frame = frame_cache.get(candidate.frame_uri)
        if saved_frame is None:
            continue
        candidate.frame_path = str(saved_frame)

        if pil_image is None:
            continue
        try:
            with pil_image.open(saved_frame) as image:
                x1, y1, x2, y2 = candidate.prediction_bbox_xyxy
                width, height = image.size
                left = max(0, min(int(round(x1)), width))
                top = max(0, min(int(round(y1)), height))
                right = max(left + 1, min(int(round(x2)), width))
                bottom = max(top + 1, min(int(round(y2)), height))
                crop = image.crop((left, top, right, bottom))
                crop_path = (
                    crops_dir
                    / sanitize_fragment(candidate.object_class)
                    / f"{candidate.example_id}.jpg"
                )
                crop_path.parent.mkdir(parents=True, exist_ok=True)
                crop.save(crop_path, format="JPEG", quality=95)
                candidate.crop_path = str(crop_path)
        except Exception as exc:
            LOG.warning("unable to create crop for %s: %s", saved_frame, exc)


def summarize_by_class(candidates: list[CandidateExample]) -> dict[str, int]:
    counts: dict[str, int] = {object_class: 0 for object_class in OBJECT_CLASSES}
    for candidate in candidates:
        counts[candidate.object_class] = counts.get(candidate.object_class, 0) + 1
    return counts


def summarize_by_day_and_class(candidates: list[CandidateExample]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for candidate in candidates:
        summary[candidate.date_str][candidate.object_class] += 1
    return {
        date_key: dict(sorted(class_counts.items()))
        for date_key, class_counts in sorted(summary.items())
    }


def write_manifest(
    *,
    args: argparse.Namespace,
    detections: list[DetectionRecord],
    trace_candidate_count: int,
    selected_examples: list[CandidateExample],
    traces_scanned: int,
    db_matches: int,
    output_dir: Path,
) -> Path:
    manifest_path = output_dir / args.manifest_name
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_hours": args.window_hours,
        "confidence_threshold": args.confidence_threshold,
        "samples_per_class": args.samples_per_class,
        "daily_limit": args.daily_limit,
        "include_shadow_disagreements": args.include_shadow_disagreements,
        "source_summary": {
            "db_low_confidence_count": len(detections),
            "debug_traces_scanned": traces_scanned,
            "trace_candidate_count": trace_candidate_count,
            "trace_to_db_matches": db_matches,
        },
        "selected_summary": {
            "example_count": len(selected_examples),
            "per_class_counts": summarize_by_class(selected_examples),
            "per_day_per_class_counts": summarize_by_day_and_class(selected_examples),
        },
        "examples": [candidate.to_json() for candidate in selected_examples],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


async def async_main(args: argparse.Namespace) -> Path:
    if not args.db_dsn:
        raise RuntimeError("--db-dsn is required (or set DATABASE_URL / DB_DSN)")

    window_start = datetime.now(UTC) - timedelta(hours=args.window_hours)
    detections = await query_low_confidence_detections(
        args.db_dsn,
        window_start=window_start,
        confidence_threshold=args.confidence_threshold,
    )
    minio_client = build_minio_client(args)
    trace_candidates, traces_scanned = collect_trace_candidates(
        minio_client=minio_client,
        bucket=args.debug_bucket,
        window_start=window_start,
        confidence_threshold=args.confidence_threshold,
        include_shadow_disagreements=args.include_shadow_disagreements,
    )
    indexed_detections = build_detection_index(detections)
    db_matches = attach_db_matches(trace_candidates, indexed_detections)
    selected = select_examples(
        trace_candidates,
        samples_per_class=args.samples_per_class,
        daily_limit=args.daily_limit,
    )
    save_assets(selected, minio_client=minio_client, output_dir=args.output_dir)
    selected = [candidate for candidate in selected if candidate.frame_path is not None]
    return write_manifest(
        args=args,
        detections=detections,
        trace_candidate_count=len(trace_candidates),
        selected_examples=selected,
        traces_scanned=traces_scanned,
        db_matches=db_matches,
        output_dir=args.output_dir,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    manifest_path = asyncio.run(async_main(args))
    print(json.dumps({"manifest_path": str(manifest_path)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
