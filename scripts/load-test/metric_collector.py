#!/usr/bin/env python3
"""Prometheus metric collection for the end-to-end stress-test harness."""

from __future__ import annotations

import asyncio
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import http_get_json, utc_now  # noqa: E402
from models import MetricSnapshot  # noqa: E402


LATENCY_METRICS: dict[str, str] = {
    "end_to_end_latency_ms": "e2e_latency_ms",
    "inference_latency_ms": "inference_latency_ms",
    "inference_embedding_latency_ms": "inference_embedding_latency_ms",
    "attribute_latency_ms": "attr_classification_latency_ms",
    "event_db_write_latency_ms": "event_db_write_latency_ms",
    "clip_extraction_latency_ms": "clip_extraction_latency_ms",
    "query_latency_ms": "query_latency_ms",
}

THROUGHPUT_QUERIES: dict[str, str] = {
    "detections_per_s": "sum(rate(inference_detections_total[5m]))",
    "events_per_s": "sum(rate(event_emitted_total[5m]))",
    "matches_per_s": "sum(rate(mtmc_matches_total[5m]))",
    "queries_per_s": "sum(rate(query_requests_total[5m]))",
    "clips_per_s": "sum(rate(clip_extracted_total[5m]))",
    "bulk_rows_per_s": "sum(rate(bulk_rows_written_total[5m]))",
    "embeddings_per_s": "sum(rate(mtmc_embeddings_consumed_total[5m]))",
}

ERROR_RATE_QUERIES: dict[str, str] = {
    "inference_publish_errors_per_s": "sum(rate(inference_publish_errors_total[5m]))",
    "attr_quality_rejected_per_s": "sum(rate(attr_quality_rejected_total[5m]))",
    "clip_extraction_errors_per_s": "sum(rate(clip_extraction_errors_total[5m]))",
    "bulk_write_errors_per_s": "sum(rate(bulk_write_errors_total[5m]))",
    "query_5xx_per_s": 'sum(rate(query_requests_total{status=~"5.."}[5m]))',
}

RESOURCE_JOBS: tuple[str, ...] = (
    "edge-agent",
    "ingress-bridge",
    "decode-service",
    "inference-worker",
    "attribute-service",
    "event-engine",
    "clip-service",
    "mtmc-service",
    "bulk-collector",
    "query-api",
    "triton",
)


class MetricCollector:
    """Query Prometheus and retain snapshot history for reporting."""

    def __init__(self, prometheus_url: str, camera_count: int = 1) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.camera_count = max(camera_count, 1)
        self._history: list[MetricSnapshot] = []

    @property
    def history(self) -> list[MetricSnapshot]:
        return list(self._history)

    async def collect_snapshot(self) -> MetricSnapshot:
        """Collect a point-in-time view of the relevant Prometheus metrics."""
        collected_at = utc_now()

        latency_tasks = {
            f"{alias}:{quantile}": asyncio.create_task(
                self._query_scalar(_histogram_quantile_expression(metric_name, quantile))
            )
            for alias, metric_name in LATENCY_METRICS.items()
            for quantile in (0.50, 0.95, 0.99)
        }
        throughput_tasks = {
            alias: asyncio.create_task(self._query_scalar(expression))
            for alias, expression in THROUGHPUT_QUERIES.items()
        }
        error_tasks = {
            alias: asyncio.create_task(self._query_scalar(expression))
            for alias, expression in ERROR_RATE_QUERIES.items()
        }
        grouped_tasks = {
            "cpu_by_job": asyncio.create_task(
                self._query_grouped(
                    f'sum by (job) (rate(process_cpu_seconds_total{{job=~"{_job_regex()}"}}[5m]))',
                    "job",
                )
            ),
            "memory_by_job": asyncio.create_task(
                self._query_grouped(
                    f'sum by (job) (process_resident_memory_bytes{{job=~"{_job_regex()}"}})',
                    "job",
                )
            ),
            "health_by_job": asyncio.create_task(
                self._query_grouped(
                    f'avg by (job) (up{{job=~"{_job_regex()}"}})',
                    "job",
                )
            ),
            "lag_canonical": asyncio.create_task(
                self._query_grouped("max by (group, topic) (kafka_consumer_lag)", "group", "topic")
            ),
            "lag_inference": asyncio.create_task(
                self._query_grouped("max by (topic) (inference_consumer_lag)", "topic")
            ),
            "lag_bulk": asyncio.create_task(self._query_grouped("max(bulk_consumer_lag)",)),
        }
        raw_scalar_tasks = {
            "avg_inference_fps_per_camera": asyncio.create_task(
                self._query_scalar(
                    f"sum(rate(inference_frames_consumed_total[5m])) / {self.camera_count}"
                )
            ),
            "active_tracks_per_camera": asyncio.create_task(
                self._query_scalar(f"sum(inference_tracks_active) / {self.camera_count}")
            ),
            "total_active_tracks": asyncio.create_task(
                self._query_scalar("sum(inference_tracks_active)")
            ),
            "match_score_p95": asyncio.create_task(
                self._query_scalar(_histogram_quantile_expression("mtmc_match_score", 0.95))
            ),
        }

        await asyncio.gather(
            *latency_tasks.values(),
            *throughput_tasks.values(),
            *error_tasks.values(),
            *grouped_tasks.values(),
            *raw_scalar_tasks.values(),
        )

        latency_quantiles = _build_latency_quantiles(latency_tasks)
        throughput_rates = {
            name: task.result() for name, task in throughput_tasks.items()
        }
        error_rates = {name: task.result() for name, task in error_tasks.items()}
        resource_cpu = _normalise_job_map(grouped_tasks["cpu_by_job"].result())
        resource_memory = _normalise_job_map(grouped_tasks["memory_by_job"].result())
        service_health = _normalise_job_map(grouped_tasks["health_by_job"].result())
        kafka_consumer_lag = _build_lag_map(
            grouped_tasks["lag_canonical"].result(),
            grouped_tasks["lag_inference"].result(),
            grouped_tasks["lag_bulk"].result(),
        )
        raw_scalars = {
            name: task.result() for name, task in raw_scalar_tasks.items()
        }

        snapshot = MetricSnapshot(
            collected_at=collected_at,
            latency_quantiles=latency_quantiles,
            throughput_rates=throughput_rates,
            resource_cpu_cores=resource_cpu,
            resource_memory_bytes=resource_memory,
            kafka_consumer_lag=kafka_consumer_lag,
            error_rates=error_rates,
            service_health=service_health,
            raw_scalars=raw_scalars,
        )
        self._history.append(snapshot)
        return snapshot

    def compute_percentiles(
        self,
        metric: str,
        quantiles: list[float],
    ) -> dict[str, float | None]:
        """Compute percentiles for a raw scalar series retained in history."""
        values = [
            value
            for snapshot in self._history
            if (value := snapshot.raw_scalars.get(metric)) is not None
        ]
        return {
            f"p{int(q * 100):02d}": _percentile(values, q) if values else None
            for q in quantiles
        }

    async def _query_scalar(self, expression: str) -> float | None:
        results = await self._query_vector(expression)
        if not results:
            return None
        return _extract_value(results[0])

    async def _query_grouped(
        self,
        expression: str,
        *label_keys: str,
    ) -> dict[str, float | None]:
        results = await self._query_vector(expression)
        if not results:
            return {}
        grouped: dict[str, float | None] = {}
        for result in results:
            metric = result.get("metric", {})
            key_parts = [str(metric.get(label, "")).strip() for label in label_keys]
            key = ":".join(part for part in key_parts if part) or "value"
            grouped[key] = _extract_value(result)
        return grouped

    async def _query_vector(self, expression: str) -> list[dict[str, Any]]:
        payload = await asyncio.to_thread(
            http_get_json,
            f"{self.prometheus_url}/api/v1/query",
            params={"query": expression},
        )
        if payload.get("status") != "success":
            return []
        data = payload.get("data", {})
        results = data.get("result", [])
        return results if isinstance(results, list) else []


def _build_lag_map(
    canonical: dict[str, float | None],
    inference: dict[str, float | None],
    bulk: dict[str, float | None],
) -> dict[str, float | None]:
    lag_map = dict(canonical)
    for topic, value in inference.items():
        lag_map.setdefault(f"detector-worker:{topic}", value)
    for key, value in bulk.items():
        lag_map.setdefault("bulk-collector", value if key == "value" else value)
    return lag_map


def _build_latency_quantiles(
    tasks: dict[str, "asyncio.Task[float | None]"],
) -> dict[str, dict[str, float | None]]:
    quantiles: dict[str, dict[str, float | None]] = {}
    for task_key, task in tasks.items():
        metric_alias, quantile = task_key.split(":")
        quantiles.setdefault(metric_alias, {})[_quantile_label(float(quantile))] = task.result()
    return quantiles


def _normalise_job_map(values: dict[str, float | None]) -> dict[str, float | None]:
    return {job: values.get(job) for job in RESOURCE_JOBS}


def _extract_value(result: dict[str, Any]) -> float | None:
    value = result.get("value")
    if not isinstance(value, list) or len(value) != 2:
        return None
    raw_value = value[1]
    if raw_value in {"NaN", "nan", "+Inf", "-Inf"}:
        return None
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _histogram_quantile_expression(metric_name: str, quantile: float) -> str:
    return (
        f"histogram_quantile({quantile:.2f}, "
        f"sum(rate({metric_name}_bucket[5m])) by (le))"
    )


def _job_regex() -> str:
    return "|".join(RESOURCE_JOBS)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _quantile_label(quantile: float) -> str:
    return f"p{int(round(quantile * 100)):02d}"


def _history_times(history: list[MetricSnapshot]) -> tuple[datetime, datetime] | None:
    if not history:
        return None
    return history[0].collected_at, history[-1].collected_at


def summarise_scalar_history(
    history: list[MetricSnapshot],
    metric_key: str,
) -> dict[str, float | None]:
    """Summarise a raw scalar across collected snapshots."""
    values = [
        value
        for snapshot in history
        if (value := snapshot.raw_scalars.get(metric_key)) is not None
    ]
    if not values:
        return {"avg": None, "peak": None, "p95": None}
    return {
        "avg": statistics.fmean(values),
        "peak": max(values),
        "p95": _percentile(values, 0.95),
    }


if __name__ == "__main__":
    raise SystemExit(
        "metric_collector.py is a library module. Run run_stress_test.py instead."
    )
