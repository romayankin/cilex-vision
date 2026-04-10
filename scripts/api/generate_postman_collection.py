#!/usr/bin/env python3
"""Convert OpenAPI YAML to a Postman Collection v2.1 JSON file.

Usage:
    python scripts/api/generate_postman_collection.py --input docs/api/openapi.yaml \
        --output docs/api/postman-collection.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = ("get", "post", "put", "delete", "patch")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("docs/api/openapi.yaml"),
        help="Input OpenAPI YAML path.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/api/postman-collection.json"),
        help="Output Postman collection path.",
    )
    return parser.parse_args()


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    target: Any = spec
    for part in ref.lstrip("#/").split("/"):
        target = target[part]
    if not isinstance(target, dict):
        raise ValueError(f"unsupported ref target for {ref}")
    return target


def _example_from_schema(
    schema: dict[str, Any],
    spec: dict[str, Any],
    seen: set[str] | None = None,
) -> Any:
    seen = seen or set()

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            return {}
        seen.add(ref)
        return _example_from_schema(_resolve_ref(spec, ref), spec, seen)

    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        for candidate in any_of:
            if candidate.get("type") != "null":
                return _example_from_schema(candidate, spec, seen.copy())
        return None

    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        return {
            key: _example_from_schema(value, spec, seen.copy())
            for key, value in properties.items()
        }
    if schema_type == "array":
        item_schema = schema.get("items", {})
        return [_example_from_schema(item_schema, spec, seen.copy())]
    if schema_type == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-04-10T00:00:00Z"
        if fmt == "date":
            return "2026-04-10"
        if fmt == "uuid":
            return "00000000-0000-0000-0000-000000000001"
        return "string"
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return True
    return {}


def _parameter_example(parameter: dict[str, Any], spec: dict[str, Any]) -> str:
    if "example" in parameter:
        return str(parameter["example"])
    schema = parameter.get("schema", {})
    example = _example_from_schema(schema, spec)
    return "" if example is None else str(example)


def _build_url(
    path: str,
    parameters: list[dict[str, Any]],
    spec: dict[str, Any],
) -> dict[str, Any]:
    query: list[dict[str, str]] = []
    variables: list[dict[str, str]] = []

    path_segments: list[str] = []
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        if segment.startswith("{") and segment.endswith("}"):
            key = segment[1:-1]
            value = ""
            for parameter in parameters:
                if parameter.get("in") == "path" and parameter.get("name") == key:
                    value = _parameter_example(parameter, spec)
                    break
            variables.append({"key": key, "value": value})
            path_segments.append(f":{key}")
        else:
            path_segments.append(segment)

    for parameter in parameters:
        if parameter.get("in") != "query":
            continue
        query.append(
            {
                "key": parameter["name"],
                "value": _parameter_example(parameter, spec),
                "description": parameter.get("description", ""),
            }
        )

    raw = "{{base_url}}"
    if path_segments:
        raw += "/" + "/".join(path_segments)
    if query:
        raw += "?" + "&".join(f"{item['key']}={item['value']}" for item in query)

    return {
        "raw": raw,
        "host": ["{{base_url}}"],
        "path": path_segments,
        "query": query,
        "variable": variables,
    }


def _build_request_body(
    operation: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    content = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json")
    )
    if content is None:
        return None

    example = content.get("example")
    if example is None:
        example = _example_from_schema(content.get("schema", {}), spec)

    return {
        "mode": "raw",
        "raw": json.dumps(example, indent=2, sort_keys=False),
        "options": {"raw": {"language": "json"}},
    }


def _build_responses(
    operation: dict[str, Any],
    method: str,
    path: str,
    spec: dict[str, Any],
) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []

    for status_code, response in operation.get("responses", {}).items():
        content = response.get("content", {})
        body = ""
        preview_language = "text"
        headers: list[dict[str, str]] = []

        if "application/json" in content:
            media_type = content["application/json"]
            example = media_type.get("example")
            if example is None:
                example = _example_from_schema(media_type.get("schema", {}), spec)
            body = json.dumps(example, indent=2, sort_keys=False)
            preview_language = "json"
            headers.append({"key": "Content-Type", "value": "application/json"})
        elif "text/plain" in content:
            media_type = content["text/plain"]
            example = _example_from_schema(media_type.get("schema", {}), spec)
            body = str(example)
            preview_language = "text"
            headers.append({"key": "Content-Type", "value": "text/plain"})

        responses.append(
            {
                "name": response.get("description", f"{status_code} response"),
                "originalRequest": {
                    "method": method.upper(),
                    "header": [],
                    "url": {"raw": "{{base_url}}" + path},
                },
                "status": response.get("description", "Response"),
                "code": int(status_code),
                "_postman_previewlanguage": preview_language,
                "header": headers,
                "body": body,
            }
        )

    return responses


def _build_item(
    path: str,
    method: str,
    operation: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    parameters = operation.get("parameters", [])
    body = _build_request_body(operation, spec)
    item: dict[str, Any] = {
        "name": operation.get("summary", f"{method.upper()} {path}"),
        "request": {
            "method": method.upper(),
            "header": [
                {
                    "key": "Accept",
                    "value": "application/json",
                }
            ],
            "description": operation.get("description", ""),
            "url": _build_url(path, parameters, spec),
        },
        "response": _build_responses(operation, method, path, spec),
    }

    if body is not None:
        item["request"]["header"].append(
            {"key": "Content-Type", "value": "application/json"}
        )
        item["request"]["body"] = body

    if operation.get("security") is None:
        item["request"]["auth"] = {"type": "noauth"}

    return item


def _build_collection(spec: dict[str, Any]) -> dict[str, Any]:
    folders: dict[str, list[dict[str, Any]]] = {}

    for path, path_item in spec.get("paths", {}).items():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            tags = operation.get("tags", ["public"])
            folder_name = tags[0]
            folders.setdefault(folder_name, []).append(
                _build_item(path, method, operation, spec)
            )

    items = [
        {"name": folder_name, "item": entries}
        for folder_name, entries in sorted(folders.items())
    ]

    return {
        "info": {
            "name": spec.get("info", {}).get("title", "Cilex Vision API"),
            "description": spec.get("info", {}).get("description", ""),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": {
            "type": "apikey",
            "apikey": [
                {"key": "key", "value": "access_token", "type": "string"},
                {"key": "value", "value": "{{access_token}}", "type": "string"},
                {"key": "in", "value": "cookie", "type": "string"},
            ],
        },
        "variable": [
            {"key": "base_url", "value": "http://localhost:8000"},
            {"key": "access_token", "value": "PASTE_JWT_HERE"},
        ],
        "item": items,
    }


def main() -> None:
    args = _parse_args()
    with args.input.open("r", encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)

    collection = _build_collection(spec)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(collection, handle, indent=2)
        handle.write("\n")

    print(f"wrote {args.output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
