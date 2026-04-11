#!/usr/bin/env python3
"""Generate a synthetic multi-zone dataset for MTMC zone sharding benchmarks.

Usage:
    python generate_zone_dataset.py --zones 3 --cameras-per-zone 10 \
        --identities 500 --cross-zone-fraction 0.2 \
        --output data/eval/zone-benchmark/dataset.json
"""

from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)


@dataclass(frozen=True)
class ZoneSpec:
    zone_id: str
    cameras: tuple[str, ...]
    boundary_cameras: tuple[str, ...]
    adjacent_zones: tuple[str, ...]


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            f"missing optional dependency '{module_name}'; install {install_hint}"
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zones", type=int, default=3, help="Number of zones to generate.")
    parser.add_argument(
        "--cameras-per-zone",
        type=int,
        default=10,
        help="Number of cameras per synthetic zone.",
    )
    parser.add_argument(
        "--identities",
        type=int,
        default=500,
        help="Number of synthetic identities to generate.",
    )
    parser.add_argument(
        "--cross-zone-fraction",
        type=float,
        default=0.2,
        help="Fraction of identities that span adjacent zones.",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=512,
        help="Embedding dimensionality.",
    )
    parser.add_argument(
        "--noise",
        type=float,
        default=0.05,
        help="Embedding noise factor for same-identity sightings.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination JSON dataset path.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic RNG seed.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.zones <= 0:
        raise RuntimeError("--zones must be positive")
    if args.cameras_per_zone < 3:
        raise RuntimeError("--cameras-per-zone must be at least 3")
    if args.identities <= 0:
        raise RuntimeError("--identities must be positive")
    if not 0.0 <= args.cross_zone_fraction <= 1.0:
        raise RuntimeError("--cross-zone-fraction must be between 0.0 and 1.0")
    if args.embedding_dim <= 0:
        raise RuntimeError("--embedding-dim must be positive")
    if not 0.0 <= args.noise < 1.0:
        raise RuntimeError("--noise must be between 0.0 and 1.0")


def normalize_vector(vector: Any) -> Any:
    numpy = require_module("numpy", "numpy")
    norm = float(numpy.linalg.norm(vector))
    if norm == 0.0:
        raise RuntimeError("encountered a zero-norm embedding")
    return vector / norm


def make_l2_normalised(
    dim: int,
    rng: Any,
) -> Any:
    numpy = require_module("numpy", "numpy")
    vector = rng.standard_normal(dim).astype(numpy.float32)
    return normalize_vector(vector)


def make_similar_vector(
    base: Any,
    similarity: float,
    rng: Any,
) -> Any:
    numpy = require_module("numpy", "numpy")
    clamped_similarity = min(max(similarity, 0.0), 0.9999)
    noise = rng.standard_normal(len(base)).astype(numpy.float32)
    noise = normalize_vector(noise)
    candidate = clamped_similarity * base + numpy.sqrt(1 - clamped_similarity**2) * noise
    return normalize_vector(candidate).astype(numpy.float32)


def build_zones(
    *,
    zone_count: int,
    cameras_per_zone: int,
) -> tuple[list[ZoneSpec], list[str]]:
    zones: list[ZoneSpec] = []
    camera_order: list[str] = []
    camera_counter = 1
    for zone_index in range(zone_count):
        zone_id = f"zone-{zone_index:02d}"
        cameras = tuple(
            f"cam-{camera_counter + offset:03d}" for offset in range(cameras_per_zone)
        )
        camera_counter += cameras_per_zone
        boundary_cameras: list[str] = []
        adjacent_zones: list[str] = []
        if zone_index > 0:
            boundary_cameras.append(cameras[0])
            adjacent_zones.append(f"zone-{zone_index - 1:02d}")
        if zone_index < zone_count - 1:
            boundary_cameras.append(cameras[-1])
            adjacent_zones.append(f"zone-{zone_index + 1:02d}")
        if not boundary_cameras:
            boundary_cameras.append(cameras[-1])
        zones.append(
            ZoneSpec(
                zone_id=zone_id,
                cameras=cameras,
                boundary_cameras=tuple(boundary_cameras),
                adjacent_zones=tuple(adjacent_zones),
            )
        )
        camera_order.extend(cameras)
    return zones, camera_order


def choose_intra_zone_cameras(
    zone: ZoneSpec,
    sighting_count: int,
    rng: Any,
) -> list[str]:
    preferred = [camera_id for camera_id in zone.cameras if camera_id not in zone.boundary_cameras]
    pool = preferred if len(preferred) >= sighting_count else list(zone.cameras)
    return sorted(rng.choice(pool, size=sighting_count, replace=False).tolist())


def choose_cross_zone_cameras(
    left_zone: ZoneSpec,
    right_zone: ZoneSpec,
    sighting_count: int,
    rng: Any,
) -> list[tuple[str, str]]:
    left_boundary = left_zone.cameras[-1]
    right_boundary = right_zone.cameras[0]
    assignments: list[tuple[str, str]] = [
        (left_boundary, left_zone.zone_id),
        (right_boundary, right_zone.zone_id),
    ]
    remaining = sighting_count - len(assignments)
    if remaining <= 0:
        return assignments

    expansion_pool: list[tuple[str, str]] = []
    for camera_id in left_zone.cameras[:-1]:
        expansion_pool.append((camera_id, left_zone.zone_id))
    for camera_id in right_zone.cameras[1:]:
        expansion_pool.append((camera_id, right_zone.zone_id))

    extra = rng.choice(len(expansion_pool), size=remaining, replace=False).tolist()
    for index in extra:
        assignments.append(expansion_pool[index])
    return assignments


def build_timestamp(
    base_time: datetime,
    identity_index: int,
    sighting_index: int,
    offset_seconds: int,
) -> str:
    timestamp = base_time + timedelta(minutes=identity_index * 3, seconds=offset_seconds)
    timestamp = timestamp + timedelta(seconds=sighting_index * 25)
    return timestamp.isoformat().replace("+00:00", "Z")


def build_pair_payload(
    identities: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pairs: list[dict[str, Any]] = []
    cross_zone_pairs: list[dict[str, Any]] = []
    for identity in identities:
        sightings = identity["sightings"]
        for left, right in combinations(sightings, 2):
            if left["camera_id"] == right["camera_id"]:
                continue
            pair = {
                "identity_id": identity["identity_id"],
                "track_a": left["local_track_id"],
                "track_b": right["local_track_id"],
                "camera_a": left["camera_id"],
                "camera_b": right["camera_id"],
                "zone_a": left["zone_id"],
                "zone_b": right["zone_id"],
                "cross_zone": left["zone_id"] != right["zone_id"],
            }
            pairs.append(pair)
            if pair["cross_zone"]:
                cross_zone_pairs.append(pair)
    return pairs, cross_zone_pairs


def generate_dataset(args: argparse.Namespace) -> dict[str, Any]:
    numpy = require_module("numpy", "numpy")
    rng = numpy.random.default_rng(args.seed)
    zones, camera_order = build_zones(
        zone_count=args.zones,
        cameras_per_zone=args.cameras_per_zone,
    )
    zone_by_id = {zone.zone_id: zone for zone in zones}

    cross_zone_identities = min(
        args.identities,
        int(round(args.identities * args.cross_zone_fraction)),
    )
    base_time = datetime(2026, 4, 10, 8, 0, 0, tzinfo=timezone.utc)

    identities: list[dict[str, Any]] = []
    identity_groups: list[dict[str, Any]] = []
    intra_similarity = max(0.75, 1.0 - args.noise)
    cross_zone_similarity = max(0.70, 1.0 - (args.noise * 1.6))

    for identity_index in range(args.identities):
        identity_id = f"id-{identity_index + 1:04d}"
        object_class = OBJECT_CLASSES[identity_index % len(OBJECT_CLASSES)]
        base_vector = make_l2_normalised(args.embedding_dim, rng)
        is_cross_zone = identity_index < cross_zone_identities and len(zones) > 1
        sighting_count = int(rng.integers(2, 5))
        sightings: list[dict[str, Any]] = []

        if is_cross_zone:
            left_index = int(rng.integers(0, len(zones) - 1))
            left_zone = zones[left_index]
            right_zone = zones[left_index + 1]
            assignments = choose_cross_zone_cameras(left_zone, right_zone, sighting_count, rng)
            for sighting_index, (camera_id, zone_id) in enumerate(assignments):
                vector = make_similar_vector(base_vector, cross_zone_similarity, rng)
                offset_seconds = 110 if zone_id == right_zone.zone_id else 0
                timestamp = build_timestamp(
                    base_time,
                    identity_index,
                    sighting_index,
                    offset_seconds,
                )
                sightings.append(
                    {
                        "local_track_id": f"track-{identity_id}-{sighting_index:02d}",
                        "camera_id": camera_id,
                        "zone_id": zone_id,
                        "timestamp": timestamp,
                        "embedding": [float(value) for value in vector.tolist()],
                        "object_class": object_class,
                    }
                )
        else:
            zone = zones[int(rng.integers(0, len(zones)))]
            camera_ids = choose_intra_zone_cameras(zone, sighting_count, rng)
            for sighting_index, camera_id in enumerate(camera_ids):
                vector = make_similar_vector(base_vector, intra_similarity, rng)
                timestamp = build_timestamp(base_time, identity_index, sighting_index, 0)
                sightings.append(
                    {
                        "local_track_id": f"track-{identity_id}-{sighting_index:02d}",
                        "camera_id": camera_id,
                        "zone_id": zone.zone_id,
                        "timestamp": timestamp,
                        "embedding": [float(value) for value in vector.tolist()],
                        "object_class": object_class,
                    }
                )

        identities.append(
            {
                "identity_id": identity_id,
                "object_class": object_class,
                "cross_zone": is_cross_zone,
                "sightings": sightings,
            }
        )
        identity_groups.append(
            {
                "global_id": identity_id,
                "sightings": [
                    {
                        "local_track_id": sighting["local_track_id"],
                        "camera_id": sighting["camera_id"],
                        "timestamp": sighting["timestamp"],
                        "object_class": sighting["object_class"],
                    }
                    for sighting in sightings
                ],
            }
        )

    pairs, cross_zone_pairs = build_pair_payload(identities)
    zone_payload = [
        {
            "zone_id": zone.zone_id,
            "cameras": list(zone.cameras),
            "boundary_cameras": list(zone.boundary_cameras),
            "adjacent_zones": list(zone.adjacent_zones),
        }
        for zone in zones
    ]
    camera_zone_map = {
        camera_id: zone.zone_id
        for zone in zones
        for camera_id in zone.cameras
    }
    return {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "seed": args.seed,
            "zone_count": args.zones,
            "cameras_per_zone": args.cameras_per_zone,
            "camera_count": len(camera_order),
            "identity_count": args.identities,
            "cross_zone_fraction": args.cross_zone_fraction,
            "cross_zone_identity_count": cross_zone_identities,
            "embedding_dim": args.embedding_dim,
            "noise": args.noise,
        },
        "zones": zone_payload,
        "camera_order": camera_order,
        "camera_zone_map": camera_zone_map,
        "identities": identities,
        "ground_truth": {
            "identity_groups": identity_groups,
            "pairs": pairs,
            "cross_zone_pairs": cross_zone_pairs,
        },
        "summary": {
            "total_pairs": len(pairs),
            "cross_zone_pairs": len(cross_zone_pairs),
            "zone_sizes": {zone.zone_id: len(zone_by_id[zone.zone_id].cameras) for zone in zones},
        },
    }


def main() -> None:
    args = parse_args()
    validate_args(args)
    dataset = generate_dataset(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "zones": dataset["metadata"]["zone_count"],
                "camera_count": dataset["metadata"]["camera_count"],
                "identities": dataset["metadata"]["identity_count"],
                "cross_zone_identities": dataset["metadata"]["cross_zone_identity_count"],
                "pairs": dataset["summary"]["total_pairs"],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
