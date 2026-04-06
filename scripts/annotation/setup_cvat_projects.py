#!/usr/bin/env python3
"""Create the baseline CVAT projects used by the pilot annotation workflow.

The script uses the CVAT REST API directly so it stays compatible with the
Docker deployment pinned in `infra/ansible/playbooks/deploy-cvat.yml`.

It creates three projects:

- detection-eval: 7-class rectangle labels for detector evaluation
- tracking-eval: same 7 classes plus MOT-compatible track attributes
- attribute-eval: only labels that own color attributes in the taxonomy

Existing projects are left untouched when their label schema already matches
the expected baseline. If a project exists with drifted labels, the script
fails unless `--recreate-existing` is used.
"""

from __future__ import annotations

import argparse
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
COLOR_VALUES: tuple[str, ...] = (
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
    expected_export: str
    annotation_mode: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "labels": [label.to_payload() for label in self.labels],
        }


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
        help="Optional CVAT organization slug. Projects are created in that org context.",
    )
    parser.add_argument(
        "--expected-server-version-prefix",
        default=EXPECTED_CVAT_VERSION_PREFIX,
        help="Expected CVAT server version prefix. Defaults to the repo-pinned 2.62.x deployment.",
    )
    parser.add_argument(
        "--skip-version-check",
        action="store_true",
        help="Skip the /api/server/about compatibility check.",
    )
    parser.add_argument(
        "--recreate-existing",
        action="store_true",
        help="Delete and recreate any existing project whose schema does not match the baseline.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate server compatibility and print planned changes without mutating CVAT.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for self-signed internal deployments.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path for a machine-readable report.",
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


def build_headers(args: argparse.Namespace, include_json: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.cvat+json, application/json",
    }
    if include_json:
        headers["Content-Type"] = "application/json"

    if args.organization_slug:
        headers["X-Organization"] = args.organization_slug

    if args.access_token:
        headers["Authorization"] = f"Bearer {args.access_token}"
    else:
        token = base64.b64encode(f"{args.username}:{args.password}".encode("utf-8")).decode("ascii")
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


def desired_projects() -> tuple[ProjectSpec, ...]:
    track_attributes = (
        AttributeSpec(
            name="visibility",
            input_type="number",
            values=("0;1;0.01",),
            default_value="1",
            mutable=True,
        ),
        AttributeSpec(
            name="ignored",
            input_type="checkbox",
            values=(),
            default_value="false",
            mutable=True,
        ),
    )

    vehicle_color = AttributeSpec(
        name="vehicle_color",
        input_type="select",
        values=COLOR_VALUES,
        default_value="unknown",
        mutable=False,
    )
    person_upper_color = AttributeSpec(
        name="person_upper_color",
        input_type="select",
        values=COLOR_VALUES,
        default_value="unknown",
        mutable=False,
    )
    person_lower_color = AttributeSpec(
        name="person_lower_color",
        input_type="select",
        values=COLOR_VALUES,
        default_value="unknown",
        mutable=False,
    )

    detection_labels = tuple(
        LabelSpec(name=label_name, color=LABEL_COLORS[label_name])
        for label_name in OBJECT_CLASSES
    )
    tracking_labels = tuple(
        LabelSpec(name=label_name, color=LABEL_COLORS[label_name], attributes=track_attributes)
        for label_name in OBJECT_CLASSES
    )
    attribute_labels = (
        LabelSpec("person", LABEL_COLORS["person"], (person_upper_color, person_lower_color)),
        LabelSpec("car", LABEL_COLORS["car"], (vehicle_color,)),
        LabelSpec("truck", LABEL_COLORS["truck"], (vehicle_color,)),
        LabelSpec("bus", LABEL_COLORS["bus"], (vehicle_color,)),
        LabelSpec("motorcycle", LABEL_COLORS["motorcycle"], (vehicle_color,)),
    )

    return (
        ProjectSpec(
            name="detection-eval",
            labels=detection_labels,
            purpose="Detector bake-off labels aligned to docs/taxonomy.md.",
            expected_export="COCO 1.0 or Datumaro",
            annotation_mode="annotation",
        ),
        ProjectSpec(
            name="tracking-eval",
            labels=tracking_labels,
            purpose="Tracker bake-off labels with MOT-compatible attributes.",
            expected_export="MOT 1.1",
            annotation_mode="interpolation",
        ),
        ProjectSpec(
            name="attribute-eval",
            labels=attribute_labels,
            purpose="Attribute bake-off labels for color annotation only.",
            expected_export="CVAT for images 1.1 or Datumaro",
            annotation_mode="annotation",
        ),
    )


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
                (normalize_attribute(attribute) for attribute in label.get("attributes", [])),
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


def ensure_server_compatibility(args: argparse.Namespace, headers: dict[str, str]) -> dict[str, Any]:
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
    if not args.skip_version_check and not version.startswith(args.expected_server_version_prefix):
        raise RuntimeError(
            "CVAT version mismatch: "
            f"expected prefix {args.expected_server_version_prefix!r}, got {version!r}"
        )

    formats = request_json(
        "GET",
        args.base_url,
        "/api/server/annotation/formats",
        headers=headers,
        insecure=args.insecure,
    )
    format_strings = {value.strip().lower() for value in extract_strings(formats)}
    if not any(re.match(r"^mot(\s|$)", value) for value in format_strings):
        raise RuntimeError("CVAT server does not advertise MOT export support")
    return about


def create_project(
    args: argparse.Namespace,
    headers: dict[str, str],
    spec: ProjectSpec,
) -> dict[str, Any]:
    return request_json(
        "POST",
        args.base_url,
        "/api/projects",
        headers={**headers, "Content-Type": "application/json"},
        payload=spec.to_payload(),
        insecure=args.insecure,
    )


def delete_project(args: argparse.Namespace, headers: dict[str, str], project_id: int) -> None:
    request_json(
        "DELETE",
        args.base_url,
        f"/api/projects/{project_id}",
        headers=headers,
        insecure=args.insecure,
    )


def main() -> None:
    args = parse_args()
    validate_auth(args)
    if not re.match(r"^https?://", args.base_url):
        raise SystemExit("base URL must include an explicit http:// or https:// scheme")

    headers = build_headers(args)
    about = ensure_server_compatibility(args, headers)

    summary: dict[str, Any] = {
        "server": {
            "base_url": args.base_url,
            "version": about.get("version"),
            "organization_slug": args.organization_slug,
        },
        "projects": [],
    }
    failures: list[str] = []

    for spec in desired_projects():
        existing = get_project_by_name(args.base_url, headers, spec.name, insecure=args.insecure)
        desired_labels = tuple(sorted((normalize_label(label.to_payload()) for label in spec.labels), key=lambda item: item["name"]))

        if existing is None:
            if args.dry_run:
                summary["projects"].append(
                    {
                        "name": spec.name,
                        "status": "planned-create",
                        "annotation_mode": spec.annotation_mode,
                        "expected_export": spec.expected_export,
                        "purpose": spec.purpose,
                    }
                )
                continue

            created = create_project(args, headers, spec)
            summary["projects"].append(
                {
                    "name": spec.name,
                    "id": created.get("id"),
                    "status": "created",
                    "annotation_mode": spec.annotation_mode,
                    "expected_export": spec.expected_export,
                    "purpose": spec.purpose,
                }
            )
            continue

        existing_labels = tuple(
            sorted((normalize_label(label) for label in existing.get("labels", [])), key=lambda item: item["name"])
        )
        if existing_labels == desired_labels:
            summary["projects"].append(
                {
                    "name": spec.name,
                    "id": existing.get("id"),
                    "status": "unchanged",
                    "annotation_mode": spec.annotation_mode,
                    "expected_export": spec.expected_export,
                    "purpose": spec.purpose,
                }
            )
            continue

        if not args.recreate_existing:
            failures.append(
                f"project {spec.name!r} already exists but its label schema differs from the baseline; "
                "rerun with --recreate-existing to replace it"
            )
            summary["projects"].append(
                {
                    "name": spec.name,
                    "id": existing.get("id"),
                    "status": "schema-drift",
                    "annotation_mode": spec.annotation_mode,
                    "expected_export": spec.expected_export,
                    "purpose": spec.purpose,
                }
            )
            continue

        if args.dry_run:
            summary["projects"].append(
                {
                    "name": spec.name,
                    "id": existing.get("id"),
                    "status": "planned-recreate",
                    "annotation_mode": spec.annotation_mode,
                    "expected_export": spec.expected_export,
                    "purpose": spec.purpose,
                }
            )
            continue

        delete_project(args, headers, int(existing["id"]))
        recreated = create_project(args, headers, spec)
        summary["projects"].append(
            {
                "name": spec.name,
                "id": recreated.get("id"),
                "status": "recreated",
                "annotation_mode": spec.annotation_mode,
                "expected_export": spec.expected_export,
                "purpose": spec.purpose,
            }
        )

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2))

    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
