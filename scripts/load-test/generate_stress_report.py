#!/usr/bin/env python3
"""Generate a Markdown report for the end-to-end stress test."""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import load_cost_model_params  # noqa: E402
from models import ChaosResult, MetricSnapshot, TestConfig  # noqa: E402


LATENCY_LABELS: dict[str, str] = {
    "end_to_end_latency_ms": "End-to-end latency",
    "inference_latency_ms": "Inference latency",
    "inference_embedding_latency_ms": "Embedding latency",
    "attribute_latency_ms": "Attribute classification latency",
    "event_db_write_latency_ms": "Event DB write latency",
    "clip_extraction_latency_ms": "Clip extraction latency",
    "query_latency_ms": "Query latency",
}

THROUGHPUT_LABELS: dict[str, str] = {
    "detections_per_s": "Detections / s",
    "events_per_s": "Events / s",
    "matches_per_s": "MTMC matches / s",
    "queries_per_s": "Queries / s",
    "clips_per_s": "Clips / s",
    "bulk_rows_per_s": "Bulk rows / s",
    "embeddings_per_s": "Embeddings / s",
}

SYNTHETIC_DUTY_CYCLE = 0.15


def generate_report(
    metrics: list[MetricSnapshot],
    chaos_results: list[ChaosResult],
    config: TestConfig,
    output_path: Path,
) -> None:
    """Write the Markdown stress-test report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sustained_metrics = _select_sustained_metrics(metrics, config.duration_s)
    active_metrics = sustained_metrics or metrics

    report = "\n".join(
        [
            "# End-to-End Stress Test Report",
            "",
            "## Test Configuration",
            f"- Duration: {config.duration_s} s",
            f"- Cameras: {config.camera_count}",
            f"- Target FPS per camera: {config.camera_fps}",
            f"- Query load target: {config.query_qps} QPS",
            f"- Prometheus: `{config.prometheus_url}`",
            f"- Query API: `{config.query_api_url}`",
            f"- Chaos enabled: {'yes' if config.chaos_enabled else 'no'}",
            f"- Replay media: `{config.replay_frame_dir}`"
            if config.replay_frame_dir is not None
            else "- Replay media: synthetic frames only",
            "",
            "## NFR Pass/Fail",
            _build_nfr_table(active_metrics, config),
            "",
            "## Per-Stage Latency",
            _build_latency_table(active_metrics),
            "",
            "## Throughput",
            _build_throughput_table(active_metrics),
            "",
            "## Resource Utilization",
            _build_resource_table(active_metrics),
            "",
            "## Kafka Consumer Lag",
            _build_lag_table(active_metrics),
            "",
            "## Chaos Scenarios",
            _build_chaos_table(chaos_results),
            "",
            "## Bottleneck Analysis",
            _build_bottleneck_summary(active_metrics),
            "",
            "## Cost Model Comparison",
            _build_cost_model_table(active_metrics, config),
            "",
            "## Notes",
            "- Sustained-phase summaries use the middle test window after ramp-up and before ramp-down when sufficient snapshots are available.",
            "- Latency rows report the worst observed rolling quantile during the sustained phase, not a single global histogram scrape at report time.",
            "- If the canonical observability metric is absent in Prometheus, the report marks the NFR as `FAIL` and describes any fallback metric that was used for context.",
            "- Synthetic frames are useful for throughput and recovery tests but may not trigger realistic detections or downstream events. Supply a replay directory for event-heavy validation.",
        ]
    )
    output_path.write_text(report + "\n", encoding="utf-8")


def _build_nfr_table(metrics: list[MetricSnapshot], config: TestConfig) -> str:
    rows = [
        "| NFR | Target | Measured | Result | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]

    e2e_p95 = _latency_quantile(metrics, "end_to_end_latency_ms", "p95")
    rows.append(
        _table_row(
            "End-to-end latency (p95)",
            "< 2,000 ms",
            _format_ms(e2e_p95),
            e2e_p95 is not None and e2e_p95 < 2000.0,
            "Derived from `e2e_latency_ms`; missing metric is treated as FAIL.",
        )
    )

    inference_fps = _average_raw_scalar(metrics, "avg_inference_fps_per_camera")
    fps_pass = inference_fps is not None and 5.0 <= inference_fps <= 10.0
    rows.append(
        _table_row(
            "Inference throughput",
            "5-10 FPS per camera",
            _format_rate(inference_fps, suffix="fps/camera"),
            fps_pass,
            "Current inference service does not expose `inference_fps{camera_id}`. This uses `inference_frames_consumed_total / camera_count` as a fallback context metric.",
        )
    )

    rows.append(
        _table_row(
            "Pilot cameras",
            "4 cameras",
            str(config.camera_count),
            config.camera_count == 4,
            "The current services expose `/health` but not an active-stream count, so the harness validates the configured camera fan-out.",
        )
    )

    query_p95 = _latency_quantile(metrics, "query_latency_ms", "p95")
    rows.append(
        _table_row(
            "Query latency (p95)",
            "< 500 ms",
            _format_ms(query_p95),
            query_p95 is not None and query_p95 < 500.0,
            "From the Query API `query_latency_ms` histogram.",
        )
    )

    max_lag = _max_kafka_lag(metrics)
    rows.append(
        _table_row(
            "Kafka consumer lag",
            "< 10,000 messages",
            _format_number(max_lag),
            max_lag is not None and max_lag < 10_000.0,
            "Uses canonical `kafka_consumer_lag` when present, otherwise available service-specific lag gauges.",
        )
    )
    return "\n".join(rows)


def _build_latency_table(metrics: list[MetricSnapshot]) -> str:
    rows = [
        "| Stage | p50 | p95 | p99 |",
        "| --- | --- | --- | --- |",
    ]
    for metric_key, label in LATENCY_LABELS.items():
        rows.append(
            "| "
            + " | ".join(
                [
                    label,
                    _format_ms(_latency_quantile(metrics, metric_key, "p50")),
                    _format_ms(_latency_quantile(metrics, metric_key, "p95")),
                    _format_ms(_latency_quantile(metrics, metric_key, "p99")),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _build_throughput_table(metrics: list[MetricSnapshot]) -> str:
    rows = [
        "| Metric | Average | Peak |",
        "| --- | --- | --- |",
    ]
    for metric_key, label in THROUGHPUT_LABELS.items():
        values = _collect_from_map(metrics, "throughput_rates", metric_key)
        rows.append(
            "| "
            + " | ".join(
                [
                    label,
                    _format_rate(_safe_mean(values)),
                    _format_rate(max(values) if values else None),
                ]
            )
            + " |"
        )

    fps_values = _collect_raw_scalar(metrics, "avg_inference_fps_per_camera")
    rows.append(
        "| "
        + " | ".join(
            [
                "Derived FPS / camera",
                _format_rate(_safe_mean(fps_values), suffix="fps"),
                _format_rate(max(fps_values) if fps_values else None, suffix="fps"),
            ]
        )
        + " |"
    )
    return "\n".join(rows)


def _build_resource_table(metrics: list[MetricSnapshot]) -> str:
    rows = [
        "| Service | Avg CPU (cores) | Peak CPU (cores) | Avg RAM | Peak RAM |",
        "| --- | --- | --- | --- | --- |",
    ]
    jobs = sorted(
        {
            *(
                service
                for snapshot in metrics
                for service in snapshot.resource_cpu_cores
                if snapshot.resource_cpu_cores.get(service) is not None
            ),
            *(
                service
                for snapshot in metrics
                for service in snapshot.resource_memory_bytes
                if snapshot.resource_memory_bytes.get(service) is not None
            ),
        }
    )
    for job in jobs:
        cpu_values = _collect_from_map(metrics, "resource_cpu_cores", job)
        ram_values = _collect_from_map(metrics, "resource_memory_bytes", job)
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


def _build_lag_table(metrics: list[MetricSnapshot]) -> str:
    rows = [
        "| Group / Topic | Max Lag |",
        "| --- | --- |",
    ]
    lag_map: dict[str, list[float]] = {}
    for snapshot in metrics:
        for key, value in snapshot.kafka_consumer_lag.items():
            if value is None:
                continue
            lag_map.setdefault(key, []).append(value)
    for key in sorted(lag_map):
        rows.append(f"| {key} | {_format_number(max(lag_map[key]))} |")
    if len(rows) == 2:
        rows.append("| no data | n/a |")
    return "\n".join(rows)


def _build_chaos_table(results: list[ChaosResult]) -> str:
    rows = [
        "| Scenario | Target | Success | Recovery Time | Data Loss | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if not results:
        rows.append("| Chaos disabled | n/a | n/a | n/a | n/a | No chaos scenarios were executed. |")
        return "\n".join(rows)
    for result in results:
        rows.append(
            "| "
            + " | ".join(
                [
                    result.name,
                    result.target,
                    "yes" if result.success else "no",
                    _format_duration(result.recovery_time_s),
                    _format_bool(result.data_loss),
                    result.notes.replace("|", "/"),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _build_bottleneck_summary(metrics: list[MetricSnapshot]) -> str:
    if not metrics:
        return "- No Prometheus snapshots were collected."

    slowest_stage = None
    slowest_p95 = -1.0
    for metric_key, label in LATENCY_LABELS.items():
        p95 = _latency_quantile(metrics, metric_key, "p95")
        if p95 is not None and p95 > slowest_p95:
            slowest_stage = label
            slowest_p95 = p95

    top_cpu_service = _top_resource_consumer(metrics, "resource_cpu_cores")
    top_ram_service = _top_resource_consumer(metrics, "resource_memory_bytes")
    bullets = [
        f"- Slowest observed stage: {slowest_stage or 'n/a'}"
        + (f" at {_format_ms(slowest_p95)} p95." if slowest_stage is not None else "."),
        f"- Highest CPU consumer: {top_cpu_service or 'n/a'}.",
        f"- Highest RAM consumer: {top_ram_service or 'n/a'}.",
        "- Investigate missing stage metrics before treating a PASS as production-ready; absent telemetry is itself a readiness gap.",
    ]
    return "\n".join(bullets)


def _build_cost_model_table(metrics: list[MetricSnapshot], config: TestConfig) -> str:
    params = load_cost_model_params(config.cost_model_params_path)
    predicted_fps = _nested_float(params, "cost_model", "workload", "inference_fps", "value")
    predicted_active_tracks = _nested_float(
        params,
        "cost_model",
        "workload",
        "active_tracks_per_camera",
        "value",
    )
    predicted_duty_cycle = _nested_float(
        params,
        "cost_model",
        "motion_duty_cycle_scenarios",
        "P50",
        "value",
    )
    actual_fps = _average_raw_scalar(metrics, "avg_inference_fps_per_camera")
    actual_tracks = _average_raw_scalar(metrics, "active_tracks_per_camera")

    rows = [
        "| Parameter | Predicted | Actual / Observed | Delta |",
        "| --- | --- | --- | --- |",
        _cost_row(
            "Inference FPS per camera",
            _format_rate(predicted_fps, suffix="fps"),
            _format_rate(actual_fps, suffix="fps"),
            _format_delta(actual_fps, predicted_fps),
        ),
        _cost_row(
            "Active tracks per camera",
            _format_number(predicted_active_tracks),
            _format_number(actual_tracks),
            _format_delta(actual_tracks, predicted_active_tracks),
        ),
        _cost_row(
            "Motion duty cycle",
            _format_percent(predicted_duty_cycle),
            "replay-driven"
            if config.replay_frame_dir is not None
            else _format_percent(SYNTHETIC_DUTY_CYCLE),
            "n/a" if config.replay_frame_dir is not None else _format_delta(SYNTHETIC_DUTY_CYCLE, predicted_duty_cycle),
        ),
    ]
    return "\n".join(rows)


def _select_sustained_metrics(
    metrics: list[MetricSnapshot],
    duration_s: int,
) -> list[MetricSnapshot]:
    if len(metrics) < 3:
        return metrics
    start_time = metrics[0].collected_at
    ramp_up_s = min(300.0, max(duration_s / 12.0, 30.0))
    ramp_down_s = ramp_up_s
    sustain_start = start_time.timestamp() + ramp_up_s
    sustain_end = start_time.timestamp() + max(duration_s - ramp_down_s, ramp_up_s)
    selected = [
        snapshot
        for snapshot in metrics
        if sustain_start <= snapshot.collected_at.timestamp() <= sustain_end
    ]
    return selected or metrics


def _latency_quantile(
    metrics: list[MetricSnapshot],
    metric_key: str,
    quantile: str,
) -> float | None:
    values = [
        value
        for snapshot in metrics
        if (series := snapshot.latency_quantiles.get(metric_key))
        and (value := series.get(quantile)) is not None
    ]
    return max(values) if values else None


def _average_raw_scalar(metrics: list[MetricSnapshot], metric_key: str) -> float | None:
    values = _collect_raw_scalar(metrics, metric_key)
    return _safe_mean(values)


def _collect_raw_scalar(metrics: list[MetricSnapshot], metric_key: str) -> list[float]:
    return [
        value
        for snapshot in metrics
        if (value := snapshot.raw_scalars.get(metric_key)) is not None
    ]


def _collect_from_map(
    metrics: list[MetricSnapshot],
    attribute_name: str,
    key: str,
) -> list[float]:
    values: list[float] = []
    for snapshot in metrics:
        mapping = getattr(snapshot, attribute_name)
        value = mapping.get(key)
        if value is not None:
            values.append(value)
    return values


def _max_kafka_lag(metrics: list[MetricSnapshot]) -> float | None:
    values = [
        value
        for snapshot in metrics
        for value in snapshot.kafka_consumer_lag.values()
        if value is not None
    ]
    return max(values) if values else None


def _top_resource_consumer(
    metrics: list[MetricSnapshot],
    attribute_name: str,
) -> str | None:
    peaks: dict[str, float] = {}
    for snapshot in metrics:
        mapping = getattr(snapshot, attribute_name)
        for name, value in mapping.items():
            if value is None:
                continue
            peaks[name] = max(peaks.get(name, value), value)
    if not peaks:
        return None
    return max(peaks, key=peaks.get)


def _safe_mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _nested_float(payload: dict[str, object], *path: str) -> float | None:
    cursor: object = payload
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    if cursor is None:
        return None
    try:
        value = float(cursor)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _table_row(
    nfr: str,
    target: str,
    measured: str,
    passed: bool,
    notes: str,
) -> str:
    return f"| {nfr} | {target} | {measured} | {'PASS' if passed else 'FAIL'} | {notes} |"


def _cost_row(parameter: str, predicted: str, actual: str, delta: str) -> str:
    return f"| {parameter} | {predicted} | {actual} | {delta} |"


def _format_ms(value: float | None) -> str:
    return f"{value:.1f} ms" if value is not None else "n/a"


def _format_rate(value: float | None, *, suffix: str = "/s") -> str:
    return f"{value:.2f} {suffix}" if value is not None else "n/a"


def _format_number(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


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


def _format_bool(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def _format_duration(value: float | None) -> str:
    return f"{value:.1f} s" if value is not None else "n/a"


def _format_delta(actual: float | None, predicted: float | None) -> str:
    if actual is None or predicted is None:
        return "n/a"
    return f"{actual - predicted:+.2f}"


def _format_percent(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "n/a"


if __name__ == "__main__":
    raise SystemExit(
        "generate_stress_report.py is a library module. Run run_stress_test.py instead."
    )
