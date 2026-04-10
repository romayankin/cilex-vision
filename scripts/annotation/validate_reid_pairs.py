#!/usr/bin/env python3
"""Sample Re-ID pairs for human validation in CVAT.

Usage:
    python validate_reid_pairs.py --manifest data/reid-training/raw/triplet-manifest.json \
        --cvat-url http://localhost:8080 --sample-size 200
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from auto_create_cvat_tasks import (
    create_task,
    get_project_tasks,
    upload_annotations,
    upload_task_media,
    wait_for_task_media,
)
from hard_example_miner import sanitize_fragment
from setup_cvat_projects import (
    LABEL_COLORS,
    OBJECT_CLASSES,
    AttributeSpec,
    LabelSpec,
    ProjectSpec,
    build_headers,
    build_url,
    create_ssl_context,
    get_project_by_name,
    normalize_label,
    request_json,
)


DEFAULT_BASE_URL = "http://127.0.0.1:8080"


@dataclass(frozen=True)
class PairCandidate:
    pair_id: str
    triplet_id: str
    pair_role: str
    object_class: str
    expected_match: bool
    anchor_camera_id: str
    comparison_camera_id: str
    anchor_track_id: str
    comparison_track_id: str
    anchor_timestamp: datetime
    anchor_crop_path: Path
    comparison_crop_path: Path

    @property
    def camera_pair_key(self) -> str:
        return f"{self.anchor_camera_id}__{self.comparison_camera_id}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Triplet manifest produced by collect_reid_training_data.py.",
    )
    parser.add_argument(
        "--cvat-url",
        dest="base_url",
        default=os.environ.get("CVAT_URL", DEFAULT_BASE_URL),
        help="CVAT base URL.",
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
        "--access-token",
        default=os.environ.get("CVAT_ACCESS_TOKEN"),
        help="CVAT access token.",
    )
    parser.add_argument(
        "--organization-slug",
        default=os.environ.get("CVAT_ORG"),
        help="Optional CVAT organization slug.",
    )
    parser.add_argument(
        "--project",
        default="reid-training-validation",
        help="Target CVAT project name.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=200,
        help="Maximum number of sampled pair images to send for review.",
    )
    parser.add_argument(
        "--task-prefix",
        default="reid-training-validation",
        help="Task name prefix.",
    )
    parser.add_argument(
        "--max-images-per-task",
        type=int,
        default=100,
        help="Maximum pair images to upload per CVAT task.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/reid-training"),
        help="Directory receiving pair images, reports, and exports.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic seed used for stratified sampling order.",
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


def reid_validation_project_spec(name: str) -> ProjectSpec:
    match_attr = AttributeSpec(
        name="match",
        input_type="checkbox",
        values=(),
        default_value="false",
        mutable=True,
    )
    labels = tuple(
        LabelSpec(name=label_name, color=LABEL_COLORS[label_name], attributes=(match_attr,))
        for label_name in OBJECT_CLASSES
    )
    return ProjectSpec(
        name=name,
        labels=labels,
        purpose="Human validation of mined Re-ID training pairs before dataset assembly.",
        expected_export="CVAT for images 1.1",
        annotation_mode="annotation",
    )


def ensure_project(args: argparse.Namespace, headers: dict[str, str]) -> dict[str, Any]:
    spec = reid_validation_project_spec(args.project)
    desired_labels = tuple(
        sorted(
            (normalize_label(label.to_payload()) for label in spec.labels),
            key=lambda item: item["name"],
        )
    )
    existing = get_project_by_name(args.base_url, headers, args.project, insecure=args.insecure)
    if existing is None:
        created = request_json(
            "POST",
            args.base_url,
            "/api/projects",
            headers={**headers, "Content-Type": "application/json"},
            payload=spec.to_payload(),
            insecure=args.insecure,
        )
        project_id = created.get("id")
        if project_id is None:
            raise RuntimeError("CVAT project creation did not return an id")
        return request_json(
            "GET",
            args.base_url,
            f"/api/projects/{project_id}",
            headers=headers,
            insecure=args.insecure,
        )

    existing_labels = tuple(
        sorted(
            (normalize_label(label) for label in existing.get("labels", [])),
            key=lambda item: item["name"],
        )
    )
    if existing_labels != desired_labels:
        raise RuntimeError(
            f"project {args.project!r} exists but its label schema differs from the expected validation schema"
        )
    return existing


def parse_iso8601(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def ensure_crop_path(
    pair_id: str,
    endpoint: dict[str, Any],
    *,
    cache_dir: Path,
) -> Path:
    crop_path = endpoint.get("crop_path")
    if crop_path:
        path = Path(str(crop_path))
        if path.exists():
            return path

    frame_path = endpoint.get("frame_path")
    bbox = endpoint.get("representative_bbox_xyxy") or endpoint.get("representative_bbox_xywh")
    if not frame_path or not bbox:
        raise RuntimeError(f"pair {pair_id} is missing usable crop or frame asset metadata")

    from PIL import Image

    source_path = Path(str(frame_path))
    if not source_path.exists():
        raise RuntimeError(f"pair {pair_id} references missing frame path {source_path}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    generated_path = cache_dir / f"{sanitize_fragment(pair_id)}-{sanitize_fragment(endpoint['local_track_id'])}.jpg"
    if generated_path.exists():
        return generated_path

    if len(bbox) != 4:
        raise RuntimeError(f"pair {pair_id} has an invalid bbox payload")

    with Image.open(source_path) as image:
        if endpoint.get("representative_bbox_xyxy"):
            x1, y1, x2, y2 = [float(value) for value in bbox]
        else:
            x, y, w, h = [float(value) for value in bbox]
            x1, y1, x2, y2 = (x, y, x + w, y + h)
        width, height = image.size
        left = max(0, min(int(round(x1)), width))
        top = max(0, min(int(round(y1)), height))
        right = max(left + 1, min(int(round(x2)), width))
        bottom = max(top + 1, min(int(round(y2)), height))
        crop = image.crop((left, top, right, bottom))
        crop.save(generated_path, format="JPEG", quality=95)
    return generated_path


def load_pair_candidates(manifest_path: Path, *, cache_dir: Path) -> list[PairCandidate]:
    if not manifest_path.exists():
        raise RuntimeError(f"manifest not found: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    triplets_raw = payload.get("triplets")
    if not isinstance(triplets_raw, list) or not triplets_raw:
        raise RuntimeError("triplet manifest must contain a non-empty triplets list")

    candidates: list[PairCandidate] = []
    for triplet in triplets_raw:
        anchor = triplet.get("anchor")
        positive = triplet.get("positive")
        negative = triplet.get("negative")
        pair_ids = triplet.get("validation_pair_ids", {})
        if not isinstance(anchor, dict) or not isinstance(positive, dict) or not isinstance(negative, dict):
            continue
        if not isinstance(pair_ids, dict):
            continue

        try:
            anchor_crop = ensure_crop_path(
                str(pair_ids.get("positive") or triplet["triplet_id"]),
                anchor,
                cache_dir=cache_dir,
            )
            positive_crop = ensure_crop_path(
                str(pair_ids.get("positive") or triplet["triplet_id"]),
                positive,
                cache_dir=cache_dir,
            )
            negative_crop = ensure_crop_path(
                str(pair_ids.get("negative") or triplet["triplet_id"]),
                negative,
                cache_dir=cache_dir,
            )
        except Exception:
            continue
        anchor_timestamp = parse_iso8601(str(anchor["reference_time"]))

        candidates.append(
            PairCandidate(
                pair_id=str(pair_ids["positive"]),
                triplet_id=str(triplet["triplet_id"]),
                pair_role="positive",
                object_class=str(triplet["object_class"]),
                expected_match=True,
                anchor_camera_id=str(anchor["camera_id"]),
                comparison_camera_id=str(positive["camera_id"]),
                anchor_track_id=str(anchor["local_track_id"]),
                comparison_track_id=str(positive["local_track_id"]),
                anchor_timestamp=anchor_timestamp,
                anchor_crop_path=anchor_crop,
                comparison_crop_path=positive_crop,
            )
        )
        candidates.append(
            PairCandidate(
                pair_id=str(pair_ids["negative"]),
                triplet_id=str(triplet["triplet_id"]),
                pair_role="negative",
                object_class=str(triplet["object_class"]),
                expected_match=False,
                anchor_camera_id=str(anchor["camera_id"]),
                comparison_camera_id=str(negative["camera_id"]),
                anchor_track_id=str(anchor["local_track_id"]),
                comparison_track_id=str(negative["local_track_id"]),
                anchor_timestamp=anchor_timestamp,
                anchor_crop_path=anchor_crop,
                comparison_crop_path=negative_crop,
            )
        )

    if not candidates:
        raise RuntimeError("no usable validation pairs could be derived from the triplet manifest")
    return candidates


def stratified_sample(
    candidates: list[PairCandidate],
    *,
    sample_size: int,
    seed: int,
) -> list[PairCandidate]:
    if sample_size <= 0:
        raise RuntimeError("--sample-size must be > 0")
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.object_class,
            item.pair_role,
            hashlib_like(item.pair_id, seed),
        ),
    )
    by_class: dict[str, list[PairCandidate]] = defaultdict(list)
    for candidate in ordered:
        by_class[candidate.object_class].append(candidate)

    base_quota = sample_size // len(OBJECT_CLASSES)
    remainder = sample_size % len(OBJECT_CLASSES)
    selected_ids: set[str] = set()
    selected: list[PairCandidate] = []

    for index, class_name in enumerate(OBJECT_CLASSES):
        class_candidates = by_class.get(class_name, [])
        quota = base_quota + (1 if index < remainder else 0)
        positives = [item for item in class_candidates if item.expected_match]
        negatives = [item for item in class_candidates if not item.expected_match]
        class_selected: list[PairCandidate] = []
        positive_quota = min(len(positives), math.ceil(quota / 2))
        negative_quota = min(len(negatives), quota - positive_quota)
        if positive_quota + negative_quota < quota:
            remaining_quota = quota - positive_quota - negative_quota
            extra_pool = positives[positive_quota:] + negatives[negative_quota:]
            extra_pool.sort(key=lambda item: hashlib_like(item.pair_id, seed))
            class_selected.extend(positives[:positive_quota])
            class_selected.extend(negatives[:negative_quota])
            class_selected.extend(extra_pool[:remaining_quota])
        else:
            class_selected.extend(positives[:positive_quota])
            class_selected.extend(negatives[:negative_quota])

        for candidate in class_selected:
            if candidate.pair_id in selected_ids:
                continue
            selected_ids.add(candidate.pair_id)
            selected.append(candidate)

    if len(selected) >= sample_size:
        return sorted(selected, key=lambda item: hashlib_like(item.pair_id, seed))[:sample_size]

    for candidate in ordered:
        if candidate.pair_id in selected_ids:
            continue
        selected_ids.add(candidate.pair_id)
        selected.append(candidate)
        if len(selected) >= sample_size:
            break
    return selected


def hashlib_like(value: str, seed: int) -> str:
    import hashlib

    return hashlib.sha1(f"{seed}:{value}".encode("utf-8")).hexdigest()


def build_pair_image(
    candidate: PairCandidate,
    *,
    output_dir: Path,
) -> Path:
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"{sanitize_fragment(candidate.pair_id)}.jpg"
    if image_path.exists():
        return image_path

    with Image.open(candidate.anchor_crop_path) as anchor_image, Image.open(candidate.comparison_crop_path) as comparison_image:
        anchor = anchor_image.convert("RGB")
        comparison = comparison_image.convert("RGB")
        gap = 24
        padding = 12
        canvas_width = anchor.width + comparison.width + gap + padding * 2
        canvas_height = max(anchor.height, comparison.height) + padding * 2
        canvas = Image.new("RGB", (canvas_width, canvas_height), color=(24, 24, 24))
        canvas.paste(anchor, (padding, padding))
        canvas.paste(comparison, (padding + anchor.width + gap, padding))
        canvas.save(image_path, format="JPEG", quality=95)
    return image_path


def build_annotation_payload(
    samples: list[PairCandidate],
    *,
    frame_index_by_path: dict[str, int],
    label_id_by_name: dict[str, int],
    match_attr_id_by_name: dict[str, int],
    image_sizes: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    shapes: list[dict[str, Any]] = []
    for sample in samples:
        image_name = f"{sanitize_fragment(sample.pair_id)}.jpg"
        width, height = image_sizes[image_name]
        label_id = label_id_by_name.get(sample.object_class)
        attr_id = match_attr_id_by_name.get(sample.object_class)
        if label_id is None:
            raise RuntimeError(f"project is missing label {sample.object_class!r}")
        if attr_id is None:
            raise RuntimeError(f"project label {sample.object_class!r} is missing the 'match' attribute")
        shapes.append(
            {
                "type": "rectangle",
                "occluded": False,
                "outside": False,
                "z_order": 0,
                "rotation": 0,
                "source": "auto",
                "group": 0,
                "frame": frame_index_by_path[image_name],
                "label_id": label_id,
                "points": [0.0, 0.0, float(width), float(height)],
                "attributes": [
                    {
                        "spec_id": attr_id,
                        "value": "true" if sample.expected_match else "false",
                    }
                ],
            }
        )
    return {"version": 0, "tags": [], "shapes": shapes, "tracks": []}


def export_task_dataset(
    base_url: str,
    headers: dict[str, str],
    task_id: int,
    *,
    insecure: bool,
    poll_interval_s: float = 2.0,
    max_wait_s: float = 180.0,
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
    task_dir = output_dir / sanitize_fragment(task_name)
    if task_dir.exists():
        shutil.rmtree(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    archive_path = task_dir / "task-export.zip"
    archive_path.write_bytes(data)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(task_dir)
    return task_dir


def parse_validation_annotations(task_dir: Path) -> dict[str, bool]:
    annotations_paths = sorted(task_dir.rglob("annotations.xml"))
    if not annotations_paths:
        raise RuntimeError(f"export {task_dir} does not contain annotations.xml")
    root = ElementTree.parse(annotations_paths[0]).getroot()
    match_by_image: dict[str, bool] = {}
    for image_elem in root.findall(".//image"):
        image_name = Path(image_elem.attrib.get("name", "")).name
        if not image_name:
            continue
        decision = None
        for box_elem in image_elem.findall("box"):
            for attr_elem in box_elem.findall("attribute"):
                if attr_elem.attrib.get("name") != "match":
                    continue
                raw = (attr_elem.text or "").strip().lower()
                decision = raw in {"1", "true", "yes"}
                break
            if decision is not None:
                break
        if decision is not None:
            match_by_image[image_name] = decision
    return match_by_image


def main() -> None:
    args = parse_args()
    validate_auth(args)
    if not args.base_url.startswith(("http://", "https://")):
        raise RuntimeError("CVAT URL must include an explicit http:// or https:// scheme")

    output_dir = args.output_dir.resolve()
    crops_cache_dir = output_dir / "validation-crops"
    pair_images_dir = output_dir / "validation-pairs"
    exports_dir = output_dir / "validation-exports"
    report_path = output_dir / "validation-report.json"
    sample_manifest_path = output_dir / "validation-sample.json"

    candidates = load_pair_candidates(args.manifest, cache_dir=crops_cache_dir)
    sampled = stratified_sample(candidates, sample_size=args.sample_size, seed=args.seed)
    if not sampled:
        raise RuntimeError("sampling produced no reviewable pairs")

    pair_images: dict[str, Path] = {}
    image_sizes: dict[str, tuple[int, int]] = {}
    from PIL import Image

    for sample in sampled:
        pair_image = build_pair_image(sample, output_dir=pair_images_dir)
        pair_images[sample.pair_id] = pair_image
        with Image.open(pair_image) as image:
            image_sizes[pair_image.name] = image.size

    sample_payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_manifest": str(args.manifest),
        "sample_size_requested": args.sample_size,
        "sample_size_actual": len(sampled),
        "pairs": [
            {
                "pair_id": sample.pair_id,
                "triplet_id": sample.triplet_id,
                "pair_role": sample.pair_role,
                "object_class": sample.object_class,
                "expected_match": sample.expected_match,
                "anchor_camera_id": sample.anchor_camera_id,
                "comparison_camera_id": sample.comparison_camera_id,
                "anchor_track_id": sample.anchor_track_id,
                "comparison_track_id": sample.comparison_track_id,
                "anchor_timestamp": sample.anchor_timestamp.isoformat(),
                "pair_image_path": str(pair_images[sample.pair_id].resolve()),
            }
            for sample in sampled
        ],
    }
    sample_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    sample_manifest_path.write_text(json.dumps(sample_payload, indent=2) + "\n", encoding="utf-8")

    headers = build_headers(args)
    project = ensure_project(args, headers)
    project_id = project.get("id")
    if project_id is None:
        raise RuntimeError("resolved CVAT project does not have an id")
    label_id_by_name = {
        str(label["name"]): int(label["id"])
        for label in project.get("labels", [])
        if label.get("id") is not None and label.get("name")
    }
    match_attr_id_by_name = {}
    for label in project.get("labels", []):
        label_name = str(label.get("name", ""))
        if not label_name:
            continue
        for attribute in label.get("attributes", []):
            if str(attribute.get("name", "")) == "match" and attribute.get("id") is not None:
                match_attr_id_by_name[label_name] = int(attribute["id"])
                break

    grouped: dict[str, list[PairCandidate]] = defaultdict(list)
    for sample in sampled:
        grouped[sample.camera_pair_key].append(sample)

    task_plans: dict[str, list[PairCandidate]] = {}
    for group_key, group_samples in sorted(grouped.items()):
        ordered = sorted(group_samples, key=lambda item: hashlib_like(item.pair_id, args.seed))
        if len(ordered) <= args.max_images_per_task:
            task_plans[group_key] = ordered
            continue
        chunks = math.ceil(len(ordered) / args.max_images_per_task)
        for chunk_index in range(chunks):
            split_key = f"{group_key}:part-{chunk_index + 1:02d}"
            task_plans[split_key] = ordered[
                chunk_index * args.max_images_per_task : (chunk_index + 1) * args.max_images_per_task
            ]

    existing_tasks = get_project_tasks(args.base_url, headers, int(project_id), insecure=args.insecure)
    existing_by_name = {str(task.get("name")): task for task in existing_tasks}
    task_reports: list[dict[str, Any]] = []

    for group_key, task_samples in task_plans.items():
        task_name = f"{args.task_prefix}: {group_key.replace('__', ' ')}"
        unique_paths = [pair_images[sample.pair_id] for sample in task_samples]
        task_entry: dict[str, Any] = {
            "task_name": task_name,
            "pair_count": len(task_samples),
            "pair_ids": [sample.pair_id for sample in task_samples],
        }

        existing = existing_by_name.get(task_name)
        if existing is not None:
            task_entry["task_id"] = existing.get("id")
            task_entry["status"] = "existing"
            task_reports.append(task_entry)
            continue

        created = create_task(args, headers, int(project_id), task_name)
        task_id = created.get("id")
        if task_id is None:
            raise RuntimeError(f"CVAT task creation failed for {task_name!r}: missing id")
        upload_task_media(args, headers, int(task_id), unique_paths)
        wait_for_task_media(args, headers, int(task_id), len(unique_paths))

        frame_index_by_path = {path.name: index for index, path in enumerate(unique_paths)}
        annotation_payload = build_annotation_payload(
            task_samples,
            frame_index_by_path=frame_index_by_path,
            label_id_by_name=label_id_by_name,
            match_attr_id_by_name=match_attr_id_by_name,
            image_sizes=image_sizes,
        )
        upload_annotations(args, headers, int(task_id), annotation_payload)
        task_entry["task_id"] = task_id
        task_entry["status"] = "created"
        task_reports.append(task_entry)

    pair_by_image_name = {
        f"{sanitize_fragment(sample.pair_id)}.jpg": sample for sample in sampled
    }
    pair_results: list[dict[str, Any]] = []
    for task_entry in task_reports:
        task_id = task_entry.get("task_id")
        task_name = task_entry["task_name"]
        task = next(
            (
                item
                for item in get_project_tasks(args.base_url, headers, int(project_id), insecure=args.insecure)
                if str(item.get("name")) == task_name
            ),
            None,
        )
        if task is None:
            continue
        status_value = str(task.get("status", "")).lower()
        if status_value != "completed":
            continue
        dataset_bytes = export_task_dataset(
            args.base_url,
            headers,
            int(task_id),
            insecure=args.insecure,
        )
        task_dir = extract_zip_to_dir(dataset_bytes, exports_dir, task_name)
        decisions = parse_validation_annotations(task_dir)
        for image_name, reviewed_match in sorted(decisions.items()):
            sample = pair_by_image_name.get(Path(image_name).name)
            if sample is None:
                continue
            correct = reviewed_match == sample.expected_match
            pair_results.append(
                {
                    "pair_id": sample.pair_id,
                    "triplet_id": sample.triplet_id,
                    "pair_role": sample.pair_role,
                    "object_class": sample.object_class,
                    "expected_match": sample.expected_match,
                    "reviewed_match": reviewed_match,
                    "correct": correct,
                    "task_name": task_name,
                    "image_name": Path(image_name).name,
                }
            )

    approved_pair_ids = sorted(
        result["pair_id"] for result in pair_results if bool(result["correct"])
    )
    rejected_pair_ids = sorted(
        result["pair_id"] for result in pair_results if not bool(result["correct"])
    )
    pair_results_by_triplet: dict[str, dict[str, bool]] = defaultdict(dict)
    for result in pair_results:
        pair_results_by_triplet[str(result["triplet_id"])][str(result["pair_role"])] = bool(result["correct"])
    approved_triplet_ids = sorted(
        triplet_id
        for triplet_id, roles in pair_results_by_triplet.items()
        if roles.get("positive") and roles.get("negative")
    )
    reviewed_count = len(pair_results)
    correct_count = sum(1 for result in pair_results if bool(result["correct"]))
    accuracy = correct_count / reviewed_count if reviewed_count else 0.0

    report_payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_manifest": str(args.manifest),
        "sample_manifest": str(sample_manifest_path),
        "project": args.project,
        "sample_size_requested": args.sample_size,
        "sample_size_actual": len(sampled),
        "task_reports": task_reports,
        "pair_results": pair_results,
        "approved_pair_ids": approved_pair_ids,
        "rejected_pair_ids": rejected_pair_ids,
        "approved_triplet_ids": approved_triplet_ids,
        "metrics": {
            "reviewed_count": reviewed_count,
            "correct_count": correct_count,
            "validation_accuracy": round(accuracy, 6),
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report_payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "sample_manifest": str(sample_manifest_path),
                "validation_report": str(report_path),
                "sample_size": len(sampled),
                "reviewed_count": reviewed_count,
                "validation_accuracy": round(accuracy, 6),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
