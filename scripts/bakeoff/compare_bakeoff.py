#!/usr/bin/env python3
"""Compare detector bake-off MLflow runs and emit Markdown plus charts."""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SAFE_DEFAULT_DETECTOR = "yolov8l"
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
    per_class_ap: dict[str, float]


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(f"missing optional dependency '{module_name}'; install {install_hint}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
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
    lines = [
        "# Detector Bake-Off Comparison",
        "",
        recommendation,
        "",
        "| Rank | Candidate | Score | mAP@0.5 | mAP@0.5:0.95 | Small AP | Night AP | Throughput FPS | p95 Latency ms | Best Batch | Run ID |",
        "|------|-----------|-------|---------|--------------|----------|----------|----------------|----------------|------------|--------|",
    ]
    ordered = sorted(runs, key=lambda item: item.score, reverse=True)
    for index, run in enumerate(ordered, start=1):
        lines.append(
            f"| {index} | {run.candidate} | {run.score:.4f} | {run.map_50:.4f} | {run.map_50_95:.4f} | "
            f"{run.small_object_ap:.4f} | {run.night_ap:.4f} | {run.throughput_fps:.2f} | "
            f"{run.latency_p95_ms:.2f} | {run.best_batch_size:.0f} | `{run.run_id}` |"
        )
    lines.extend(
        [
            "",
            "## Charts",
            "",
            f"- Score ranking: `{chart_paths['score'].name}`",
            f"- Metric breakdown: `{chart_paths['breakdown'].name}`",
            f"- Per-class AP heatmap: `{chart_paths['per_class'].name}`",
            "",
        ]
    )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_charts(output_dir: Path, runs: list[RunSummary]) -> dict[str, Path]:
    matplotlib = require_module("matplotlib", "matplotlib")
    matplotlib.use("Agg")
    pyplot = require_module("matplotlib.pyplot", "matplotlib")
    numpy = require_module("numpy", "numpy")

    ordered = sorted(runs, key=lambda item: item.score, reverse=True)
    candidates = [run.candidate for run in ordered]

    score_path = output_dir / "score_ranking.png"
    figure, axis = pyplot.subplots(figsize=(8, 4.5))
    axis.bar(candidates, [run.score for run in ordered], color="#1f77b4")
    axis.set_ylabel("Composite score")
    axis.set_title("Detector bake-off score ranking")
    axis.set_ylim(0.0, max(1.0, max(run.score for run in ordered) * 1.15))
    figure.tight_layout()
    figure.savefig(score_path, dpi=160)
    pyplot.close(figure)

    breakdown_path = output_dir / "metric_breakdown.png"
    figure, axis = pyplot.subplots(figsize=(9, 5))
    x = numpy.arange(len(candidates))
    width = 0.2
    throughput_norm = [min(run.throughput_fps / 40.0, 1.0) for run in ordered]
    axis.bar(x - 1.5 * width, [run.map_50_95 for run in ordered], width, label="mAP@0.5:0.95")
    axis.bar(x - 0.5 * width, throughput_norm, width, label="Throughput term")
    axis.bar(x + 0.5 * width, [run.small_object_ap for run in ordered], width, label="Small AP")
    axis.bar(x + 1.5 * width, [run.night_ap for run in ordered], width, label="Night AP")
    axis.set_xticks(x)
    axis.set_xticklabels(candidates)
    axis.set_ylim(0.0, 1.05)
    axis.set_title("Detector metric breakdown")
    axis.legend()
    figure.tight_layout()
    figure.savefig(breakdown_path, dpi=160)
    pyplot.close(figure)

    per_class_path = output_dir / "per_class_ap_heatmap.png"
    matrix = numpy.asarray(
        [[run.per_class_ap[class_name] for class_name in OBJECT_CLASSES] for run in ordered],
        dtype=numpy.float64,
    )
    figure, axis = pyplot.subplots(figsize=(10, 4.5))
    heatmap = axis.imshow(matrix, aspect="auto", cmap="viridis", vmin=0.0, vmax=1.0)
    axis.set_xticks(numpy.arange(len(OBJECT_CLASSES)))
    axis.set_xticklabels(OBJECT_CLASSES, rotation=30, ha="right")
    axis.set_yticks(numpy.arange(len(candidates)))
    axis.set_yticklabels(candidates)
    axis.set_title("Per-class AP@0.5:0.95")
    figure.colorbar(heatmap, ax=axis)
    figure.tight_layout()
    figure.savefig(per_class_path, dpi=160)
    pyplot.close(figure)

    return {
        "score": score_path,
        "breakdown": breakdown_path,
        "per_class": per_class_path,
    }


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    runs = fetch_best_runs(args)
    recommended_run, recommendation = choose_recommendation(
        runs,
        safe_default=args.safe_default,
        clear_winner_margin=args.clear_winner_margin,
    )
    chart_paths = build_charts(output_dir, runs)
    markdown_path = output_dir / "comparison.md"
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
