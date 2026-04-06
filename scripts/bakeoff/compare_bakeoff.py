#!/usr/bin/env python3
"""Compare detector bake-off MLflow runs and emit Markdown plus charts."""

from __future__ import annotations

import argparse
import html
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SAFE_DEFAULT_DETECTOR = "yolov8l"
PILOT_AGGREGATE_FPS_TARGET = 40.0
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
class RunSummary:
    run_id: str
    candidate: str
    score: float
    map_50: float
    map_50_95: float
    small_object_ap: float
    night_ap: float
    throughput_fps: float
    latency_p95_ms: float
    best_batch_size: float
    per_class_ap: dict[str, float] = field(default_factory=dict)
    throughput_estimate: str | None = None
    latency_estimate: str | None = None
    notes: tuple[str, ...] = ()


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(f"missing optional dependency '{module_name}'; install {install_hint}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmarks-json",
        type=Path,
        help="Offline JSON bundle with published benchmark inputs; skips the MLflow query path when provided.",
    )
    parser.add_argument(
        "--tracking-uri",
        default="http://127.0.0.1:5000",
        help="MLflow tracking URI.",
    )
    parser.add_argument(
        "--mlflow-experiment",
        default="detector-bakeoff",
        help="MLflow experiment name to query.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/bakeoff-results/detector"),
        help="Directory for Markdown and chart artifacts.",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Explicit Markdown output path. Defaults to <output-dir>/comparison.md.",
    )
    parser.add_argument(
        "--safe-default",
        default=SAFE_DEFAULT_DETECTOR,
        help="Safest default candidate when no clear winner emerges.",
    )
    parser.add_argument(
        "--clear-winner-margin",
        type=float,
        default=0.02,
        help="Minimum absolute score lead required to declare a clear winner.",
    )
    return parser.parse_args()


def metric(run: Any, key: str) -> float:
    return float(run.data.metrics.get(key, 0.0))


def compute_score(
    map_50_95: float,
    throughput_fps: float,
    small_object_ap: float,
    night_ap: float,
) -> float:
    throughput_term = min(throughput_fps / PILOT_AGGREGATE_FPS_TARGET, 1.0)
    return (
        0.35 * map_50_95
        + 0.25 * throughput_term
        + 0.20 * small_object_ap
        + 0.20 * night_ap
    )


def fetch_best_runs(args: argparse.Namespace) -> list[RunSummary]:
    mlflow = require_module("mlflow", "mlflow")
    client = mlflow.tracking.MlflowClient(tracking_uri=args.tracking_uri)
    experiment = client.get_experiment_by_name(args.mlflow_experiment)
    if experiment is None:
        raise RuntimeError(f"MLflow experiment not found: {args.mlflow_experiment}")

    runs = client.search_runs(
        experiment_ids=[experiment.experiment_id],
        filter_string="tags.`bakeoff.phase` = 'detector'",
        order_by=["metrics.detector_score DESC"],
        max_results=200,
    )
    if not runs:
        raise RuntimeError("no detector bake-off runs were found in MLflow")

    chosen: dict[str, Any] = {}
    for run in runs:
        candidate = run.data.tags.get("bakeoff.candidate") or run.data.params.get("candidate_name")
        if not candidate:
            continue
        if candidate not in chosen:
            chosen[candidate] = run

    summaries: list[RunSummary] = []
    for candidate, run in sorted(chosen.items(), key=lambda item: metric(item[1], "detector_score"), reverse=True):
        per_class = {
            class_name: metric(run, f"detector_ap50_95_{class_name}")
            for class_name in OBJECT_CLASSES
        }
        summaries.append(
            RunSummary(
                run_id=run.info.run_id,
                candidate=candidate,
                score=metric(run, "detector_score"),
                map_50=metric(run, "detector_map_50"),
                map_50_95=metric(run, "detector_map_50_95"),
                small_object_ap=metric(run, "detector_small_object_ap"),
                night_ap=metric(run, "detector_night_ap"),
                throughput_fps=metric(run, "detector_throughput_fps"),
                latency_p95_ms=metric(run, "detector_latency_p95_ms"),
                best_batch_size=metric(run, "detector_best_batch_size"),
                per_class_ap=per_class,
            )
        )
    return summaries


def load_runs_from_benchmarks(path: Path) -> list[RunSummary]:
    if not path.exists():
        raise FileNotFoundError(f"published benchmark JSON not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_runs = payload.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("published benchmark JSON must contain a non-empty runs array")

    summaries: list[RunSummary] = []
    for raw in raw_runs:
        candidate = str(raw["candidate"])
        map_50 = float(raw["map_50"])
        map_50_95 = float(raw["map_50_95"])
        small_object_ap = float(raw["small_object_ap"])
        night_ap = float(raw["night_ap"])
        throughput_fps = float(raw["throughput_fps"])
        latency_p95_ms = float(raw["latency_p95_ms"])
        best_batch_size = float(raw.get("best_batch_size", 8))
        per_class_ap_raw = raw.get("per_class_ap") or {}
        per_class_ap = {
            str(class_name): float(value)
            for class_name, value in per_class_ap_raw.items()
            if value is not None
        }
        notes = tuple(str(item) for item in (raw.get("notes") or []))
        score = raw.get("score")
        summaries.append(
            RunSummary(
                run_id=str(raw.get("run_id", f"published:{candidate}")),
                candidate=candidate,
                score=compute_score(map_50_95, throughput_fps, small_object_ap, night_ap)
                if score is None
                else float(score),
                map_50=map_50,
                map_50_95=map_50_95,
                small_object_ap=small_object_ap,
                night_ap=night_ap,
                throughput_fps=throughput_fps,
                latency_p95_ms=latency_p95_ms,
                best_batch_size=best_batch_size,
                per_class_ap=per_class_ap,
                throughput_estimate=str(raw["throughput_estimate"]) if raw.get("throughput_estimate") else None,
                latency_estimate=str(raw["latency_estimate"]) if raw.get("latency_estimate") else None,
                notes=notes,
            )
        )
    return summaries


def choose_recommendation(
    runs: list[RunSummary],
    safe_default: str,
    clear_winner_margin: float,
) -> tuple[RunSummary, str]:
    ordered = sorted(runs, key=lambda item: item.score, reverse=True)
    if len(ordered) == 1:
        return ordered[0], f"Clear winner: {ordered[0].candidate} (only finished run available)."

    leader = ordered[0]
    runner_up = ordered[1]
    if leader.score - runner_up.score >= clear_winner_margin:
        return leader, f"Clear winner: {leader.candidate} (score lead {leader.score - runner_up.score:.4f})."

    safe_candidate = next((run for run in ordered if run.candidate == safe_default), None)
    if safe_candidate is not None:
        return (
            safe_candidate,
            "No clear winner after time box; choose safest default: "
            f"{safe_candidate.candidate} (lead margin {leader.score - runner_up.score:.4f} < {clear_winner_margin:.4f}).",
        )

    return (
        leader,
        "No clear winner after time box and safest default run is unavailable; "
        f"fall back to top score: {leader.candidate}.",
    )


def write_markdown_report(
    output_path: Path,
    runs: list[RunSummary],
    recommendation: str,
    chart_paths: dict[str, Path],
) -> None:
    include_throughput_estimate = any(run.throughput_estimate for run in runs)
    include_latency_estimate = any(run.latency_estimate for run in runs)
    lines = [
        "# Detector Bake-Off Comparison",
        "",
        recommendation,
        "",
    ]
    header = [
        "Rank",
        "Candidate",
        "Score",
        "mAP@0.5",
        "mAP@0.5:0.95",
        "Small AP",
        "Night AP",
        "Throughput FPS",
    ]
    separator = ["------", "-----------", "-------", "---------", "--------------", "----------", "----------", "----------------"]
    if include_throughput_estimate:
        header.append("Throughput Estimate")
        separator.append("--------------------")
    header.extend(["p95 Latency ms"])
    separator.extend(["----------------"])
    if include_latency_estimate:
        header.append("Latency Estimate")
        separator.append("----------------")
    header.extend(["Best Batch", "Run ID"])
    separator.extend(["------------", "--------"])
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(separator) + " |")
    ordered = sorted(runs, key=lambda item: item.score, reverse=True)
    for index, run in enumerate(ordered, start=1):
        row = [
            str(index),
            run.candidate,
            f"{run.score:.4f}",
            f"{run.map_50:.4f}",
            f"{run.map_50_95:.4f}",
            f"{run.small_object_ap:.4f}",
            f"{run.night_ap:.4f}",
            f"{run.throughput_fps:.2f}",
        ]
        if include_throughput_estimate:
            row.append(run.throughput_estimate or "")
        row.append(f"{run.latency_p95_ms:.2f}")
        if include_latency_estimate:
            row.append(run.latency_estimate or "")
        row.extend([f"{run.best_batch_size:.0f}", f"`{run.run_id}`"])
        lines.append("| " + " | ".join(row) + " |")

    noted_runs = [run for run in ordered if run.notes]
    if noted_runs:
        lines.extend(
            [
                "",
                "## Candidate Notes",
                "",
            ]
        )
        for run in noted_runs:
            lines.append(f"### {run.candidate}")
            lines.extend(f"- {note}" for note in run.notes)
            lines.append("")
    lines.extend(
        [
            "",
            "## Charts",
            "",
            f"- Score ranking: `{chart_paths['score'].name}`",
            f"- Metric breakdown: `{chart_paths['breakdown'].name}`",
            "",
        ]
    )
    per_class_chart = chart_paths.get("per_class")
    if per_class_chart is not None:
        lines.insert(len(lines) - 1, f"- Per-class AP heatmap: `{per_class_chart.name}`")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_svg_bar_chart(
    output_path: Path,
    title: str,
    labels: list[str],
    values: list[float],
    value_label: str,
    fill: str,
    y_max: float,
) -> None:
    width = 800
    height = 420
    left = 70
    right = 30
    top = 55
    bottom = 75
    plot_width = width - left - right
    plot_height = height - top - bottom
    slot_width = plot_width / max(len(labels), 1)
    bar_width = slot_width * 0.6
    tick_values = [0.0, y_max / 2.0, y_max]

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2:.1f}" y="28" text-anchor="middle" font-family="monospace" font-size="20">{html.escape(title)}</text>',
        f'<text x="18" y="{top + plot_height / 2:.1f}" transform="rotate(-90 18 {top + plot_height / 2:.1f})" '
        f'font-family="monospace" font-size="12">{html.escape(value_label)}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#333333" stroke-width="1"/>',
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#333333" stroke-width="1"/>',
    ]
    for tick in tick_values:
        y = top + plot_height - (tick / y_max * plot_height if y_max else 0.0)
        svg_lines.append(
            f'<line x1="{left - 4}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#dddddd" stroke-width="1"/>'
        )
        svg_lines.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-family="monospace" font-size="11">{tick:.2f}</text>'
        )
    for index, (label, value) in enumerate(zip(labels, values, strict=True)):
        bar_height = 0.0 if y_max == 0.0 else value / y_max * plot_height
        x = left + index * slot_width + (slot_width - bar_width) / 2.0
        y = top + plot_height - bar_height
        svg_lines.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{fill}"/>'
        )
        svg_lines.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{max(top + 14, y - 6):.1f}" text-anchor="middle" font-family="monospace" font-size="11">{value:.3f}</text>'
        )
        svg_lines.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{height - 28}" text-anchor="middle" font-family="monospace" font-size="12">{html.escape(label)}</text>'
        )
    svg_lines.append("</svg>")
    output_path.write_text("\n".join(svg_lines) + "\n", encoding="utf-8")


def render_svg_grouped_bar_chart(
    output_path: Path,
    title: str,
    labels: list[str],
    series: list[tuple[str, list[float], str]],
    y_max: float,
) -> None:
    width = 920
    height = 440
    left = 70
    right = 40
    top = 70
    bottom = 80
    plot_width = width - left - right
    plot_height = height - top - bottom
    group_width = plot_width / max(len(labels), 1)
    bar_width = group_width * 0.7 / max(len(series), 1)
    tick_values = [0.0, 0.5 * y_max, y_max]

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2:.1f}" y="28" text-anchor="middle" font-family="monospace" font-size="20">{html.escape(title)}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#333333" stroke-width="1"/>',
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#333333" stroke-width="1"/>',
    ]
    for tick in tick_values:
        y = top + plot_height - (tick / y_max * plot_height if y_max else 0.0)
        svg_lines.append(
            f'<line x1="{left - 4}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#dddddd" stroke-width="1"/>'
        )
        svg_lines.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-family="monospace" font-size="11">{tick:.2f}</text>'
        )

    legend_x = left
    for legend_label, _, color in series:
        svg_lines.append(
            f'<rect x="{legend_x}" y="40" width="14" height="14" fill="{color}"/>'
        )
        svg_lines.append(
            f'<text x="{legend_x + 20}" y="52" font-family="monospace" font-size="12">{html.escape(legend_label)}</text>'
        )
        legend_x += 170

    for group_index, label in enumerate(labels):
        start_x = left + group_index * group_width + group_width * 0.15
        for series_index, (_, values, color) in enumerate(series):
            value = values[group_index]
            bar_height = 0.0 if y_max == 0.0 else value / y_max * plot_height
            x = start_x + series_index * bar_width
            y = top + plot_height - bar_height
            svg_lines.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}"/>'
            )
        svg_lines.append(
            f'<text x="{left + group_index * group_width + group_width / 2:.1f}" y="{height - 30}" text-anchor="middle" font-family="monospace" font-size="12">{html.escape(label)}</text>'
        )
    svg_lines.append("</svg>")
    output_path.write_text("\n".join(svg_lines) + "\n", encoding="utf-8")


def heatmap_color(value: float) -> str:
    clamped = max(0.0, min(1.0, value))
    red = int(34 + (253 - 34) * clamped)
    green = int(60 + (231 - 60) * clamped)
    blue = int(153 + (37 - 153) * clamped)
    return f"rgb({red},{green},{blue})"


def render_svg_heatmap(
    output_path: Path,
    title: str,
    row_labels: list[str],
    column_labels: list[str],
    matrix: list[list[float]],
) -> None:
    width = 980
    height = 420
    left = 130
    right = 30
    top = 70
    bottom = 80
    plot_width = width - left - right
    plot_height = height - top - bottom
    cell_width = plot_width / max(len(column_labels), 1)
    cell_height = plot_height / max(len(row_labels), 1)

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2:.1f}" y="28" text-anchor="middle" font-family="monospace" font-size="20">{html.escape(title)}</text>',
    ]
    for row_index, row_label in enumerate(row_labels):
        y = top + row_index * cell_height
        svg_lines.append(
            f'<text x="{left - 10}" y="{y + cell_height / 2 + 4:.1f}" text-anchor="end" font-family="monospace" font-size="12">{html.escape(row_label)}</text>'
        )
        for column_index, value in enumerate(matrix[row_index]):
            x = left + column_index * cell_width
            svg_lines.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_width:.1f}" height="{cell_height:.1f}" fill="{heatmap_color(value)}" stroke="#ffffff" stroke-width="1"/>'
            )
            svg_lines.append(
                f'<text x="{x + cell_width / 2:.1f}" y="{y + cell_height / 2 + 4:.1f}" text-anchor="middle" font-family="monospace" font-size="11">{value:.3f}</text>'
            )
    for column_index, column_label in enumerate(column_labels):
        x = left + column_index * cell_width + cell_width / 2.0
        svg_lines.append(
            f'<text x="{x:.1f}" y="{height - 32}" text-anchor="middle" font-family="monospace" font-size="12">{html.escape(column_label)}</text>'
        )
    svg_lines.append("</svg>")
    output_path.write_text("\n".join(svg_lines) + "\n", encoding="utf-8")


def build_charts(output_dir: Path, runs: list[RunSummary]) -> dict[str, Path]:
    ordered = sorted(runs, key=lambda item: item.score, reverse=True)
    candidates = [run.candidate for run in ordered]

    score_path = output_dir / "score_ranking.svg"
    render_svg_bar_chart(
        score_path,
        title="Detector bake-off score ranking",
        labels=candidates,
        values=[run.score for run in ordered],
        value_label="Composite score",
        fill="#1f77b4",
        y_max=max(1.0, max(run.score for run in ordered) * 1.15),
    )

    breakdown_path = output_dir / "metric_breakdown.svg"
    throughput_norm = [min(run.throughput_fps / 40.0, 1.0) for run in ordered]
    render_svg_grouped_bar_chart(
        breakdown_path,
        title="Detector metric breakdown",
        labels=candidates,
        series=[
            ("mAP@0.5:0.95", [run.map_50_95 for run in ordered], "#1f77b4"),
            ("Throughput term", throughput_norm, "#ff7f0e"),
            ("Small AP", [run.small_object_ap for run in ordered], "#2ca02c"),
            ("Night AP", [run.night_ap for run in ordered], "#d62728"),
        ],
        y_max=1.05,
    )

    chart_paths: dict[str, Path] = {
        "score": score_path,
        "breakdown": breakdown_path,
    }
    if all(all(class_name in run.per_class_ap for class_name in OBJECT_CLASSES) for run in ordered):
        per_class_path = output_dir / "per_class_ap_heatmap.svg"
        render_svg_heatmap(
            per_class_path,
            title="Per-class AP@0.5:0.95",
            row_labels=candidates,
            column_labels=list(OBJECT_CLASSES),
            matrix=[[run.per_class_ap[class_name] for class_name in OBJECT_CLASSES] for run in ordered],
        )
        chart_paths["per_class"] = per_class_path

    return chart_paths


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    runs = load_runs_from_benchmarks(args.benchmarks_json) if args.benchmarks_json else fetch_best_runs(args)
    recommended_run, recommendation = choose_recommendation(
        runs,
        safe_default=args.safe_default,
        clear_winner_margin=args.clear_winner_margin,
    )
    chart_paths = build_charts(output_dir, runs)
    markdown_path = args.markdown_output or output_dir / "comparison.md"
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    write_markdown_report(markdown_path, runs, recommendation, chart_paths)

    print(recommendation)
    print(f"Recommended run ID: {recommended_run.run_id}")
    print(f"Markdown report: {markdown_path}")
    for name, path in chart_paths.items():
        print(f"{name} chart: {path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc
