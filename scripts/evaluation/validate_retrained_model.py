#!/usr/bin/env python3
"""Automated comparison of retrained vs production model.

Loads both models' evaluation results from MLflow or exported JSON, compares
all required offline-qualification slices, writes local artifacts, logs a
validation run to MLflow, and exits non-zero on NO-GO.

Usage:
    python validate_retrained_model.py \
        --candidate-run-id <mlflow_run_id> \
        --baseline-run-id <mlflow_run_id> \
        --mlflow-uri http://localhost:5000
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from regression_checker import (
    RegressionReport,
    compare_metrics,
    format_comparison_markdown,
)

OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)

COMPARISON_SLICES: tuple[str, ...] = (
    "map50",
    "map50_95",
    "small_object_ap",
    "night_ap",
    "operational_precision",
    "operational_recall",
    "operational_f1",
    "ap_person",
    "ap_car",
    "ap_truck",
    "ap_bus",
    "ap_bicycle",
    "ap_motorcycle",
    "ap_animal",
)

LATENCY_SLICES: tuple[str, ...] = (
    "latency_p50_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "throughput_fps",
)

DEFAULT_REGRESSION_THRESHOLD = 0.02
DEFAULT_MLFLOW_URI = "http://localhost:5000"
DEFAULT_MLFLOW_EXPERIMENT = "retraining-validation"
DEFAULT_OUTPUT_DIR = Path("artifacts/evaluation/retraining-validation")

METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "map50": ("map50", "mAP@0.5", "val_mAP@0.5"),
    "map50_95": ("map50_95", "mAP@0.5:0.95", "val_mAP@0.5:0.95", "best_mAP@0.5:0.95"),
    "small_object_ap": ("small_object_ap", "small_object_AP"),
    "night_ap": ("night_ap", "night_AP"),
    "operational_precision": ("operational_precision",),
    "operational_recall": ("operational_recall",),
    "operational_f1": ("operational_f1",),
    "latency_p50_ms": ("latency_p50_ms",),
    "latency_p95_ms": ("latency_p95_ms",),
    "latency_p99_ms": ("latency_p99_ms",),
    "throughput_fps": ("throughput_fps",),
}
for object_class in OBJECT_CLASSES:
    METRIC_ALIASES[f"ap_{object_class}"] = (
        f"ap_{object_class}",
        f"AP_{object_class}",
        f"val_AP_{object_class}",
        f"per_class_ap_{object_class}",
    )


@dataclass(frozen=True)
class MetricSource:
    label: str
    source: str
    metrics: dict[str, float]
    metadata: dict[str, Any]


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
        "--candidate-run-id",
        default=os.environ.get("CANDIDATE_RUN_ID"),
        help="MLflow run ID for the retrained candidate. If omitted, models/latest_run_id.txt is used when available.",
    )
    parser.add_argument(
        "--baseline-run-id",
        default=os.environ.get("BASELINE_RUN_ID"),
        help="MLflow run ID for the production baseline.",
    )
    parser.add_argument(
        "--candidate-json",
        type=Path,
        help="Optional exported evaluation JSON for the candidate model.",
    )
    parser.add_argument(
        "--baseline-json",
        type=Path,
        help="Optional exported evaluation JSON for the baseline model.",
    )
    parser.add_argument(
        "--mlflow-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", DEFAULT_MLFLOW_URI),
        help="MLflow tracking URI.",
    )
    parser.add_argument(
        "--mlflow-experiment",
        default=DEFAULT_MLFLOW_EXPERIMENT,
        help="MLflow experiment name for validation runs.",
    )
    parser.add_argument(
        "--regression-threshold",
        type=float,
        default=DEFAULT_REGRESSION_THRESHOLD,
        help="Maximum allowed absolute regression per gated slice.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for local validation artifacts.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional explicit Markdown report path.",
    )
    return parser.parse_args()


def resolve_candidate_run_id(args: argparse.Namespace) -> str | None:
    if args.candidate_run_id:
        return str(args.candidate_run_id).strip() or None
    latest_run_id_path = Path(__file__).resolve().parents[2] / "models" / "latest_run_id.txt"
    if latest_run_id_path.exists():
        value = latest_run_id_path.read_text(encoding="utf-8").strip()
        return value or None
    return None


def ensure_inputs(args: argparse.Namespace) -> tuple[str | None, str | None]:
    candidate_run_id = resolve_candidate_run_id(args)
    baseline_run_id = (
        str(args.baseline_run_id).strip() if args.baseline_run_id is not None else None
    )
    if args.candidate_json is None and candidate_run_id is None:
        raise RuntimeError(
            "candidate input is required: pass --candidate-run-id, --candidate-json, "
            "or create models/latest_run_id.txt via the training pipeline"
        )
    if args.baseline_json is None and baseline_run_id is None:
        raise RuntimeError("baseline input is required: pass --baseline-run-id or --baseline-json")
    return candidate_run_id, baseline_run_id


def is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def normalize_metrics(raw_metrics: Mapping[str, Any]) -> dict[str, float]:
    normalized: dict[str, float] = {}

    per_class_ap = raw_metrics.get("per_class_ap")
    if isinstance(per_class_ap, Mapping):
        for object_class in OBJECT_CLASSES:
            value = per_class_ap.get(object_class)
            if is_number(value):
                normalized[f"ap_{object_class}"] = float(value)

    for metric_name, aliases in METRIC_ALIASES.items():
        for alias in aliases:
            value = raw_metrics.get(alias)
            if is_number(value):
                normalized[metric_name] = float(value)
                break

    return normalized


def extract_metrics_from_payload(payload: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    evaluation_block = payload.get("evaluation")
    if isinstance(evaluation_block, Mapping):
        metrics = normalize_metrics(evaluation_block)
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in {"evaluation", "regressions"}
        }
        return metrics, metadata
    metrics = normalize_metrics(payload)
    return metrics, {}


def load_metrics_from_json(path: Path, label: str) -> MetricSource:
    if not path.exists():
        raise RuntimeError(f"{label} JSON not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} JSON must be a top-level object")
    metrics, metadata = extract_metrics_from_payload(payload)
    return MetricSource(
        label=label,
        source=str(path),
        metrics=metrics,
        metadata=metadata,
    )


def load_metrics_from_mlflow(
    *,
    tracking_uri: str,
    run_id: str,
    label: str,
) -> MetricSource:
    mlflow = require_module("mlflow", "mlflow")
    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.tracking.MlflowClient()
    run = client.get_run(run_id)
    metrics = normalize_metrics(run.data.metrics)
    metadata: dict[str, Any] = {
        "run_id": run_id,
        "artifact_uri": run.info.artifact_uri,
        "status": run.info.status,
        "experiment_id": run.info.experiment_id,
    }
    if run.data.tags:
        metadata["tags"] = dict(run.data.tags)
    return MetricSource(
        label=label,
        source=run_id,
        metrics=metrics,
        metadata=metadata,
    )


def require_metrics(
    metric_source: MetricSource,
    required_metrics: tuple[str, ...],
) -> dict[str, float]:
    missing = [metric_name for metric_name in required_metrics if metric_name not in metric_source.metrics]
    if missing:
        raise RuntimeError(
            f"{metric_source.label} is missing required metrics: {', '.join(missing)}"
        )
    return {
        metric_name: metric_source.metrics[metric_name]
        for metric_name in required_metrics
    }


def detect_git_state() -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        )
        return revision, dirty
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - env dependent
        return None, None


def build_latency_rows(
    baseline: MetricSource,
    candidate: MetricSource,
) -> list[tuple[str, float, float, float]]:
    rows: list[tuple[str, float, float, float]] = []
    for metric_name in LATENCY_SLICES:
        baseline_value = baseline.metrics.get(metric_name)
        candidate_value = candidate.metrics.get(metric_name)
        if baseline_value is None or candidate_value is None:
            continue
        rows.append(
            (
                metric_name,
                baseline_value,
                candidate_value,
                candidate_value - baseline_value,
            )
        )
    return rows


def format_source_details(metric_source: MetricSource) -> list[str]:
    lines = [f"- {metric_source.label.title()} source: `{metric_source.source}`"]
    run_id = metric_source.metadata.get("run_id")
    if isinstance(run_id, str):
        lines.append(f"- {metric_source.label.title()} run ID: `{run_id}`")
    return lines


def build_validation_report(
    *,
    baseline: MetricSource,
    candidate: MetricSource,
    regression_report: RegressionReport,
    threshold: float,
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    condition_metrics = (
        "small_object_ap",
        "night_ap",
        "operational_precision",
        "operational_recall",
        "operational_f1",
    )
    latency_rows = build_latency_rows(baseline, candidate)

    lines = [
        "# Retrained Model Validation Report",
        "",
        f"**Generated at:** `{generated_at}`",
        f"**Decision:** `{'GO' if regression_report.go else 'NO-GO'}`",
        f"**Regression threshold:** `{threshold:.4f}`",
        "",
        "## Sources",
        "",
        *format_source_details(candidate),
        *format_source_details(baseline),
        "",
        format_comparison_markdown(regression_report).rstrip(),
        "",
        "## Per-Class AP Breakdown",
        "",
        "| Class | Baseline | Candidate | Delta | Status |",
        "|-------|----------|-----------|-------|--------|",
    ]

    comparison_map = {
        comparison.metric_name: comparison
        for comparison in regression_report.comparisons
    }
    for object_class in OBJECT_CLASSES:
        comparison = comparison_map[f"ap_{object_class}"]
        status = "REGRESSION" if comparison.regression else "OK"
        lines.append(
            f"| {object_class} | {comparison.baseline_value:.4f} | "
            f"{comparison.candidate_value:.4f} | {comparison.delta:+.4f} | {status} |"
        )

    lines.extend(
        [
            "",
            "## Condition and Operational Slices",
            "",
            "| Metric | Baseline | Candidate | Delta | Status |",
            "|--------|----------|-----------|-------|--------|",
        ]
    )
    for metric_name in condition_metrics:
        comparison = comparison_map[metric_name]
        status = "REGRESSION" if comparison.regression else "OK"
        lines.append(
            f"| {metric_name} | {comparison.baseline_value:.4f} | "
            f"{comparison.candidate_value:.4f} | {comparison.delta:+.4f} | {status} |"
        )

    if latency_rows:
        lines.extend(
            [
                "",
                "## Latency and Throughput",
                "",
                "| Metric | Baseline | Candidate | Delta | Note |",
                "|--------|----------|-----------|-------|------|",
            ]
        )
        for metric_name, baseline_value, candidate_value, delta in latency_rows:
            note = "lower is better" if metric_name.startswith("latency_") else "higher is better"
            lines.append(
                f"| {metric_name} | {baseline_value:.4f} | {candidate_value:.4f} | "
                f"{delta:+.4f} | {note} |"
            )
    else:
        lines.extend(
            [
                "",
                "## Latency and Throughput",
                "",
                "Latency and throughput metrics were not available in both sources, so they are not part of the gate.",
            ]
        )

    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            regression_report.summary,
            "",
            "- Offline qualification blocks shadow deployment when any gated slice regresses by more than the configured threshold.",
            "- Latency and throughput are reported when available but are not currently part of the absolute regression gate in this harness.",
        ]
    )
    return "\n".join(lines) + "\n"


def metric_source_to_json(metric_source: MetricSource) -> dict[str, Any]:
    return {
        "label": metric_source.label,
        "source": metric_source.source,
        "metrics": metric_source.metrics,
        "metadata": metric_source.metadata,
    }


def regression_report_to_json(report: RegressionReport) -> dict[str, Any]:
    return {
        "go": report.go,
        "summary": report.summary,
        "comparisons": [
            {
                "metric_name": comparison.metric_name,
                "baseline_value": comparison.baseline_value,
                "candidate_value": comparison.candidate_value,
                "delta": comparison.delta,
                "relative_delta": comparison.relative_delta,
                "regression": comparison.regression,
                "threshold": comparison.threshold,
            }
            for comparison in report.comparisons
        ],
        "regressions": [
            {
                "metric_name": comparison.metric_name,
                "baseline_value": comparison.baseline_value,
                "candidate_value": comparison.candidate_value,
                "delta": comparison.delta,
                "relative_delta": comparison.relative_delta,
                "regression": comparison.regression,
                "threshold": comparison.threshold,
            }
            for comparison in report.regressions
        ],
    }


def write_local_artifacts(
    *,
    output_dir: Path,
    report_path: Path,
    baseline: MetricSource,
    candidate: MetricSource,
    regression_report: RegressionReport,
    regression_threshold: float,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_metrics_path = output_dir / "candidate_metrics.json"
    baseline_metrics_path = output_dir / "baseline_metrics.json"
    comparison_path = output_dir / "regression_report.json"
    summary_path = output_dir / "validation_summary.json"

    candidate_metrics_path.write_text(
        json.dumps(metric_source_to_json(candidate), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    baseline_metrics_path.write_text(
        json.dumps(metric_source_to_json(baseline), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    comparison_path.write_text(
        json.dumps(regression_report_to_json(regression_report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        build_validation_report(
            baseline=baseline,
            candidate=candidate,
            regression_report=regression_report,
            threshold=regression_threshold,
        ),
        encoding="utf-8",
    )
    summary_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision": "GO" if regression_report.go else "NO-GO",
        "regression_threshold": regression_threshold,
        "candidate_source": candidate.source,
        "baseline_source": baseline.source,
        "regression_count": len(regression_report.regressions),
        "report_path": str(report_path),
    }
    summary_path.write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "candidate_metrics": candidate_metrics_path,
        "baseline_metrics": baseline_metrics_path,
        "comparison": comparison_path,
        "report": report_path,
        "summary": summary_path,
    }


def sanitize_metric_name(metric_name: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in metric_name)


def log_validation_to_mlflow(
    *,
    args: argparse.Namespace,
    baseline: MetricSource,
    candidate: MetricSource,
    regression_report: RegressionReport,
    artifact_paths: dict[str, Path],
) -> str:
    mlflow = require_module("mlflow", "mlflow")
    mlflow.set_tracking_uri(args.mlflow_uri)
    mlflow.set_experiment(args.mlflow_experiment)

    git_revision, git_dirty = detect_git_state()
    with mlflow.start_run(run_name="retraining-validation") as run:
        mlflow.set_tags(
            {
                "eval.phase": "retraining-validation",
                "eval_gate": "passed" if regression_report.go else "failed",
                "regression_detected": "false" if regression_report.go else "true",
            }
        )
        params = {
            "candidate_source": candidate.source,
            "baseline_source": baseline.source,
            "regression_threshold": args.regression_threshold,
            "comparison_slice_count": len(COMPARISON_SLICES),
        }
        candidate_run_id = candidate.metadata.get("run_id")
        baseline_run_id = baseline.metadata.get("run_id")
        if isinstance(candidate_run_id, str):
            params["candidate_run_id"] = candidate_run_id
        if isinstance(baseline_run_id, str):
            params["baseline_run_id"] = baseline_run_id
        if git_revision is not None:
            params["git_revision"] = git_revision
        if git_dirty is not None:
            params["git_dirty"] = str(git_dirty).lower()
        mlflow.log_params(params)
        mlflow.log_metric("gate_passed", 1.0 if regression_report.go else 0.0)
        mlflow.log_metric("regression_count", float(len(regression_report.regressions)))
        for comparison in regression_report.comparisons:
            suffix = sanitize_metric_name(comparison.metric_name)
            mlflow.log_metric(f"baseline_{suffix}", comparison.baseline_value)
            mlflow.log_metric(f"candidate_{suffix}", comparison.candidate_value)
            mlflow.log_metric(f"delta_{suffix}", comparison.delta)
            mlflow.log_metric(f"relative_delta_{suffix}", comparison.relative_delta)
        for path in artifact_paths.values():
            mlflow.log_artifact(str(path))
        return run.info.run_id


def load_sources(
    args: argparse.Namespace,
    *,
    candidate_run_id: str | None,
    baseline_run_id: str | None,
) -> tuple[MetricSource, MetricSource]:
    if args.candidate_json is not None:
        candidate = load_metrics_from_json(args.candidate_json, "candidate")
    else:
        assert candidate_run_id is not None
        candidate = load_metrics_from_mlflow(
            tracking_uri=args.mlflow_uri,
            run_id=candidate_run_id,
            label="candidate",
        )

    if args.baseline_json is not None:
        baseline = load_metrics_from_json(args.baseline_json, "baseline")
    else:
        assert baseline_run_id is not None
        baseline = load_metrics_from_mlflow(
            tracking_uri=args.mlflow_uri,
            run_id=baseline_run_id,
            label="baseline",
        )
    return candidate, baseline


def main() -> None:
    args = parse_args()
    candidate_run_id, baseline_run_id = ensure_inputs(args)
    candidate, baseline = load_sources(
        args,
        candidate_run_id=candidate_run_id,
        baseline_run_id=baseline_run_id,
    )

    candidate_gated = require_metrics(candidate, COMPARISON_SLICES)
    baseline_gated = require_metrics(baseline, COMPARISON_SLICES)
    regression_report = compare_metrics(
        baseline=baseline_gated,
        candidate=candidate_gated,
        threshold=args.regression_threshold,
    )

    report_path = args.report_path or (args.output_dir / "validation-report.md")
    artifact_paths = write_local_artifacts(
        output_dir=args.output_dir,
        report_path=report_path,
        baseline=baseline,
        candidate=candidate,
        regression_report=regression_report,
        regression_threshold=args.regression_threshold,
    )
    mlflow_run_id = log_validation_to_mlflow(
        args=args,
        baseline=baseline,
        candidate=candidate,
        regression_report=regression_report,
        artifact_paths=artifact_paths,
    )

    summary = {
        "decision": "GO" if regression_report.go else "NO-GO",
        "regression_count": len(regression_report.regressions),
        "candidate_source": candidate.source,
        "baseline_source": baseline.source,
        "report_path": str(report_path),
        "mlflow_run_id": mlflow_run_id,
    }
    print(json.dumps(summary))

    if not regression_report.go:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
