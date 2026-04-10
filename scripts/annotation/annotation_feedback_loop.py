#!/usr/bin/env python3
"""Export completed hard-example annotations and compare them to predictions.

Usage:
    python annotation_feedback_loop.py --cvat-url http://localhost:8080 \
        --project hard-examples --output-dir data/feedback \
        --training-manifest data/training/raw/feedback-additions.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as etree
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from compute_iaa import compute_iou
from setup_cvat_projects import (
    build_headers,
    build_url,
    create_ssl_context,
    get_project_by_name,
    request_json,
)


DEFAULT_BASE_URL = "http://127.0.0.1:8080"


@dataclass(frozen=True)
class PredictionRecord:
    example_id: str
    camera_id: str
    timestamp: str
    date: str
    frame_path: str
    object_class: str
    bbox_xywh: tuple[float, float, float, float]
    bbox_xyxy: tuple[float, float, float, float]
    source_reason: str


@dataclass(frozen=True)
class AnnotationRecord:
    object_class: str
    bbox_xywh: tuple[float, float, float, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cvat-url",
        dest="base_url",
        default=os.environ.get("CVAT_URL", DEFAULT_BASE_URL),
        help="CVAT base URL.",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("CVAT_ACCESS_TOKEN"),
        help="CVAT personal access token.",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("CVAT_USERNAME"),
        help="CVAT username for basic auth.",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("CVAT_PASSWORD"),
        help="CVAT password for basic auth.",
    )
    parser.add_argument(
        "--organization-slug",
        default=os.environ.get("CVAT_ORG"),
        help="Optional CVAT organization slug.",
    )
    parser.add_argument(
        "--project",
        default="hard-examples",
        help="CVAT project name.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/hard-examples/manifest.json"),
        help="Source hard-example manifest with original model predictions.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/feedback"),
        help="Directory for exported datasets and feedback reports.",
    )
    parser.add_argument(
        "--training-manifest",
        type=Path,
        default=Path("data/training/raw/feedback-additions.json"),
        help="Output path for the retraining-ready manifest.",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=2.0,
        help="Polling interval for async dataset export.",
    )
    parser.add_argument(
        "--max-wait-s",
        type=float,
        default=300.0,
        help="Maximum wait for a task export before failing.",
    )
    parser.add_argument(
        "--iou-threshold",
        type=float,
        default=0.5,
        help="IoU threshold used when matching predictions to annotations.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification for self-signed internal CVAT deployments.",
    )
    return parser.parse_args()


def validate_auth(args: argparse.Namespace) -> None:
    if args.access_token:
        return
    if args.username and args.password:
        return
    raise RuntimeError(
        "authentication required: supply --access-token or both --username and --password"
    )


def get_project_tasks(
    base_url: str,
    headers: dict[str, str],
    project_id: int,
    *,
    insecure: bool,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = request_json(
            "GET",
            base_url,
            "/api/tasks",
            headers=headers,
            query={"project_id": project_id, "page_size": 100, "page": page},
            insecure=insecure,
        )
        results = payload.get("results", []) if isinstance(payload, dict) else payload
        tasks.extend(results)
        if isinstance(payload, dict) and payload.get("next"):
            page += 1
        else:
            break
    return tasks


def export_task_dataset(
    base_url: str,
    headers: dict[str, str],
    task_id: int,
    *,
    insecure: bool,
    poll_interval_s: float,
    max_wait_s: float,
) -> bytes:
    request = urllib.request.Request(
        build_url(base_url, f"/api/tasks/{task_id}/dataset", {"format": "CVAT for images 1.1"}),
        headers=headers,
        method="GET",
    )
    ssl_context = create_ssl_context(insecure)
    elapsed = 0.0
    while elapsed < max_wait_s:
        try:
            with urllib.request.urlopen(request, context=ssl_context) as response:
                if response.status == 200:
                    return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 202:
                time.sleep(poll_interval_s)
                elapsed += poll_interval_s
                continue
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"task {task_id} export failed with HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"unable to reach CVAT at {base_url}: {exc.reason}") from exc

        time.sleep(poll_interval_s)
        elapsed += poll_interval_s
    raise RuntimeError(f"task {task_id} export timed out after {max_wait_s:.0f}s")


def extract_zip_to_dir(data: bytes, output_dir: Path, task_name: str) -> Path:
    safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in task_name)
    task_dir = output_dir / safe_name
    if task_dir.exists():
        shutil.rmtree(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    archive_path = task_dir / "task-export.zip"
    archive_path.write_bytes(data)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(task_dir)
    return task_dir


def find_first_matching_file(task_dir: Path, pattern: str) -> Path:
    matches = sorted(task_dir.rglob(pattern))
    if not matches:
        raise RuntimeError(f"export {task_dir} does not contain {pattern}")
    return matches[0]


def xyxy_to_xywh(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = box
    return (x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1))


def load_predictions(path: Path) -> dict[str, list[PredictionRecord]]:
    if not path.exists():
        raise RuntimeError(f"manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    examples_raw = payload.get("examples")
    if not isinstance(examples_raw, list) or not examples_raw:
        raise RuntimeError("manifest must contain a non-empty examples list")

    predictions_by_image: dict[str, list[PredictionRecord]] = defaultdict(list)
    for raw in examples_raw:
        frame_path = raw.get("frame_path")
        bbox_xyxy = raw.get("prediction_bbox_xyxy")
        if not frame_path or not bbox_xyxy:
            continue
        frame_name = Path(str(frame_path)).name
        if not frame_name:
            continue
        if not isinstance(bbox_xyxy, list) or len(bbox_xyxy) != 4:
            continue
        box_xyxy = tuple(float(value) for value in bbox_xyxy)
        predictions_by_image[frame_name].append(
            PredictionRecord(
                example_id=str(raw["example_id"]),
                camera_id=str(raw["camera_id"]),
                timestamp=str(raw["timestamp"]),
                date=str(raw.get("date") or str(raw["timestamp"])[:10]),
                frame_path=str(frame_path),
                object_class=str(raw["object_class"]),
                bbox_xywh=xyxy_to_xywh(box_xyxy),
                bbox_xyxy=box_xyxy,
                source_reason=str(raw.get("selection_reason", "unknown")),
            )
        )
    if not predictions_by_image:
        raise RuntimeError("manifest does not contain any usable predictions keyed by frame_path")
    return predictions_by_image


def parse_export_annotations(task_dir: Path) -> dict[str, list[AnnotationRecord]]:
    annotations_path = find_first_matching_file(task_dir, "annotations.xml")
    tree = etree.parse(annotations_path)
    root = tree.getroot()
    annotations_by_image: dict[str, list[AnnotationRecord]] = defaultdict(list)
    for image_elem in root.findall(".//image"):
        image_name = Path(image_elem.attrib.get("name", "")).name
        if not image_name:
            continue
        annotations_by_image.setdefault(image_name, [])
        for box_elem in image_elem.findall("box"):
            object_class = box_elem.attrib.get("label", "").strip()
            if not object_class:
                continue
            xtl = float(box_elem.attrib["xtl"])
            ytl = float(box_elem.attrib["ytl"])
            xbr = float(box_elem.attrib["xbr"])
            ybr = float(box_elem.attrib["ybr"])
            annotations_by_image[image_name].append(
                AnnotationRecord(
                    object_class=object_class,
                    bbox_xywh=(xtl, ytl, max(0.0, xbr - xtl), max(0.0, ybr - ytl)),
                )
            )
    return annotations_by_image


def compare_image_predictions(
    predictions: list[PredictionRecord],
    annotations: list[AnnotationRecord],
    *,
    iou_threshold: float,
) -> dict[str, Any]:
    matched_annotation_indexes: set[int] = set()
    true_positives = 0
    false_positives = 0
    false_negatives = 0
    per_class: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})

    for prediction in predictions:
        best_index: int | None = None
        best_iou = 0.0
        for index, annotation in enumerate(annotations):
            if index in matched_annotation_indexes:
                continue
            if annotation.object_class != prediction.object_class:
                continue
            iou = compute_iou(prediction.bbox_xywh, annotation.bbox_xywh)
            if iou >= iou_threshold and iou > best_iou:
                best_index = index
                best_iou = iou
        if best_index is None:
            false_positives += 1
            per_class[prediction.object_class]["fp"] += 1
            continue
        matched_annotation_indexes.add(best_index)
        true_positives += 1
        per_class[prediction.object_class]["tp"] += 1

    for index, annotation in enumerate(annotations):
        if index in matched_annotation_indexes:
            continue
        false_negatives += 1
        per_class[annotation.object_class]["fn"] += 1

    return {
        "tp": true_positives,
        "fp": false_positives,
        "fn": false_negatives,
        "per_class": {name: dict(values) for name, values in sorted(per_class.items())},
    }


def safe_divide(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def build_training_items(
    *,
    task_name: str,
    image_name: str,
    predictions: list[PredictionRecord],
    annotations: list[AnnotationRecord],
) -> dict[str, Any]:
    anchor = predictions[0]
    return {
        "item_id": anchor.example_id,
        "camera_id": anchor.camera_id,
        "capture_ts": anchor.timestamp,
        "sequence_id": task_name,
        "source_uri": anchor.frame_path,
        "feedback_source": "hard-examples",
        "image_name": image_name,
        "predictions": [
            {
                "object_class": prediction.object_class,
                "bbox_xywh": [round(value, 4) for value in prediction.bbox_xywh],
                "source_reason": prediction.source_reason,
            }
            for prediction in predictions
        ],
        "annotations": [
            {
                "object_class": annotation.object_class,
                "bbox_xywh": [round(value, 4) for value in annotation.bbox_xywh],
            }
            for annotation in annotations
        ],
    }


def build_markdown_report(
    *,
    project_name: str,
    task_summaries: list[dict[str, Any]],
    overall: dict[str, Any],
    per_class: dict[str, dict[str, float]],
) -> str:
    lines = [
        "# Annotation Feedback Report",
        "",
        f"- Project: `{project_name}`",
        f"- Generated at: `{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}`",
        f"- Completed tasks exported: `{len(task_summaries)}`",
        "",
        "## Overall Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| True positives | {overall['tp']} |",
        f"| False positives | {overall['fp']} |",
        f"| Missed detections | {overall['fn']} |",
        f"| False positive rate | {overall['false_positive_rate']:.4f} |",
        f"| Miss rate | {overall['miss_rate']:.4f} |",
        f"| Error rate | {overall['error_rate']:.4f} |",
        "",
        "## Per-Class Metrics",
        "",
        "| Class | TP | FP | FN | False Positive Rate | Miss Rate | Error Rate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for class_name, metrics in sorted(per_class.items()):
        lines.append(
            "| "
            f"{class_name} | {metrics['tp']} | {metrics['fp']} | {metrics['fn']} | "
            f"{metrics['false_positive_rate']:.4f} | {metrics['miss_rate']:.4f} | {metrics['error_rate']:.4f} |"
        )

    lines.extend(
        [
            "",
            "## Task Breakdown",
            "",
            "| Task | Status | Frames Compared | TP | FP | FN |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for task_summary in task_summaries:
        lines.append(
            "| "
            f"{task_summary['task_name']} | {task_summary['status']} | {task_summary['frames_compared']} | "
            f"{task_summary['tp']} | {task_summary['fp']} | {task_summary['fn']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    validate_auth(args)
    if not args.base_url.startswith(("http://", "https://")):
        raise RuntimeError("CVAT URL must include an explicit http:// or https:// scheme")

    headers = build_headers(args)
    project = get_project_by_name(args.base_url, headers, args.project, insecure=args.insecure)
    if project is None:
        raise RuntimeError(f"project {args.project!r} not found in CVAT")

    predictions_by_image = load_predictions(args.manifest)
    tasks = get_project_tasks(args.base_url, headers, int(project["id"]), insecure=args.insecure)
    completed_tasks = [task for task in tasks if str(task.get("status", "")).lower() == "completed"]
    if not completed_tasks:
        raise RuntimeError(f"project {args.project!r} has no completed tasks to export")

    exports_dir = args.output_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    task_summaries: list[dict[str, Any]] = []
    overall_counts = {"tp": 0, "fp": 0, "fn": 0}
    per_class_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    training_items: list[dict[str, Any]] = []

    for task in completed_tasks:
        task_id = int(task["id"])
        task_name = str(task.get("name", f"task-{task_id}"))
        dataset_bytes = export_task_dataset(
            args.base_url,
            headers,
            task_id,
            insecure=args.insecure,
            poll_interval_s=args.poll_interval_s,
            max_wait_s=args.max_wait_s,
        )
        task_dir = extract_zip_to_dir(dataset_bytes, exports_dir, task_name)
        annotations_by_image = parse_export_annotations(task_dir)

        task_tp = 0
        task_fp = 0
        task_fn = 0
        frames_compared = 0
        for image_name, image_annotations in sorted(annotations_by_image.items()):
            predictions = predictions_by_image.get(image_name)
            if not predictions:
                continue
            comparison = compare_image_predictions(
                predictions,
                image_annotations,
                iou_threshold=args.iou_threshold,
            )
            frames_compared += 1
            task_tp += comparison["tp"]
            task_fp += comparison["fp"]
            task_fn += comparison["fn"]
            overall_counts["tp"] += comparison["tp"]
            overall_counts["fp"] += comparison["fp"]
            overall_counts["fn"] += comparison["fn"]
            for class_name, counts in comparison["per_class"].items():
                per_class_counts[class_name]["tp"] += counts.get("tp", 0)
                per_class_counts[class_name]["fp"] += counts.get("fp", 0)
                per_class_counts[class_name]["fn"] += counts.get("fn", 0)
            training_items.append(
                build_training_items(
                    task_name=task_name,
                    image_name=image_name,
                    predictions=predictions,
                    annotations=image_annotations,
                )
            )

        task_summaries.append(
            {
                "task_id": task_id,
                "task_name": task_name,
                "status": "completed",
                "frames_compared": frames_compared,
                "tp": task_tp,
                "fp": task_fp,
                "fn": task_fn,
            }
        )

    overall_metrics = {
        "tp": overall_counts["tp"],
        "fp": overall_counts["fp"],
        "fn": overall_counts["fn"],
        "false_positive_rate": safe_divide(
            overall_counts["fp"],
            overall_counts["tp"] + overall_counts["fp"],
        ),
        "miss_rate": safe_divide(
            overall_counts["fn"],
            overall_counts["tp"] + overall_counts["fn"],
        ),
        "error_rate": safe_divide(
            overall_counts["fp"] + overall_counts["fn"],
            overall_counts["tp"] + overall_counts["fp"] + overall_counts["fn"],
        ),
    }

    per_class_metrics: dict[str, dict[str, float]] = {}
    for class_name, counts in sorted(per_class_counts.items()):
        tp = counts["tp"]
        fp = counts["fp"]
        fn = counts["fn"]
        per_class_metrics[class_name] = {
            "tp": float(tp),
            "fp": float(fp),
            "fn": float(fn),
            "false_positive_rate": safe_divide(fp, tp + fp),
            "miss_rate": safe_divide(fn, tp + fn),
            "error_rate": safe_divide(fp + fn, tp + fp + fn),
        }

    report_payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project": args.project,
        "manifest": str(args.manifest),
        "task_summaries": task_summaries,
        "overall_metrics": overall_metrics,
        "per_class_metrics": per_class_metrics,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_json_path = args.output_dir / "feedback-report.json"
    report_json_path.write_text(json.dumps(report_payload, indent=2) + "\n", encoding="utf-8")

    report_md_path = args.output_dir / "feedback-report.md"
    report_md_path.write_text(
        build_markdown_report(
            project_name=args.project,
            task_summaries=task_summaries,
            overall=overall_metrics,
            per_class=per_class_metrics,
        )
        + "\n",
        encoding="utf-8",
    )

    training_manifest_payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "hard-example-feedback",
        "project": args.project,
        "items": training_items,
    }
    args.training_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.training_manifest.write_text(
        json.dumps(training_manifest_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "report_json": str(report_json_path),
                "report_markdown": str(report_md_path),
                "training_manifest": str(args.training_manifest),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
