#!/usr/bin/env python3
"""Generate the 50-100 camera load-test Markdown report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

DEFAULT_OUTPUT_PATH = REPO_ROOT / "docs" / "evaluation-results" / "load-test-100cam-report.md"
DEFAULT_METRICS_PATH = REPO_ROOT / "artifacts" / "load-test" / "measure-e2e.json"
DEFAULT_CHAOS_PATH = REPO_ROOT / "artifacts" / "load-test" / "100cam-chaos-results.json"
DEFAULT_COST_PARAMS = REPO_ROOT / "scripts" / "cost-model" / "params-measured.yaml"
DEFAULT_TOPICS_PATH = REPO_ROOT / "infra" / "kafka" / "topics.yaml"
DEFAULT_COMPOSE_PATH = REPO_ROOT / "infra" / "docker-compose.yml"

STAGE_LABELS = {
    "ingest_latency_ms": "Ingest / end-to-end latency",
    "decode_latency_ms": "Decode latency",
    "inference_latency_ms": "Inference latency",
    "embedding_latency_ms": "Embedding latency",
    "db_write_latency_ms": "DB write latency",
    "query_latency_ms": "Query API latency (Prometheus)",
    "mtmc_match_latency_ms": "MTMC match latency",
}
THROUGHPUT_LABELS = {
    "frames_in_per_s": "Frames in / s",
    "frames_decoded_per_s": "Frames decoded / s",
    "inference_frames_per_s": "Inference frames / s",
    "detections_per_s": "Detections / s",
    "events_per_s": "Events / s",
    "matches_per_s": "MTMC matches / s",
    "queries_per_s": "Query requests / s",
    "bulk_rows_per_s": "Bulk rows / s",
    "fps_per_camera": "FPS / camera",
    "active_tracks_per_camera": "Active tracks / camera",
}
DIRECT_QUERY_ENDPOINTS = ("/detections", "/tracks", "/events")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-json", type=Path, default=DEFAULT_METRICS_PATH)
    parser.add_argument("--chaos-json", type=Path, default=DEFAULT_CHAOS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--cost-params", type=Path, default=DEFAULT_COST_PARAMS)
    parser.add_argument("--topics-file", type=Path, default=DEFAULT_TOPICS_PATH)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metrics_payload = _load_json(args.metrics_json)
    chaos_payload = _load_json_if_exists(args.chaos_json)
    report = build_report(
        metrics_payload=metrics_payload,
        chaos_payload=chaos_payload,
        cost_params_path=args.cost_params,
        topics_path=args.topics_file,
        compose_path=args.compose_file,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


def build_report(
    *,
    metrics_payload: dict[str, Any],
    chaos_payload: dict[str, Any] | None,
    cost_params_path: Path,
    topics_path: Path,
    compose_path: Path,
) -> str:
    config = metrics_payload.get("config", {})
    snapshots = metrics_payload.get("snapshots", [])
    if not isinstance(config, dict) or not isinstance(snapshots, list):
        raise ValueError("metrics JSON payload is missing config or snapshots")

    camera_count = int(config.get("camera_count", 0))
    duration_s = int(config.get("duration_s", 0))
    chaos_results = chaos_payload.get("results", []) if isinstance(chaos_payload, dict) else []
    predicted = _load_predicted_costs(
        camera_count=camera_count,
        cost_params_path=cost_params_path,
        topics_path=topics_path,
        compose_path=compose_path,
    )

    nfr_rows, failed_nfrs = _build_nfr_rows(snapshots)
    requirement_rows = _build_requirement_rows(duration_s=duration_s, chaos_results=chaos_results, camera_count=camera_count)
    throughput_table = _build_throughput_table(snapshots)
    latency_table = _build_latency_table(snapshots)
    query_probe_table = _build_query_probe_table(snapshots)
    cpu_ram_table = _build_service_resource_table(snapshots, "cpu_cores", "memory_bytes")
    disk_network_table = _build_service_disk_network_table(snapshots)
    gpu_table = _build_gpu_table(snapshots)
    bucket_table, actual_hot_storage_gb = _build_bucket_table(
        snapshots,
        predicted.get("retention_days"),
    )
    lag_table = _build_lag_table(snapshots)
    chaos_table = _build_chaos_table(chaos_results)
    bottleneck_lines = _build_bottleneck_lines(snapshots)
    cost_table = _build_cost_table(snapshots, predicted, camera_count, actual_hot_storage_gb)
    recommendations = _build_recommendations(
        failed_nfrs=failed_nfrs,
        requirement_rows=requirement_rows,
        bottleneck_lines=bottleneck_lines,
        chaos_results=chaos_results,
    )

    lines = [
        "# 50-100 Camera Load Test Report",
        "",
        "## Test Configuration",
        f"- Camera count: `{camera_count}`",
        f"- Duration: `{duration_s}` seconds",
        f"- Snapshot interval: `{config.get('interval_s', 'n/a')}` seconds",
        f"- Prometheus: `{config.get('prometheus_url', 'n/a')}`",
        f"- Query API: `{config.get('query_api_url', 'n/a')}`",
        f"- Probe camera: `{config.get('probe_camera_id', 'n/a')}`",
        f"- DB probe enabled: `{'yes' if config.get('db_probe_enabled') else 'no'}`",
        f"- Snapshot count: `{len(snapshots)}`",
        "",
        "## Scenario Requirements",
        requirement_rows,
        "",
        "## NFR Pass/Fail",
        nfr_rows,
        "",
        "## Throughput Achieved vs Target",
        throughput_table,
        "",
        "## Latency Percentiles per Stage",
        latency_table,
        "",
        "## Direct Query API Probes",
        query_probe_table,
        "",
        "## Resource Utilization — CPU and RAM",
        cpu_ram_table,
        "",
        "## Resource Utilization — Disk and Network",
        disk_network_table,
        "",
        "## Resource Utilization — GPU",
        gpu_table,
        "",
        "## Storage Growth and Bucket Footprint",
        bucket_table,
        "",
        "## Kafka Consumer Lag",
        lag_table,
        "",
        "## Chaos Recovery Times",
        chaos_table,
        "",
        "## Bottleneck Identification",
        *bottleneck_lines,
        "",
        "## Cost Model Comparison",
        cost_table,
        "",
        "## Recommendations",
        *recommendations,
        "",
        "## Notes",
        "- Missing metrics remain explicit `FAIL` conditions rather than silent passes.",
        "- Query API latency is reported from both Prometheus histograms and direct HTTP probes so operator-visible latency drift is visible even when Prometheus aggregation is stale.",
        "- `Monthly platform cost` is only directly comparable when billing export data exists; otherwise this report validates the measured operational drivers from the P3 cost model.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _build_nfr_rows(snapshots: list[dict[str, Any]]) -> tuple[str, list[str]]:
    rows = [
        "| NFR | Target | Measured | Result | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    failed: list[str] = []

    ingest_p95 = _stage_worst_quantile(snapshots, "ingest_latency_ms", "p95")
    ingest_source = _stage_source_metric(snapshots, "ingest_latency_ms")
    passed = ingest_p95 is not None and ingest_p95 < 2000.0 and ingest_source == "e2e_latency_ms"
    if not passed:
        failed.append("End-to-end latency (p95)")
    rows.append(
        _table_row(
            "End-to-end latency (p95)",
            "< 2,000 ms",
            _format_ms(ingest_p95),
            passed,
            (
                "Uses canonical `e2e_latency_ms` when present. "
                f"Observed source: `{ingest_source or 'missing'}`."
            ),
        )
    )

    fps_per_camera = _series_average(snapshots, ("throughput", "fps_per_camera"))
    passed = fps_per_camera is not None and 5.0 <= fps_per_camera <= 10.0
    if not passed:
        failed.append("Inference throughput")
    rows.append(
        _table_row(
            "Inference throughput",
            "5-10 FPS per camera",
            _format_rate(fps_per_camera, "fps/camera"),
            passed,
            "Derived from `inference_frames_consumed_total / camera_count`.",
        )
    )

    query_probe_p95 = _direct_query_percentile(snapshots, 0.95)
    passed = query_probe_p95 is not None and query_probe_p95 < 500.0
    if not passed:
        failed.append("Query latency (p95)")
    rows.append(
        _table_row(
            "Query latency (p95)",
            "< 500 ms",
            _format_ms(query_probe_p95),
            passed,
            "From direct probes to `/detections`, `/tracks`, and `/events`.",
        )
    )

    max_lag = _max_lag(snapshots)
    passed = max_lag is not None and max_lag < 10_000.0
    if not passed:
        failed.append("Kafka consumer lag")
    rows.append(
        _table_row(
            "Kafka consumer lag",
            "< 10,000 messages",
            _format_number(max_lag),
            passed,
            "Uses canonical `kafka_consumer_lag` when present, otherwise service-specific lag gauges.",
        )
    )

    availability_pct = _series_average(snapshots, ("availability", "overall_pct"))
    passed = availability_pct is not None and availability_pct >= 99.5
    if not passed:
        failed.append("System availability")
    rows.append(
        _table_row(
            "System availability",
            ">= 99.5%",
            _format_percent(availability_pct),
            passed,
            "Average of Prometheus `up` samples across scraped jobs during the run.",
        )
    )

    return "\n".join(rows), failed


def _build_requirement_rows(
    *,
    duration_s: int,
    chaos_results: list[dict[str, Any]],
    camera_count: int,
) -> str:
    rows = [
        "| Requirement | Target | Measured | Result | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows.append(
        _table_row(
            "Sustained duration",
            ">= 14,400 s",
            f"{duration_s} s",
            duration_s >= 14_400,
            "Phase 4 scale tests must hold sustained load for at least 4 hours.",
        )
    )
    rows.append(
        _table_row(
            "Scale target",
            "50-100 cameras",
            str(camera_count),
            50 <= camera_count <= 100,
            "Configured camera fan-out for the replay workload.",
        )
    )
    executed = [result for result in chaos_results if result.get("status") != "skipped"]
    rows.append(
        _table_row(
            "Chaos coverage",
            ">= 1 executed scenario",
            str(len(executed)),
            len(executed) > 0,
            "Scale sign-off requires at least one executed chaos scenario.",
        )
    )
    return "\n".join(rows)


def _build_throughput_table(snapshots: list[dict[str, Any]]) -> str:
    rows = ["| Metric | Average | Peak |", "| --- | --- | --- |"]
    for metric_key, label in THROUGHPUT_LABELS.items():
        values = _series_values(snapshots, ("throughput", metric_key))
        rows.append(
            "| "
            + " | ".join(
                [
                    label,
                    _format_rate(_safe_mean(values), "per_s" if "tracks" not in metric_key and metric_key != "fps_per_camera" else ("fps" if metric_key == "fps_per_camera" else "tracks/camera")),
                    _format_rate(max(values) if values else None, "per_s" if "tracks" not in metric_key and metric_key != "fps_per_camera" else ("fps" if metric_key == "fps_per_camera" else "tracks/camera")),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _build_latency_table(snapshots: list[dict[str, Any]]) -> str:
    rows = ["| Stage | p50 | p95 | p99 | Source |", "| --- | --- | --- | --- | --- |"]
    for stage_key, label in STAGE_LABELS.items():
        rows.append(
            "| "
            + " | ".join(
                [
                    label,
                    _format_ms(_stage_worst_quantile(snapshots, stage_key, "p50")),
                    _format_ms(_stage_worst_quantile(snapshots, stage_key, "p95")),
                    _format_ms(_stage_worst_quantile(snapshots, stage_key, "p99")),
                    f"`{_stage_source_metric(snapshots, stage_key) or 'missing'}`",
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _build_query_probe_table(snapshots: list[dict[str, Any]]) -> str:
    rows = ["| Endpoint | p50 | p95 | p99 | Average |", "| --- | --- | --- | --- | --- |"]
    for endpoint in DIRECT_QUERY_ENDPOINTS:
        values = _direct_query_values(snapshots, endpoint)
        rows.append(
            "| "
            + " | ".join(
                [
                    endpoint,
                    _format_ms(_percentile(values, 0.50)),
                    _format_ms(_percentile(values, 0.95)),
                    _format_ms(_percentile(values, 0.99)),
                    _format_ms(_safe_mean(values)),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _build_service_resource_table(
    snapshots: list[dict[str, Any]],
    cpu_key: str,
    memory_key: str,
) -> str:
    rows = [
        "| Service | Avg CPU (cores) | Peak CPU (cores) | Avg RAM | Peak RAM |",
        "| --- | --- | --- | --- | --- |",
    ]
    for job in _jobs_with_data(snapshots, ("resources", cpu_key), ("resources", memory_key)):
        cpu_values = _series_values(snapshots, ("resources", cpu_key, job))
        ram_values = _series_values(snapshots, ("resources", memory_key, job))
        rows.append(
            "| "
            + " | ".join(
                [
                    job,
                    _format_number(_safe_mean(cpu_values)),
                    _format_number(max(cpu_values) if cpu_values else None),
                    _format_bytes(_safe_mean(ram_values)),
                    _format_bytes(max(ram_values) if ram_values else None),
                ]
            )
            + " |"
        )
    if len(rows) == 2:
        rows.append("| no data | n/a | n/a | n/a | n/a |")
    return "\n".join(rows)


def _build_service_disk_network_table(snapshots: list[dict[str, Any]]) -> str:
    rows = [
        "| Service | Avg Disk | Peak Disk | Avg RX | Peak RX | Avg TX | Peak TX |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for job in _jobs_with_data(
        snapshots,
        ("resources", "disk_bytes"),
        ("resources", "network_rx_bytes_per_s"),
        ("resources", "network_tx_bytes_per_s"),
    ):
        disk_values = _series_values(snapshots, ("resources", "disk_bytes", job))
        rx_values = _series_values(snapshots, ("resources", "network_rx_bytes_per_s", job))
        tx_values = _series_values(snapshots, ("resources", "network_tx_bytes_per_s", job))
        rows.append(
            "| "
            + " | ".join(
                [
                    job,
                    _format_bytes(_safe_mean(disk_values)),
                    _format_bytes(max(disk_values) if disk_values else None),
                    _format_bytes_per_s(_safe_mean(rx_values)),
                    _format_bytes_per_s(max(rx_values) if rx_values else None),
                    _format_bytes_per_s(_safe_mean(tx_values)),
                    _format_bytes_per_s(max(tx_values) if tx_values else None),
                ]
            )
            + " |"
        )
    if len(rows) == 2:
        rows.append("| no data | n/a | n/a | n/a | n/a | n/a | n/a |")
    return "\n".join(rows)


def _build_gpu_table(snapshots: list[dict[str, Any]]) -> str:
    rows = [
        "| Metric | Average | Peak |",
        "| --- | --- | --- |",
        f"| GPU utilization | {_format_percent(_series_average(snapshots, ('gpu', 'avg_utilization_pct')))} | {_format_percent(_series_peak(snapshots, ('gpu', 'max_utilization_pct')))} |",
        f"| GPU memory used | {_format_bytes(_series_average(snapshots, ('gpu', 'memory_used_bytes')))} | {_format_bytes(_series_peak(snapshots, ('gpu', 'memory_used_bytes')))} |",
        f"| GPU memory total | {_format_bytes(_series_peak(snapshots, ('gpu', 'memory_total_bytes')))} | {_format_bytes(_series_peak(snapshots, ('gpu', 'memory_total_bytes')))} |",
        f"| GPU count | {_format_number(_series_average(snapshots, ('gpu', 'gpu_count')))} | {_format_number(_series_peak(snapshots, ('gpu', 'gpu_count')))} |",
    ]
    return "\n".join(rows)


def _build_bucket_table(
    snapshots: list[dict[str, Any]],
    retention_days: float | None,
) -> tuple[str, float | None]:
    rows = [
        "| Bucket | Latest Size | Growth / day | Notes |",
        "| --- | --- | --- | --- |",
    ]
    actual_hot_storage_gb: float | None = None
    bucket_names = sorted(
        {
            name
            for snapshot in snapshots
            for name, value in snapshot.get("bucket_bytes", {}).items()
            if value is not None
        }
    )
    for bucket in bucket_names:
        series = [
            (
                snapshot.get("collected_at"),
                snapshot.get("bucket_bytes", {}).get(bucket),
            )
            for snapshot in snapshots
            if snapshot.get("bucket_bytes", {}).get(bucket) is not None
        ]
        latest_value = series[-1][1] if series else None
        growth_per_day = _bucket_growth_per_day(series)
        note = ""
        if bucket == "frame-blobs" and growth_per_day is not None and retention_days is not None:
            actual_hot_storage_gb = growth_per_day * retention_days
            note = f"steady-state hot storage ~= {_format_number(actual_hot_storage_gb)} GB"
        rows.append(
            "| "
            + " | ".join(
                [
                    bucket,
                    _format_bytes(latest_value),
                    _format_number(growth_per_day, suffix=" GB/day"),
                    note or "—",
                ]
            )
            + " |"
        )
    if len(rows) == 2:
        rows.append("| no data | n/a | n/a | MinIO bucket metrics missing from Prometheus. |")
    return "\n".join(rows), actual_hot_storage_gb


def _build_lag_table(snapshots: list[dict[str, Any]]) -> str:
    rows = ["| Group / Topic | Max Lag |", "| --- | --- |"]
    lag_map: dict[str, list[float]] = {}
    for snapshot in snapshots:
        for key, value in snapshot.get("kafka_consumer_lag", {}).items():
            if value is None:
                continue
            lag_map.setdefault(key, []).append(float(value))
    for key in sorted(lag_map):
        rows.append(f"| {key} | {_format_number(max(lag_map[key]))} |")
    if len(rows) == 2:
        rows.append("| no data | n/a |")
    return "\n".join(rows)


def _build_chaos_table(chaos_results: list[dict[str, Any]]) -> str:
    rows = [
        "| Scenario | Target | Status | Recovery Time | Data Loss | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if not chaos_results:
        rows.append("| no data | n/a | n/a | n/a | n/a | Chaos output JSON not provided. |")
        return "\n".join(rows)
    for result in chaos_results:
        rows.append(
            "| "
            + " | ".join(
                [
                    str(result.get("name", "unknown")),
                    str(result.get("target", "n/a")),
                    str(result.get("status", "unknown")),
                    _format_duration(_as_float(result.get("recovery_time_s"))),
                    _format_bool(result.get("data_loss")),
                    str(result.get("notes", "")).replace("|", "/"),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _build_bottleneck_lines(snapshots: list[dict[str, Any]]) -> list[str]:
    slowest_stage_label = "n/a"
    slowest_stage_value = -1.0
    for stage_key, label in STAGE_LABELS.items():
        p95 = _stage_worst_quantile(snapshots, stage_key, "p95")
        if p95 is not None and p95 > slowest_stage_value:
            slowest_stage_label = label
            slowest_stage_value = p95

    cpu_job = _top_job_by_metric(snapshots, ("resources", "cpu_cores"))
    ram_job = _top_job_by_metric(snapshots, ("resources", "memory_bytes"))
    lag_value = _max_lag(snapshots)
    return [
        f"- Slowest observed stage: {slowest_stage_label} at {_format_ms(slowest_stage_value if slowest_stage_value >= 0 else None)} p95.",
        f"- Highest CPU consumer: {cpu_job or 'n/a'}.",
        f"- Highest RAM consumer: {ram_job or 'n/a'}.",
        f"- Peak Kafka lag observed: {_format_number(lag_value)} messages.",
    ]


def _build_cost_table(
    snapshots: list[dict[str, Any]],
    predicted: dict[str, float | None],
    camera_count: int,
    actual_hot_storage_gb: float | None,
) -> str:
    actual_fps = _series_average(snapshots, ("throughput", "fps_per_camera"))
    actual_tracks = _series_average(snapshots, ("throughput", "active_tracks_per_camera"))
    gpu_count = _series_peak(snapshots, ("gpu", "gpu_count"))
    actual_cameras_per_gpu = (camera_count / gpu_count) if gpu_count not in (None, 0.0) else None

    rows = [
        "| Parameter | Predicted | Actual / Observed | Delta | Notes |",
        "| --- | --- | --- | --- | --- |",
        _cost_row(
            "Monthly platform cost",
            predicted=_format_currency(predicted.get("monthly_total_usd")),
            actual="n/a",
            delta="n/a",
            notes="Requires billing export; the load harness validates the drivers below.",
        ),
        _cost_row(
            "Inference FPS / camera",
            predicted=_format_rate(predicted.get("inference_fps"), "fps"),
            actual=_format_rate(actual_fps, "fps"),
            delta=_format_delta(actual_fps, predicted.get("inference_fps")),
            notes="Measured from `inference_frames_consumed_total / camera_count`.",
        ),
        _cost_row(
            "Active tracks / camera",
            predicted=_format_number(predicted.get("active_tracks_per_camera")),
            actual=_format_number(actual_tracks),
            delta=_format_delta(actual_tracks, predicted.get("active_tracks_per_camera")),
            notes="Compared against the measured P3-X03 operating point.",
        ),
        _cost_row(
            "Cameras / GPU",
            predicted=_format_number(predicted.get("cameras_per_gpu")),
            actual=_format_number(actual_cameras_per_gpu),
            delta=_format_delta(actual_cameras_per_gpu, predicted.get("cameras_per_gpu")),
            notes="Observed lower bound from configured camera fan-out and scraped GPU count.",
        ),
        _cost_row(
            "Hot storage steady state",
            predicted=_format_number(predicted.get("hot_storage_gb"), suffix=" GB"),
            actual=_format_number(actual_hot_storage_gb, suffix=" GB"),
            delta=_format_delta(actual_hot_storage_gb, predicted.get("hot_storage_gb")),
            notes="Actual is extrapolated from `frame-blobs` bucket growth over the test window.",
        ),
    ]
    return "\n".join(rows)


def _build_recommendations(
    *,
    failed_nfrs: list[str],
    requirement_rows: str,
    bottleneck_lines: list[str],
    chaos_results: list[dict[str, Any]],
) -> list[str]:
    recommendations: list[str] = []
    if failed_nfrs:
        recommendations.append(
            f"- Resolve failed NFRs before production sign-off: {', '.join(failed_nfrs)}."
        )
    if "FAIL" in requirement_rows:
        recommendations.append(
            "- Re-run the scale test with a full 4-hour sustained window and at least one executed chaos scenario if those requirement rows failed."
        )
    skipped = [result.get("name") for result in chaos_results if result.get("status") == "skipped"]
    if skipped:
        recommendations.append(
            f"- Provide environment-specific commands or topology for skipped chaos scenarios: {', '.join(str(item) for item in skipped)}."
        )
    if any("Slowest observed stage" in line and "n/a" in line for line in bottleneck_lines):
        recommendations.append(
            "- Wire the missing Prometheus histograms before treating this report as a go/no-go artifact."
        )
    if not recommendations:
        recommendations.append("- No immediate blockers were detected in the captured report inputs.")
    return recommendations


def _load_predicted_costs(
    *,
    camera_count: int,
    cost_params_path: Path,
    topics_path: Path,
    compose_path: Path,
) -> dict[str, float | None]:
    module_path = REPO_ROOT / "scripts" / "cost-model" / "cost_model_v2.py"
    spec = importlib.util.spec_from_file_location("cilex_cost_model_v2", module_path)
    if spec is None or spec.loader is None:
        return {}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inputs = module.load_cost_model_inputs(cost_params_path)
    topics = module.load_topic_catalog(topics_path)
    inventory = module.load_compose_inventory(compose_path)
    reports = module.build_reports(inputs=inputs, topics=topics, inventory=inventory)
    report = next((item for item in reports if str(item.scenario.name).upper() == "P50"), None)
    if report is None:
        return {}
    row = next((item for item in report.summary_rows if int(item.cameras) == camera_count), None)
    if row is None and report.summary_rows:
        row = min(report.summary_rows, key=lambda item: abs(int(item.cameras) - camera_count))
    if row is None:
        return {}
    return {
        "monthly_total_usd": float(row.total_monthly_usd),
        "hot_storage_gb": float(row.hot_object_storage_gb),
        "inference_fps": float(inputs.inference_fps),
        "active_tracks_per_camera": float(inputs.active_tracks_per_camera),
        "cameras_per_gpu": float(inputs.cameras_per_gpu),
        "gpu_headroom_factor": float(inputs.gpu_headroom_factor),
        "retention_days": float(inputs.central_frame_blob_retention_days),
    }


def _stage_worst_quantile(
    snapshots: list[dict[str, Any]],
    stage_key: str,
    quantile: str,
) -> float | None:
    values = [
        _as_float(snapshot.get("stage_latency", {}).get(stage_key, {}).get(quantile))
        for snapshot in snapshots
    ]
    numeric = [value for value in values if value is not None]
    return max(numeric) if numeric else None


def _stage_source_metric(snapshots: list[dict[str, Any]], stage_key: str) -> str | None:
    for snapshot in snapshots:
        source = snapshot.get("stage_latency", {}).get(stage_key, {}).get("source_metric")
        if isinstance(source, str) and source:
            return source
    return None


def _direct_query_values(snapshots: list[dict[str, Any]], endpoint: str) -> list[float]:
    values = []
    for snapshot in snapshots:
        value = snapshot.get("direct_query_latency_ms", {}).get(endpoint)
        numeric = _as_float(value)
        if numeric is not None:
            values.append(numeric)
    return values


def _direct_query_percentile(snapshots: list[dict[str, Any]], quantile: float) -> float | None:
    values: list[float] = []
    for endpoint in DIRECT_QUERY_ENDPOINTS:
        values.extend(_direct_query_values(snapshots, endpoint))
    return _percentile(values, quantile)


def _series_values(snapshots: list[dict[str, Any]], path: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for snapshot in snapshots:
        cursor: Any = snapshot
        for key in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        numeric = _as_float(cursor)
        if numeric is not None:
            values.append(numeric)
    return values


def _series_average(snapshots: list[dict[str, Any]], path: tuple[str, ...]) -> float | None:
    return _safe_mean(_series_values(snapshots, path))


def _series_peak(snapshots: list[dict[str, Any]], path: tuple[str, ...]) -> float | None:
    values = _series_values(snapshots, path)
    return max(values) if values else None


def _max_lag(snapshots: list[dict[str, Any]]) -> float | None:
    values = [
        _as_float(value)
        for snapshot in snapshots
        for value in snapshot.get("kafka_consumer_lag", {}).values()
    ]
    numeric = [value for value in values if value is not None]
    return max(numeric) if numeric else None


def _jobs_with_data(snapshots: list[dict[str, Any]], *paths: tuple[str, ...]) -> list[str]:
    jobs: set[str] = set()
    for path in paths:
        mapping_values = _mapping_values(snapshots, path)
        jobs.update(mapping_values)
    return sorted(jobs)


def _mapping_values(snapshots: list[dict[str, Any]], path: tuple[str, ...]) -> set[str]:
    names: set[str] = set()
    for snapshot in snapshots:
        cursor: Any = snapshot
        for key in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        if isinstance(cursor, dict):
            names.update(name for name, value in cursor.items() if _as_float(value) is not None)
    return names


def _top_job_by_metric(snapshots: list[dict[str, Any]], path: tuple[str, ...]) -> str | None:
    peaks: dict[str, float] = {}
    for job in _mapping_values(snapshots, path):
        values = _series_values(snapshots, path + (job,))
        if values:
            peaks[job] = max(values)
    if not peaks:
        return None
    return max(peaks, key=peaks.get)


def _bucket_growth_per_day(series: list[tuple[Any, Any]]) -> float | None:
    if len(series) < 2:
        return None
    first_ts = _parse_iso_datetime(series[0][0])
    last_ts = _parse_iso_datetime(series[-1][0])
    first_value = _as_float(series[0][1])
    last_value = _as_float(series[-1][1])
    if first_ts is None or last_ts is None or first_value is None or last_value is None:
        return None
    elapsed_s = (last_ts - first_ts).total_seconds()
    if elapsed_s <= 0:
        return None
    growth_bytes_per_s = max(last_value - first_value, 0.0) / elapsed_s
    return growth_bytes_per_s * 86_400.0 / 1_000_000_000.0


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}")
    return payload


def _load_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _load_json(path)


def _parse_iso_datetime(value: object) -> Any:
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime  # noqa: PLC0415

        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = max(min(int(round((len(sorted_values) - 1) * quantile)), len(sorted_values) - 1), 0)
    return sorted_values[index]


def _safe_mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _table_row(
    name: str,
    target: str,
    measured: str,
    passed: bool | None,
    notes: str,
    *,
    force_result: str | None = None,
) -> str:
    result = force_result or ("PASS" if passed else "FAIL")
    return f"| {name} | {target} | {measured} | {result} | {notes} |"


def _cost_row(
    name: str,
    *,
    predicted: str,
    actual: str,
    delta: str,
    notes: str,
) -> str:
    return f"| {name} | {predicted} | {actual} | {delta} | {notes} |"


def _format_ms(value: float | None) -> str:
    return f"{value:.1f} ms" if value is not None else "n/a"


def _format_duration(value: float | None) -> str:
    return f"{value:.1f} s" if value is not None else "n/a"


def _format_rate(value: float | None, unit: str) -> str:
    return f"{value:.2f} {unit}" if value is not None else "n/a"


def _format_percent(value: float | None) -> str:
    return f"{value:.2f}%" if value is not None else "n/a"


def _format_number(value: float | None, suffix: str = "") -> str:
    return f"{value:.2f}{suffix}" if value is not None else "n/a"


def _format_delta(actual: float | None, predicted: float | None) -> str:
    if actual is None or predicted is None or predicted == 0.0:
        return "n/a"
    return f"{((actual - predicted) / predicted):+.1%}"


def _format_currency(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "n/a"


def _format_bytes(value: float | None) -> str:
    if value is None:
        return "n/a"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"


def _format_bytes_per_s(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{_format_bytes(value)}/s"


def _format_bool(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "n/a"


def _as_float(value: object) -> float | None:
    if isinstance(value, (float, int)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    return None


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        raise SystemExit(str(exc)) from exc
