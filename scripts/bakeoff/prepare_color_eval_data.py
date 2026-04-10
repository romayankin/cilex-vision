#!/usr/bin/env python3
"""Export color-annotated crops from CVAT and build the attribute eval set.

Usage:
    python prepare_color_eval_data.py --cvat-url http://localhost:8080 \
        --project attribute-eval --output-dir data/eval/attribute
"""

from __future__ import annotations

import argparse
import base64
import io
import importlib
import json
import os
import shutil
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as etree
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COLOR_VOCABULARY: tuple[str, ...] = (
    "red",
    "blue",
    "white",
    "black",
    "silver",
    "green",
    "yellow",
    "brown",
    "orange",
    "unknown",
)
VEHICLE_COLOR_CLASSES: frozenset[str] = frozenset({"car", "truck", "bus", "motorcycle"})
SUPPORTED_EXPORT_FORMATS: dict[str, str] = {
    "cvat": "CVAT for images 1.1",
    "cvat for images 1.1": "CVAT for images 1.1",
    "datumaro": "Datumaro 1.0",
    "datumaro 1.0": "Datumaro 1.0",
}
IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
)
DEFAULT_BASE_URL = "http://127.0.0.1:8080"


@dataclass(frozen=True)
class CropCandidate:
    task_id: int
    task_name: str
    source_image_name: str
    source_image_path: Path
    object_class: str
    attribute_name: str
    color_name: str
    bbox_xyxy: tuple[float, float, float, float]
    image_width: int
    image_height: int


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
        "--cvat-url",
        default=os.environ.get("CVAT_URL", DEFAULT_BASE_URL),
        help="CVAT base URL with scheme, for example http://127.0.0.1:8080.",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("CVAT_ACCESS_TOKEN"),
        help="Optional CVAT personal access token.",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("CVAT_USERNAME"),
        help="CVAT username for basic auth when no access token is provided.",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("CVAT_PASSWORD"),
        help="CVAT password for basic auth when no access token is provided.",
    )
    parser.add_argument(
        "--organization-slug",
        default=os.environ.get("CVAT_ORG"),
        help="Optional CVAT organization slug.",
    )
    parser.add_argument(
        "--project",
        default="attribute-eval",
        help="CVAT project name that owns the attribute evaluation data.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/eval/attribute"),
        help="Directory where color-organized crops and manifest.json will be written.",
    )
    parser.add_argument(
        "--format",
        default="CVAT",
        help="Export format: CVAT or Datumaro.",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=2.0,
        help="Polling interval for async CVAT dataset export.",
    )
    parser.add_argument(
        "--max-wait-s",
        type=float,
        default=300.0,
        help="Maximum wait for a single task export before failing.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for self-signed CVAT deployments.",
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


def build_headers(args: argparse.Namespace, *, include_json: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/vnd.cvat+json, application/json"}
    if include_json:
        headers["Content-Type"] = "application/json"
    if args.organization_slug:
        headers["X-Organization"] = args.organization_slug
    if args.access_token:
        headers["Authorization"] = f"Bearer {args.access_token}"
    else:
        token = base64.b64encode(
            f"{args.username}:{args.password}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def create_ssl_context(insecure: bool) -> ssl.SSLContext | None:
    if not insecure:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def build_url(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    normalized_base = base_url.rstrip("/") + "/"
    url = urllib.parse.urljoin(normalized_base, path.lstrip("/"))
    if query:
        url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
    return url


def request_json(
    method: str,
    base_url: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    insecure: bool = False,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        build_url(base_url, path, query=query),
        data=body,
        headers=headers,
        method=method,
    )
    ssl_context = create_ssl_context(insecure)
    try:
        with urllib.request.urlopen(request, context=ssl_context) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        message = detail or exc.reason
        raise RuntimeError(
            f"{method} {path} failed with HTTP {exc.code}: {message}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"unable to reach CVAT at {base_url}: {exc.reason}") from exc

    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def export_task_dataset(
    base_url: str,
    headers: dict[str, str],
    task_id: int,
    export_format: str,
    *,
    insecure: bool,
    poll_interval_s: float,
    max_wait_s: float,
) -> bytes:
    request = urllib.request.Request(
        build_url(base_url, f"/api/tasks/{task_id}/dataset", {"format": export_format}),
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
            message = detail or exc.reason
            raise RuntimeError(
                f"task {task_id} export failed with HTTP {exc.code}: {message}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"unable to reach CVAT at {base_url}: {exc.reason}") from exc

        time.sleep(poll_interval_s)
        elapsed += poll_interval_s
    raise RuntimeError(f"task {task_id} export timed out after {max_wait_s:.0f}s")


def normalize_export_format(raw_value: str) -> str:
    normalized = raw_value.strip().lower()
    if normalized not in SUPPORTED_EXPORT_FORMATS:
        raise RuntimeError(
            "unsupported export format; use one of: CVAT, CVAT for images 1.1, "
            "Datumaro, Datumaro 1.0"
        )
    return SUPPORTED_EXPORT_FORMATS[normalized]


def get_project_by_name(
    base_url: str,
    headers: dict[str, str],
    name: str,
    *,
    insecure: bool,
) -> dict[str, Any]:
    payload = request_json(
        "GET",
        base_url,
        "/api/projects",
        headers=headers,
        query={"page_size": 1000, "search": name},
        insecure=insecure,
    )
    results = payload.get("results", payload) if isinstance(payload, dict) else payload
    for project in results:
        if project.get("name") == name:
            return project
    raise RuntimeError(f"project {name!r} not found in CVAT")


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


def clear_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for color_name in COLOR_VOCABULARY:
        shutil.rmtree(output_dir / color_name, ignore_errors=True)
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()


def sanitize_fragment(raw_value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in raw_value)


def build_image_index(task_dir: Path) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    by_relative_path: dict[str, Path] = {}
    by_basename: dict[str, list[Path]] = {}
    for path in task_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        relative = path.relative_to(task_dir).as_posix()
        by_relative_path[relative] = path
        by_basename.setdefault(path.name, []).append(path)
    return by_relative_path, by_basename


def resolve_image_path(
    task_dir: Path,
    raw_image_path: str,
    by_relative_path: dict[str, Path],
    by_basename: dict[str, list[Path]],
) -> Path:
    normalized = raw_image_path.strip().lstrip("./")
    for candidate in (
        normalized,
        f"images/{normalized}",
        f"images/default/{normalized}",
    ):
        if candidate in by_relative_path:
            return by_relative_path[candidate]
    basename = Path(normalized).name
    basename_matches = by_basename.get(basename, [])
    if len(basename_matches) == 1:
        return basename_matches[0]
    if not basename_matches:
        raise FileNotFoundError(
            f"could not resolve source image {raw_image_path!r} under {task_dir}"
        )
    raise RuntimeError(
        f"ambiguous source image {raw_image_path!r} under {task_dir}; "
        "export contains multiple files with the same basename"
    )


def attribute_candidates_for_box(
    object_class: str,
    attributes: dict[str, str],
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if object_class == "person":
        for attribute_name in ("person_upper_color", "person_lower_color"):
            raw_value = attributes.get(attribute_name, "").strip().lower()
            if raw_value:
                candidates.append((attribute_name, raw_value))
    elif object_class in VEHICLE_COLOR_CLASSES:
        raw_value = attributes.get("vehicle_color", "").strip().lower()
        if raw_value:
            candidates.append(("vehicle_color", raw_value))
    return candidates


def validate_color_name(color_name: str) -> str:
    if color_name not in COLOR_VOCABULARY:
        allowed = ", ".join(COLOR_VOCABULARY)
        raise RuntimeError(
            f"unsupported color label {color_name!r}; expected one of: {allowed}"
        )
    return color_name


def find_first_matching_file(task_dir: Path, pattern: str) -> Path:
    matches = sorted(task_dir.rglob(pattern))
    if not matches:
        raise RuntimeError(f"export {task_dir} does not contain {pattern}")
    return matches[0]


def parse_cvat_export(task_dir: Path, task_id: int, task_name: str) -> list[CropCandidate]:
    xml_path = find_first_matching_file(task_dir, "annotations.xml")
    tree = etree.parse(xml_path)
    root = tree.getroot()
    by_relative_path, by_basename = build_image_index(task_dir)
    crop_candidates: list[CropCandidate] = []

    for image_elem in root.findall(".//image"):
        raw_image_name = image_elem.attrib.get("name", "")
        source_image_path = resolve_image_path(
            task_dir,
            raw_image_name,
            by_relative_path,
            by_basename,
        )
        image_width = int(float(image_elem.attrib["width"]))
        image_height = int(float(image_elem.attrib["height"]))
        for box_elem in image_elem.findall("box"):
            object_class = box_elem.attrib.get("label", "").strip()
            attributes = {
                attr_elem.attrib.get("name", "").strip(): (attr_elem.text or "").strip()
                for attr_elem in box_elem.findall("attribute")
            }
            bbox_xyxy = (
                float(box_elem.attrib["xtl"]),
                float(box_elem.attrib["ytl"]),
                float(box_elem.attrib["xbr"]),
                float(box_elem.attrib["ybr"]),
            )
            for attribute_name, color_name in attribute_candidates_for_box(
                object_class, attributes
            ):
                crop_candidates.append(
                    CropCandidate(
                        task_id=task_id,
                        task_name=task_name,
                        source_image_name=raw_image_name,
                        source_image_path=source_image_path,
                        object_class=object_class,
                        attribute_name=attribute_name,
                        color_name=validate_color_name(color_name),
                        bbox_xyxy=bbox_xyxy,
                        image_width=image_width,
                        image_height=image_height,
                    )
                )
    return crop_candidates


def datumaro_label_map(payload: dict[str, Any]) -> dict[int, str]:
    categories = payload.get("categories", {})
    label_payload = categories.get("label", {})
    labels = label_payload.get("labels") or label_payload.get("items") or []
    mapping: dict[int, str] = {}
    for index, label in enumerate(labels):
        if isinstance(label, dict) and "name" in label:
            mapping[index] = str(label["name"])
        elif isinstance(label, str):
            mapping[index] = label
    return mapping


def parse_datumaro_export(task_dir: Path, task_id: int, task_name: str) -> list[CropCandidate]:
    annotations_path = find_first_matching_file(task_dir, "default.json")
    payload = json.loads(annotations_path.read_text(encoding="utf-8"))
    label_map = datumaro_label_map(payload)
    by_relative_path, by_basename = build_image_index(task_dir)
    crop_candidates: list[CropCandidate] = []

    for item in payload.get("items", []):
        image_payload = item.get("image") or item.get("media") or {}
        raw_image_name = str(image_payload.get("path") or item.get("id") or "")
        source_image_path = resolve_image_path(
            task_dir,
            raw_image_name,
            by_relative_path,
            by_basename,
        )
        image_width = int(image_payload.get("size", [0, 0])[1] or 0)
        image_height = int(image_payload.get("size", [0, 0])[0] or 0)
        for annotation in item.get("annotations", []):
            annotation_type = annotation.get("type")
            if annotation_type not in ("bbox", "rectangle"):
                continue
            label_id = int(annotation.get("label_id", -1))
            object_class = label_map.get(label_id, "")
            bbox = annotation.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            x_min, y_min, width, height = [float(value) for value in bbox]
            attributes = {
                str(key): str(value)
                for key, value in (annotation.get("attributes") or {}).items()
            }
            for attribute_name, color_name in attribute_candidates_for_box(
                object_class, attributes
            ):
                crop_candidates.append(
                    CropCandidate(
                        task_id=task_id,
                        task_name=task_name,
                        source_image_name=raw_image_name,
                        source_image_path=source_image_path,
                        object_class=object_class,
                        attribute_name=attribute_name,
                        color_name=validate_color_name(color_name),
                        bbox_xyxy=(x_min, y_min, x_min + width, y_min + height),
                        image_width=image_width,
                        image_height=image_height,
                    )
                )
    return crop_candidates


def parse_export(
    task_dir: Path,
    export_format: str,
    task_id: int,
    task_name: str,
) -> list[CropCandidate]:
    if export_format == "CVAT for images 1.1":
        return parse_cvat_export(task_dir, task_id, task_name)
    if export_format == "Datumaro 1.0":
        return parse_datumaro_export(task_dir, task_id, task_name)
    raise RuntimeError(f"unsupported export format implementation: {export_format}")


def clamp_crop_region(
    bbox_xyxy: tuple[float, float, float, float],
    *,
    image_width: int,
    image_height: int,
    attribute_name: str,
) -> tuple[int, int, int, int]:
    x_min, y_min, x_max, y_max = bbox_xyxy
    left = max(0, min(image_width, int(x_min)))
    upper = max(0, min(image_height, int(y_min)))
    right = max(0, min(image_width, int(x_max + 0.9999)))
    lower = max(0, min(image_height, int(y_max + 0.9999)))
    if right <= left or lower <= upper:
        raise RuntimeError(f"invalid crop box after clamping: {bbox_xyxy!r}")
    if attribute_name == "person_upper_color":
        midpoint = upper + max(1, (lower - upper) // 2)
        lower = max(midpoint, upper + 1)
    elif attribute_name == "person_lower_color":
        midpoint = upper + max(1, (lower - upper) // 2)
        upper = min(midpoint, lower - 1)
    if right <= left or lower <= upper:
        raise RuntimeError(f"degenerate crop region for {attribute_name}: {bbox_xyxy!r}")
    return left, upper, right, lower


def write_crops(
    crop_candidates: list[CropCandidate],
    output_dir: Path,
) -> dict[str, Any]:
    image_module = require_module("PIL.Image", "Pillow")
    per_color_counts = {color_name: 0 for color_name in COLOR_VOCABULARY}
    items: list[dict[str, Any]] = []

    for index, candidate in enumerate(crop_candidates, start=1):
        crop_region = clamp_crop_region(
            candidate.bbox_xyxy,
            image_width=candidate.image_width,
            image_height=candidate.image_height,
            attribute_name=candidate.attribute_name,
        )
        source_stub = sanitize_fragment(Path(candidate.source_image_name).stem or "image")
        task_stub = sanitize_fragment(candidate.task_name or f"task-{candidate.task_id}")
        object_stub = sanitize_fragment(candidate.object_class)
        attribute_stub = sanitize_fragment(candidate.attribute_name)
        filename = (
            f"{task_stub}_{source_stub}_{object_stub}_{attribute_stub}_{index:06d}.jpg"
        )
        relative_crop_path = Path(candidate.color_name) / filename
        destination = output_dir / relative_crop_path
        destination.parent.mkdir(parents=True, exist_ok=True)

        with image_module.open(candidate.source_image_path) as image:
            crop = image.convert("RGB").crop(crop_region)
            crop.save(destination, format="JPEG", quality=95)

        per_color_counts[candidate.color_name] += 1
        items.append(
            {
                "crop_path": relative_crop_path.as_posix(),
                "color": candidate.color_name,
                "object_class": candidate.object_class,
                "attribute_name": candidate.attribute_name,
                "task_id": candidate.task_id,
                "task_name": candidate.task_name,
                "source_image": candidate.source_image_name,
                "bbox_xyxy": [
                    round(candidate.bbox_xyxy[0], 3),
                    round(candidate.bbox_xyxy[1], 3),
                    round(candidate.bbox_xyxy[2], 3),
                    round(candidate.bbox_xyxy[3], 3),
                ],
                "crop_region_xyxy": list(crop_region),
            }
        )

    return {
        "total_crops": len(items),
        "per_color_counts": per_color_counts,
        "items": items,
    }


def prepare_eval_data(args: argparse.Namespace) -> dict[str, Any]:
    validate_auth(args)
    export_format = normalize_export_format(args.format)
    headers = build_headers(args)
    project = get_project_by_name(
        args.cvat_url,
        headers,
        args.project,
        insecure=args.insecure,
    )
    tasks = get_project_tasks(
        args.cvat_url,
        headers,
        int(project["id"]),
        insecure=args.insecure,
    )
    if not tasks:
        raise RuntimeError(f"project {args.project!r} has no tasks to export")

    clear_output_dir(args.output_dir)

    crop_candidates: list[CropCandidate] = []
    exported_tasks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="attribute-eval-export-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        for task in tasks:
            task_id = int(task["id"])
            task_name = str(task.get("name", f"task-{task_id}"))
            dataset_bytes = export_task_dataset(
                args.cvat_url,
                headers,
                task_id,
                export_format,
                insecure=args.insecure,
                poll_interval_s=args.poll_interval_s,
                max_wait_s=args.max_wait_s,
            )
            task_dir = temp_dir / sanitize_fragment(task_name or f"task-{task_id}")
            task_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(dataset_bytes)) as archive:
                archive.extractall(task_dir)
            parsed = parse_export(task_dir, export_format, task_id, task_name)
            crop_candidates.extend(parsed)
            exported_tasks.append(
                {
                    "task_id": task_id,
                    "task_name": task_name,
                    "status": task.get("status", "unknown"),
                    "crop_candidates": len(parsed),
                }
            )

    if not crop_candidates:
        raise RuntimeError(
            f"project {args.project!r} contains no usable color annotations"
        )

    crop_payload = write_crops(crop_candidates, args.output_dir)
    manifest = {
        "colors": list(COLOR_VOCABULARY),
        "total_crops": crop_payload["total_crops"],
        "per_color_counts": crop_payload["per_color_counts"],
        "export_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_project": args.project,
        "project_id": int(project["id"]),
        "format": export_format,
        "task_count": len(tasks),
        "tasks": exported_tasks,
        "items": crop_payload["items"],
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"manifest_path": manifest_path, "manifest": manifest}


def main() -> None:
    args = parse_args()
    result = prepare_eval_data(args)
    manifest = result["manifest"]
    summary = {
        "manifest_path": str(result["manifest_path"]),
        "source_project": manifest["source_project"],
        "format": manifest["format"],
        "total_crops": manifest["total_crops"],
        "per_color_counts": manifest["per_color_counts"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
