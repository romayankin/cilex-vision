#!/usr/bin/env python3
"""Run the tracker bake-off: MOT-format eval -> MLflow.

The script is intentionally strict about missing prerequisites:

- if the evaluation manifest is absent or empty, it exits with a clear error
- if MOT ground-truth files are missing, it exits with a clear error
- if tracker detections or predictions are missing, it exits with a clear error
- if optional Python dependencies are unavailable at runtime, it exits with a
  clear install hint

The repository currently does not contain evaluation data, so this script is
expected to fail fast until `data/eval/mot/` is populated by later tasks.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SAFE_DEFAULT_TRACKER = "bytetrack"
DEFAULT_DETECTOR = "yolov8l"
REPO_ROOT = Path(__file__).resolve().parents[2]
INFERENCE_WORKER_DIR = REPO_ROOT / "services" / "inference-worker"


@dataclass(frozen=True)
class DatasetMetadata:
    manifest_path: Path
    split_identifiers: tuple[str, ...]
    dataset_revision: str | None
    detector_name: str


@dataclass(frozen=True)
class SequenceRecord:
    sequence_id: str
    camera_id: str
    sequence_dir: Path
    gt_path: Path
    detections_path: Path | None
    image_width: int
    image_height: int
    frame_rate: float
    prediction_files: dict[str, Path]


@dataclass(frozen=True)
class DetectionInput:
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    confidence: float
    class_index: int


@dataclass(frozen=True)
class DetectionFrame:
    frame_index: int
    timestamp_sec: float | None
    detections: tuple[DetectionInput, ...]


@dataclass(frozen=True)
class MOTRow:
    frame_index: int
    track_id: str
    bbox_xywh: tuple[float, float, float, float]
    confidence: float
    class_id: int
    visibility: float | None = None


@dataclass(frozen=True)
class TrackerMetrics:
    mota: float
    idf1: float
    id_switches: int
    fragmentation: int
    mostly_tracked_pct: float
    mostly_lost_pct: float
    throughput_fps: float
    evaluated_sequences: int


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(f"missing optional dependency '{module_name}'; install {install_hint}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tracker",
        required=True,
        choices=("bytetrack", "botsort"),
        help="Tracker candidate to evaluate.",
    )
    parser.add_argument(
        "--detector",
        default=DEFAULT_DETECTOR,
        help="Detector winner whose outputs feed the tracker bake-off.",
    )
    parser.add_argument(
        "--dataset-root",
        "--gt",
        dest="dataset_root",
        type=Path,
        default=Path("data/eval/mot"),
        help="Root directory for the MOT evaluation dataset.",
    )
    parser.add_argument(
        "--dataset-manifest",
        type=Path,
        help="Manifest describing MOT sequences, GT files, and detector outputs.",
    )
    parser.add_argument(
        "--tracking-uri",
        default="http://127.0.0.1:5000",
        help="MLflow tracking URI.",
    )
    parser.add_argument(
        "--mlflow-experiment",
        "--experiment",
        dest="mlflow_experiment",
        default="tracker-bakeoff",
        help="MLflow experiment name.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/bakeoff/tracker"),
        help="Directory for prediction outputs and MLflow artifacts.",
    )
    parser.add_argument(
        "--predictions-root",
        type=Path,
        help="Directory with precomputed MOT prediction files. Useful for BoT-SORT or offline re-eval.",
    )
    parser.add_argument(
        "--external-tracker-command",
        help=(
            "Optional shell command template for trackers not implemented locally. "
            "The template may use {tracker}, {detector}, {dataset_root}, {dataset_manifest}, "
            "{output_dir}, and {predictions_dir}."
        ),
    )
    parser.add_argument(
        "--iou-threshold",
        type=float,
        default=0.5,
        help="IoU threshold used for MOT matching.",
    )
    parser.add_argument(
        "--track-thresh",
        type=float,
        default=0.5,
        help="ByteTrack high-confidence threshold.",
    )
    parser.add_argument(
        "--match-thresh",
        type=float,
        default=0.8,
        help="ByteTrack first-pass IoU threshold.",
    )
    parser.add_argument(
        "--second-match-thresh",
        type=float,
        default=0.5,
        help="ByteTrack second-pass IoU threshold.",
    )
    parser.add_argument(
        "--max-lost-frames",
        type=int,
        default=50,
        help="ByteTrack termination threshold for lost tracks.",
    )
    args = parser.parse_args()
    if args.dataset_manifest is None:
        args.dataset_manifest = args.dataset_root / "manifest.json"
    return args


def normalize_identifier_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int, float)):
        return (str(value),)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if item is not None)
    raise ValueError(f"manifest split identifier must be a scalar or sequence, got {type(value).__name__}")


def build_dataset_metadata(payload: dict[str, Any], manifest_path: Path, detector_name: str) -> DatasetMetadata:
    split_identifiers: list[str] = []
    for key in ("split_ids", "splits"):
        split_identifiers.extend(normalize_identifier_list(payload.get(key)))
    for key in ("split_id", "split", "dataset_split"):
        split_identifiers.extend(normalize_identifier_list(payload.get(key)))
    if not split_identifiers:
        split_identifiers.append(manifest_path.stem)

    dataset_revision = None
    for key in ("dataset_revision", "dataset_version", "revision", "version"):
        raw = payload.get(key)
        if raw is not None:
            dataset_revision = str(raw)
            break

    return DatasetMetadata(
        manifest_path=manifest_path,
        split_identifiers=tuple(dict.fromkeys(split_identifiers)),
        dataset_revision=dataset_revision,
        detector_name=detector_name,
    )


def parse_seqinfo(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def resolve_path(base_dir: Path, raw_path: str | None) -> Path | None:
    if raw_path is None:
        return None
    path = Path(raw_path)
    return path if path.is_absolute() else base_dir / path


def load_dataset(manifest_path: Path, dataset_root: Path, detector_name: str) -> tuple[list[SequenceRecord], DatasetMetadata]:
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"evaluation manifest not found: {manifest_path}. "
            "Populate data/eval/mot/ before running the bake-off."
        )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared_detector = payload.get("detector_name")
    if declared_detector is not None and str(declared_detector) != detector_name:
        raise ValueError(
            f"manifest detector_name mismatch: expected {detector_name}, found {declared_detector}"
        )

    dataset_metadata = build_dataset_metadata(payload, manifest_path, detector_name)
    raw_sequences = payload.get("sequences")
    if not isinstance(raw_sequences, list) or not raw_sequences:
        raise ValueError("manifest must contain a non-empty sequences array")

    sequences: list[SequenceRecord] = []
    for raw in raw_sequences:
        sequence_id = str(raw.get("sequence_id") or raw.get("name") or "")
        if not sequence_id:
            raise ValueError("each sequence entry must include sequence_id or name")
        sequence_dir = resolve_path(dataset_root, raw.get("sequence_dir") or sequence_id)
        if sequence_dir is None:
            raise ValueError(f"sequence_dir could not be resolved for {sequence_id}")
        seqinfo = parse_seqinfo(resolve_path(sequence_dir, raw.get("seqinfo_path") or "seqinfo.ini") or sequence_dir / "seqinfo.ini")
        gt_path = resolve_path(sequence_dir, raw.get("gt_path") or "gt/gt.txt")
        if gt_path is None or not gt_path.exists():
            raise FileNotFoundError(f"MOT ground-truth file not found for {sequence_id}: {gt_path}")

        detections_path = resolve_path(sequence_dir, raw.get("detections_path"))
        if detections_path is not None and not detections_path.exists():
            raise FileNotFoundError(f"detector outputs file not found for {sequence_id}: {detections_path}")

        width_raw = raw.get("image_width") or seqinfo.get("imWidth")
        height_raw = raw.get("image_height") or seqinfo.get("imHeight")
        if width_raw is None or height_raw is None:
            raise ValueError(f"sequence {sequence_id} is missing image_width / image_height metadata")

        fps_raw = raw.get("frame_rate") or seqinfo.get("frameRate") or seqinfo.get("fps")
        if fps_raw is None:
            raise ValueError(f"sequence {sequence_id} is missing frame_rate metadata")

        prediction_files_raw = raw.get("prediction_files") or {}
        prediction_files: dict[str, Path] = {}
        for candidate, candidate_path in prediction_files_raw.items():
            resolved = resolve_path(sequence_dir, str(candidate_path))
            if resolved is None:
                continue
            prediction_files[str(candidate)] = resolved

        sequences.append(
            SequenceRecord(
                sequence_id=sequence_id,
                camera_id=str(raw.get("camera_id") or sequence_id),
                sequence_dir=sequence_dir,
                gt_path=gt_path,
                detections_path=detections_path,
                image_width=int(width_raw),
                image_height=int(height_raw),
                frame_rate=float(fps_raw),
                prediction_files=prediction_files,
            )
        )

    return sequences, dataset_metadata


def detect_git_state() -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            ).stdout.strip()
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None, None
    return revision or None, dirty


def load_bytetrack_components() -> tuple[Any, Any, Any, Any]:
    if str(INFERENCE_WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(INFERENCE_WORKER_DIR))
    config_module = importlib.import_module("config")
    detector_client_module = importlib.import_module("detector_client")
    tracker_module = importlib.import_module("tracker")
    return (
        config_module.TrackerConfig,
        detector_client_module.RawDetection,
        tracker_module.ByteTracker,
        tracker_module.TrackState,
    )


def load_detection_frames(path: Path) -> list[DetectionFrame]:
    if not path.exists():
        raise FileNotFoundError(f"detector outputs file not found: {path}")

    if path.suffix == ".jsonl":
        raw_frames = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            raw_frames = payload.get("frames")
        else:
            raw_frames = payload
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError(f"detection frames file must contain a non-empty frames list: {path}")

    frames: list[DetectionFrame] = []
    for raw in raw_frames:
        frame_index = int(raw.get("frame_index") or raw.get("frame") or raw.get("frame_id"))
        timestamp_sec = raw.get("timestamp_sec")
        raw_detections = raw.get("detections") or []
        detections: list[DetectionInput] = []
        for raw_det in raw_detections:
            if "bbox_xyxy" in raw_det:
                bbox = raw_det["bbox_xyxy"]
            elif "bbox" in raw_det:
                bbox = raw_det["bbox"]
            else:
                bbox = [
                    raw_det["x_min"],
                    raw_det["y_min"],
                    raw_det["x_max"],
                    raw_det["y_max"],
                ]
            if len(bbox) != 4:
                raise ValueError(f"detection bbox must have 4 elements, got {bbox}")
            class_index = raw_det.get("class_index")
            if class_index is None:
                class_id = raw_det.get("class_id")
                if class_id is None:
                    raise ValueError(f"detection is missing class_index / class_id in {path}")
                class_index = int(class_id) - 1
            detections.append(
                DetectionInput(
                    x_min=float(bbox[0]),
                    y_min=float(bbox[1]),
                    x_max=float(bbox[2]),
                    y_max=float(bbox[3]),
                    confidence=float(raw_det["confidence"]),
                    class_index=int(class_index),
                )
            )
        frames.append(
            DetectionFrame(
                frame_index=frame_index,
                timestamp_sec=float(timestamp_sec) if timestamp_sec is not None else None,
                detections=tuple(detections),
            )
        )

    frames.sort(key=lambda item: item.frame_index)
    return frames


def clamp_bbox_xyxy_to_pixels(
    bbox_xyxy: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[float, float, float, float]:
    x1 = max(0.0, min(float(bbox_xyxy[0]) * width, float(width)))
    y1 = max(0.0, min(float(bbox_xyxy[1]) * height, float(height)))
    x2 = max(0.0, min(float(bbox_xyxy[2]) * width, float(width)))
    y2 = max(0.0, min(float(bbox_xyxy[3]) * height, float(height)))
    return x1, y1, x2, y2


def write_mot_predictions(path: Path, rows: list[MOTRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(
                [
                    row.frame_index,
                    row.track_id,
                    f"{row.bbox_xywh[0]:.3f}",
                    f"{row.bbox_xywh[1]:.3f}",
                    f"{row.bbox_xywh[2]:.3f}",
                    f"{row.bbox_xywh[3]:.3f}",
                    f"{row.confidence:.6f}",
                    row.class_id,
                    f"{row.visibility:.6f}" if row.visibility is not None else "-1",
                ]
            )


def run_local_bytetrack(
    sequence: SequenceRecord,
    args: argparse.Namespace,
    predictions_path: Path,
) -> tuple[list[MOTRow], float]:
    if sequence.detections_path is None:
        raise ValueError(
            f"sequence {sequence.sequence_id} is missing detections_path; "
            "ByteTrack local evaluation needs detector outputs."
        )

    frames = load_detection_frames(sequence.detections_path)
    TrackerConfig, RawDetection, ByteTracker, TrackState = load_bytetrack_components()
    tracker_cfg = TrackerConfig(
        track_thresh=args.track_thresh,
        match_thresh=args.match_thresh,
        second_match_thresh=args.second_match_thresh,
        max_lost_frames=args.max_lost_frames,
    )
    tracker = ByteTracker(sequence.camera_id, tracker_cfg)
    numeric_track_ids: dict[str, int] = {}
    next_track_id = 1
    rows: list[MOTRow] = []
    start_time = time.monotonic()

    for frame in frames:
        raw_detections = [
            RawDetection(
                x_min=det.x_min,
                y_min=det.y_min,
                x_max=det.x_max,
                y_max=det.y_max,
                confidence=det.confidence,
                class_index=det.class_index,
            )
            for det in frame.detections
        ]
        timestamp_sec = frame.timestamp_sec
        if timestamp_sec is None:
            timestamp_sec = (frame.frame_index - 1) / sequence.frame_rate
        updated_tracks, _ = tracker.update(raw_detections, timestamp_sec)
        for track in updated_tracks:
            if track.state not in (TrackState.ACTIVE, TrackState.NEW):
                continue
            track_numeric_id = numeric_track_ids.get(track.track_id)
            if track_numeric_id is None:
                track_numeric_id = next_track_id
                numeric_track_ids[track.track_id] = track_numeric_id
                next_track_id += 1
            x1, y1, x2, y2 = clamp_bbox_xyxy_to_pixels(
                (float(track.bbox[0]), float(track.bbox[1]), float(track.bbox[2]), float(track.bbox[3])),
                sequence.image_width,
                sequence.image_height,
            )
            rows.append(
                MOTRow(
                    frame_index=frame.frame_index,
                    track_id=str(track_numeric_id),
                    bbox_xywh=(x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1)),
                    confidence=float(track.confidence),
                    class_id=int(track.majority_class) + 1,
                )
            )

    elapsed = max(time.monotonic() - start_time, 1e-9)
    throughput_fps = len(frames) / elapsed
    write_mot_predictions(predictions_path, rows)
    return rows, throughput_fps


def resolve_prediction_file(sequence: SequenceRecord, tracker_name: str, predictions_root: Path | None) -> Path | None:
    explicit = sequence.prediction_files.get(tracker_name)
    if explicit is not None and explicit.exists():
        return explicit
    if predictions_root is None:
        return None
    candidate_dir = predictions_root / tracker_name
    if candidate_dir.exists():
        direct = candidate_dir / f"{sequence.sequence_id}.txt"
        if direct.exists():
            return direct
    direct = predictions_root / f"{sequence.sequence_id}.txt"
    if direct.exists():
        return direct
    return None


def maybe_run_external_tracker(args: argparse.Namespace, predictions_dir: Path) -> None:
    if not args.external_tracker_command:
        return
    command = args.external_tracker_command.format(
        tracker=args.tracker,
        detector=args.detector,
        dataset_root=str(args.dataset_root),
        dataset_manifest=str(args.dataset_manifest),
        output_dir=str(args.output_dir),
        predictions_dir=str(predictions_dir),
    )
    result = subprocess.run(
        ["bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"external tracker command failed for {args.tracker}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def load_mot_rows(path: Path, *, is_gt: bool) -> list[MOTRow]:
    if not path.exists():
        raise FileNotFoundError(f"MOT file not found: {path}")

    rows: list[MOTRow] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for raw in reader:
            if not raw:
                continue
            if len(raw) < 6:
                raise ValueError(f"MOT row must have at least 6 columns in {path}: {raw}")
            frame_index = int(float(raw[0]))
            track_id = str(raw[1]).strip()
            x = float(raw[2])
            y = float(raw[3])
            w = float(raw[4])
            h = float(raw[5])
            confidence = float(raw[6]) if len(raw) > 6 else 1.0
            class_id = int(float(raw[7])) if len(raw) > 7 and raw[7] else 1
            visibility = float(raw[8]) if len(raw) > 8 and raw[8] not in ("", "-1") else None
            if is_gt and confidence <= 0.0:
                continue
            if class_id <= 0:
                continue
            rows.append(
                MOTRow(
                    frame_index=frame_index,
                    track_id=track_id,
                    bbox_xywh=(x, y, w, h),
                    confidence=confidence,
                    class_id=class_id,
                    visibility=visibility,
                )
            )
    return rows


def bbox_iou_xywh(box_a: tuple[float, float, float, float], box_b: tuple[float, float, float, float]) -> float:
    ax1, ay1, aw, ah = box_a
    bx1, by1, bw, bh = box_b
    ax2 = ax1 + aw
    ay2 = ay1 + ah
    bx2 = bx1 + bw
    by2 = by1 + bh
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    intersection = inter_w * inter_h
    if intersection <= 0.0:
        return 0.0
    area_a = max(0.0, aw) * max(0.0, ah)
    area_b = max(0.0, bw) * max(0.0, bh)
    union = area_a + area_b - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


def build_frame_index(rows: list[MOTRow]) -> dict[int, list[MOTRow]]:
    frames: dict[int, list[MOTRow]] = {}
    for row in rows:
        frames.setdefault(row.frame_index, []).append(row)
    return frames


def compute_tracker_metrics(
    sequences: list[SequenceRecord],
    prediction_files: dict[str, Path],
    iou_threshold: float,
) -> TrackerMetrics:
    motmetrics = require_module("motmetrics", "motmetrics")
    accumulator = motmetrics.MOTAccumulator(auto_id=False)
    frame_id = 0

    for sequence in sequences:
        prediction_path = prediction_files.get(sequence.sequence_id)
        if prediction_path is None:
            raise FileNotFoundError(f"prediction file missing for sequence {sequence.sequence_id}")
        gt_rows = load_mot_rows(sequence.gt_path, is_gt=True)
        pred_rows = load_mot_rows(prediction_path, is_gt=False)
        gt_by_frame = build_frame_index(gt_rows)
        pred_by_frame = build_frame_index(pred_rows)
        all_frames = sorted(set(gt_by_frame) | set(pred_by_frame))

        for current_frame in all_frames:
            frame_id += 1
            gt_frame = gt_by_frame.get(current_frame, [])
            pred_frame = pred_by_frame.get(current_frame, [])
            gt_ids = [f"{sequence.sequence_id}:gt:{row.class_id}:{row.track_id}" for row in gt_frame]
            pred_ids = [f"{sequence.sequence_id}:pred:{row.class_id}:{row.track_id}" for row in pred_frame]
            distances: list[list[float]] = []
            for gt_row in gt_frame:
                row_distances: list[float] = []
                for pred_row in pred_frame:
                    if gt_row.class_id != pred_row.class_id:
                        row_distances.append(math.nan)
                        continue
                    iou = bbox_iou_xywh(gt_row.bbox_xywh, pred_row.bbox_xywh)
                    if iou < iou_threshold:
                        row_distances.append(math.nan)
                        continue
                    row_distances.append(1.0 - iou)
                distances.append(row_distances)
            accumulator.update(gt_ids, pred_ids, distances, frameid=frame_id)

    metrics_handler = motmetrics.metrics.create()
    summary = metrics_handler.compute(
        accumulator,
        metrics=[
            "mota",
            "idf1",
            "num_switches",
            "num_fragmentations",
            "mostly_tracked",
            "mostly_lost",
            "num_unique_objects",
        ],
        name="overall",
    )
    row = summary.loc["overall"]
    unique_objects = float(row["num_unique_objects"]) if float(row["num_unique_objects"]) > 0.0 else 0.0
    mostly_tracked_pct = 100.0 * float(row["mostly_tracked"]) / unique_objects if unique_objects else 0.0
    mostly_lost_pct = 100.0 * float(row["mostly_lost"]) / unique_objects if unique_objects else 0.0
    return TrackerMetrics(
        mota=100.0 * float(row["mota"]),
        idf1=100.0 * float(row["idf1"]),
        id_switches=int(row["num_switches"]),
        fragmentation=int(row["num_fragmentations"]),
        mostly_tracked_pct=mostly_tracked_pct,
        mostly_lost_pct=mostly_lost_pct,
        throughput_fps=0.0,
        evaluated_sequences=len(sequences),
    )


def build_markdown_artifact(
    tracker_name: str,
    detector_name: str,
    dataset_metadata: DatasetMetadata,
    metrics: TrackerMetrics,
) -> str:
    return "\n".join(
        [
            "# Tracker Bake-Off Run",
            "",
            f"- candidate: `{tracker_name}`",
            f"- detector: `{detector_name}`",
            f"- split identifiers: `{', '.join(dataset_metadata.split_identifiers)}`",
            f"- dataset revision: `{dataset_metadata.dataset_revision or 'unknown'}`",
            f"- MOTA: `{metrics.mota:.2f}`",
            f"- IDF1: `{metrics.idf1:.2f}`",
            f"- ID switches: `{metrics.id_switches}`",
            f"- fragmentation: `{metrics.fragmentation}`",
            f"- mostly tracked %: `{metrics.mostly_tracked_pct:.2f}`",
            f"- mostly lost %: `{metrics.mostly_lost_pct:.2f}`",
            f"- tracker FPS: `{metrics.throughput_fps:.2f}`",
            "",
        ]
    )


def log_run_to_mlflow(
    args: argparse.Namespace,
    dataset_metadata: DatasetMetadata,
    metrics: TrackerMetrics,
    git_revision: str | None,
    git_dirty: bool | None,
    prediction_files: dict[str, Path],
) -> str:
    mlflow = require_module("mlflow", "mlflow")
    mlflow.set_tracking_uri(args.tracking_uri)
    mlflow.set_experiment(args.mlflow_experiment)

    with tempfile.TemporaryDirectory(prefix="tracker-bakeoff-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        summary_path = temp_dir / "summary.json"
        markdown_path = temp_dir / "comparison-input.md"
        summary_payload = {
            "tracker": args.tracker,
            "detector": args.detector,
            "dataset_manifest": str(dataset_metadata.manifest_path),
            "split_identifiers": list(dataset_metadata.split_identifiers),
            "dataset_revision": dataset_metadata.dataset_revision,
            "git_revision": git_revision,
            "git_dirty": git_dirty,
            "metrics": asdict(metrics),
            "prediction_files": {key: str(path) for key, path in prediction_files.items()},
        }
        summary_path.write_text(json.dumps(summary_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        markdown_path.write_text(
            build_markdown_artifact(args.tracker, args.detector, dataset_metadata, metrics),
            encoding="utf-8",
        )

        with mlflow.start_run(run_name=f"{args.tracker}-{args.detector}") as run:
            mlflow.set_tags(
                {
                    "bakeoff.phase": "tracker",
                    "bakeoff.candidate": args.tracker,
                    "bakeoff.detector": args.detector,
                    "bakeoff.safe_default": str(args.tracker == SAFE_DEFAULT_TRACKER).lower(),
                }
            )
            params = {
                "candidate_name": args.tracker,
                "detector_name": args.detector,
                "dataset_manifest": str(dataset_metadata.manifest_path),
                "dataset_split_identifiers": ",".join(dataset_metadata.split_identifiers),
                "dataset_revision": dataset_metadata.dataset_revision or "",
                "iou_threshold": args.iou_threshold,
                "track_thresh": args.track_thresh,
                "match_thresh": args.match_thresh,
                "second_match_thresh": args.second_match_thresh,
                "max_lost_frames": args.max_lost_frames,
            }
            if git_revision is not None:
                params["git_revision"] = git_revision
            if git_dirty is not None:
                params["git_dirty"] = str(git_dirty).lower()
            mlflow.log_params(params)
            mlflow.log_metrics(
                {
                    "tracker_mota": metrics.mota,
                    "tracker_idf1": metrics.idf1,
                    "tracker_id_switches": float(metrics.id_switches),
                    "tracker_fragmentation": float(metrics.fragmentation),
                    "tracker_mostly_tracked_pct": metrics.mostly_tracked_pct,
                    "tracker_mostly_lost_pct": metrics.mostly_lost_pct,
                    "tracker_throughput_fps": metrics.throughput_fps,
                    "tracker_evaluated_sequences": float(metrics.evaluated_sequences),
                }
            )
            mlflow.log_artifact(str(summary_path))
            mlflow.log_artifact(str(markdown_path))
            return run.info.run_id


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    predictions_dir = args.output_dir / "predictions" / args.tracker
    predictions_dir.mkdir(parents=True, exist_ok=True)

    sequences, dataset_metadata = load_dataset(args.dataset_manifest, args.dataset_root, args.detector)
    prediction_files: dict[str, Path] = {}
    throughput_samples: list[float] = []

    if args.tracker == "bytetrack":
        for sequence in sequences:
            precomputed = resolve_prediction_file(sequence, args.tracker, args.predictions_root)
            if precomputed is not None:
                prediction_files[sequence.sequence_id] = precomputed
                continue
            prediction_path = predictions_dir / f"{sequence.sequence_id}.txt"
            _, throughput_fps = run_local_bytetrack(sequence, args, prediction_path)
            prediction_files[sequence.sequence_id] = prediction_path
            throughput_samples.append(throughput_fps)
    else:
        maybe_run_external_tracker(args, predictions_dir)
        for sequence in sequences:
            prediction_path = resolve_prediction_file(sequence, args.tracker, predictions_dir)
            if prediction_path is None:
                prediction_path = resolve_prediction_file(sequence, args.tracker, args.predictions_root)
            if prediction_path is None:
                raise RuntimeError(
                    "BoT-SORT is not implemented locally in this repo. "
                    "Provide --predictions-root with MOT txt files or --external-tracker-command."
                )
            prediction_files[sequence.sequence_id] = prediction_path

    metrics = compute_tracker_metrics(sequences, prediction_files, args.iou_threshold)
    if throughput_samples:
        metrics = TrackerMetrics(
            mota=metrics.mota,
            idf1=metrics.idf1,
            id_switches=metrics.id_switches,
            fragmentation=metrics.fragmentation,
            mostly_tracked_pct=metrics.mostly_tracked_pct,
            mostly_lost_pct=metrics.mostly_lost_pct,
            throughput_fps=sum(throughput_samples) / len(throughput_samples),
            evaluated_sequences=metrics.evaluated_sequences,
        )

    git_revision, git_dirty = detect_git_state()
    run_id = log_run_to_mlflow(args, dataset_metadata, metrics, git_revision, git_dirty, prediction_files)

    print(f"tracker: {args.tracker}")
    print(f"detector: {args.detector}")
    print(f"MOTA: {metrics.mota:.2f}")
    print(f"IDF1: {metrics.idf1:.2f}")
    print(f"ID switches: {metrics.id_switches}")
    print(f"fragmentation: {metrics.fragmentation}")
    print(f"mostly tracked %: {metrics.mostly_tracked_pct:.2f}")
    print(f"mostly lost %: {metrics.mostly_lost_pct:.2f}")
    print(f"tracker FPS: {metrics.throughput_fps:.2f}")
    print(f"MLflow run ID: {run_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc
