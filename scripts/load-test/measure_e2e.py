#!/usr/bin/env python3
"""Instrument end-to-end pipeline latency and throughput at scale.

Collects Prometheus histogram quantiles plus direct Query API probes, then
emits raw JSON snapshots and an aggregated CSV summary suitable for the
100-camera report generator.

Usage:
    python measure_e2e.py --prometheus http://localhost:9090 \
        --query-api http://localhost:8000 --duration 14400 --interval 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import signal
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import build_query_headers, http_get_json, isoformat_utc, utc_now  # noqa: E402


LOGGER = logging.getLogger("measure_e2e")
DEFAULT_OUTPUT_JSON = SCRIPT_DIR.parents[1] / "artifacts" / "load-test" / "measure-e2e.json"
DEFAULT_SUMMARY_CSV = SCRIPT_DIR.parents[1] / "artifacts" / "load-test" / "measure-e2e-summary.csv"
RESOURCE_JOBS = (
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
QUERY_ENDPOINTS = {
    "/detections": lambda camera_id: {"camera_id": camera_id, "limit": "5"},
    "/tracks": lambda camera_id: {"camera_id": camera_id, "limit": "5"},
    "/events": lambda camera_id: {"camera_id": camera_id, "limit": "5"},
}


@dataclass(frozen=True, slots=True)
class StageDefinition:
    """Prometheus histogram stage definition."""

    name: str
    unit: str
    metric_names: tuple[str, ...]
    scale: float = 1.0


STAGE_DEFINITIONS = (
    StageDefinition(
        name="ingest_latency_ms",
        unit="ms",
        metric_names=("e2e_latency_ms", "edge_nats_publish_latency_ms"),
    ),
    StageDefinition(
        name="decode_latency_ms",
        unit="ms",
        metric_names=("decode_latency_ms",),
    ),
    StageDefinition(
        name="inference_latency_ms",
        unit="ms",
        metric_names=("inference_latency_ms",),
    ),
    StageDefinition(
        name="embedding_latency_ms",
        unit="ms",
        metric_names=("inference_embedding_latency_ms",),
    ),
    StageDefinition(
        name="db_write_latency_ms",
        unit="ms",
        metric_names=("bulk_write_latency_ms",),
    ),
    StageDefinition(
        name="query_latency_ms",
        unit="ms",
        metric_names=("query_latency_ms",),
    ),
    StageDefinition(
        name="mtmc_match_latency_ms",
        unit="ms",
        metric_names=("mtmc_match_duration_seconds",),
        scale=1000.0,
    ),
)


class E2ECollector:
    """Collect Prometheus snapshots plus direct probe timings."""

    def __init__(
        self,
        *,
        prometheus_url: str,
        query_api_url: str,
        query_headers: dict[str, str],
        probe_camera_id: str,
        camera_count: int,
        db_dsn: str | None,
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.query_api_url = query_api_url.rstrip("/")
        self.query_headers = query_headers
        self.probe_camera_id = probe_camera_id
        self.camera_count = max(camera_count, 1)
        self.db_dsn = db_dsn
        self._db_pool: Any = None

    async def start(self) -> None:
        if self.db_dsn is None:
            return
        try:
            import asyncpg  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'asyncpg'; omit --db-dsn or install it"
            ) from exc
        self._db_pool = await asyncpg.create_pool(self.db_dsn, min_size=1, max_size=2)

    async def close(self) -> None:
        if self._db_pool is not None:
            await self._db_pool.close()

    async def collect_snapshot(self) -> dict[str, Any]:
        collected_at = utc_now()
        stage_data, stage_notes = await self._collect_stage_data()
        query_probe_data, query_probe_notes = await self._collect_query_probes()
        throughput = await self._collect_throughput()
        lag = await self._collect_lag()
        resources = await self._collect_resources()
        gpu = await self._collect_gpu()
        bucket_bytes = await self._collect_bucket_sizes()
        availability = await self._collect_availability()
        db_probe_ms = await self._collect_db_probe()

        notes = [*stage_notes, *query_probe_notes]
        return {
            "collected_at": isoformat_utc(collected_at),
            "stage_latency": stage_data,
            "direct_query_latency_ms": query_probe_data,
            "throughput": throughput,
            "kafka_consumer_lag": lag,
            "resources": resources,
            "gpu": gpu,
            "bucket_bytes": bucket_bytes,
            "availability": availability,
            "db_probe_ms": db_probe_ms,
            "notes": notes,
        }

    async def _collect_stage_data(self) -> tuple[dict[str, Any], list[str]]:
        notes: list[str] = []
        results: dict[str, Any] = {}
        for definition in STAGE_DEFINITIONS:
            stage_result, note = await self._collect_stage(definition)
            results[definition.name] = stage_result
            if note is not None:
                notes.append(note)
        return results, notes

    async def _collect_stage(self, definition: StageDefinition) -> tuple[dict[str, Any], str | None]:
        for metric_name in definition.metric_names:
            quantiles = await asyncio.gather(
                self._query_scalar(_histogram_quantile_expr(metric_name, 0.50, definition.scale)),
                self._query_scalar(_histogram_quantile_expr(metric_name, 0.95, definition.scale)),
                self._query_scalar(_histogram_quantile_expr(metric_name, 0.99, definition.scale)),
            )
            if any(value is not None for value in quantiles):
                return (
                    {
                        "p50": quantiles[0],
                        "p95": quantiles[1],
                        "p99": quantiles[2],
                        "source_metric": metric_name,
                        "unit": definition.unit,
                    },
                    None,
                )
        return (
            {
                "p50": None,
                "p95": None,
                "p99": None,
                "source_metric": None,
                "unit": definition.unit,
            },
            f"missing stage metric for {definition.name}: tried {', '.join(definition.metric_names)}",
        )

    async def _collect_query_probes(self) -> tuple[dict[str, float | None], list[str]]:
        notes: list[str] = []
        tasks = {
            endpoint: asyncio.create_task(self._probe_query_endpoint(endpoint, params_builder(self.probe_camera_id)))
            for endpoint, params_builder in QUERY_ENDPOINTS.items()
        }
        await asyncio.gather(*tasks.values())
        latencies: dict[str, float | None] = {}
        for endpoint, task in tasks.items():
            latency_ms, error = task.result()
            latencies[endpoint] = latency_ms
            if error is not None:
                notes.append(f"direct query probe failed for {endpoint}: {error}")
        return latencies, notes

    async def _collect_throughput(self) -> dict[str, float | None]:
        expressions = {
            "frames_in_per_s": "sum(rate(decode_frames_consumed_total[5m]))",
            "frames_decoded_per_s": "sum(rate(decode_frames_decoded_total[5m]))",
            "inference_frames_per_s": "sum(rate(inference_frames_consumed_total[5m]))",
            "detections_per_s": "sum(rate(inference_detections_total[5m]))",
            "events_per_s": "sum(rate(event_emitted_total[5m]))",
            "matches_per_s": "sum(rate(mtmc_matches_total[5m]))",
            "queries_per_s": "sum(rate(query_requests_total[5m]))",
            "bulk_rows_per_s": "sum(rate(bulk_rows_written_total[5m]))",
            "total_active_tracks": "sum(inference_tracks_active)",
        }
        tasks = {
            key: asyncio.create_task(self._query_scalar(expr))
            for key, expr in expressions.items()
        }
        await asyncio.gather(*tasks.values())
        throughput = {key: task.result() for key, task in tasks.items()}
        frames_per_s = throughput.get("inference_frames_per_s")
        throughput["fps_per_camera"] = (
            frames_per_s / self.camera_count if frames_per_s is not None else None
        )
        active_tracks = throughput.get("total_active_tracks")
        throughput["active_tracks_per_camera"] = (
            active_tracks / self.camera_count if active_tracks is not None else None
        )
        return throughput

    async def _collect_lag(self) -> dict[str, float | None]:
        canonical = await self._query_grouped(
            "max by (group, topic) (kafka_consumer_lag)",
            ("group", "topic"),
        )
        if canonical:
            return canonical
        fallback: dict[str, float | None] = {}
        inference = await self._query_grouped(
            "max by (topic) (inference_consumer_lag)",
            ("topic",),
        )
        for topic, value in inference.items():
            fallback[f"detector-worker:{topic}"] = value
        bulk = await self._query_grouped(
            "max by (group, topic) (bulk_consumer_lag)",
            ("group", "topic"),
        )
        fallback.update(bulk)
        decode = await self._query_grouped(
            "max by (topic) (decode_consumer_lag)",
            ("topic",),
        )
        for topic, value in decode.items():
            fallback.setdefault(f"decode-service:{topic}", value)
        return fallback

    async def _collect_resources(self) -> dict[str, dict[str, float | None]]:
        job_regex = "|".join(RESOURCE_JOBS)
        cpu = await self._query_grouped(
            f'sum by (job) (rate(process_cpu_seconds_total{{job=~"{job_regex}"}}[5m]))',
            ("job",),
        )
        memory = await self._query_grouped(
            f'sum by (job) (process_resident_memory_bytes{{job=~"{job_regex}"}})',
            ("job",),
        )
        disk = await self._query_grouped(
            f'sum by (job) (container_fs_usage_bytes{{job=~"{job_regex}"}})',
            ("job",),
        )
        network_rx = await self._query_grouped(
            f'sum by (job) (rate(container_network_receive_bytes_total{{job=~"{job_regex}"}}[5m]))',
            ("job",),
        )
        network_tx = await self._query_grouped(
            f'sum by (job) (rate(container_network_transmit_bytes_total{{job=~"{job_regex}"}}[5m]))',
            ("job",),
        )
        return {
            "cpu_cores": self._normalise_job_map(cpu),
            "memory_bytes": self._normalise_job_map(memory),
            "disk_bytes": self._normalise_job_map(disk),
            "network_rx_bytes_per_s": self._normalise_job_map(network_rx),
            "network_tx_bytes_per_s": self._normalise_job_map(network_tx),
        }

    async def _collect_gpu(self) -> dict[str, float | None]:
        tasks = {
            "avg_utilization_pct": asyncio.create_task(self._query_scalar("avg(nv_gpu_utilization)")),
            "max_utilization_pct": asyncio.create_task(self._query_scalar("max(nv_gpu_utilization)")),
            "memory_used_bytes": asyncio.create_task(self._query_scalar("sum(nv_gpu_memory_used_bytes)")),
            "memory_total_bytes": asyncio.create_task(self._query_scalar("sum(nv_gpu_memory_total_bytes)")),
            "gpu_count": asyncio.create_task(
                self._query_scalar("count(max by (gpu_uuid) (nv_gpu_memory_total_bytes))")
            ),
        }
        await asyncio.gather(*tasks.values())
        return {key: task.result() for key, task in tasks.items()}

    async def _collect_bucket_sizes(self) -> dict[str, float | None]:
        return await self._query_grouped(
            "max by (bucket) (minio_bucket_usage_total_bytes)",
            ("bucket",),
        )

    async def _collect_availability(self) -> dict[str, Any]:
        job_regex = "|".join(RESOURCE_JOBS)
        overall = await self._query_scalar(f'avg(up{{job=~"{job_regex}"}}) * 100')
        by_job = await self._query_grouped(
            f'avg by (job) (up{{job=~"{job_regex}"}}) * 100',
            ("job",),
        )
        return {"overall_pct": overall, "by_job_pct": by_job}

    async def _collect_db_probe(self) -> float | None:
        if self._db_pool is None:
            return None
        started_at = time.perf_counter()
        async with self._db_pool.acquire() as connection:
            await connection.fetchval(
                "SELECT COUNT(*) FROM detections WHERE time >= now() - interval '5 minutes'"
            )
        return (time.perf_counter() - started_at) * 1000.0

    async def _probe_query_endpoint(
        self,
        endpoint: str,
        params: dict[str, str],
    ) -> tuple[float | None, str | None]:
        started_at = time.perf_counter()
        try:
            await asyncio.to_thread(
                http_get_json,
                f"{self.query_api_url}{endpoint}",
                params=params,
                headers=self.query_headers,
            )
        except Exception as exc:
            return None, str(exc)
        return (time.perf_counter() - started_at) * 1000.0, None

    async def _query_scalar(self, expression: str) -> float | None:
        results = await self._query_vector(expression)
        if not results:
            return None
        return _extract_value(results[0])

    async def _query_grouped(
        self,
        expression: str,
        labels: tuple[str, ...],
    ) -> dict[str, float | None]:
        results = await self._query_vector(expression)
        grouped: dict[str, float | None] = {}
        for item in results:
            metric = item.get("metric", {})
            key = ":".join(str(metric.get(label, "")).strip() for label in labels).strip(":")
            grouped[key or "value"] = _extract_value(item)
        return grouped

    async def _query_vector(self, expression: str) -> list[dict[str, Any]]:
        payload = await asyncio.to_thread(
            http_get_json,
            f"{self.prometheus_url}/api/v1/query",
            params={"query": expression},
        )
        if payload.get("status") != "success":
            return []
        results = payload.get("data", {}).get("result", [])
        return results if isinstance(results, list) else []

    def _normalise_job_map(self, values: dict[str, float | None]) -> dict[str, float | None]:
        return {job: values.get(job) for job in RESOURCE_JOBS}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", dest="prometheus_url", required=True)
    parser.add_argument("--query-api", dest="query_api_url", default="http://localhost:8000")
    parser.add_argument("--duration", dest="duration_s", type=int, default=14_400)
    parser.add_argument("--interval", dest="interval_s", type=float, default=30.0)
    parser.add_argument("--camera-count", type=int, default=100)
    parser.add_argument("--probe-camera-id", default="cam-001")
    parser.add_argument("--query-jwt-secret", default="pilot-jwt-secret-change-me")
    parser.add_argument("--query-cookie-name", default="access_token")
    parser.add_argument("--query-role", default="admin")
    parser.add_argument("--db-dsn", default=None)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--summary-csv", type=Path, default=DEFAULT_SUMMARY_CSV)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def run_measurement(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    collector = E2ECollector(
        prometheus_url=args.prometheus_url,
        query_api_url=args.query_api_url,
        query_headers=build_query_headers(
            secret=args.query_jwt_secret,
            cookie_name=args.query_cookie_name,
            role=args.query_role,
            camera_scope=None,
        ),
        probe_camera_id=args.probe_camera_id,
        camera_count=args.camera_count,
        db_dsn=args.db_dsn,
    )
    snapshots: list[dict[str, Any]] = []
    started_at = time.monotonic()
    await collector.start()
    try:
        while not stop_event.is_set():
            snapshots.append(await collector.collect_snapshot())
            if (time.monotonic() - started_at) >= args.duration_s:
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=args.interval_s)
            except asyncio.TimeoutError:
                continue
    finally:
        await collector.close()

    _write_outputs(
        snapshots=snapshots,
        config={
            "prometheus_url": args.prometheus_url,
            "query_api_url": args.query_api_url,
            "duration_s": args.duration_s,
            "interval_s": args.interval_s,
            "camera_count": args.camera_count,
            "probe_camera_id": args.probe_camera_id,
            "db_probe_enabled": args.db_dsn is not None,
        },
        output_json=args.output_json,
        summary_csv=args.summary_csv,
    )
    LOGGER.info(
        "wrote %d snapshots to %s and summary to %s",
        len(snapshots),
        args.output_json,
        args.summary_csv,
    )


def _write_outputs(
    *,
    snapshots: list[dict[str, Any]],
    config: dict[str, Any],
    output_json: Path,
    summary_csv: Path,
) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    summary_csv.parent.mkdir(parents=True, exist_ok=True)
    payload = {"config": config, "snapshots": snapshots}
    output_json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with summary_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "category",
                "name",
                "unit",
                "average",
                "peak",
                "p50",
                "p95",
                "p99",
            ],
        )
        writer.writeheader()
        for row in _build_summary_rows(snapshots):
            writer.writerow(row)


def _build_summary_rows(snapshots: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    for stage in STAGE_DEFINITIONS:
        series = [snapshot["stage_latency"][stage.name] for snapshot in snapshots]
        for quantile in ("p50", "p95", "p99"):
            values = [item.get(quantile) for item in series if item.get(quantile) is not None]
            rows.append(
                _summary_row(
                    category="stage_latency",
                    name=f"{stage.name}:{quantile}",
                    unit=stage.unit,
                    values=values,
                )
            )

    for metric_name in (
        "frames_in_per_s",
        "frames_decoded_per_s",
        "inference_frames_per_s",
        "detections_per_s",
        "events_per_s",
        "matches_per_s",
        "queries_per_s",
        "bulk_rows_per_s",
        "fps_per_camera",
        "total_active_tracks",
        "active_tracks_per_camera",
    ):
        values = [
            snapshot["throughput"].get(metric_name)
            for snapshot in snapshots
            if snapshot["throughput"].get(metric_name) is not None
        ]
        rows.append(
            _summary_row(
                category="throughput",
                name=metric_name,
                unit="per_s" if metric_name != "fps_per_camera" else "fps",
                values=values,
            )
        )

    for endpoint in QUERY_ENDPOINTS:
        values = [
            snapshot["direct_query_latency_ms"].get(endpoint)
            for snapshot in snapshots
            if snapshot["direct_query_latency_ms"].get(endpoint) is not None
        ]
        rows.append(
            _summary_row(
                category="direct_probe",
                name=endpoint,
                unit="ms",
                values=values,
            )
        )

    lag_values = [
        value
        for snapshot in snapshots
        for value in snapshot["kafka_consumer_lag"].values()
        if value is not None
    ]
    rows.append(
        _summary_row(
            category="kafka_lag",
            name="max_consumer_lag",
            unit="messages",
            values=lag_values,
        )
    )

    for gpu_metric in ("avg_utilization_pct", "max_utilization_pct", "memory_used_bytes"):
        values = [
            snapshot["gpu"].get(gpu_metric)
            for snapshot in snapshots
            if snapshot["gpu"].get(gpu_metric) is not None
        ]
        unit = "pct" if "utilization" in gpu_metric else "bytes"
        rows.append(_summary_row(category="gpu", name=gpu_metric, unit=unit, values=values))

    for job in RESOURCE_JOBS:
        for resource_key, unit in (
            ("cpu_cores", "cores"),
            ("memory_bytes", "bytes"),
            ("disk_bytes", "bytes"),
            ("network_rx_bytes_per_s", "bytes_per_s"),
            ("network_tx_bytes_per_s", "bytes_per_s"),
        ):
            values = [
                snapshot["resources"][resource_key].get(job)
                for snapshot in snapshots
                if snapshot["resources"][resource_key].get(job) is not None
            ]
            rows.append(
                _summary_row(
                    category=f"resource:{resource_key}",
                    name=job,
                    unit=unit,
                    values=values,
                )
            )

    return rows


def _summary_row(
    *,
    category: str,
    name: str,
    unit: str,
    values: list[float | None],
) -> dict[str, str]:
    numeric_values = [float(value) for value in values if value is not None and math.isfinite(value)]
    return {
        "category": category,
        "name": name,
        "unit": unit,
        "average": _format_float(statistics.fmean(numeric_values) if numeric_values else None),
        "peak": _format_float(max(numeric_values) if numeric_values else None),
        "p50": _format_float(_percentile(numeric_values, 0.50)),
        "p95": _format_float(_percentile(numeric_values, 0.95)),
        "p99": _format_float(_percentile(numeric_values, 0.99)),
    }


def _histogram_quantile_expr(metric_name: str, quantile: float, scale: float) -> str:
    prefix = f"{scale} * " if scale != 1.0 else ""
    return (
        f"{prefix}histogram_quantile("
        f"{quantile}, sum by (le) (rate({metric_name}_bucket[5m])))"
    )


def _extract_value(result: dict[str, Any]) -> float | None:
    value = result.get("value")
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        numeric = float(value[1])
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = max(min(int(round((len(sorted_values) - 1) * quantile)), len(sorted_values) - 1), 0)
    return sorted_values[index]


def _format_float(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        LOGGER.warning("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            signal.signal(sig, lambda _signum, _frame: stop_event.set())


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    asyncio.run(run_measurement(args))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        raise SystemExit(str(exc)) from exc
