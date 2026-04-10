#!/usr/bin/env python3
"""Generate an OpenAPI document for the Query API.

The spec is built from the real FastAPI route metadata without starting the
application lifespan, so no live PostgreSQL or MinIO connections are required.

Usage:
    python scripts/api/generate_openapi.py --output docs/api/openapi.yaml
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
QUERY_API_ROOT = REPO_ROOT / "services" / "query-api"

PUBLIC_ENDPOINTS: set[tuple[str, str]] = {
    ("/health", "get"),
    ("/ready", "get"),
    ("/metrics", "get"),
}

TAG_METADATA: list[dict[str, str]] = [
    {
        "name": "detections",
        "description": "Time-windowed detection search from the TimescaleDB hypertable.",
    },
    {
        "name": "tracks",
        "description": "Track summaries and per-track detail, including joined attributes.",
    },
    {
        "name": "events",
        "description": "Event search with signed clip URLs when clip assets are available.",
    },
    {
        "name": "topology",
        "description": "Site topology graph read/write operations for cameras and transition edges.",
    },
    {
        "name": "debug",
        "description": "Engineering-only debug trace listing and retrieval from MinIO.",
    },
    {
        "name": "public",
        "description": "Unauthenticated health, readiness, and Prometheus endpoints.",
    },
]

OPERATION_METADATA: dict[tuple[str, str], dict[str, Any]] = {
    ("/detections", "get"): {
        "roles": ["admin", "operator", "viewer", "engineering"],
        "parameter_examples": {
            "camera_id": "cam-01",
            "start": "2026-04-10T00:00:00Z",
            "end": "2026-04-10T23:59:59Z",
            "class": "person",
            "min_confidence": 0.6,
            "offset": 0,
            "limit": 10,
        },
        "responses": ["401", "403"],
    },
    ("/tracks", "get"): {
        "roles": ["admin", "operator", "viewer", "engineering"],
        "parameter_examples": {
            "camera_id": "cam-01",
            "start": "2026-04-10T00:00:00Z",
            "end": "2026-04-10T23:59:59Z",
            "class": "person",
            "state": "active",
            "offset": 0,
            "limit": 10,
        },
        "responses": ["401", "403"],
    },
    ("/tracks/{local_track_id}", "get"): {
        "roles": ["admin", "operator", "viewer", "engineering"],
        "parameter_examples": {
            "local_track_id": "00000000-0000-0000-0000-000000000001",
        },
        "responses": ["401", "403", "404"],
    },
    ("/events", "get"): {
        "roles": ["admin", "operator", "viewer"],
        "parameter_examples": {
            "site_id": "11111111-1111-1111-1111-111111111111",
            "camera_id": "cam-01",
            "start": "2026-04-10T00:00:00Z",
            "end": "2026-04-10T23:59:59Z",
            "event_type": "entered_scene",
            "state": "closed",
            "offset": 0,
            "limit": 10,
        },
        "responses": ["401", "403"],
    },
    ("/debug/traces", "get"): {
        "roles": ["engineering", "admin"],
        "parameter_examples": {
            "camera_id": "cam-01",
            "start": "2026-04-10",
            "end": "2026-04-10",
            "track_id": "00000000-0000-0000-0000-000000000001",
            "limit": 10,
        },
        "responses": ["401", "403"],
    },
    ("/debug/traces/{trace_id}", "get"): {
        "roles": ["engineering", "admin"],
        "parameter_examples": {
            "trace_id": "trace-20260410-0001",
            "camera_id": "cam-01",
            "date": "2026-04-10",
        },
        "responses": ["401", "403", "404", "503"],
    },
    ("/topology/{site_id}", "get"): {
        "roles": ["admin", "operator"],
        "parameter_examples": {
            "site_id": "11111111-1111-1111-1111-111111111111",
        },
        "responses": ["401", "403", "503"],
    },
    ("/topology/{site_id}/edges", "put"): {
        "roles": ["admin"],
        "parameter_examples": {
            "site_id": "11111111-1111-1111-1111-111111111111",
        },
        "request_example": {
            "camera_a_id": "cam-entrance",
            "camera_b_id": "cam-lobby",
            "transition_time_s": 12.0,
            "confidence": 0.95,
            "enabled": True,
        },
        "responses": ["401", "403", "404", "503"],
    },
    ("/topology/{site_id}/cameras", "post"): {
        "roles": ["admin"],
        "parameter_examples": {
            "site_id": "11111111-1111-1111-1111-111111111111",
        },
        "request_example": {
            "camera_id": "cam-lobby",
            "name": "Lobby Camera",
            "zone_id": "lobby",
            "latitude": 55.7522,
            "longitude": 37.6156,
            "location_description": "Main lobby entrance",
        },
        "responses": ["401", "403", "404", "409", "503"],
    },
    ("/topology/{site_id}/cameras/{camera_id}", "delete"): {
        "roles": ["admin"],
        "parameter_examples": {
            "site_id": "11111111-1111-1111-1111-111111111111",
            "camera_id": "cam-lobby",
        },
        "responses": ["401", "403", "404", "503"],
    },
}

ERROR_RESPONSE_EXAMPLES: dict[str, dict[str, str]] = {
    "401": {
        "description": "Missing, expired, or invalid access token.",
        "detail": "Authentication required",
    },
    "403": {
        "description": "Authenticated user does not have the required role or scope.",
        "detail": "Role 'viewer' not authorized for this resource",
    },
    "404": {
        "description": "Requested resource was not found.",
        "detail": "Track 00000000-0000-0000-0000-000000000001 not found",
    },
    "409": {
        "description": "Request conflicts with an existing resource.",
        "detail": "Camera cam-lobby already exists",
    },
    "503": {
        "description": "A required backing dependency is unavailable.",
        "detail": "Database not available",
    },
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "docs" / "api" / "openapi.yaml",
        help="OpenAPI YAML output path.",
    )
    return parser.parse_args()


def _build_app() -> Any:
    sys.path.insert(0, str(QUERY_API_ROOT))

    from config import Settings  # noqa: PLC0415
    from main import create_app  # noqa: PLC0415

    return create_app(Settings())


def _error_response(status_code: str) -> dict[str, Any]:
    example = ERROR_RESPONSE_EXAMPLES[status_code]
    return {
        "description": example["description"],
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "example": {"detail": example["detail"]},
            }
        },
    }


def _apply_parameter_examples(operation: dict[str, Any], examples: dict[str, Any]) -> None:
    for parameter in operation.get("parameters", []):
        name = parameter.get("name")
        if name not in examples:
            continue
        parameter["example"] = examples[name]


def _apply_request_example(operation: dict[str, Any], example: dict[str, Any]) -> None:
    content = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json")
    )
    if content is not None:
        content["example"] = example


def _append_auth_details(operation: dict[str, Any], roles: list[str]) -> None:
    auth_note = (
        "Required roles: "
        + ", ".join(f"`{role}`" for role in roles)
        + ". Admin bypasses camera scope; detections, tracks, and events apply "
        + "non-admin `camera_scope` filtering."
    )
    description = operation.get("description", "").strip()
    operation["description"] = (
        f"{description}\n\n{auth_note}" if description else auth_note
    )
    operation["x-cilex-allowed-roles"] = roles


def _patch_spec(spec: dict[str, Any]) -> dict[str, Any]:
    spec["info"]["title"] = "Cilex Vision Query API"
    spec["info"]["description"] = (
        "Read-oriented API for detections, tracks, events, site topology, and "
        "engineering debug traces. This document is generated from the real "
        "FastAPI app and patched with auth and public-endpoint metadata."
    )
    spec["servers"] = [
        {
            "url": "http://localhost:8000",
            "description": "Local development default",
        }
    ]
    spec["tags"] = TAG_METADATA

    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["ErrorResponse"] = {
        "title": "ErrorResponse",
        "type": "object",
        "properties": {
            "detail": {
                "type": "string",
                "description": "Human-readable error description.",
            }
        },
        "required": ["detail"],
    }
    components.setdefault("securitySchemes", {})["cookieAuth"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": "access_token",
        "description": (
            "JWT access token carried in the httpOnly `access_token` cookie. "
            "The Query API does not mint tokens itself."
        ),
    }

    for (path, method), metadata in OPERATION_METADATA.items():
        operation = spec.get("paths", {}).get(path, {}).get(method)
        if operation is None:
            continue

        _apply_parameter_examples(
            operation,
            metadata.get("parameter_examples", {}),
        )

        if metadata.get("request_example") is not None:
            _apply_request_example(operation, metadata["request_example"])

        roles = metadata.get("roles")
        if roles:
            operation["security"] = [{"cookieAuth": []}]
            _append_auth_details(operation, roles)

        for status_code in metadata.get("responses", []):
            operation.setdefault("responses", {}).setdefault(
                status_code,
                _error_response(status_code),
            )

    # Health and readiness are normal FastAPI routes; /metrics is a mounted ASGI
    # app and needs to be described explicitly in the exported spec.
    for path, method in PUBLIC_ENDPOINTS:
        operation = spec.setdefault("paths", {}).setdefault(path, {}).setdefault(method, {})
        operation.setdefault("tags", ["public"])
        if path == "/health":
            operation.setdefault("summary", "Health")
            operation.setdefault("description", "Liveness probe for the Query API.")
            operation.setdefault(
                "responses",
                {
                    "200": {
                        "description": "Service process is alive.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string", "example": "ok"}
                                    },
                                    "required": ["status"],
                                }
                            }
                        },
                    }
                },
            )
        elif path == "/ready":
            operation.setdefault("summary", "Ready")
            operation.setdefault(
                "description",
                "Readiness probe that verifies database connectivity.",
            )
            operation.setdefault(
                "responses",
                {
                    "200": {
                        "description": "Readiness status payload.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string", "example": "ready"},
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["status"],
                                }
                            }
                        },
                    }
                },
            )
        elif path == "/metrics":
            operation.setdefault("summary", "Metrics")
            operation.setdefault(
                "description",
                "Prometheus text exposition for Query API metrics.",
            )
            operation.setdefault(
                "operationId",
                "metrics_metrics_get",
            )
            operation.setdefault(
                "responses",
                {
                    "200": {
                        "description": "Prometheus metrics payload.",
                        "content": {
                            "text/plain": {
                                "schema": {
                                    "type": "string",
                                    "example": (
                                        "# HELP query_requests_total Total API requests by endpoint and method\n"
                                        "# TYPE query_requests_total counter\n"
                                        'query_requests_total{endpoint="/detections",method="GET",status="200"} 42\n'
                                    ),
                                }
                            }
                        },
                    }
                },
            )

    return spec


def main() -> None:
    args = _parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    app = _build_app()
    raw_spec = copy.deepcopy(app.openapi())
    patched_spec = _patch_spec(raw_spec)

    with args.output.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            patched_spec,
            handle,
            sort_keys=False,
            allow_unicode=False,
        )

    print(f"wrote {args.output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
