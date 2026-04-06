#!/usr/bin/env python3
"""Run the detector bake-off: ONNX -> TensorRT -> Triton -> eval -> MLflow.

The script is intentionally strict about missing prerequisites:

- if the evaluation manifest is absent or empty, it exits with a clear error
- if ONNX files are missing, it exits with a clear error
- if Triton is unreachable or the model repository is not writable, it exits

The repository currently does not contain evaluation data, so this script is
expected to fail fast until `data/eval/` is populated by later tasks.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)
IOU_THRESHOLDS: tuple[float, ...] = tuple(round(0.5 + 0.05 * i, 2) for i in range(10))
PILOT_AGGREGATE_FPS_TARGET = 40.0
SAFE_DEFAULT_DETECTOR = "yolov8l"


@dataclass(frozen=True)
class CandidateProfile:
    name: str
    parser_kind: str
    onnx_path: Path
    input_name: str = "images"
    output_names: tuple[str, ...] = ("output0",)
    output_shapes: tuple[tuple[int, ...], ...] = ((11, 8400),)
    max_batch_size: int = 8
    preferred_batch_sizes: tuple[int, ...] = (1, 4, 8)
    max_queue_delay_us: int = 50_000
    confidence_threshold: float = 0.40
    nms_iou_threshold: float = 0.50

    @property
    def triton_model_name(self) -> str:
        return self.name.replace("-", "_")


@dataclass(frozen=True)
class AnnotationRecord:
    class_name: str
    bbox_xywh: tuple[float, float, float, float]
    is_small: bool


@dataclass(frozen=True)
class ImageRecord:
    image_id: str
    image_path: Path
    width: int
    height: int
    is_night: bool
    annotations: tuple[AnnotationRecord, ...]


@dataclass(frozen=True)
class DatasetMetadata:
    manifest_path: Path
    split_identifiers: tuple[str, ...]
    dataset_revision: str | None


@dataclass(frozen=True)
class PreparedImage:
    record: ImageRecord
    tensor: Any
    scale: float
    pad_x: float
    pad_y: float


@dataclass(frozen=True)
class PredictionRecord:
    image_id: str
    class_name: str
    confidence: float
    bbox_xyxy: tuple[float, float, float, float]


@dataclass(frozen=True)
class LatencyStats:
    p50_ms: float
    p95_ms: float
    p99_ms: float


@dataclass(frozen=True)
class OperationalSliceMetrics:
    threshold: float
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class DetectorMetrics:
    map_50: float
    map_50_95: float
    small_object_ap: float
    night_ap: float
    throughput_fps: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    score: float
    best_batch_size: int
    operational_threshold: float
    operational_precision: float
    operational_recall: float
    operational_f1: float
    per_class_ap: dict[str, float]
    per_class_ap50: dict[str, float]


DEFAULT_CANDIDATES: dict[str, CandidateProfile] = {
    "yolov8l": CandidateProfile(
        name="yolov8l",
        parser_kind="yolo_dense",
        onnx_path=Path("artifacts/models/detector/yolov8l.onnx"),
        output_names=("output0",),
        output_shapes=((11, 8400),),
    ),
    "yolov9c": CandidateProfile(
        name="yolov9c",
        parser_kind="yolo_dense",
        onnx_path=Path("artifacts/models/detector/yolov9c.onnx"),
        output_names=("output0",),
        output_shapes=((11, 8400),),
    ),
    "rtdetr-l": CandidateProfile(
        name="rtdetr-l",
        parser_kind="rtdetr",
        onnx_path=Path("artifacts/models/detector/rtdetr-l.onnx"),
        output_names=("boxes", "scores"),
        output_shapes=((300, 4), (300, 7)),
    ),
}


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(f"missing optional dependency '{module_name}'; install {install_hint}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-manifest",
        type=Path,
        default=Path("data/eval/detector/manifest.json"),
        help="COCO-style manifest with images, annotations, and a night-image flag.",
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=Path("data/eval/detector"),
        help="Root directory for image paths referenced by the manifest.",
    )
    parser.add_argument(
        "--triton-url",
        default="http://127.0.0.1:8000",
        help="Base URL for Triton HTTP API.",
    )
    parser.add_argument(
        "--triton-model-repository",
        type=Path,
        default=Path("artifacts/triton-bakeoff"),
        help="Filesystem path mounted into the running Triton instance as its model repository.",
    )
    parser.add_argument(
        "--tracking-uri",
        default="http://127.0.0.1:5000",
        help="MLflow tracking URI.",
    )
    parser.add_argument(
        "--mlflow-experiment",
        default="detector-bakeoff",
        help="MLflow experiment name.",
    )
    parser.add_argument(
        "--candidates",
        nargs="+",
        default=list(DEFAULT_CANDIDATES),
        choices=sorted(DEFAULT_CANDIDATES),
        help="Detector candidates to evaluate.",
    )
    parser.add_argument(
        "--candidate-onnx",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Override the default ONNX path for a candidate.",
    )
    parser.add_argument(
        "--batch-sizes",
        nargs="+",
        type=int,
        default=[1, 4, 8],
        help="Batch sizes used for throughput measurement.",
    )
    parser.add_argument(
        "--workspace-mb",
        type=int,
        default=1024,
        help="TensorRT workspace budget for trtexec.",
    )
    parser.add_argument(
        "--gpu-index",
        type=int,
        default=0,
        help="GPU index used in generated Triton configs.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse any existing TensorRT engine in the Triton model repository.",
    )
    parser.add_argument(
        "--skip-triton-load",
        action="store_true",
        help="Assume the Triton model is already loaded and skip repository API calls.",
    )
    parser.add_argument(
        "--keep-models-loaded",
        action="store_true",
        help="Leave Triton models loaded after evaluation.",
    )
    return parser.parse_args()


def parse_candidate_overrides(values: Sequence[str]) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"candidate override must look like NAME=PATH, got: {item}")
        name, path_text = item.split("=", 1)
        if name not in DEFAULT_CANDIDATES:
            raise ValueError(f"unknown candidate override: {name}")
        overrides[name] = Path(path_text)
    return overrides


def resolve_candidates(args: argparse.Namespace) -> list[CandidateProfile]:
    overrides = parse_candidate_overrides(args.candidate_onnx)
    resolved: list[CandidateProfile] = []
    for name in args.candidates:
        base = DEFAULT_CANDIDATES[name]
        onnx_path = overrides.get(name, base.onnx_path)
        resolved.append(
            CandidateProfile(
                name=base.name,
                parser_kind=base.parser_kind,
                onnx_path=onnx_path,
                input_name=base.input_name,
                output_names=base.output_names,
                output_shapes=base.output_shapes,
                max_batch_size=base.max_batch_size,
                preferred_batch_sizes=base.preferred_batch_sizes,
                max_queue_delay_us=base.max_queue_delay_us,
                confidence_threshold=base.confidence_threshold,
                nms_iou_threshold=base.nms_iou_threshold,
            )
        )
    return resolved


def normalize_identifier_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int, float)):
        return (str(value),)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if item is not None)
    raise ValueError(f"manifest split identifier must be a scalar or sequence, got {type(value).__name__}")


def build_dataset_metadata(payload: dict[str, Any], manifest_path: Path) -> DatasetMetadata:
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
    )


def load_dataset(manifest_path: Path, dataset_root: Path) -> tuple[list[ImageRecord], DatasetMetadata]:
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"evaluation manifest not found: {manifest_path}. "
            "Populate data/eval/ before running the bake-off."
        )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset_metadata = build_dataset_metadata(payload, manifest_path)
    images_raw = payload.get("images")
    annotations_raw = payload.get("annotations")
    categories_raw = payload.get("categories")
    if not images_raw or not annotations_raw or not categories_raw:
        raise ValueError("manifest must contain non-empty images, annotations, and categories arrays")

    category_lookup: dict[Any, str] = {}
    for item in categories_raw:
        category_lookup[item["id"]] = item.get("name", item["id"])

    missing_classes = [class_name for class_name in OBJECT_CLASSES if class_name not in category_lookup.values()]
    if missing_classes:
        raise ValueError(f"manifest categories are missing required classes: {', '.join(missing_classes)}")

    annotations_by_image: dict[str, list[AnnotationRecord]] = {}
    for raw in annotations_raw:
        class_name = category_lookup.get(raw["category_id"], raw["category_id"])
        if class_name not in OBJECT_CLASSES:
            continue
        bbox = raw["bbox"]
        if len(bbox) != 4:
            raise ValueError(f"annotation bbox must have 4 elements, got {bbox}")
        width = float(bbox[2])
        height = float(bbox[3])
        area = width * height
        annotations_by_image.setdefault(str(raw["image_id"]), []).append(
            AnnotationRecord(
                class_name=class_name,
                bbox_xywh=(float(bbox[0]), float(bbox[1]), width, height),
                is_small=area < (32.0 * 32.0),
            )
        )

    records: list[ImageRecord] = []
    for raw in images_raw:
        image_id = str(raw["id"])
        relative_path = Path(raw["file_name"])
        image_path = relative_path if relative_path.is_absolute() else dataset_root / relative_path
        records.append(
            ImageRecord(
                image_id=image_id,
                image_path=image_path,
                width=int(raw["width"]),
                height=int(raw["height"]),
                is_night=bool(raw.get("is_night", raw.get("night", False))),
                annotations=tuple(annotations_by_image.get(image_id, [])),
            )
        )

    if not records:
        raise ValueError("evaluation manifest contained zero images")

    missing_files = [str(record.image_path) for record in records if not record.image_path.exists()]
    if missing_files:
        raise FileNotFoundError(
            "evaluation manifest references image files that do not exist; first missing file: "
            f"{missing_files[0]}"
        )

    return records, dataset_metadata


def detect_git_state() -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None, None
    return revision or None, dirty


def letterbox_image(image: Any, target_size: int = 640) -> tuple[Any, float, float, float]:
    numpy = require_module("numpy", "numpy")
    pil_image_module = require_module("PIL.Image", "Pillow")

    image = image.convert("RGB")
    orig_w, orig_h = image.size
    scale = min(target_size / orig_w, target_size / orig_h)
    resized_w = max(1, int(round(orig_w * scale)))
    resized_h = max(1, int(round(orig_h * scale)))
    resized = image.resize((resized_w, resized_h), pil_image_module.BILINEAR)

    canvas = pil_image_module.new("RGB", (target_size, target_size), color=(114, 114, 114))
    pad_x = (target_size - resized_w) / 2.0
    pad_y = (target_size - resized_h) / 2.0
    canvas.paste(resized, (int(round(pad_x)), int(round(pad_y))))

    array = numpy.asarray(canvas, dtype=numpy.float32) / 255.0
    chw = numpy.transpose(array, (2, 0, 1))
    return chw, scale, pad_x, pad_y


def prepare_images(records: Sequence[ImageRecord]) -> list[PreparedImage]:
    pil_image_module = require_module("PIL.Image", "Pillow")
    prepared: list[PreparedImage] = []
    for record in records:
        with pil_image_module.open(record.image_path) as image:
            tensor, scale, pad_x, pad_y = letterbox_image(image)
        prepared.append(
            PreparedImage(
                record=record,
                tensor=tensor,
                scale=scale,
                pad_x=pad_x,
                pad_y=pad_y,
            )
        )
    return prepared


def ensure_onnx_exists(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(
            f"candidate ONNX file not found: {path}. "
            "Export the model to ONNX before running the bake-off."
        )


def run_command(command: Sequence[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "command failed: "
            + " ".join(command)
            + f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def build_tensorrt_engine(candidate: CandidateProfile, engine_path: Path, workspace_mb: int) -> None:
    ensure_onnx_exists(candidate.onnx_path)
    engine_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "trtexec",
        f"--onnx={candidate.onnx_path}",
        f"--saveEngine={engine_path}",
        "--fp16",
        f"--minShapes={candidate.input_name}:1x3x640x640",
        f"--optShapes={candidate.input_name}:4x3x640x640",
        f"--maxShapes={candidate.input_name}:8x3x640x640",
        f"--workspace={workspace_mb}",
    ]
    run_command(command)


def render_triton_config(candidate: CandidateProfile, gpu_index: int) -> str:
    outputs = []
    for output_name, output_shape in zip(candidate.output_names, candidate.output_shapes, strict=True):
        dims = ", ".join(str(value) for value in output_shape)
        outputs.append(
            "  {\n"
            f'    name: "{output_name}"\n'
            "    data_type: TYPE_FP32\n"
            f"    dims: [{dims}]\n"
            "  }"
        )
    output_block = ",\n".join(outputs)
    preferred_batches = ", ".join(str(value) for value in candidate.preferred_batch_sizes)
    return (
        f'name: "{candidate.triton_model_name}"\n'
        'platform: "tensorrt_plan"\n'
        f"max_batch_size: {candidate.max_batch_size}\n\n"
        "input [\n"
        "  {\n"
        f'    name: "{candidate.input_name}"\n'
        "    data_type: TYPE_FP32\n"
        "    dims: [3, 640, 640]\n"
        "  }\n"
        "]\n\n"
        "output [\n"
        f"{output_block}\n"
        "]\n\n"
        "dynamic_batching {\n"
        f"  preferred_batch_size: [{preferred_batches}]\n"
        f"  max_queue_delay_microseconds: {candidate.max_queue_delay_us}\n"
        "}\n\n"
        "instance_group [\n"
        "  {\n"
        "    count: 1\n"
        "    kind: KIND_GPU\n"
        f"    gpus: [{gpu_index}]\n"
        "  }\n"
        "]\n\n"
        "version_policy {\n"
        "  latest {\n"
        "    num_versions: 2\n"
        "  }\n"
        "}\n"
    )


def install_model_repository_entry(
    candidate: CandidateProfile,
    repository_root: Path,
    engine_path: Path,
    gpu_index: int,
) -> None:
    model_dir = repository_root / candidate.triton_model_name
    version_dir = model_dir / "1"
    version_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "config.pbtxt").write_text(render_triton_config(candidate, gpu_index), encoding="utf-8")
    target_engine = version_dir / "model.plan"
    target_engine.write_bytes(engine_path.read_bytes())


def http_json_request(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach Triton at {url}: {exc}") from exc
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def unload_triton_model(triton_url: str, model_name: str) -> None:
    http_json_request("POST", f"{triton_url}/v2/repository/models/{model_name}/unload")


def load_triton_model(triton_url: str, model_name: str) -> None:
    http_json_request("POST", f"{triton_url}/v2/repository/models/{model_name}/load")


def wait_for_triton_model_ready(triton_url: str, model_name: str, timeout_seconds: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"{triton_url}/v2/models/{model_name}/ready",
                timeout=10,
            ).read()
            return
        except Exception:
            time.sleep(2.0)
    raise TimeoutError(f"Triton model {model_name} did not become ready within {timeout_seconds:.0f} seconds")


def to_original_coords(
    box_xyxy: Sequence[float],
    prepared: PreparedImage,
) -> tuple[float, float, float, float]:
    x1 = (float(box_xyxy[0]) - prepared.pad_x) / prepared.scale
    y1 = (float(box_xyxy[1]) - prepared.pad_y) / prepared.scale
    x2 = (float(box_xyxy[2]) - prepared.pad_x) / prepared.scale
    y2 = (float(box_xyxy[3]) - prepared.pad_y) / prepared.scale
    x1 = max(0.0, min(x1, prepared.record.width))
    y1 = max(0.0, min(y1, prepared.record.height))
    x2 = max(0.0, min(x2, prepared.record.width))
    y2 = max(0.0, min(y2, prepared.record.height))
    return (x1, y1, x2, y2)


def compute_iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    intersection = inter_w * inter_h
    if intersection <= 0.0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return 0.0 if union <= 0.0 else intersection / union


def apply_nms(predictions: list[PredictionRecord], iou_threshold: float) -> list[PredictionRecord]:
    by_class: dict[str, list[PredictionRecord]] = {}
    for prediction in predictions:
        by_class.setdefault(prediction.class_name, []).append(prediction)

    kept: list[PredictionRecord] = []
    for class_predictions in by_class.values():
        ordered = sorted(class_predictions, key=lambda item: item.confidence, reverse=True)
        while ordered:
            best = ordered.pop(0)
            kept.append(best)
            ordered = [
                candidate
                for candidate in ordered
                if compute_iou(best.bbox_xyxy, candidate.bbox_xyxy) < iou_threshold
            ]
    return kept


def decode_yolo_outputs(candidate: CandidateProfile, prepared_batch: Sequence[PreparedImage], outputs: dict[str, Any]) -> list[list[PredictionRecord]]:
    numpy = require_module("numpy", "numpy")
    output = outputs[candidate.output_names[0]]
    array = numpy.asarray(output)
    if array.ndim == 2:
        array = array[numpy.newaxis, ...]
    if array.ndim != 3:
        raise ValueError(f"unexpected YOLO output rank: {array.shape}")
    if array.shape[1] == len(OBJECT_CLASSES) + 4:
        batch_array = numpy.transpose(array, (0, 2, 1))
    elif array.shape[2] == len(OBJECT_CLASSES) + 4:
        batch_array = array
    else:
        raise ValueError(f"unexpected YOLO output shape: {array.shape}")

    decoded: list[list[PredictionRecord]] = []
    for prepared, sample_output in zip(prepared_batch, batch_array, strict=True):
        sample_predictions: list[PredictionRecord] = []
        for row in sample_output:
            cx, cy, width, height = [float(value) for value in row[:4]]
            scores = row[4:]
            class_index = int(numpy.argmax(scores))
            confidence = float(scores[class_index])
            if confidence < candidate.confidence_threshold:
                continue
            x1 = cx - width / 2.0
            y1 = cy - height / 2.0
            x2 = cx + width / 2.0
            y2 = cy + height / 2.0
            sample_predictions.append(
                PredictionRecord(
                    image_id=prepared.record.image_id,
                    class_name=OBJECT_CLASSES[class_index],
                    confidence=confidence,
                    bbox_xyxy=to_original_coords((x1, y1, x2, y2), prepared),
                )
            )
        decoded.append(apply_nms(sample_predictions, candidate.nms_iou_threshold))
    return decoded


def _extract_rtdetr_arrays(candidate: CandidateProfile, outputs: dict[str, Any]) -> tuple[Any, Any]:
    numpy = require_module("numpy", "numpy")
    if len(candidate.output_names) == 2 and all(name in outputs for name in candidate.output_names):
        first = numpy.asarray(outputs[candidate.output_names[0]])
        second = numpy.asarray(outputs[candidate.output_names[1]])
        if first.shape[-1] == 4:
            return first, second
        if second.shape[-1] == 4:
            return second, first
    combined = numpy.asarray(outputs[candidate.output_names[0]])
    if combined.ndim == 3 and combined.shape[-1] == len(OBJECT_CLASSES) + 4:
        return combined[..., :4], combined[..., 4:]
    if combined.ndim == 3 and combined.shape[1] == len(OBJECT_CLASSES) + 4:
        transposed = numpy.transpose(combined, (0, 2, 1))
        return transposed[..., :4], transposed[..., 4:]
    raise ValueError(f"unexpected RT-DETR output layout for candidate {candidate.name}")


def decode_rtdetr_outputs(candidate: CandidateProfile, prepared_batch: Sequence[PreparedImage], outputs: dict[str, Any]) -> list[list[PredictionRecord]]:
    numpy = require_module("numpy", "numpy")
    boxes, scores = _extract_rtdetr_arrays(candidate, outputs)
    if boxes.ndim == 2:
        boxes = boxes[numpy.newaxis, ...]
    if scores.ndim == 2:
        scores = scores[numpy.newaxis, ...]
    decoded: list[list[PredictionRecord]] = []
    for prepared, batch_boxes, batch_scores in zip(prepared_batch, boxes, scores, strict=True):
        sample_predictions: list[PredictionRecord] = []
        for raw_box, raw_scores in zip(batch_boxes, batch_scores, strict=True):
            class_index = int(numpy.argmax(raw_scores))
            confidence = float(raw_scores[class_index])
            if confidence < candidate.confidence_threshold:
                continue
            x1, y1, x2, y2 = [float(value) for value in raw_box[:4]]
            if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.5:
                x1 *= 640.0
                y1 *= 640.0
                x2 *= 640.0
                y2 *= 640.0
            sample_predictions.append(
                PredictionRecord(
                    image_id=prepared.record.image_id,
                    class_name=OBJECT_CLASSES[class_index],
                    confidence=confidence,
                    bbox_xyxy=to_original_coords((x1, y1, x2, y2), prepared),
                )
            )
        decoded.append(apply_nms(sample_predictions, candidate.nms_iou_threshold))
    return decoded


def infer_batch(
    candidate: CandidateProfile,
    triton_url: str,
    prepared_batch: Sequence[PreparedImage],
) -> tuple[list[list[PredictionRecord]], float]:
    numpy = require_module("numpy", "numpy")
    batch_tensor = numpy.stack([prepared.tensor for prepared in prepared_batch], axis=0)
    payload = {
        "inputs": [
            {
                "name": candidate.input_name,
                "shape": list(batch_tensor.shape),
                "datatype": "FP32",
                "data": batch_tensor.reshape(-1).tolist(),
            }
        ],
        "outputs": [{"name": output_name} for output_name in candidate.output_names],
    }
    start = time.perf_counter()
    response = http_json_request(
        "POST",
        f"{triton_url}/v2/models/{candidate.triton_model_name}/infer",
        payload,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    outputs: dict[str, Any] = {}
    for output in response.get("outputs", []):
        shape = output["shape"]
        array = numpy.asarray(output["data"], dtype=numpy.float32).reshape(shape)
        outputs[output["name"]] = array

    if candidate.parser_kind == "yolo_dense":
        return decode_yolo_outputs(candidate, prepared_batch, outputs), elapsed_ms
    if candidate.parser_kind == "rtdetr":
        return decode_rtdetr_outputs(candidate, prepared_batch, outputs), elapsed_ms
    raise ValueError(f"unsupported parser kind: {candidate.parser_kind}")


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    lower_value = ordered[lower]
    upper_value = ordered[upper]
    weight = index - lower
    return lower_value + (upper_value - lower_value) * weight


def latency_stats(latencies_ms: Sequence[float]) -> LatencyStats:
    return LatencyStats(
        p50_ms=percentile(latencies_ms, 0.50),
        p95_ms=percentile(latencies_ms, 0.95),
        p99_ms=percentile(latencies_ms, 0.99),
    )


def run_candidate_inference(
    candidate: CandidateProfile,
    prepared_images: Sequence[PreparedImage],
    triton_url: str,
    batch_size: int,
) -> tuple[list[PredictionRecord], float, LatencyStats]:
    all_predictions: list[PredictionRecord] = []
    all_latencies_ms: list[float] = []
    start = time.perf_counter()
    for index in range(0, len(prepared_images), batch_size):
        prepared_batch = prepared_images[index : index + batch_size]
        decoded, elapsed_ms = infer_batch(candidate, triton_url, prepared_batch)
        all_latencies_ms.append(elapsed_ms)
        for sample_predictions in decoded:
            all_predictions.extend(sample_predictions)
    elapsed = time.perf_counter() - start
    throughput_fps = len(prepared_images) / elapsed if elapsed > 0 else 0.0
    return all_predictions, throughput_fps, latency_stats(all_latencies_ms)


def xywh_to_xyxy(bbox_xywh: Sequence[float]) -> tuple[float, float, float, float]:
    x, y, width, height = bbox_xywh
    return (x, y, x + width, y + height)


def average_precision(
    predictions: Sequence[PredictionRecord],
    dataset: Sequence[ImageRecord],
    class_name: str,
    iou_threshold: float,
    image_filter: Callable[[ImageRecord], bool],
    gt_filter: Callable[[AnnotationRecord], bool],
) -> float | None:
    eligible_images = {record.image_id: record for record in dataset if image_filter(record)}
    gt_by_image: dict[str, list[AnnotationRecord]] = {}
    gt_count = 0
    for record in eligible_images.values():
        filtered = [annotation for annotation in record.annotations if annotation.class_name == class_name and gt_filter(annotation)]
        gt_by_image[record.image_id] = filtered
        gt_count += len(filtered)
    if gt_count == 0:
        return None

    matched: dict[tuple[str, int], bool] = {}
    relevant_predictions = [
        prediction
        for prediction in predictions
        if prediction.class_name == class_name and prediction.image_id in eligible_images
    ]
    relevant_predictions.sort(key=lambda item: item.confidence, reverse=True)

    tp: list[float] = []
    fp: list[float] = []
    for prediction in relevant_predictions:
        gts = gt_by_image.get(prediction.image_id, [])
        best_index = -1
        best_iou = 0.0
        for index, annotation in enumerate(gts):
            if matched.get((prediction.image_id, index), False):
                continue
            iou = compute_iou(prediction.bbox_xyxy, xywh_to_xyxy(annotation.bbox_xywh))
            if iou > best_iou:
                best_iou = iou
                best_index = index
        if best_index >= 0 and best_iou >= iou_threshold:
            matched[(prediction.image_id, best_index)] = True
            tp.append(1.0)
            fp.append(0.0)
        else:
            tp.append(0.0)
            fp.append(1.0)

    numpy = require_module("numpy", "numpy")
    tp_cum = numpy.cumsum(numpy.asarray(tp, dtype=numpy.float64))
    fp_cum = numpy.cumsum(numpy.asarray(fp, dtype=numpy.float64))
    recalls = tp_cum / max(gt_count, 1)
    precisions = tp_cum / numpy.maximum(tp_cum + fp_cum, 1e-9)

    recall_grid = numpy.linspace(0.0, 1.0, 101)
    interpolated = []
    for recall_level in recall_grid:
        precision_candidates = precisions[recalls >= recall_level]
        interpolated.append(float(precision_candidates.max()) if precision_candidates.size else 0.0)
    return float(numpy.mean(numpy.asarray(interpolated, dtype=numpy.float64)))


def mean_defined(values: Iterable[float | None]) -> float:
    materialized = [value for value in values if value is not None]
    if not materialized:
        return 0.0
    return statistics.fmean(materialized)


def compute_operational_slice(
    predictions: Sequence[PredictionRecord],
    dataset: Sequence[ImageRecord],
    confidence_threshold: float,
    iou_threshold: float = 0.50,
) -> OperationalSliceMetrics:
    filtered_predictions = [prediction for prediction in predictions if prediction.confidence >= confidence_threshold]
    gt_by_key: dict[tuple[str, str], list[AnnotationRecord]] = {}
    for record in dataset:
        for class_name in OBJECT_CLASSES:
            gt_by_key[(record.image_id, class_name)] = [
                annotation for annotation in record.annotations if annotation.class_name == class_name
            ]

    predictions_by_key: dict[tuple[str, str], list[PredictionRecord]] = {}
    for prediction in filtered_predictions:
        predictions_by_key.setdefault((prediction.image_id, prediction.class_name), []).append(prediction)

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    for key, ground_truths in gt_by_key.items():
        matched = [False] * len(ground_truths)
        candidate_predictions = sorted(
            predictions_by_key.get(key, []),
            key=lambda item: item.confidence,
            reverse=True,
        )
        for prediction in candidate_predictions:
            best_index = -1
            best_iou = 0.0
            for index, annotation in enumerate(ground_truths):
                if matched[index]:
                    continue
                iou = compute_iou(prediction.bbox_xyxy, xywh_to_xyxy(annotation.bbox_xywh))
                if iou > best_iou:
                    best_iou = iou
                    best_index = index
            if best_index >= 0 and best_iou >= iou_threshold:
                matched[best_index] = True
                true_positives += 1
            else:
                false_positives += 1
        false_negatives += sum(1 for item in matched if not item)

    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) else 0.0
    recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) else 0.0
    f1 = (2.0 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return OperationalSliceMetrics(
        threshold=confidence_threshold,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def evaluate_predictions(
    predictions: Sequence[PredictionRecord],
    dataset: Sequence[ImageRecord],
    throughput_fps: float,
    latencies: LatencyStats,
    best_batch_size: int,
    operational_threshold: float,
) -> DetectorMetrics:
    per_class_ap50: dict[str, float] = {}
    per_class_ap: dict[str, float] = {}

    all_image_filter = lambda record: True
    all_gt_filter = lambda annotation: True
    small_gt_filter = lambda annotation: annotation.is_small
    night_image_filter = lambda record: record.is_night

    for class_name in OBJECT_CLASSES:
        ap50 = average_precision(predictions, dataset, class_name, 0.50, all_image_filter, all_gt_filter)
        per_class_ap50[class_name] = 0.0 if ap50 is None else ap50
        class_ap = mean_defined(
            average_precision(predictions, dataset, class_name, threshold, all_image_filter, all_gt_filter)
            for threshold in IOU_THRESHOLDS
        )
        per_class_ap[class_name] = class_ap

    map_50 = mean_defined(per_class_ap50.values())
    map_50_95 = mean_defined(per_class_ap.values())
    small_object_ap = mean_defined(
        average_precision(predictions, dataset, class_name, threshold, all_image_filter, small_gt_filter)
        for class_name in OBJECT_CLASSES
        for threshold in IOU_THRESHOLDS
    )
    night_ap = mean_defined(
        average_precision(predictions, dataset, class_name, threshold, night_image_filter, all_gt_filter)
        for class_name in OBJECT_CLASSES
        for threshold in IOU_THRESHOLDS
    )
    operational_slice = compute_operational_slice(
        predictions,
        dataset,
        confidence_threshold=operational_threshold,
        iou_threshold=0.50,
    )
    throughput_term = min(throughput_fps / PILOT_AGGREGATE_FPS_TARGET, 1.0)
    score = (
        0.35 * map_50_95
        + 0.25 * throughput_term
        + 0.20 * small_object_ap
        + 0.20 * night_ap
    )
    return DetectorMetrics(
        map_50=map_50,
        map_50_95=map_50_95,
        small_object_ap=small_object_ap,
        night_ap=night_ap,
        throughput_fps=throughput_fps,
        latency_p50_ms=latencies.p50_ms,
        latency_p95_ms=latencies.p95_ms,
        latency_p99_ms=latencies.p99_ms,
        score=score,
        best_batch_size=best_batch_size,
        operational_threshold=operational_slice.threshold,
        operational_precision=operational_slice.precision,
        operational_recall=operational_slice.recall,
        operational_f1=operational_slice.f1,
        per_class_ap=per_class_ap,
        per_class_ap50=per_class_ap50,
    )


def log_run_to_mlflow(
    tracking_uri: str,
    experiment_name: str,
    candidate: CandidateProfile,
    metrics: DetectorMetrics,
    args: argparse.Namespace,
    dataset_metadata: DatasetMetadata,
    git_revision: str | None,
    git_dirty: bool | None,
) -> str:
    mlflow = require_module("mlflow", "mlflow")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_name=f"detector-{candidate.name}") as run:
        mlflow.set_tags(
            {
                "bakeoff.phase": "detector",
                "bakeoff.candidate": candidate.name,
                "bakeoff.safe_default": str(candidate.name == SAFE_DEFAULT_DETECTOR).lower(),
                "bakeoff.protocol_version": "1.0.0",
            }
        )
        params = {
            "candidate_name": candidate.name,
            "onnx_path": str(candidate.onnx_path),
            "parser_kind": candidate.parser_kind,
            "input_name": candidate.input_name,
            "output_names": ",".join(candidate.output_names),
            "max_batch_size": candidate.max_batch_size,
            "preferred_batch_sizes": ",".join(str(value) for value in candidate.preferred_batch_sizes),
            "max_queue_delay_us": candidate.max_queue_delay_us,
            "confidence_threshold": candidate.confidence_threshold,
            "nms_iou_threshold": candidate.nms_iou_threshold,
            "dataset_manifest": str(dataset_metadata.manifest_path),
            "dataset_split_identifiers": ",".join(dataset_metadata.split_identifiers),
            "triton_url": args.triton_url,
            "throughput_target_fps": PILOT_AGGREGATE_FPS_TARGET,
        }
        if dataset_metadata.dataset_revision is not None:
            params["dataset_revision"] = dataset_metadata.dataset_revision
        if git_revision is not None:
            params["git_revision"] = git_revision
        if git_dirty is not None:
            params["git_dirty"] = str(git_dirty).lower()
        mlflow.log_params(params)
        mlflow.log_metrics(
            {
                "detector_map_50": metrics.map_50,
                "detector_map_50_95": metrics.map_50_95,
                "detector_small_object_ap": metrics.small_object_ap,
                "detector_night_ap": metrics.night_ap,
                "detector_throughput_fps": metrics.throughput_fps,
                "detector_latency_p50_ms": metrics.latency_p50_ms,
                "detector_latency_p95_ms": metrics.latency_p95_ms,
                "detector_latency_p99_ms": metrics.latency_p99_ms,
                "detector_score": metrics.score,
                "detector_best_batch_size": metrics.best_batch_size,
                "detector_operational_threshold": metrics.operational_threshold,
                "detector_operational_precision": metrics.operational_precision,
                "detector_operational_recall": metrics.operational_recall,
                "detector_operational_f1": metrics.operational_f1,
            }
        )
        for class_name, value in metrics.per_class_ap.items():
            mlflow.log_metric(f"detector_ap50_95_{class_name}", value)
        for class_name, value in metrics.per_class_ap50.items():
            mlflow.log_metric(f"detector_ap50_{class_name}", value)

        summary = {
            "candidate": candidate.name,
            "metrics": asdict(metrics),
            "safe_default_detector": SAFE_DEFAULT_DETECTOR,
            "dataset_metadata": {
                "manifest_path": str(dataset_metadata.manifest_path),
                "split_identifiers": list(dataset_metadata.split_identifiers),
                "dataset_revision": dataset_metadata.dataset_revision,
            },
            "git": {
                "revision": git_revision,
                "dirty": git_dirty,
            },
        }
        with tempfile.TemporaryDirectory(prefix="bakeoff-") as tmp_dir:
            summary_path = Path(tmp_dir) / f"{candidate.name}_summary.json"
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
            mlflow.log_artifact(str(summary_path), artifact_path="summaries")
            comparison_input_path = Path(tmp_dir) / f"{candidate.name}_comparison_input.md"
            comparison_input_lines = [
                "| Candidate | mAP@0.5 | mAP@0.5:0.95 | Small AP | Night AP | Throughput FPS | p95 Latency ms | Op. Precision @0.40 | Op. Recall @0.40 | Op. F1 @0.40 |",
                "|-----------|---------|--------------|----------|----------|----------------|----------------|--------------------|-----------------|-------------|",
                f"| {candidate.name} | {metrics.map_50:.4f} | {metrics.map_50_95:.4f} | {metrics.small_object_ap:.4f} | "
                f"{metrics.night_ap:.4f} | {metrics.throughput_fps:.2f} | {metrics.latency_p95_ms:.2f} | "
                f"{metrics.operational_precision:.4f} | {metrics.operational_recall:.4f} | {metrics.operational_f1:.4f} |",
            ]
            comparison_input_path.write_text("\n".join(comparison_input_lines) + "\n", encoding="utf-8")
            mlflow.log_artifact(str(comparison_input_path), artifact_path="comparison-inputs")
        return run.info.run_id


def evaluate_candidate(
    candidate: CandidateProfile,
    prepared_images: Sequence[PreparedImage],
    args: argparse.Namespace,
) -> DetectorMetrics:
    repository_root = args.triton_model_repository.resolve()
    engine_path = repository_root / "_engines" / f"{candidate.triton_model_name}.plan"
    if not args.skip_build:
        build_tensorrt_engine(candidate, engine_path, args.workspace_mb)
    elif not engine_path.exists():
        raise FileNotFoundError(
            f"--skip-build was used but the TensorRT engine is missing: {engine_path}"
        )

    install_model_repository_entry(candidate, repository_root, engine_path, args.gpu_index)

    if not args.skip_triton_load:
        try:
            unload_triton_model(args.triton_url, candidate.triton_model_name)
        except RuntimeError:
            pass
        load_triton_model(args.triton_url, candidate.triton_model_name)
        wait_for_triton_model_ready(args.triton_url, candidate.triton_model_name)

    best_metrics: DetectorMetrics | None = None
    best_predictions: list[PredictionRecord] = []
    for batch_size in args.batch_sizes:
        predictions, throughput_fps, latencies = run_candidate_inference(
            candidate,
            prepared_images,
            args.triton_url,
            batch_size=batch_size,
        )
        metrics = evaluate_predictions(
            predictions,
            [prepared.record for prepared in prepared_images],
            throughput_fps,
            latencies,
            batch_size,
            operational_threshold=candidate.confidence_threshold,
        )
        if best_metrics is None or metrics.score > best_metrics.score:
            best_metrics = metrics
            best_predictions = predictions

    if best_metrics is None:
        raise RuntimeError(f"no metrics were produced for candidate {candidate.name}")

    if not args.keep_models_loaded and not args.skip_triton_load:
        unload_triton_model(args.triton_url, candidate.triton_model_name)

    # Keep predictions referenced for future extension; currently only metrics are logged.
    _ = best_predictions
    return best_metrics


def main() -> None:
    args = parse_args()
    candidates = resolve_candidates(args)
    dataset, dataset_metadata = load_dataset(args.dataset_manifest, args.dataset_root)
    prepared_images = prepare_images(dataset)
    git_revision, git_dirty = detect_git_state()

    summaries: list[tuple[str, DetectorMetrics, str]] = []
    for candidate in candidates:
        metrics = evaluate_candidate(candidate, prepared_images, args)
        run_id = log_run_to_mlflow(
            args.tracking_uri,
            args.mlflow_experiment,
            candidate,
            metrics,
            args,
            dataset_metadata,
            git_revision,
            git_dirty,
        )
        summaries.append((candidate.name, metrics, run_id))

    print("Detector bake-off completed.\n")
    for name, metrics, run_id in summaries:
        print(
            f"{name:10s} "
            f"score={metrics.score:.4f} "
            f"mAP50-95={metrics.map_50_95:.4f} "
            f"throughput_fps={metrics.throughput_fps:.2f} "
            f"night_AP={metrics.night_ap:.4f} "
            f"run_id={run_id}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc
