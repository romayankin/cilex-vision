#!/usr/bin/env python3
"""Export Re-ID ground truth from CVAT or from a pre-exported JSON file.

Usage:
    # From CVAT directly:
    python export_reid_gt.py --cvat-url http://localhost:8080 \
        --project reid-eval --output data/eval/reid/ground_truth.json

    # From pre-exported pairs JSON:
    python export_reid_gt.py --input exported_pairs.json \
        --output data/eval/reid/ground_truth.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.annotation.export_reid_pairs import (  # noqa: E402
    DEFAULT_BASE_URL,
    build_headers,
    export_pairs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        help="Optional pre-exported reid pairs JSON from scripts/annotation/export_reid_pairs.py.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/eval/reid/ground_truth.json"),
        help="Output path for the evaluation-ready ground truth JSON.",
    )
    parser.add_argument(
        "--cvat-url",
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
        default="reid-eval",
        help="CVAT project name when exporting directly from CVAT.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for CVAT HTTPS.",
    )
    return parser.parse_args()


def load_source_payload(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    if args.input is not None:
        if not args.input.exists():
            raise RuntimeError(f"input JSON not found: {args.input}")
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("input JSON must be a top-level object")
        metadata = {
            "source": "pairs_json",
            "source_path": str(args.input),
            "source_project": payload.get("metadata", {}).get("source_project")
            if isinstance(payload.get("metadata"), dict)
            else None,
        }
        return payload, metadata

    validate_auth(args)
    payload = export_pairs(
        args.cvat_url,
        build_headers(args),
        args.project,
        insecure=args.insecure,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("CVAT export returned an unexpected payload type")
    metadata = {
        "source": "cvat",
        "source_path": None,
        "source_project": args.project,
        "cvat_url": args.cvat_url,
    }
    return payload, metadata


def transform_payload(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    raw_pairs = payload.get("pairs")
    if not isinstance(raw_pairs, list):
        raise RuntimeError("source payload must contain a 'pairs' list")

    identity_groups: list[dict[str, Any]] = []
    invalid_groups = 0
    total_sightings = 0

    for raw_pair in raw_pairs:
        if not isinstance(raw_pair, dict):
            raise RuntimeError("each entry in 'pairs' must be an object")
        global_id = require_non_empty_string(raw_pair.get("global_id"), "global_id")
        raw_sightings = raw_pair.get("sightings")
        if not isinstance(raw_sightings, list):
            raise RuntimeError(f"pair {global_id!r} must contain a sightings list")

        normalized_sightings = normalize_sightings(raw_sightings, global_id)
        camera_ids = {sighting["camera_id"] for sighting in normalized_sightings}
        if len(normalized_sightings) < 2 or len(camera_ids) < 2:
            invalid_groups += 1
            continue
        total_sightings += len(normalized_sightings)
        identity_groups.append(
            {
                "global_id": global_id,
                "sightings": normalized_sightings,
            }
        )

    if not identity_groups:
        raise RuntimeError(
            "no valid cross-camera identities found; expected at least one identity "
            "with 2+ sightings across 2+ cameras"
        )

    return {
        "identity_groups": identity_groups,
        "metadata": {
            "export_timestamp": datetime.now(tz=UTC).isoformat(),
            "source": metadata["source"],
            "source_path": metadata.get("source_path"),
            "source_project": metadata.get("source_project"),
            "cvat_url": metadata.get("cvat_url"),
            "agreement": payload.get("agreement"),
            "identity_group_count": len(identity_groups),
            "invalid_group_count": invalid_groups,
            "total_sightings": total_sightings,
        },
    }


def normalize_sightings(
    raw_sightings: list[dict[str, Any]],
    global_id: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str | None]] = set()
    for raw_sighting in raw_sightings:
        if not isinstance(raw_sighting, dict):
            raise RuntimeError(f"identity {global_id!r} contains a non-object sighting")
        local_track_id = require_non_empty_string(
            raw_sighting.get("local_track_id"),
            "local_track_id",
        )
        validate_uuid(local_track_id, global_id)
        camera_id = require_non_empty_string(raw_sighting.get("camera_id"), "camera_id")
        timestamp = optional_string(raw_sighting.get("timestamp"))
        sighting_key = (local_track_id, camera_id, timestamp)
        if sighting_key in seen_keys:
            continue
        seen_keys.add(sighting_key)
        normalized.append(
            {
                "local_track_id": local_track_id,
                "camera_id": camera_id,
                "timestamp": timestamp,
                "crop_uri": optional_string(raw_sighting.get("crop_uri")),
                "object_class": optional_string(raw_sighting.get("object_class")),
            }
        )
    return normalized


def validate_uuid(local_track_id: str, global_id: str) -> None:
    try:
        UUID(local_track_id)
    except ValueError as exc:
        raise RuntimeError(
            f"identity {global_id!r} contains non-UUID local_track_id {local_track_id!r}; "
            "evaluation requires DB local_tracks UUIDs. If this came from CVAT, "
            "the annotation export is missing the DB track mapping."
        ) from exc


def require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{field_name} must be a non-empty string")
    return value.strip()


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("optional string field must be a string when present")
    stripped = value.strip()
    return stripped if stripped else None


def validate_auth(args: argparse.Namespace) -> None:
    if args.access_token:
        return
    if args.username and args.password:
        return
    raise RuntimeError(
        "authentication required: supply --access-token or both --username and --password"
    )


def main() -> None:
    args = parse_args()
    payload, metadata = load_source_payload(args)
    transformed = transform_payload(payload, metadata)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(transformed, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "identity_group_count": transformed["metadata"]["identity_group_count"],
                "source": transformed["metadata"]["source"],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
