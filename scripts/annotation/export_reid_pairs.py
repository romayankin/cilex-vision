#!/usr/bin/env python3
"""Export annotated Re-ID identity pairs from CVAT as structured JSON.

Fetches tasks and annotations from the reid-eval CVAT project, groups
annotations by global_id attribute across tasks/cameras, and computes
cross-annotator agreement (Cohen's kappa on identity assignment).

Output JSON:
{
  "pairs": [
    {
      "global_id": "A-001",
      "sightings": [
        {
          "camera_id": "cam-entrance",
          "local_track_id": "...",
          "timestamp": "...",
          "crop_uri": "..."
        },
        {
          "camera_id": "cam-lobby",
          "local_track_id": "...",
          "timestamp": "...",
          "crop_uri": "..."
        }
      ]
    }
  ],
  "agreement": {
    "identity_kappa": 0.82,
    "pair_count": 150,
    "disagreement_count": 12
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
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8080"


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


def get_task_jobs(
    base_url: str,
    headers: dict[str, str],
    task_id: int,
    *,
    insecure: bool,
) -> list[dict[str, Any]]:
    """Fetch jobs for a task (to identify multiple annotators)."""
    response = request_json(
        "GET",
        base_url,
        f"/api/tasks/{task_id}/jobs",
        headers=headers,
        query={"page_size": 100},
        insecure=insecure,
    )
    return response.get("results", []) if isinstance(response, dict) else response


def get_job_annotations(
    base_url: str,
    headers: dict[str, str],
    job_id: int,
    *,
    insecure: bool,
) -> dict[str, Any]:
    """Fetch annotations for a single job."""
    return request_json(
        "GET",
        base_url,
        f"/api/jobs/{job_id}/annotations",
        headers=headers,
        insecure=insecure,
    )


# ---------------------------------------------------------------------------
# Annotation parsing
# ---------------------------------------------------------------------------


def extract_global_id(shape: dict[str, Any]) -> str | None:
    """Extract the global_id text attribute from a CVAT shape."""
    for attr in shape.get("attributes", []):
        if attr.get("spec_id") is not None:
            # Attribute by name if available
            pass
        name = str(attr.get("name", ""))
        if name == "global_id":
            value = str(attr.get("value", "")).strip()
            return value if value else None
    return None


def extract_camera_id_from_task(task: dict[str, Any]) -> str | None:
    """Try to extract camera_id from task name or metadata.

    Task names follow the pattern: "reid: cam-entrance → cam-lobby (...)"
    We extract both cameras from the task name.
    """
    name = task.get("name", "")
    # Pattern: "reid: cam-a → cam-b"
    parts = name.split("→")
    if len(parts) == 2:
        cam_a = parts[0].replace("reid:", "").strip()
        cam_b = parts[1].strip().split("(")[0].strip()
        return f"{cam_a}|{cam_b}"
    return None


def parse_sighting(
    shape: dict[str, Any],
    camera_id: str,
    task_name: str,
) -> dict[str, Any] | None:
    """Parse a CVAT shape into a sighting record."""
    global_id = extract_global_id(shape)
    if not global_id:
        return None

    label = shape.get("label", "")
    frame = shape.get("frame", 0)

    # Build a synthetic local_track_id from the shape
    track_id = str(shape.get("id", ""))

    return {
        "global_id": global_id,
        "camera_id": camera_id,
        "local_track_id": track_id,
        "object_class": label,
        "frame": frame,
        "task_name": task_name,
    }


# ---------------------------------------------------------------------------
# Agreement computation
# ---------------------------------------------------------------------------


def cohens_kappa(labels_a: list[str], labels_b: list[str]) -> float | None:
    """Compute Cohen's kappa for two lists of categorical labels."""
    if len(labels_a) != len(labels_b):
        raise ValueError("kappa inputs must have the same length")
    if not labels_a:
        return None

    # Collect all categories
    categories = sorted(set(labels_a) | set(labels_b))

    total = len(labels_a)
    observed = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / total

    counts_a = Counter(labels_a)
    counts_b = Counter(labels_b)
    expected = sum(
        (counts_a.get(c, 0) / total) * (counts_b.get(c, 0) / total) for c in categories
    )

    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def compute_agreement(
    annotator_assignments: list[dict[str, str]],
) -> dict[str, Any]:
    """Compute agreement across multiple annotators.

    Each annotator_assignments entry maps shape_key → global_id.
    We compute pairwise Cohen's kappa on identity assignment.
    """
    if len(annotator_assignments) < 2:
        return {
            "identity_kappa": None,
            "pair_count": 0,
            "disagreement_count": 0,
            "note": "fewer than 2 annotators; agreement cannot be computed",
        }

    # Compare first two annotators (dual-annotator model)
    ann_a = annotator_assignments[0]
    ann_b = annotator_assignments[1]

    # Find common shape keys
    common_keys = sorted(set(ann_a.keys()) & set(ann_b.keys()))
    if not common_keys:
        return {
            "identity_kappa": None,
            "pair_count": 0,
            "disagreement_count": 0,
            "note": "no overlapping annotations between annotators",
        }

    labels_a = [ann_a[k] for k in common_keys]
    labels_b = [ann_b[k] for k in common_keys]

    disagreements = sum(1 for a, b in zip(labels_a, labels_b) if a != b)
    kappa = cohens_kappa(labels_a, labels_b)

    return {
        "identity_kappa": round(kappa, 4) if kappa is not None else None,
        "pair_count": len(common_keys),
        "disagreement_count": disagreements,
    }


# ---------------------------------------------------------------------------
# Main export logic
# ---------------------------------------------------------------------------


def export_pairs(
    base_url: str,
    headers: dict[str, str],
    project_name: str,
    *,
    insecure: bool,
) -> dict[str, Any]:
    """Fetch and structure all Re-ID pairs from the CVAT project."""
    project = get_project_by_name(base_url, headers, project_name, insecure=insecure)
    if project is None:
        raise RuntimeError(f"project {project_name!r} not found in CVAT")

    project_id = project["id"]
    tasks = get_project_tasks(base_url, headers, project_id, insecure=insecure)

    # Collect sightings grouped by global_id
    sightings_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # Collect per-annotator assignments for agreement computation
    annotator_assignments: dict[str, dict[str, str]] = defaultdict(dict)

    for task in tasks:
        task_id = task["id"]
        task_name = task.get("name", "")
        camera_info = extract_camera_id_from_task(task)

        # Try to get per-job annotations (multi-annotator)
        jobs = get_task_jobs(base_url, headers, task_id, insecure=insecure)

        if len(jobs) >= 2:
            # Multi-annotator: process each job separately for agreement
            for job in jobs:
                job_id = job["id"]
                assignee = job.get("assignee", {})
                annotator_id = str(
                    assignee.get("username", f"job-{job_id}")
                    if isinstance(assignee, dict)
                    else f"job-{job_id}"
                )

                ann_data = get_job_annotations(
                    base_url, headers, job_id, insecure=insecure
                )
                shapes = ann_data.get("shapes", []) if isinstance(ann_data, dict) else []

                for shape in shapes:
                    global_id = extract_global_id(shape)
                    if not global_id:
                        continue

                    shape_key = f"{task_id}:{shape.get('frame', 0)}:{shape.get('id', '')}"
                    annotator_assignments[annotator_id][shape_key] = global_id

                    camera_id = camera_info.split("|")[0] if camera_info else task_name
                    sighting = parse_sighting(shape, camera_id, task_name)
                    if sighting:
                        sightings_by_id[global_id].append(
                            {
                                "camera_id": sighting["camera_id"],
                                "local_track_id": sighting["local_track_id"],
                                "timestamp": None,
                                "crop_uri": None,
                            }
                        )
        else:
            # Single annotator or merged annotations
            ann_data = get_task_annotations(
                base_url, headers, task_id, insecure=insecure
            )
            shapes = ann_data.get("shapes", []) if isinstance(ann_data, dict) else []

            for shape in shapes:
                global_id = extract_global_id(shape)
                if not global_id:
                    continue

                camera_id = camera_info.split("|")[0] if camera_info else task_name
                sighting = parse_sighting(shape, camera_id, task_name)
                if sighting:
                    sightings_by_id[global_id].append(
                        {
                            "camera_id": sighting["camera_id"],
                            "local_track_id": sighting["local_track_id"],
                            "timestamp": None,
                            "crop_uri": None,
                        }
                    )

    # Build pairs (only include identities with 2+ sightings across cameras)
    pairs: list[dict[str, Any]] = []
    for global_id in sorted(sightings_by_id.keys()):
        sightings = sightings_by_id[global_id]
        camera_ids = {s["camera_id"] for s in sightings}
        if len(camera_ids) < 2:
            continue
        pairs.append({"global_id": global_id, "sightings": sightings})

    # Compute agreement
    agreement = compute_agreement(list(annotator_assignments.values()))

    return {
        "pairs": pairs,
        "agreement": agreement,
    }


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
        "--project-name",
        default="reid-eval",
        help="CVAT project name (default: reid-eval).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/annotation/reid-pairs.json"),
        help="Output JSON path.",
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
    result = export_pairs(
        args.cvat_url, headers, args.project_name, insecure=args.insecure
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
