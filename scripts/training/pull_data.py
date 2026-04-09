#!/usr/bin/env python3
"""Export latest approved annotations from CVAT and organize for training.

Fetches tasks from a CVAT project, exports annotations in the requested
format (COCO 1.0, Datumaro, CVAT for images), and saves them alongside an
export manifest with metadata.

Usage:
    python pull_data.py --cvat-url http://localhost:8080 --project detection-eval \
        --output-dir data/training/raw --format COCO

Export manifest JSON:
{
  "export_time": "2026-04-10T12:00:00+00:00",
  "project": "detection-eval",
  "project_id": 1,
  "format": "COCO 1.0",
  "task_count": 12,
  "annotation_count": 3400,
  "tasks": [
    {"task_id": 1, "name": "...", "status": "completed", "annotation_count": 280}
  ]
}
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8080"

SUPPORTED_FORMATS: dict[str, str] = {
    "COCO": "COCO 1.0",
    "Datumaro": "Datumaro 1.0",
    "CVAT": "CVAT for images 1.1",
}


# ---------------------------------------------------------------------------
# HTTP helpers (same pattern as setup_cvat_projects.py)
# ---------------------------------------------------------------------------


def build_headers(args: argparse.Namespace) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/vnd.cvat+json, application/json",
    }
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


def request_bytes(
    method: str,
    base_url: str,
    path: str,
    *,
    headers: dict[str, str],
    query: dict[str, Any] | None = None,
    insecure: bool = False,
    timeout: int = 300,
) -> bytes:
    """Make an HTTP request and return raw bytes (for dataset downloads)."""
    url = build_url(base_url, path, query=query)
    request = urllib.request.Request(url, headers=headers, method=method)
    ssl_context = create_ssl_context(insecure)

    try:
        with urllib.request.urlopen(request, context=ssl_context, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        message = detail or exc.reason
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"unable to reach CVAT at {base_url}: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# CVAT data extraction
# ---------------------------------------------------------------------------


def get_project_by_name(
    base_url: str,
    headers: dict[str, str],
    name: str,
    *,
    insecure: bool,
) -> dict[str, Any] | None:
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
    return None


def get_project_tasks(
    base_url: str,
    headers: dict[str, str],
    project_id: int,
    *,
    insecure: bool,
) -> list[dict[str, Any]]:
    """Fetch all tasks in a project, handling pagination."""
    tasks: list[dict[str, Any]] = []
    page = 1
    while True:
        response = request_json(
            "GET",
            base_url,
            "/api/tasks",
            headers=headers,
            query={"project_id": project_id, "page_size": 100, "page": page},
            insecure=insecure,
        )
        results = response.get("results", []) if isinstance(response, dict) else response
        tasks.extend(results)
        if isinstance(response, dict) and response.get("next"):
            page += 1
        else:
            break
    return tasks


def get_task_annotations(
    base_url: str,
    headers: dict[str, str],
    task_id: int,
    *,
    insecure: bool,
) -> dict[str, Any]:
    """Fetch annotations for a single task."""
    return request_json(
        "GET",
        base_url,
        f"/api/tasks/{task_id}/annotations",
        headers=headers,
        insecure=insecure,
    )


def count_annotations(ann_data: Any) -> int:
    """Count shapes/tags/tracks in annotation data."""
    if not isinstance(ann_data, dict):
        return 0
    shapes = len(ann_data.get("shapes", []))
    tags = len(ann_data.get("tags", []))
    tracks = len(ann_data.get("tracks", []))
    return shapes + tags + tracks


def export_task_dataset(
    base_url: str,
    headers: dict[str, str],
    task_id: int,
    fmt: str,
    *,
    insecure: bool,
    poll_interval: float = 2.0,
    max_wait: float = 300.0,
) -> bytes:
    """Export a task's dataset in the given format.

    CVAT dataset export is async: POST to create the export, then poll
    until ready, then GET the result.
    """
    # Initiate export
    query: dict[str, Any] = {"format": fmt}
    url = build_url(base_url, f"/api/tasks/{task_id}/dataset", query=query)
    req = urllib.request.Request(url, headers=headers, method="GET")
    ssl_context = create_ssl_context(insecure)

    elapsed = 0.0
    while elapsed < max_wait:
        try:
            with urllib.request.urlopen(req, context=ssl_context) as response:
                if response.status == 200:
                    return response.read()
                # 202 = still preparing
        except urllib.error.HTTPError as exc:
            if exc.code == 202:
                time.sleep(poll_interval)
                elapsed += poll_interval
                continue
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"export task {task_id} failed with HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"unable to reach CVAT at {base_url}: {exc.reason}") from exc

        time.sleep(poll_interval)
        elapsed += poll_interval

    raise RuntimeError(f"export task {task_id} timed out after {max_wait}s")


def extract_zip_to_dir(data: bytes, output_dir: Path, task_name: str) -> Path:
    """Extract a zip archive into a task-specific subdirectory."""
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_name)
    task_dir = output_dir / safe_name
    task_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(task_dir)

    return task_dir


# ---------------------------------------------------------------------------
# Main export logic
# ---------------------------------------------------------------------------


def pull_data(
    base_url: str,
    headers: dict[str, str],
    project_name: str,
    output_dir: Path,
    fmt: str,
    *,
    insecure: bool,
) -> dict[str, Any]:
    """Export all task datasets from the given CVAT project."""
    project = get_project_by_name(base_url, headers, project_name, insecure=insecure)
    if project is None:
        raise RuntimeError(f"project {project_name!r} not found in CVAT")

    project_id = project["id"]
    tasks = get_project_tasks(base_url, headers, project_id, insecure=insecure)

    output_dir.mkdir(parents=True, exist_ok=True)

    cvat_format = SUPPORTED_FORMATS.get(fmt, fmt)
    task_manifests: list[dict[str, Any]] = []
    total_annotations = 0

    for task in tasks:
        task_id = task["id"]
        task_name = task.get("name", f"task-{task_id}")
        task_status = task.get("status", "unknown")

        # Count annotations
        ann_data = get_task_annotations(base_url, headers, task_id, insecure=insecure)
        ann_count = count_annotations(ann_data)
        total_annotations += ann_count

        # Export dataset
        try:
            dataset_bytes = export_task_dataset(
                base_url, headers, task_id, cvat_format, insecure=insecure
            )
            extract_zip_to_dir(dataset_bytes, output_dir, task_name)
            export_status = "exported"
        except RuntimeError as exc:
            export_status = f"failed: {exc}"

        task_manifests.append({
            "task_id": task_id,
            "name": task_name,
            "status": task_status,
            "annotation_count": ann_count,
            "export_status": export_status,
        })

    manifest = {
        "export_time": datetime.now(timezone.utc).isoformat(),
        "project": project_name,
        "project_id": project_id,
        "format": cvat_format,
        "task_count": len(tasks),
        "annotation_count": total_annotations,
        "tasks": task_manifests,
    }

    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cvat-url",
        default=os.environ.get("CVAT_URL", DEFAULT_BASE_URL),
        help="CVAT base URL.",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("CVAT_ACCESS_TOKEN"),
        help="Personal Access Token.",
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
        default="detection-eval",
        help="CVAT project name (default: detection-eval).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/training/raw"),
        help="Output directory for exported datasets.",
    )
    parser.add_argument(
        "--format",
        dest="export_format",
        choices=list(SUPPORTED_FORMATS.keys()),
        default="COCO",
        help="Export format (default: COCO).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification.",
    )
    return parser.parse_args()


def validate_auth(args: argparse.Namespace) -> None:
    if args.access_token:
        return
    if args.username and args.password:
        return
    raise SystemExit(
        "authentication required: supply --access-token or both --username and --password"
    )


def main() -> None:
    args = parse_args()
    validate_auth(args)

    headers = build_headers(args)
    manifest = pull_data(
        args.cvat_url,
        headers,
        args.project,
        args.output_dir,
        args.export_format,
        insecure=args.insecure,
    )

    manifest_path = args.output_dir / "export_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
