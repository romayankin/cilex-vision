#!/usr/bin/env python3
"""Create CVAT tasks from mined hard examples with model suggestions.

Usage:
    python auto_create_cvat_tasks.py --manifest data/hard-examples/manifest.json \
        --cvat-url http://localhost:8080 --project hard-examples
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from setup_cvat_projects import (
    LABEL_COLORS,
    OBJECT_CLASSES,
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
class ExampleRecord:
    example_id: str
    camera_id: str
    date: str
    timestamp: str
    object_class: str
    frame_path: Path
    prediction_bbox_xyxy: tuple[float, float, float, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Manifest produced by hard_example_miner.py.",
    )
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
        help="Target CVAT project name.",
    )
    parser.add_argument(
        "--task-prefix",
        default="hard-examples",
        help="Task name prefix.",
    )
    parser.add_argument(
        "--grouping",
        choices=("camera-day", "camera"),
        default="camera-day",
        help="Task grouping strategy.",
    )
    parser.add_argument(
        "--max-images-per-task",
        type=int,
        default=200,
        help="Split large groups into chunks with at most this many images.",
    )
    parser.add_argument(
        "--wait-timeout-s",
        type=float,
        default=180.0,
        help="Maximum time to wait for a task to finish ingesting uploaded media.",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=2.0,
        help="Polling interval while waiting for task media ingestion.",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        help="Optional output path for the task creation report JSON.",
    )
    parser.add_argument(
        "--recreate-existing",
        action="store_true",
        help="Delete and recreate an existing project when its label schema drifts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned task creation without mutating CVAT.",
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


def hard_examples_project_spec(name: str) -> ProjectSpec:
    labels = tuple(LabelSpec(name=label_name, color=LABEL_COLORS[label_name]) for label_name in OBJECT_CLASSES)
    return ProjectSpec(
        name=name,
        labels=labels,
        purpose="Continuously mined hard examples for detector retraining feedback.",
        expected_export="CVAT for images 1.1 or Datumaro",
        annotation_mode="annotation",
    )


def ensure_project(args: argparse.Namespace, headers: dict[str, str]) -> dict[str, Any]:
    spec = hard_examples_project_spec(args.project)
    desired_labels = tuple(
        sorted(
            (normalize_label(label.to_payload()) for label in spec.labels),
            key=lambda item: item["name"],
        )
    )
    existing = get_project_by_name(args.base_url, headers, spec.name, insecure=args.insecure)
    if existing is None:
        if args.dry_run:
            return {"id": None, "name": spec.name, "status": "planned-create", "labels": []}
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
    if existing_labels == desired_labels:
        return existing
    if not args.recreate_existing:
        raise RuntimeError(
            f"project {args.project!r} exists but its label schema differs; "
            "rerun with --recreate-existing to replace it"
        )
    if args.dry_run:
        return {
            "id": existing.get("id"),
            "name": spec.name,
            "status": "planned-recreate",
            "labels": existing.get("labels", []),
        }
    request_json(
        "DELETE",
        args.base_url,
        f"/api/projects/{existing['id']}",
        headers=headers,
        insecure=args.insecure,
    )
    recreated = request_json(
        "POST",
        args.base_url,
        "/api/projects",
        headers={**headers, "Content-Type": "application/json"},
        payload=spec.to_payload(),
        insecure=args.insecure,
    )
    return request_json(
        "GET",
        args.base_url,
        f"/api/projects/{recreated['id']}",
        headers=headers,
        insecure=args.insecure,
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


def load_examples(path: Path) -> list[ExampleRecord]:
    if not path.exists():
        raise RuntimeError(f"manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    examples_raw = payload.get("examples")
    if not isinstance(examples_raw, list) or not examples_raw:
        raise RuntimeError("manifest must contain a non-empty examples list")

    examples: list[ExampleRecord] = []
    for raw in examples_raw:
        frame_path = raw.get("frame_path")
        bbox = raw.get("prediction_bbox_xyxy")
        if not frame_path or not bbox:
            continue
        path_obj = Path(str(frame_path))
        if not path_obj.exists():
            continue
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        examples.append(
            ExampleRecord(
                example_id=str(raw["example_id"]),
                camera_id=str(raw["camera_id"]),
                date=str(raw.get("date") or str(raw.get("timestamp", ""))[:10]),
                timestamp=str(raw["timestamp"]),
                object_class=str(raw["object_class"]),
                frame_path=path_obj,
                prediction_bbox_xyxy=tuple(float(value) for value in bbox),
            )
        )

    if not examples:
        raise RuntimeError("manifest did not contain any usable examples with local frame_path assets")
    return examples


def group_examples(
    examples: list[ExampleRecord],
    grouping: str,
    max_images_per_task: int,
) -> dict[str, list[ExampleRecord]]:
    grouped: dict[str, list[ExampleRecord]] = defaultdict(list)
    for example in examples:
        if grouping == "camera":
            group_key = example.camera_id
        else:
            group_key = f"{example.camera_id}:{example.date}"
        grouped[group_key].append(example)

    split_groups: dict[str, list[ExampleRecord]] = {}
    for group_key, group_examples_list in sorted(grouped.items()):
        unique_frames = sorted({str(example.frame_path) for example in group_examples_list})
        if len(unique_frames) <= max_images_per_task:
            split_groups[group_key] = sorted(
                group_examples_list,
                key=lambda item: (item.frame_path.name, item.timestamp, item.example_id),
            )
            continue

        frame_to_examples: dict[str, list[ExampleRecord]] = defaultdict(list)
        for example in group_examples_list:
            frame_to_examples[str(example.frame_path)].append(example)
        ordered_frames = sorted(frame_to_examples)
        chunks = math.ceil(len(ordered_frames) / max_images_per_task)
        for chunk_index in range(chunks):
            frame_slice = ordered_frames[
                chunk_index * max_images_per_task : (chunk_index + 1) * max_images_per_task
            ]
            split_key = f"{group_key}:part-{chunk_index + 1:02d}"
            split_examples: list[ExampleRecord] = []
            for frame_key in frame_slice:
                split_examples.extend(frame_to_examples[frame_key])
            split_groups[split_key] = sorted(
                split_examples,
                key=lambda item: (item.frame_path.name, item.timestamp, item.example_id),
            )
    return split_groups


def build_task_name(task_prefix: str, group_key: str) -> str:
    parts = group_key.split(":")
    if len(parts) == 1:
        return f"{task_prefix}: {parts[0]}"
    if len(parts) == 2:
        return f"{task_prefix}: {parts[0]} {parts[1]}"
    if len(parts) >= 3:
        return f"{task_prefix}: {parts[0]} {parts[1]} {parts[2]}"
    return f"{task_prefix}: {group_key}"


def encode_multipart_form_data(
    fields: dict[str, str],
    files: list[tuple[str, Path]],
) -> tuple[bytes, str]:
    boundary = f"----cilex-boundary-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for field_name, file_path in files:
        content = file_path.read_bytes()
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode("utf-8")
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def request_multipart_json(
    method: str,
    base_url: str,
    path: str,
    *,
    headers: dict[str, str],
    fields: dict[str, str],
    files: list[tuple[str, Path]],
    query: dict[str, Any] | None = None,
    insecure: bool = False,
) -> Any:
    body, content_type = encode_multipart_form_data(fields, files)
    request_headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
    request_headers["Content-Type"] = content_type
    request = urllib.request.Request(
        build_url(base_url, path, query=query),
        data=body,
        headers=request_headers,
        method=method,
    )
    ssl_context = create_ssl_context(insecure)
    try:
        with urllib.request.urlopen(request, context=ssl_context) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"{method} {path} failed with HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"unable to reach CVAT at {base_url}: {exc.reason}") from exc

    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def create_task(
    args: argparse.Namespace,
    headers: dict[str, str],
    project_id: int,
    task_name: str,
) -> dict[str, Any]:
    return request_json(
        "POST",
        args.base_url,
        "/api/tasks",
        headers={**headers, "Content-Type": "application/json"},
        payload={"name": task_name, "project_id": project_id},
        insecure=args.insecure,
    )


def upload_task_media(
    args: argparse.Namespace,
    headers: dict[str, str],
    task_id: int,
    frame_paths: list[Path],
) -> Any:
    files = [(f"client_files[{index}]", path) for index, path in enumerate(frame_paths)]
    return request_multipart_json(
        "POST",
        args.base_url,
        f"/api/tasks/{task_id}/data/",
        headers=headers,
        fields={
            "image_quality": "95",
            "sorting_method": "lexicographical",
        },
        files=files,
        query={"upload_multiple": "true"},
        insecure=args.insecure,
    )


def wait_for_task_media(
    args: argparse.Namespace,
    headers: dict[str, str],
    task_id: int,
    expected_frames: int,
) -> dict[str, Any]:
    deadline = time.time() + args.wait_timeout_s
    last_payload: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            status_payload = request_json(
                "GET",
                args.base_url,
                f"/api/tasks/{task_id}/status",
                headers=headers,
                insecure=args.insecure,
            )
            if isinstance(status_payload, dict):
                last_payload = status_payload
                state = str(status_payload.get("state") or status_payload.get("status") or "").lower()
                message = str(status_payload.get("message", "")).lower()
                progress = status_payload.get("progress")
                if "fail" in state or "error" in message:
                    raise RuntimeError(f"task {task_id} media ingest failed: {status_payload}")
                if state in {"finished", "created", "ready"} or progress == 100:
                    break
        except RuntimeError:
            raise
        except Exception:
            pass

        task_payload = request_json(
            "GET",
            args.base_url,
            f"/api/tasks/{task_id}",
            headers=headers,
            insecure=args.insecure,
        )
        if isinstance(task_payload, dict):
            last_payload = task_payload
            size_value = task_payload.get("size")
            if isinstance(size_value, int) and size_value >= expected_frames:
                return task_payload

        time.sleep(args.poll_interval_s)

    return last_payload


def build_annotation_payload(
    examples: list[ExampleRecord],
    frame_index_by_path: dict[str, int],
    label_id_by_name: dict[str, int],
) -> dict[str, Any]:
    shapes: list[dict[str, Any]] = []
    for example in examples:
        label_id = label_id_by_name.get(example.object_class)
        if label_id is None:
            raise RuntimeError(f"project is missing label {example.object_class!r}")
        frame_index = frame_index_by_path[str(example.frame_path)]
        x1, y1, x2, y2 = example.prediction_bbox_xyxy
        shapes.append(
            {
                "type": "rectangle",
                "occluded": False,
                "outside": False,
                "z_order": 0,
                "rotation": 0,
                "source": "auto",
                "group": 0,
                "frame": frame_index,
                "label_id": label_id,
                "points": [x1, y1, x2, y2],
                "attributes": [],
            }
        )
    return {
        "version": 0,
        "tags": [],
        "shapes": shapes,
        "tracks": [],
    }


def upload_annotations(
    args: argparse.Namespace,
    headers: dict[str, str],
    task_id: int,
    payload: dict[str, Any],
) -> Any:
    return request_json(
        "PUT",
        args.base_url,
        f"/api/tasks/{task_id}/annotations/",
        headers={**headers, "Content-Type": "application/json"},
        payload=payload,
        insecure=args.insecure,
    )


def default_report_path(manifest_path: Path) -> Path:
    return manifest_path.parent / "cvat-task-report.json"


def main() -> None:
    args = parse_args()
    validate_auth(args)
    if not args.base_url.startswith(("http://", "https://")):
        raise RuntimeError("CVAT URL must include an explicit http:// or https:// scheme")

    headers = build_headers(args)
    project = ensure_project(args, headers)
    examples = load_examples(args.manifest)
    grouped = group_examples(examples, args.grouping, args.max_images_per_task)

    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest": str(args.manifest),
        "project": {
            "id": project.get("id"),
            "name": project.get("name"),
        },
        "tasks": [],
    }

    if args.dry_run:
        for group_key, grouped_examples in grouped.items():
            unique_frames = sorted({str(example.frame_path) for example in grouped_examples})
            report["tasks"].append(
                {
                    "name": build_task_name(args.task_prefix, group_key),
                    "status": "planned",
                    "frame_count": len(unique_frames),
                    "example_count": len(grouped_examples),
                }
            )
        output_path = args.report_json or default_report_path(args.manifest)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return

    project_id = project.get("id")
    if project_id is None:
        raise RuntimeError("resolved CVAT project does not have an id")

    existing_tasks = get_project_tasks(args.base_url, headers, int(project_id), insecure=args.insecure)
    existing_by_name = {str(task.get("name")): task for task in existing_tasks}
    label_id_by_name = {
        str(label["name"]): int(label["id"])
        for label in project.get("labels", [])
        if label.get("id") is not None and label.get("name")
    }

    for group_key, grouped_examples in grouped.items():
        task_name = build_task_name(args.task_prefix, group_key)
        unique_frame_paths = [Path(path) for path in sorted({str(example.frame_path) for example in grouped_examples})]
        task_entry: dict[str, Any] = {
            "name": task_name,
            "frame_count": len(unique_frame_paths),
            "example_count": len(grouped_examples),
        }

        existing = existing_by_name.get(task_name)
        if existing is not None:
            task_entry["status"] = "existing"
            task_entry["task_id"] = existing.get("id")
            report["tasks"].append(task_entry)
            continue

        created = create_task(args, headers, int(project_id), task_name)
        task_id = created.get("id")
        if task_id is None:
            raise RuntimeError(f"CVAT task creation failed for {task_name!r}: missing id")

        upload_task_media(args, headers, int(task_id), unique_frame_paths)
        wait_for_task_media(args, headers, int(task_id), len(unique_frame_paths))

        frame_index_by_path = {
            str(path): index for index, path in enumerate(unique_frame_paths)
        }
        annotation_payload = build_annotation_payload(
            grouped_examples,
            frame_index_by_path,
            label_id_by_name,
        )
        upload_annotations(args, headers, int(task_id), annotation_payload)

        task_entry["status"] = "created"
        task_entry["task_id"] = task_id
        task_entry["example_ids"] = [example.example_id for example in grouped_examples]
        report["tasks"].append(task_entry)

    output_path = args.report_json or default_report_path(args.manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
