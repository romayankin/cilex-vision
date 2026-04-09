#!/usr/bin/env python3
"""Compare production vs shadow inference outputs.

Usage:
    python compare_shadow.py --prometheus http://localhost:9090 \
        --production-topic bulk.detections --shadow-topic detections.shadow \
        --duration 3600 --output comparison-report.md
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_DEGRADATION_THRESHOLD = 0.15


@dataclass(frozen=True)
class ComparisonValue:
    label: str
    production: float | None
    shadow: float | None
    degradation_ratio: float | None
    status: str
    notes: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", required=True, help="Prometheus base URL.")
    parser.add_argument(
        "--production-topic",
        default="bulk.detections",
        help="Production topic label for the report.",
    )
    parser.add_argument(
        "--shadow-topic",
        default="detections.shadow",
        help="Shadow topic label for the report.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=3600,
        help="Comparison window in seconds.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Markdown report output path.",
    )
    parser.add_argument(
        "--degradation-threshold",
        type=float,
        default=DEFAULT_DEGRADATION_THRESHOLD,
        help="Maximum tolerated degradation ratio before FAIL.",
    )
    parser.add_argument(
        "--pushgateway",
        help="Optional Pushgateway base URL for summary metrics.",
    )
    parser.add_argument(
        "--push-job",
        default="shadow-comparison",
        help="Pushgateway job name.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    now = int(time.time())
    start = now - max(args.duration, 60)
    step = max(min(args.duration // 60, 60), 15)
    prometheus_url = args.prometheus.rstrip("/")

    comparisons = [
        _detection_rate_comparison(prometheus_url, start, now, step, args),
        _latency_comparison(prometheus_url, args),
        _publish_error_comparison(prometheus_url, start, now, step, args),
        _confidence_distribution_comparison(prometheus_url, args),
    ]

    report_path = args.output
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _build_report(comparisons, args),
        encoding="utf-8",
    )

    if args.pushgateway:
        push_summary_metrics(args.pushgateway.rstrip("/"), args.push_job, comparisons)

    print(f"Comparison report written to {report_path}")


def _detection_rate_comparison(
    prometheus_url: str,
    start: int,
    end: int,
    step: int,
    args: argparse.Namespace,
) -> ComparisonValue:
    prod_series = _query_range(
        prometheus_url,
        "sum(rate(inference_detections_total[5m]))",
        start,
        end,
        step,
    )
    shadow_series = _query_range(
        prometheus_url,
        "sum(rate(shadow_detections_total[5m]))",
        start,
        end,
        step,
    )
    prod_value = _series_mean(prod_series)
    shadow_value = _series_mean(shadow_series)
    degradation = _relative_difference(prod_value, shadow_value)
    status = _status_for_ratio(degradation, args.degradation_threshold)
    return ComparisonValue(
        label="Detection rate agreement",
        production=prod_value,
        shadow=shadow_value,
        degradation_ratio=degradation,
        status=status,
        notes=(
            "Average rate over the requested window. The topic labels are "
            f"`{args.production_topic}` vs `{args.shadow_topic}` in the report."
        ),
    )


def _latency_comparison(
    prometheus_url: str,
    args: argparse.Namespace,
) -> ComparisonValue:
    production = _query_scalar(
        prometheus_url,
        "histogram_quantile(0.95, sum by (le) (rate(inference_latency_ms_bucket[5m])))",
    )
    shadow = _query_scalar(
        prometheus_url,
        "histogram_quantile(0.95, sum by (le) (rate(shadow_inference_latency_ms_bucket[5m])))",
    )
    degradation = _increase_ratio(production, shadow)
    status = _status_for_ratio(degradation, args.degradation_threshold)
    return ComparisonValue(
        label="Detection latency p95",
        production=production,
        shadow=shadow,
        degradation_ratio=degradation,
        status=status,
        notes="Prometheus histogram_quantile at p95.",
    )


def _publish_error_comparison(
    prometheus_url: str,
    start: int,
    end: int,
    step: int,
    args: argparse.Namespace,
) -> ComparisonValue:
    shadow_series = _query_range(
        prometheus_url,
        "sum(rate(shadow_publish_errors_total[5m]))",
        start,
        end,
        step,
    )
    shadow_value = _series_mean(shadow_series)
    degradation = shadow_value if shadow_value is not None else None
    status = "PASS" if shadow_value == 0 else "FAIL"
    if shadow_value is None:
        status = "WARN"
    return ComparisonValue(
        label="Shadow publish error rate",
        production=0.0,
        shadow=shadow_value,
        degradation_ratio=degradation,
        status=status,
        notes=(
            "Shadow-only metric. PASS requires zero publish errors; WARN means the "
            "metric is not being scraped yet."
        ),
    )


def _confidence_distribution_comparison(
    prometheus_url: str,
    args: argparse.Namespace,
) -> ComparisonValue:
    production = _query_scalar(
        prometheus_url,
        "histogram_quantile(0.50, sum by (le) (rate(inference_detection_confidence_bucket[5m])))",
    )
    shadow = _query_scalar(
        prometheus_url,
        "histogram_quantile(0.50, sum by (le) (rate(shadow_detection_confidence_bucket[5m])))",
    )
    degradation = _relative_difference(production, shadow)
    if production is None or shadow is None:
        return ComparisonValue(
            label="Confidence distribution median",
            production=production,
            shadow=shadow,
            degradation_ratio=None,
            status="WARN",
            notes=(
                "Requires both `inference_detection_confidence` and "
                "`shadow_detection_confidence`. Production currently has no "
                "canonical confidence histogram in this repo."
            ),
        )
    return ComparisonValue(
        label="Confidence distribution median",
        production=production,
        shadow=shadow,
        degradation_ratio=degradation,
        status=_status_for_ratio(degradation, args.degradation_threshold),
        notes="Median confidence estimated from Prometheus histograms.",
    )


def push_summary_metrics(
    pushgateway_url: str,
    job_name: str,
    comparisons: list[ComparisonValue],
) -> None:
    gate_pass = 1.0 if all(value.status == "PASS" for value in comparisons) else 0.0
    lines = [
        "# TYPE shadow_compare_gate_pass gauge",
        f"shadow_compare_gate_pass {gate_pass}",
    ]
    for value in comparisons:
        if value.degradation_ratio is None:
            continue
        metric_name = (
            value.label.lower()
            .replace(" ", "_")
            .replace("-", "_")
            .replace("/", "_")
        )
        lines.append(
            f"shadow_compare_degradation_ratio{{metric=\"{metric_name}\"}} "
            f"{value.degradation_ratio}",
        )
    body = "\n".join(lines) + "\n"
    request = Request(
        f"{pushgateway_url}/metrics/job/{job_name}",
        data=body.encode("utf-8"),
        headers={"Content-Type": "text/plain; version=0.0.4"},
        method="PUT",
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310
        response.read()


def _build_report(
    comparisons: list[ComparisonValue],
    args: argparse.Namespace,
) -> str:
    overall = "PASS" if all(value.status == "PASS" for value in comparisons) else "FAIL"
    rows = [
        "| Metric | Production | Shadow | Degradation | Status | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for value in comparisons:
        rows.append(
            "| "
            + " | ".join(
                [
                    value.label,
                    _format_float(value.production),
                    _format_float(value.shadow),
                    _format_ratio(value.degradation_ratio),
                    value.status,
                    value.notes,
                ]
            )
            + " |",
        )

    return "\n".join(
        [
            "# Shadow Comparison Report",
            "",
            "## Configuration",
            f"- Prometheus: `{args.prometheus}`",
            f"- Production topic: `{args.production_topic}`",
            f"- Shadow topic: `{args.shadow_topic}`",
            f"- Window: {args.duration} s",
            f"- Degradation threshold: {args.degradation_threshold:.0%}",
            "",
            "## Gate",
            f"- Overall result: **{overall}**",
            "- Gate rule: any measurable degradation above the threshold is `FAIL`.",
            "",
            "## Metrics",
            *rows,
            "",
            "## Notes",
            "- Shadow inference must remain isolated from production topics and consumer groups.",
            "- Confidence-distribution comparison is only meaningful once both production and shadow histograms are scraped.",
        ]
    ) + "\n"


def _query_scalar(prometheus_url: str, query: str) -> float | None:
    payload = _query_json(f"{prometheus_url}/api/v1/query", {"query": query})
    results = payload.get("data", {}).get("result", [])
    if not results:
        return None
    value = results[0].get("value")
    if not isinstance(value, list) or len(value) != 2:
        return None
    return _safe_float(value[1])


def _query_range(
    prometheus_url: str,
    query: str,
    start: int,
    end: int,
    step: int,
) -> list[float]:
    payload = _query_json(
        f"{prometheus_url}/api/v1/query_range",
        {
            "query": query,
            "start": str(start),
            "end": str(end),
            "step": str(step),
        },
    )
    results = payload.get("data", {}).get("result", [])
    if not results:
        return []

    values = results[0].get("values", [])
    series: list[float] = []
    if not isinstance(values, list):
        return series
    for item in values:
        if not isinstance(item, list) or len(item) != 2:
            continue
        parsed = _safe_float(item[1])
        if parsed is not None:
            series.append(parsed)
    return series


def _query_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", method="GET")
    with urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return payload


def _series_mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _relative_difference(
    production: float | None,
    shadow: float | None,
) -> float | None:
    if production is None or shadow is None:
        return None
    if production == 0:
        return 0.0 if shadow == 0 else math.inf
    return abs(shadow - production) / production


def _increase_ratio(
    production: float | None,
    shadow: float | None,
) -> float | None:
    if production is None or shadow is None:
        return None
    if production == 0:
        return 0.0 if shadow == 0 else math.inf
    return max(shadow - production, 0.0) / production


def _status_for_ratio(
    degradation: float | None,
    threshold: float,
) -> str:
    if degradation is None:
        return "WARN"
    return "PASS" if degradation <= threshold else "FAIL"


def _format_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


def _format_ratio(value: float | None) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "inf"
    return f"{value:.1%}"


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
