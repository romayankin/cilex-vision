#!/usr/bin/env python3
"""Aggregate annotated datasets from multiple CVAT instances into a unified manifest.

Connects to one or more CVAT servers (potentially different versions), fetches
project tasks and annotations, normalizes labels to the canonical ObjectClass
enum, tags each sample with site metadata, and writes a deduplicated unified
manifest JSON.

Usage (multi-site via config file):
    python aggregate_datasets.py --sites sites-config.json \
        --output data/multi-site/unified-manifest.json

Usage (single-site shortcut):
    python aggregate_datasets.py \
        --cvat-url http://cvat.example.com \
        --cvat-username annotator --cvat-password secret \
        --site-id alpha --projects detection-eval,tracking-eval \
        --output data/multi-site/unified-manifest.json

Sites config format:
{
  "sites": [
    {
      "site_id": "alpha",
      "cvat_url": "https://cvat-alpha.example.com",
      "cvat_username": "annotator",
      "cvat_password_env": "CVAT_ALPHA_PASSWORD",
      "projects": ["detection-eval", "tracking-eval"],
      "conditions": {"lighting": "mixed", "weather": "indoor"},
      "camera_model": "Axis P3245-V"
    }
  ]
}

Unified manifest output:
{
  "items": [
    {
      "item_id": "alpha:detection-eval:task-42:frame-0001:0",
      "site_id": "alpha",
      "camera_id": "cam-01",
      "capture_ts": "2026-04-10T08:00:00Z",
      "source_uri": "s3://datasets/alpha/task-42/frame-0001.jpg",
      "object_class": "person",
      "bbox": {"x": 120, "y": 80, "w": 60, "h": 150},
      "conditions": {"lighting": "mixed", "weather": "indoor"},
      "camera_model": "Axis P3245-V",
      "project_name": "detection-eval",
      "task_id": 42,
      "frame_id": 1
    }
  ],
  "metadata": {
    "created_at": "2026-04-12T...",
    "sites": ["alpha"],
    "total_items": 1
  }
}
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Canonical object classes from services/db/models.py ObjectClass enum
CANONICAL_CLASSES: set[str] = {
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
}

# Aliases that CVAT annotators might use → canonical name
LABEL_ALIASES: dict[str, str] = {
    "pedestrian": "person",
    "human": "person",
    "vehicle": "car",
    "automobile": "car",
    "van": "truck",
    "lorry": "truck",
    "bike": "bicycle",
    "cycle": "bicycle",
    "motorbike": "motorcycle",
    "dog": "animal",
    "cat": "animal",
}


# ---------------------------------------------------------------------------
# HTTP helpers (matching existing annotation script patterns)
# ---------------------------------------------------------------------------


def build_headers(
    username: str | None,
    password: str | None,
    access_token: str | None = None,
    include_json: bool = False,
) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/vnd.cvat+json, application/json",
    }
    if include_json:
        headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    elif username and password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
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
        query_string = urllib.parse.urlencode(query, doseq=True)
        url = f"{url}?{query_string}"
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
    url = build_url(base_url, path, query=query)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    ssl_context = create_ssl_context(insecure)

    try:
        with urllib.request.urlopen(request, context=ssl_context) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        message = detail or exc.reason
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"unable to reach CVAT at {base_url}: {exc.reason}") from exc

    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# CVAT version handling
# ---------------------------------------------------------------------------


def check_cvat_version(
    base_url: str,
    headers: dict[str, str],
    *,
    insecure: bool = False,
    skip_check: bool = False,
) -> str:
    """Return server version string. Warns if unexpected but continues."""
    about = request_json("GET", base_url, "/api/server/about", headers=headers, insecure=insecure)
    version = str(about.get("version", "unknown")) if isinstance(about, dict) else "unknown"
    if not skip_check and not version.startswith("2."):
        print(f"  WARNING: CVAT version {version!r} at {base_url} — expected 2.x")
    return version


# ---------------------------------------------------------------------------
# CVAT data fetching
# ---------------------------------------------------------------------------


def fetch_all_pages(
    base_url: str,
    path: str,
    headers: dict[str, str],
    *,
    insecure: bool = False,
    page_size: int = 500,
) -> list[dict[str, Any]]:
    """Paginate through a CVAT list endpoint, collecting all results."""
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        data = request_json(
            "GET",
            base_url,
            path,
            headers=headers,
            query={"page_size": page_size, "page": page},
            insecure=insecure,
        )
        if isinstance(data, dict):
            page_results = data.get("results", [])
        elif isinstance(data, list):
            page_results = data
        else:
            break
        results.extend(page_results)
        # No more pages if fewer results than page_size or no next link
        if isinstance(data, dict) and not data.get("next"):
            break
        if len(page_results) < page_size:
            break
        page += 1
    return results


def find_projects_by_name(
    base_url: str,
    headers: dict[str, str],
    project_names: list[str],
    *,
    insecure: bool = False,
) -> list[dict[str, Any]]:
    """Find CVAT projects matching the given names."""
    all_projects = fetch_all_pages(base_url, "/api/projects", headers, insecure=insecure)
    matched = []
    for proj in all_projects:
        if proj.get("name") in project_names:
            matched.append(proj)
    return matched


def fetch_tasks_for_project(
    base_url: str,
    headers: dict[str, str],
    project_id: int,
    *,
    insecure: bool = False,
) -> list[dict[str, Any]]:
    return fetch_all_pages(
        base_url,
        "/api/tasks",
        headers,
        insecure=insecure,
    )


def fetch_task_annotations(
    base_url: str,
    headers: dict[str, str],
    task_id: int,
    *,
    insecure: bool = False,
) -> dict[str, Any]:
    result = request_json(
        "GET",
        base_url,
        f"/api/tasks/{task_id}/annotations",
        headers=headers,
        insecure=insecure,
    )
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Annotation normalization
# ---------------------------------------------------------------------------


def normalize_label(label: str) -> str | None:
    """Map a CVAT label name to canonical ObjectClass. Returns None if unmapped."""
    lower = label.strip().lower()
    if lower in CANONICAL_CLASSES:
        return lower
    return LABEL_ALIASES.get(lower)


def normalize_bbox(shape: dict[str, Any]) -> dict[str, float] | None:
    """Extract (x, y, w, h) bbox from a CVAT shape. Handles rectangle type."""
    shape_type = shape.get("type", "rectangle")
    if shape_type != "rectangle":
        return None
    points = shape.get("points", [])
    if len(points) < 4:
        return None
    x_min, y_min, x_max, y_max = points[0], points[1], points[2], points[3]
    return {
        "x": round(float(x_min), 2),
        "y": round(float(y_min), 2),
        "w": round(float(x_max) - float(x_min), 2),
        "h": round(float(y_max) - float(y_min), 2),
    }


def extract_camera_id_from_task(task: dict[str, Any]) -> str:
    """Try to extract camera_id from task name or data."""
    name = task.get("name", "")
    # Common patterns: "cam-01: ...", "cam_01 ...", "reid: cam-a → cam-b"
    for prefix in ("cam-", "cam_", "camera-", "camera_"):
        if prefix in name.lower():
            idx = name.lower().index(prefix)
            segment = name[idx:].split()[0].split(":")[0].split(",")[0]
            return segment
    return f"task-{task.get('id', 'unknown')}"


# ---------------------------------------------------------------------------
# Site processing
# ---------------------------------------------------------------------------


def process_site(
    site_config: dict[str, Any],
    *,
    insecure: bool = False,
    skip_version_check: bool = False,
) -> list[dict[str, Any]]:
    """Fetch and normalize all annotations from a single site's CVAT instance."""
    site_id = site_config["site_id"]
    cvat_url = site_config["cvat_url"]
    username = site_config.get("cvat_username", os.environ.get("CVAT_USERNAME"))
    password_env = site_config.get("cvat_password_env")
    password = os.environ.get(password_env) if password_env else site_config.get("cvat_password", os.environ.get("CVAT_PASSWORD"))
    project_names = site_config.get("projects", ["detection-eval", "tracking-eval"])
    conditions = site_config.get("conditions", {})
    camera_model = site_config.get("camera_model")

    if not username or not password:
        raise RuntimeError(f"site {site_id!r}: missing CVAT credentials")

    headers = build_headers(username, password)
    print(f"  Connecting to {cvat_url} for site {site_id!r}...")

    version = check_cvat_version(
        cvat_url, headers, insecure=insecure, skip_check=skip_version_check,
    )
    print(f"  CVAT version: {version}")

    projects = find_projects_by_name(cvat_url, headers, project_names, insecure=insecure)
    if not projects:
        print(f"  WARNING: no matching projects found for {project_names}")
        return []

    items: list[dict[str, Any]] = []

    for project in projects:
        project_id = project["id"]
        project_name = project["name"]
        print(f"  Processing project {project_name!r} (id={project_id})...")

        all_tasks = fetch_all_pages(cvat_url, "/api/tasks", headers, insecure=insecure)
        project_tasks = [t for t in all_tasks if t.get("project_id") == project_id]

        for task in project_tasks:
            task_id = task["id"]
            camera_id = extract_camera_id_from_task(task)
            annotations = fetch_task_annotations(cvat_url, headers, task_id, insecure=insecure)

            shapes = annotations.get("shapes", [])
            for shape_idx, shape in enumerate(shapes):
                raw_label = shape.get("label", "")
                obj_class = normalize_label(raw_label)
                if obj_class is None:
                    continue

                bbox = normalize_bbox(shape)
                if bbox is None:
                    continue

                frame_id = shape.get("frame", 0)
                item_id = f"{site_id}:{project_name}:task-{task_id}:frame-{frame_id:04d}:{shape_idx}"

                item: dict[str, Any] = {
                    "item_id": item_id,
                    "site_id": site_id,
                    "camera_id": camera_id,
                    "capture_ts": _build_capture_ts(task, frame_id),
                    "source_uri": f"s3://datasets/{site_id}/task-{task_id}/frame-{frame_id:04d}.jpg",
                    "object_class": obj_class,
                    "bbox": bbox,
                    "conditions": conditions,
                    "project_name": project_name,
                    "task_id": task_id,
                    "frame_id": frame_id,
                }
                if camera_model:
                    item["camera_model"] = camera_model
                items.append(item)

        print(f"  {project_name}: {len([i for i in items if i['project_name'] == project_name])} annotations")

    return items


def _build_capture_ts(task: dict[str, Any], frame_id: int) -> str:
    """Build a capture timestamp from task metadata or fall back to task creation time."""
    created = task.get("created_date") or task.get("updated_date")
    if created:
        try:
            base = datetime.fromisoformat(created.replace("Z", "+00:00"))
            # Offset by frame_id to maintain ordering within a task
            ts = base.timestamp() + frame_id * 0.033  # ~30fps
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        except (ValueError, TypeError):
            pass
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate annotated datasets from multiple CVAT instances.",
    )
    parser.add_argument(
        "--sites",
        type=Path,
        help="Path to sites config JSON with per-site CVAT connection details.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/multi-site/unified-manifest.json"),
        help="Output path for the unified manifest JSON.",
    )
    # Single-site shortcut args
    parser.add_argument(
        "--cvat-url",
        default=os.environ.get("CVAT_URL"),
        help="CVAT URL for single-site mode.",
    )
    parser.add_argument(
        "--cvat-username",
        default=os.environ.get("CVAT_USERNAME"),
        help="CVAT username for single-site mode.",
    )
    parser.add_argument(
        "--cvat-password",
        default=os.environ.get("CVAT_PASSWORD"),
        help="CVAT password for single-site mode.",
    )
    parser.add_argument(
        "--site-id",
        help="Site identifier for single-site mode.",
    )
    parser.add_argument(
        "--projects",
        help="Comma-separated project names for single-site mode.",
    )
    parser.add_argument(
        "--skip-version-check",
        action="store_true",
        help="Skip CVAT version compatibility check.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification.",
    )
    return parser.parse_args()


def load_sites_config(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Build sites list from --sites config file or single-site CLI args."""
    if args.sites:
        if not args.sites.exists():
            raise FileNotFoundError(f"sites config not found: {args.sites}")
        config = json.loads(args.sites.read_text(encoding="utf-8"))
        sites = config.get("sites", [])
        if not sites:
            raise ValueError("sites config must contain a non-empty 'sites' array")
        return sites

    # Single-site fallback
    if not args.cvat_url or not args.site_id:
        raise SystemExit(
            "provide --sites config file or --cvat-url + --site-id for single-site mode"
        )
    projects = [p.strip() for p in args.projects.split(",")] if args.projects else ["detection-eval", "tracking-eval"]
    return [
        {
            "site_id": args.site_id,
            "cvat_url": args.cvat_url,
            "cvat_username": args.cvat_username,
            "cvat_password": args.cvat_password,
            "projects": projects,
            "conditions": {},
        }
    ]


def main() -> None:
    args = parse_args()
    sites = load_sites_config(args)

    all_items: list[dict[str, Any]] = []
    site_ids: list[str] = []

    for site_config in sites:
        site_id = site_config["site_id"]
        site_ids.append(site_id)
        print(f"\n=== Site: {site_id} ===")
        try:
            items = process_site(
                site_config,
                insecure=args.insecure,
                skip_version_check=args.skip_version_check,
            )
            all_items.extend(items)
            print(f"  Total from {site_id}: {len(items)} items")
        except Exception as exc:
            print(f"  ERROR processing site {site_id}: {exc}")

    # Deduplicate by item_id
    seen: set[str] = set()
    deduplicated: list[dict[str, Any]] = []
    for item in all_items:
        if item["item_id"] not in seen:
            seen.add(item["item_id"])
            deduplicated.append(item)

    manifest = {
        "items": deduplicated,
        "metadata": {
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "sites": site_ids,
            "total_items": len(deduplicated),
            "duplicates_removed": len(all_items) - len(deduplicated),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nUnified manifest: {args.output} ({len(deduplicated)} items from {len(site_ids)} sites)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
