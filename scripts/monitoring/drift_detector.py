#!/usr/bin/env python3
"""Detect confidence distribution drift per class per camera.

Queries TimescaleDB for hourly confidence histograms, compares against a stored
baseline, writes Prometheus textfile metrics, emits a Markdown report, and
returns a non-zero exit code when drift is detected.

Usage:
    python drift_detector.py --db-dsn postgresql://localhost:5432/vidanalytics \
        --baseline s3://debug-traces/baselines/confidence-baseline.json \
        --output artifacts/drift/drift-report.md
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_WINDOW_HOURS = 1.0
DEFAULT_BINS = 20
DEFAULT_KS_PVALUE_THRESHOLD = 0.01
DEFAULT_KL_THRESHOLD = 0.5
DEFAULT_REPORT_OUTPUT = Path("artifacts/monitoring/drift/drift-report.md")
DEFAULT_METRICS_OUTPUT = Path("artifacts/monitoring/prometheus/confidence_drift.prom")


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


@dataclass(frozen=True)
class HistogramGroup:
    camera_id: str
    object_class: str
    counts: list[int]
    total_count: int
    normalized: list[float]
    model_versions: dict[str, int]


@dataclass(frozen=True)
class DriftResult:
    camera_id: str
    object_class: str
    baseline_total: int
    current_total: int
    ks_statistic: float
    ks_p_value: float
    kl_divergence: float
    drift_detected: bool
    baseline_model_versions: tuple[str, ...]
    current_model_versions: tuple[str, ...]


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
        "--baseline",
        required=True,
        help="Baseline path. Supports local paths and s3://bucket/key MinIO URIs.",
    )
    parser.add_argument(
        "--minio-url",
        default=os.environ.get("MINIO_URL", "http://localhost:9000"),
        help="MinIO endpoint used for s3:// baseline paths.",
    )
    parser.add_argument(
        "--minio-access-key",
        default=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
        help="MinIO access key used for s3:// baseline paths.",
    )
    parser.add_argument(
        "--minio-secret-key",
        default=os.environ.get("MINIO_SECRET_KEY", "minioadmin123"),
        help="MinIO secret key used for s3:// baseline paths.",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help="Lookback window used for drift detection.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_KS_PVALUE_THRESHOLD,
        help="KS-test p-value threshold below which drift is flagged.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT_OUTPUT,
        help="Markdown report output path.",
    )
    parser.add_argument(
        "--metrics-output",
        type=Path,
        default=DEFAULT_METRICS_OUTPUT,
        help="Prometheus textfile output path.",
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
    if args.threshold <= 0 or args.threshold >= 1:
        raise RuntimeError("--threshold must be between 0 and 1")
    if args.bins <= 1:
        raise RuntimeError("--bins must be greater than 1")


def normalize_counts(counts: list[int]) -> list[float]:
    total = sum(counts)
    if total <= 0:
        return [0.0] * len(counts)
    return [count / total for count in counts]


def is_s3_uri(value: str) -> bool:
    return value.startswith("s3://")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise RuntimeError(f"unsupported URI scheme for MinIO access: {uri}")
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


def load_json_document(
    *,
    path_or_uri: str,
    minio_url: str,
    minio_access_key: str,
    minio_secret_key: str,
) -> dict[str, Any]:
    if is_s3_uri(path_or_uri):
        bucket, key = parse_s3_uri(path_or_uri)
        client = build_minio_client(
            minio_url=minio_url,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
        )
        response = client.get_object(bucket, key)
        try:
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            response.close()
            response.release_conn()
        if not isinstance(payload, dict):
            raise RuntimeError("baseline JSON must be a top-level object")
        return payload

    path = Path(path_or_uri)
    if not path.exists():
        raise RuntimeError(f"baseline file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("baseline JSON must be a top-level object")
    return payload


def parse_baseline_histograms(payload: dict[str, Any], *, bins: int) -> dict[tuple[str, str], HistogramGroup]:
    histograms = payload.get("histograms")
    if not isinstance(histograms, dict):
        raise RuntimeError("baseline JSON is missing a 'histograms' object")

    groups: dict[tuple[str, str], HistogramGroup] = {}
    for camera_id, camera_payload in histograms.items():
        if not isinstance(camera_id, str) or not isinstance(camera_payload, dict):
            continue
        for object_class, object_payload in camera_payload.items():
            if not isinstance(object_class, str) or not isinstance(object_payload, dict):
                continue
            counts = object_payload.get("counts")
            if not isinstance(counts, list) or len(counts) != bins:
                raise RuntimeError(
                    f"baseline histogram for {camera_id}/{object_class} does not match {bins} bins"
                )
            integer_counts = [int(value) for value in counts]
            model_versions_raw = object_payload.get("model_versions", {})
            model_versions = (
                {
                    str(key): int(value)
                    for key, value in model_versions_raw.items()
                }
                if isinstance(model_versions_raw, dict)
                else {}
            )
            groups[(camera_id, object_class)] = HistogramGroup(
                camera_id=camera_id,
                object_class=object_class,
                counts=integer_counts,
                total_count=int(object_payload.get("total_count", sum(integer_counts))),
                normalized=normalize_counts(integer_counts),
                model_versions=model_versions,
            )
    if not groups:
        raise RuntimeError("baseline JSON does not contain any valid histograms")
    return groups


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


def build_current_groups(rows: list[Any], *, bins: int) -> dict[tuple[str, str], HistogramGroup]:
    if not rows:
        raise RuntimeError("no detections found in the requested drift window")

    groups: dict[tuple[str, str], HistogramAccumulator] = {}
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

    return {
        key: HistogramGroup(
            camera_id=value.camera_id,
            object_class=value.object_class,
            counts=value.counts,
            total_count=value.total_count,
            normalized=normalize_counts(value.counts),
            model_versions=dict(sorted(value.model_versions.items())),
        )
        for key, value in sorted(groups.items())
    }


def histogram_to_samples(counts: list[int], *, bins: int) -> list[float]:
    samples: list[float] = []
    for index, count in enumerate(counts):
        if count <= 0:
            continue
        midpoint = (index + 0.5) / bins
        samples.extend([midpoint] * count)
    return samples


def compute_kl_divergence(baseline: list[float], current: list[float]) -> float:
    epsilon = 1e-12
    divergence = 0.0
    for baseline_probability, current_probability in zip(baseline, current, strict=True):
        baseline_value = max(baseline_probability, epsilon)
        current_value = max(current_probability, epsilon)
        divergence += current_value * math.log(current_value / baseline_value)
    return divergence


def compare_groups(
    *,
    baseline_groups: dict[tuple[str, str], HistogramGroup],
    current_groups: dict[tuple[str, str], HistogramGroup],
    bins: int,
    p_value_threshold: float,
) -> tuple[list[DriftResult], list[str], list[str]]:
    scipy_stats = require_module("scipy.stats", "scipy")

    results: list[DriftResult] = []
    missing_baseline: list[str] = []
    missing_current: list[str] = []

    for key, current_group in current_groups.items():
        baseline_group = baseline_groups.get(key)
        if baseline_group is None:
            missing_baseline.append(f"{current_group.camera_id}/{current_group.object_class}")
            continue

        baseline_samples = histogram_to_samples(baseline_group.counts, bins=bins)
        current_samples = histogram_to_samples(current_group.counts, bins=bins)
        ks_result = scipy_stats.ks_2samp(baseline_samples, current_samples)
        kl_divergence = compute_kl_divergence(
            baseline_group.normalized,
            current_group.normalized,
        )
        drift_detected = bool(
            float(ks_result.pvalue) < p_value_threshold or kl_divergence > DEFAULT_KL_THRESHOLD
        )
        results.append(
            DriftResult(
                camera_id=current_group.camera_id,
                object_class=current_group.object_class,
                baseline_total=baseline_group.total_count,
                current_total=current_group.total_count,
                ks_statistic=float(ks_result.statistic),
                ks_p_value=float(ks_result.pvalue),
                kl_divergence=kl_divergence,
                drift_detected=drift_detected,
                baseline_model_versions=tuple(sorted(baseline_group.model_versions)),
                current_model_versions=tuple(sorted(current_group.model_versions)),
            )
        )

    for key, baseline_group in baseline_groups.items():
        if key not in current_groups:
            missing_current.append(f"{baseline_group.camera_id}/{baseline_group.object_class}")

    results.sort(
        key=lambda item: (
            not item.drift_detected,
            item.camera_id,
            item.object_class,
        )
    )
    return results, missing_baseline, missing_current


def format_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    return f"{value:.6f}"


def render_metrics_text(results: list[DriftResult]) -> str:
    lines = [
        "# HELP confidence_drift_score KS-test statistic for confidence distribution drift.",
        "# TYPE confidence_drift_score gauge",
    ]
    for result in results:
        labels = (
            f'camera_id="{result.camera_id}",'
            f'object_class="{result.object_class}"'
        )
        lines.append(f"confidence_drift_score{{{labels}}} {format_float(result.ks_statistic)}")

    lines.extend(
        [
            "# HELP confidence_drift_p_value KS-test p-value for confidence distribution drift.",
            "# TYPE confidence_drift_p_value gauge",
        ]
    )
    for result in results:
        labels = (
            f'camera_id="{result.camera_id}",'
            f'object_class="{result.object_class}"'
        )
        lines.append(f"confidence_drift_p_value{{{labels}}} {format_float(result.ks_p_value)}")

    lines.extend(
        [
            "# HELP confidence_drift_kl_divergence KL divergence between baseline and current confidence histograms.",
            "# TYPE confidence_drift_kl_divergence gauge",
        ]
    )
    for result in results:
        labels = (
            f'camera_id="{result.camera_id}",'
            f'object_class="{result.object_class}"'
        )
        lines.append(
            f"confidence_drift_kl_divergence{{{labels}}} {format_float(result.kl_divergence)}"
        )

    lines.extend(
        [
            "# HELP confidence_drift_detected Drift flag derived from KS p-value and KL divergence thresholds.",
            "# TYPE confidence_drift_detected gauge",
        ]
    )
    for result in results:
        labels = (
            f'camera_id="{result.camera_id}",'
            f'object_class="{result.object_class}"'
        )
        lines.append(
            f"confidence_drift_detected{{{labels}}} {1 if result.drift_detected else 0}"
        )
    return "\n".join(lines) + "\n"


def write_text_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def render_markdown_report(
    *,
    generated_at: datetime,
    baseline_source: str,
    baseline_metadata: dict[str, Any],
    window_hours: float,
    p_value_threshold: float,
    results: list[DriftResult],
    missing_baseline: list[str],
    missing_current: list[str],
) -> str:
    drifted = [result for result in results if result.drift_detected]
    lines = [
        "# Confidence Drift Report",
        "",
        f"- Generated at: `{generated_at.isoformat()}`",
        f"- Baseline source: `{baseline_source}`",
        f"- Detection window: last `{window_hours}` hours",
        f"- KS p-value threshold: `{p_value_threshold}`",
        f"- KL divergence threshold: `{DEFAULT_KL_THRESHOLD}`",
        f"- Baseline snapshot time: `{baseline_metadata.get('snapshot_time', 'unknown')}`",
        f"- Groups compared: `{len(results)}`",
        f"- Drifted groups: `{len(drifted)}`",
        f"- Missing baseline groups: `{len(missing_baseline)}`",
        f"- Missing current groups: `{len(missing_current)}`",
        "",
        "## Comparison",
        "",
        "| Camera | Class | Baseline Total | Current Total | KS Statistic | KS p-value | KL Divergence | Drift | Baseline Versions | Current Versions |",
        "|---|---|---:|---:|---:|---:|---:|---|---|---|",
    ]

    for result in results:
        lines.append(
            "| {camera_id} | {object_class} | {baseline_total} | {current_total} | {ks_statistic:.4f} | "
            "{ks_p_value:.6f} | {kl_divergence:.4f} | {drift} | {baseline_versions} | {current_versions} |".format(
                camera_id=result.camera_id,
                object_class=result.object_class,
                baseline_total=result.baseline_total,
                current_total=result.current_total,
                ks_statistic=result.ks_statistic,
                ks_p_value=result.ks_p_value,
                kl_divergence=result.kl_divergence,
                drift="DRIFT" if result.drift_detected else "OK",
                baseline_versions=", ".join(result.baseline_model_versions) or "unknown",
                current_versions=", ".join(result.current_model_versions) or "unknown",
            )
        )

    if missing_baseline:
        lines.extend(
            [
                "",
                "## Current Groups Missing a Baseline",
                "",
            ]
        )
        lines.extend(f"- `{value}`" for value in missing_baseline)

    if missing_current:
        lines.extend(
            [
                "",
                "## Baseline Groups Without Recent Detections",
                "",
            ]
        )
        lines.extend(f"- `{value}`" for value in missing_current)

    if not drifted:
        lines.extend(
            [
                "",
                "## Summary",
                "",
                "No camera/class group exceeded the configured drift thresholds.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Summary",
                "",
                f"{len(drifted)} camera/class groups exceeded the configured drift thresholds.",
            ]
        )

    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    validate_args(args)
    baseline_payload = load_json_document(
        path_or_uri=str(args.baseline),
        minio_url=str(args.minio_url),
        minio_access_key=str(args.minio_access_key),
        minio_secret_key=str(args.minio_secret_key),
    )
    baseline_metadata_raw = baseline_payload.get("metadata", {})
    baseline_metadata = baseline_metadata_raw if isinstance(baseline_metadata_raw, dict) else {}
    baseline_bins = baseline_metadata.get("bins")
    if baseline_bins is not None and int(baseline_bins) != int(args.bins):
        raise RuntimeError(
            f"baseline was created with {baseline_bins} bins but --bins={args.bins}"
        )

    baseline_groups = parse_baseline_histograms(baseline_payload, bins=int(args.bins))
    rows = await query_histogram_rows(
        db_dsn=str(args.db_dsn),
        window_hours=float(args.window_hours),
        bins=int(args.bins),
    )
    current_groups = build_current_groups(rows, bins=int(args.bins))
    results, missing_baseline, missing_current = compare_groups(
        baseline_groups=baseline_groups,
        current_groups=current_groups,
        bins=int(args.bins),
        p_value_threshold=float(args.threshold),
    )
    if not results:
        raise RuntimeError("no overlapping camera/class groups between baseline and current window")

    report_text = render_markdown_report(
        generated_at=datetime.now(timezone.utc),
        baseline_source=str(args.baseline),
        baseline_metadata=baseline_metadata,
        window_hours=float(args.window_hours),
        p_value_threshold=float(args.threshold),
        results=results,
        missing_baseline=missing_baseline,
        missing_current=missing_current,
    )
    metrics_text = render_metrics_text(results)
    write_text_atomically(args.output, report_text)
    write_text_atomically(args.metrics_output, metrics_text)

    drift_count = sum(1 for result in results if result.drift_detected)
    print(
        "Drift scan complete: "
        f"{len(results)} groups compared, {drift_count} drifted, "
        f"{len(missing_baseline)} missing baseline groups, "
        f"{len(missing_current)} missing current groups."
    )
    return 1 if drift_count > 0 else 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(main_async(args))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
