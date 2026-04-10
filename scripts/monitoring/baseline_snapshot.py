#!/usr/bin/env python3
"""Capture current confidence distribution as a drift baseline.

Usage:
    python baseline_snapshot.py --db-dsn postgresql://localhost:5432/vidanalytics \
        --window-hours 24 --output s3://debug-traces/baselines/confidence-baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import io
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_BINS = 20
DEFAULT_OUTPUT = "artifacts/monitoring/drift/confidence-baseline.json"


@dataclass
class HistogramAccumulator:
    camera_id: str
    object_class: str
    counts: list[int]
    total_count: int
    model_versions: dict[str, int]

    @classmethod
    def create(cls, *, camera_id: str, object_class: str, bins: int) -> "HistogramAccumulator":
        return cls(
            camera_id=camera_id,
            object_class=object_class,
            counts=[0] * bins,
            total_count=0,
            model_versions={},
        )


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
        "--db-dsn",
        default=os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN"),
        help="PostgreSQL / TimescaleDB DSN.",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help="Lookback window used to build the baseline.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output path. Supports local paths and s3://bucket/key MinIO URIs.",
    )
    parser.add_argument(
        "--minio-url",
        default=os.environ.get("MINIO_URL", "http://localhost:9000"),
        help="MinIO endpoint used for s3:// output paths.",
    )
    parser.add_argument(
        "--minio-access-key",
        default=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
        help="MinIO access key used for s3:// output paths.",
    )
    parser.add_argument(
        "--minio-secret-key",
        default=os.environ.get("MINIO_SECRET_KEY", "minioadmin123"),
        help="MinIO secret key used for s3:// output paths.",
    )
    parser.add_argument(
        "--bins",
        type=int,
        default=DEFAULT_BINS,
        help="Number of confidence histogram bins between 0.0 and 1.0.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not args.db_dsn:
        raise RuntimeError("--db-dsn is required (or set DATABASE_URL / DB_DSN)")
    if args.window_hours <= 0:
        raise RuntimeError("--window-hours must be greater than zero")
    if args.bins <= 1:
        raise RuntimeError("--bins must be greater than 1")
    if not str(args.output).strip():
        raise RuntimeError("--output cannot be empty")


async def query_histogram_rows(
    *,
    db_dsn: str,
    window_hours: float,
    bins: int,
) -> list[Any]:
    asyncpg = require_module("asyncpg", "asyncpg")
    connection = await asyncpg.connect(db_dsn)
    try:
        rows = await connection.fetch(
            """
            SELECT
                camera_id,
                object_class,
                model_version,
                LEAST(
                    GREATEST(width_bucket(confidence, 0.0, 1.0, $1::int), 1),
                    $1::int
                ) AS bucket,
                COUNT(*)::bigint AS bucket_count
            FROM detections
            WHERE time >= now() - ($2::double precision * interval '1 hour')
            GROUP BY camera_id, object_class, model_version, bucket
            ORDER BY camera_id, object_class, model_version, bucket
            """,
            bins,
            window_hours,
        )
    finally:
        await connection.close()
    return list(rows)


def normalize_counts(counts: list[int]) -> list[float]:
    total = sum(counts)
    if total <= 0:
        return [0.0] * len(counts)
    return [count / total for count in counts]


def build_histogram_payload(rows: list[Any], *, bins: int, window_hours: float) -> dict[str, Any]:
    if not rows:
        raise RuntimeError("no detections found in the requested baseline window")

    groups: dict[tuple[str, str], HistogramAccumulator] = {}
    total_detections_per_class: defaultdict[str, int] = defaultdict(int)
    total_detections_per_camera: defaultdict[str, int] = defaultdict(int)
    model_version_totals: defaultdict[str, int] = defaultdict(int)

    for row in rows:
        camera_id = str(row["camera_id"])
        object_class = str(row["object_class"])
        model_version = str(row["model_version"])
        bucket_index = int(row["bucket"]) - 1
        bucket_count = int(row["bucket_count"])

        key = (camera_id, object_class)
        if key not in groups:
            groups[key] = HistogramAccumulator.create(
                camera_id=camera_id,
                object_class=object_class,
                bins=bins,
            )
        group = groups[key]
        group.counts[bucket_index] += bucket_count
        group.total_count += bucket_count
        group.model_versions[model_version] = group.model_versions.get(model_version, 0) + bucket_count

        total_detections_per_class[object_class] += bucket_count
        total_detections_per_camera[camera_id] += bucket_count
        model_version_totals[model_version] += bucket_count

    histograms: dict[str, dict[str, Any]] = {}
    for (camera_id, object_class), group in sorted(groups.items()):
        camera_payload = histograms.setdefault(camera_id, {})
        camera_payload[object_class] = {
            "counts": group.counts,
            "normalized": normalize_counts(group.counts),
            "total_count": group.total_count,
            "model_versions": dict(sorted(group.model_versions.items())),
        }

    snapshot_time = datetime.now(timezone.utc).isoformat()
    return {
        "metadata": {
            "snapshot_time": snapshot_time,
            "window_hours": window_hours,
            "bins": bins,
            "bucket_edges": [index / bins for index in range(bins + 1)],
            "camera_count": len(histograms),
            "group_count": len(groups),
            "total_detections": sum(total_detections_per_class.values()),
            "total_detections_per_class": dict(sorted(total_detections_per_class.items())),
            "total_detections_per_camera": dict(sorted(total_detections_per_camera.items())),
            "model_versions": dict(sorted(model_version_totals.items())),
        },
        "histograms": histograms,
    }


def is_s3_uri(value: str) -> bool:
    return value.startswith("s3://")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise RuntimeError(f"unsupported URI scheme for MinIO output: {uri}")
    bucket = parsed.netloc.strip()
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise RuntimeError(f"invalid s3 URI: {uri}")
    return bucket, key


def build_minio_client(
    *,
    minio_url: str,
    access_key: str,
    secret_key: str,
) -> Any:
    minio = require_module("minio", "minio")
    parsed = urlparse(minio_url if "://" in minio_url else f"http://{minio_url}")
    if not parsed.netloc:
        raise RuntimeError(f"invalid MinIO URL: {minio_url}")
    secure = parsed.scheme == "https"
    return minio.Minio(
        parsed.netloc,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


def write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name,
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def write_output(
    *,
    output: str,
    payload: dict[str, Any],
    minio_url: str,
    minio_access_key: str,
    minio_secret_key: str,
) -> str:
    if is_s3_uri(output):
        bucket, key = parse_s3_uri(output)
        client = build_minio_client(
            minio_url=minio_url,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
        )
        if not client.bucket_exists(bucket):
            raise RuntimeError(f"MinIO bucket does not exist: {bucket}")
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        client.put_object(
            bucket_name=bucket,
            object_name=key,
            data=io.BytesIO(body),
            length=len(body),
            content_type="application/json",
        )
        return output

    output_path = Path(output)
    write_json_atomically(output_path, payload)
    return str(output_path)


async def main_async(args: argparse.Namespace) -> None:
    validate_args(args)
    rows = await query_histogram_rows(
        db_dsn=str(args.db_dsn),
        window_hours=float(args.window_hours),
        bins=int(args.bins),
    )
    payload = build_histogram_payload(
        rows,
        bins=int(args.bins),
        window_hours=float(args.window_hours),
    )
    destination = write_output(
        output=str(args.output),
        payload=payload,
        minio_url=str(args.minio_url),
        minio_access_key=str(args.minio_access_key),
        minio_secret_key=str(args.minio_secret_key),
    )
    metadata = payload["metadata"]
    print(
        "Captured confidence baseline: "
        f"{metadata['group_count']} camera/class groups across {metadata['camera_count']} cameras, "
        f"{metadata['total_detections']} detections total -> {destination}"
    )


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
