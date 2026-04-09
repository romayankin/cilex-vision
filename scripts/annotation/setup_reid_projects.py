#!/usr/bin/env python3
"""Create the CVAT project for cross-camera Re-ID annotation.

Creates project: reid-eval
Labels: 7 object classes (person, car, truck, bus, bicycle, motorcycle, animal)
Each label has a "global_id" text attribute for cross-camera identity.

Follows the same pattern as setup_cvat_projects.py: urllib.request with basic
auth, --recreate-existing for drift handling, JSON summary output.

Optionally creates annotation tasks pairing clips from adjacent cameras with
overlapping time windows when --dsn and --site-id are provided.

Task creation JSON contract (per task):
{
  "name": "reid: cam-entrance → cam-lobby (2026-04-10T10:00Z)",
  "project_id": 42,
  "labels": [...inherited from project...]
}
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)
EXPECTED_CVAT_VERSION_PREFIX = "2.62."
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

LABEL_COLORS: dict[str, str] = {
    "person": "#ff6b6b",
    "car": "#4dabf7",
    "truck": "#339af0",
    "bus": "#845ef7",
    "bicycle": "#51cf66",
    "motorcycle": "#fcc419",
    "animal": "#ffa94d",
}


@dataclass(frozen=True)
class AttributeSpec:
    name: str
    input_type: str
    values: tuple[str, ...]
    default_value: str
    mutable: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mutable": self.mutable,
            "input_type": self.input_type,
            "default_value": self.default_value,
            "values": list(self.values),
        }


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    attributes: tuple[AttributeSpec, ...] = ()
    shape_type: str = "rectangle"

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "color": self.color,
            "type": self.shape_type,
            "attributes": [attribute.to_payload() for attribute in self.attributes],
        }


@dataclass(frozen=True)
class ProjectSpec:
    name: str
    labels: tuple[LabelSpec, ...]
    purpose: str
    annotation_mode: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "labels": [label.to_payload() for label in self.labels],
        }


# ---------------------------------------------------------------------------
# HTTP helpers (same pattern as setup_cvat_projects.py)
# ---------------------------------------------------------------------------


def build_headers(args: argparse.Namespace, include_json: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/vnd.cvat+json, application/json",
    }
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


def extract_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from extract_strings(nested)
        return
    if isinstance(value, list):
        for nested in value:
            yield from extract_strings(nested)


# ---------------------------------------------------------------------------
# Project spec
# ---------------------------------------------------------------------------


def reid_project_spec() -> ProjectSpec:
    global_id_attr = AttributeSpec(
        name="global_id",
        input_type="text",
        values=(),
        default_value="",
        mutable=False,
    )

    labels = tuple(
        LabelSpec(
            name=cls,
            color=LABEL_COLORS[cls],
            attributes=(global_id_attr,),
        )
        for cls in OBJECT_CLASSES
    )

    return ProjectSpec(
        name="reid-eval",
        labels=labels,
        purpose="Cross-camera Re-ID identity matching for MTMC evaluation.",
        annotation_mode="annotation",
    )


# ---------------------------------------------------------------------------
# Schema comparison
# ---------------------------------------------------------------------------


def normalize_attribute(attribute: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": attribute["name"],
        "mutable": bool(attribute.get("mutable", False)),
        "input_type": str(attribute.get("input_type", "")),
        "default_value": str(attribute.get("default_value", "")),
        "values": tuple(str(value) for value in attribute.get("values", [])),
    }


def normalize_label(label: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": label["name"],
        "color": label.get("color"),
        "type": label.get("type", "rectangle"),
        "attributes": tuple(
            sorted(
                (normalize_attribute(attr) for attr in label.get("attributes", [])),
                key=lambda item: item["name"],
            )
        ),
    }


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
            project_id = project["id"]
            return request_json(
                "GET",
                base_url,
                f"/api/projects/{project_id}",
                headers=headers,
                insecure=insecure,
            )
    return None


# ---------------------------------------------------------------------------
# Task creation from DB camera pairs
# ---------------------------------------------------------------------------


async def load_camera_pairs(dsn: str, site_id: str) -> list[dict[str, Any]]:
    """Query topology_edges for the site to discover adjacent camera pairs."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT te.camera_a_id, te.camera_b_id, te.transition_time_s,
                   te.transit_distributions
            FROM topology_edges te
            JOIN cameras ca ON ca.camera_id = te.camera_a_id
            WHERE ca.site_id = $1
            """,
            site_id,
        )
        return [
            {
                "camera_a": row["camera_a_id"],
                "camera_b": row["camera_b_id"],
                "transition_time_s": row["transition_time_s"],
                "transit_distributions": row["transit_distributions"],
            }
            for row in rows
        ]
    finally:
        await conn.close()


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
        payload={
            "name": task_name,
            "project_id": project_id,
        },
        insecure=args.insecure,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CVAT_URL", DEFAULT_BASE_URL),
        help="CVAT base URL with explicit scheme, e.g. http://127.0.0.1:8080",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("CVAT_ACCESS_TOKEN"),
        help="Personal Access Token. Recommended over password auth.",
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
        "--expected-server-version-prefix",
        default=EXPECTED_CVAT_VERSION_PREFIX,
        help="Expected CVAT server version prefix.",
    )
    parser.add_argument(
        "--skip-version-check",
        action="store_true",
        help="Skip the /api/server/about compatibility check.",
    )
    parser.add_argument(
        "--recreate-existing",
        action="store_true",
        help="Delete and recreate any existing project whose schema does not match.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without mutating CVAT.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path for a machine-readable report.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN for DB-driven camera pair discovery.",
    )
    parser.add_argument(
        "--site-id",
        default=os.environ.get("SITE_ID"),
        help="Site UUID for filtering topology edges.",
    )
    parser.add_argument(
        "--create-tasks",
        action="store_true",
        help="Create annotation tasks for each camera pair (requires --dsn and --site-id).",
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


def ensure_server_compatibility(
    args: argparse.Namespace, headers: dict[str, str]
) -> dict[str, Any]:
    about = request_json(
        "GET",
        args.base_url,
        "/api/server/about",
        headers=headers,
        insecure=args.insecure,
    )
    version = str(about.get("version", ""))
    if not version:
        raise RuntimeError("CVAT /api/server/about did not return a version field")
    if not args.skip_version_check and not version.startswith(
        args.expected_server_version_prefix
    ):
        raise RuntimeError(
            "CVAT version mismatch: "
            f"expected prefix {args.expected_server_version_prefix!r}, got {version!r}"
        )
    return about


def main() -> None:
    args = parse_args()
    validate_auth(args)
    if not re.match(r"^https?://", args.base_url):
        raise SystemExit("base URL must include an explicit http:// or https:// scheme")

    headers = build_headers(args)
    about = ensure_server_compatibility(args, headers)

    spec = reid_project_spec()

    summary: dict[str, Any] = {
        "server": {
            "base_url": args.base_url,
            "version": about.get("version"),
            "organization_slug": args.organization_slug,
        },
        "project": None,
        "tasks": [],
    }

    # --- Ensure project exists with correct schema ---
    existing = get_project_by_name(
        args.base_url, headers, spec.name, insecure=args.insecure
    )
    desired_labels = tuple(
        sorted(
            (normalize_label(label.to_payload()) for label in spec.labels),
            key=lambda item: item["name"],
        )
    )

    project_id: int | None = None

    if existing is None:
        if args.dry_run:
            summary["project"] = {"name": spec.name, "status": "planned-create"}
        else:
            created = request_json(
                "POST",
                args.base_url,
                "/api/projects",
                headers={**headers, "Content-Type": "application/json"},
                payload=spec.to_payload(),
                insecure=args.insecure,
            )
            project_id = created.get("id")
            summary["project"] = {
                "name": spec.name,
                "id": project_id,
                "status": "created",
            }
    else:
        existing_labels = tuple(
            sorted(
                (normalize_label(label) for label in existing.get("labels", [])),
                key=lambda item: item["name"],
            )
        )
        if existing_labels == desired_labels:
            project_id = existing.get("id")
            summary["project"] = {
                "name": spec.name,
                "id": project_id,
                "status": "unchanged",
            }
        elif not args.recreate_existing:
            raise SystemExit(
                f"project {spec.name!r} already exists but its label schema differs "
                "from the baseline; rerun with --recreate-existing to replace it"
            )
        elif args.dry_run:
            summary["project"] = {
                "name": spec.name,
                "id": existing.get("id"),
                "status": "planned-recreate",
            }
        else:
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
            project_id = recreated.get("id")
            summary["project"] = {
                "name": spec.name,
                "id": project_id,
                "status": "recreated",
            }

    # --- Optionally create tasks from DB camera pairs ---
    if args.create_tasks and project_id is not None:
        if not args.dsn or not args.site_id:
            raise SystemExit("--create-tasks requires --dsn and --site-id")
        if args.dry_run:
            summary["tasks"].append({"status": "planned", "note": "skipped in dry-run"})
        else:
            pairs = asyncio.run(load_camera_pairs(args.dsn, args.site_id))
            for pair in pairs:
                task_name = f"reid: {pair['camera_a']} → {pair['camera_b']}"
                task = create_task(args, headers, project_id, task_name)
                summary["tasks"].append(
                    {
                        "task_id": task.get("id"),
                        "name": task_name,
                        "camera_a": pair["camera_a"],
                        "camera_b": pair["camera_b"],
                        "transition_time_s": pair["transition_time_s"],
                    }
                )

    # --- Output ---
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
