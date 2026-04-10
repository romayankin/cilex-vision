#!/usr/bin/env python3
"""Reusable regression detection for comparing two sets of metrics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SliceComparison:
    metric_name: str
    baseline_value: float
    candidate_value: float
    delta: float
    relative_delta: float
    regression: bool
    threshold: float


@dataclass(frozen=True)
class RegressionReport:
    comparisons: list[SliceComparison]
    regressions: list[SliceComparison]
    go: bool
    summary: str


def compare_metrics(
    baseline: dict[str, float],
    candidate: dict[str, float],
    threshold: float = 0.02,
    per_metric_thresholds: dict[str, float] | None = None,
) -> RegressionReport:
    """Compare two metric dictionaries and flag regressions.

    A regression is defined as an absolute drop greater than the applicable
    threshold for a metric where higher values are better.
    """

    missing_in_candidate = sorted(set(baseline) - set(candidate))
    missing_in_baseline = sorted(set(candidate) - set(baseline))
    if missing_in_candidate or missing_in_baseline:
        missing_parts: list[str] = []
        if missing_in_candidate:
            missing_parts.append(
                "missing in candidate: " + ", ".join(missing_in_candidate)
            )
        if missing_in_baseline:
            missing_parts.append(
                "missing in baseline: " + ", ".join(missing_in_baseline)
            )
        raise ValueError("metric sets do not match (" + "; ".join(missing_parts) + ")")

    thresholds = per_metric_thresholds or {}
    comparisons: list[SliceComparison] = []
    regressions: list[SliceComparison] = []
    for metric_name in sorted(baseline):
        baseline_value = float(baseline[metric_name])
        candidate_value = float(candidate[metric_name])
        metric_threshold = float(thresholds.get(metric_name, threshold))
        delta = candidate_value - baseline_value
        relative_delta = delta / baseline_value if baseline_value > 0 else 0.0
        comparison = SliceComparison(
            metric_name=metric_name,
            baseline_value=baseline_value,
            candidate_value=candidate_value,
            delta=delta,
            relative_delta=relative_delta,
            regression=delta < (-metric_threshold),
            threshold=metric_threshold,
        )
        comparisons.append(comparison)
        if comparison.regression:
            regressions.append(comparison)

    summary = build_summary(comparisons, regressions)
    return RegressionReport(
        comparisons=comparisons,
        regressions=regressions,
        go=not regressions,
        summary=summary,
    )


def build_summary(
    comparisons: list[SliceComparison],
    regressions: list[SliceComparison],
) -> str:
    if not comparisons:
        return "NO-GO: no comparable metrics were provided."
    if not regressions:
        return f"GO: no regressions detected across {len(comparisons)} compared slices."
    worst = min(regressions, key=lambda item: item.delta)
    return (
        "NO-GO: "
        f"{len(regressions)} regressions detected across {len(comparisons)} compared slices. "
        f"Worst slice: {worst.metric_name} "
        f"({worst.delta:+.4f}, threshold {worst.threshold:.4f})."
    )


def format_comparison_markdown(report: RegressionReport) -> str:
    """Render a comparison report as Markdown."""

    lines = [
        "## Regression Gate",
        "",
        report.summary,
        "",
        "| Metric | Baseline | Candidate | Delta | Relative Delta | Threshold | Status |",
        "|--------|----------|-----------|-------|----------------|-----------|--------|",
    ]
    for comparison in report.comparisons:
        relative_delta = (
            f"{comparison.relative_delta:+.2%}"
            if comparison.baseline_value > 0
            else "n/a"
        )
        status = "REGRESSION" if comparison.regression else "OK"
        lines.append(
            f"| {comparison.metric_name} | {comparison.baseline_value:.4f} | "
            f"{comparison.candidate_value:.4f} | {comparison.delta:+.4f} | "
            f"{relative_delta} | {comparison.threshold:.4f} | {status} |"
        )
    lines.append("")
    return "\n".join(lines)
