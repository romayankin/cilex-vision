#!/usr/bin/env python3
"""Evaluate a trained model against the full eval dataset and production baseline.

Computes all bakeoff metrics (mAP@0.5, mAP@0.5:0.95, per-class AP,
small_object_AP, night_AP, operational slice at 0.40) and compares
against the production baseline from MLflow.

Exit codes:
    0 — all checks passed, no regression > threshold on any slice
    1 — regression detected on one or more evaluation slices

Output:
    - Evaluation results logged to MLflow
    - Markdown comparison report written to --report-path
    - JSON metrics written to --output

Usage:
    python evaluate.py --checkpoint models/best.pt --baseline-run-id abc123
    python evaluate.py --mlflow-uri http://localhost:5000 --run-id def456
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)

DEFAULT_REGRESSION_THRESHOLD = 0.02
DEFAULT_OPERATIONAL_THRESHOLD = 0.40


# ---------------------------------------------------------------------------
# Metric data structures
# ---------------------------------------------------------------------------


@dataclass
class SliceMetrics:
    """Metrics for a single evaluation slice."""
    name: str
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    ap: float = 0.0
    support: int = 0


@dataclass
class EvalResult:
    """Full evaluation result with all bakeoff metrics."""
    map50: float = 0.0
    map50_95: float = 0.0
    per_class_ap: dict[str, float] = field(default_factory=dict)
    small_object_ap: float = 0.0
    night_ap: float = 0.0
    operational_precision: float = 0.0
    operational_recall: float = 0.0
    operational_f1: float = 0.0
    operational_threshold: float = DEFAULT_OPERATIONAL_THRESHOLD
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    throughput_fps: float | None = None

    def all_slices(self) -> dict[str, float]:
        """Return all metrics as a flat dict for regression comparison."""
        slices: dict[str, float] = {
            "mAP@0.5": self.map50,
            "mAP@0.5:0.95": self.map50_95,
            "small_object_AP": self.small_object_ap,
            "night_AP": self.night_ap,
            "operational_precision": self.operational_precision,
            "operational_recall": self.operational_recall,
            "operational_f1": self.operational_f1,
        }
        for cls, ap in self.per_class_ap.items():
            slices[f"AP_{cls}"] = ap
        return slices

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mAP@0.5": self.map50,
            "mAP@0.5:0.95": self.map50_95,
            "per_class_ap": self.per_class_ap,
            "small_object_AP": self.small_object_ap,
            "night_AP": self.night_ap,
            "operational_precision": self.operational_precision,
            "operational_recall": self.operational_recall,
            "operational_f1": self.operational_f1,
            "operational_threshold": self.operational_threshold,
        }
        if self.latency_p50_ms is not None:
            result["latency_p50_ms"] = self.latency_p50_ms
            result["latency_p95_ms"] = self.latency_p95_ms
            result["latency_p99_ms"] = self.latency_p99_ms
        if self.throughput_fps is not None:
            result["throughput_fps"] = self.throughput_fps
        return result


# ---------------------------------------------------------------------------
# Evaluation (interface — real implementation runs model inference)
# ---------------------------------------------------------------------------


def run_evaluation(
    checkpoint_path: str,
    test_manifest: str,
    operational_threshold: float,
) -> EvalResult:
    """Run model inference on the test set and compute all metrics.

    In production, this would:
    1. Load the model checkpoint
    2. Run inference on all test images
    3. Compute COCO-style mAP using pycocotools
    4. Compute per-class AP, small object AP, night AP
    5. Compute precision/recall/F1 at the operational threshold

    The skeleton returns the interface shape for downstream integration.
    """
    log.info("evaluating checkpoint: %s", checkpoint_path)
    log.info("test manifest: %s", test_manifest)

    # Placeholder — replace with actual evaluation
    result = EvalResult(
        map50=0.0,
        map50_95=0.0,
        per_class_ap={cls: 0.0 for cls in OBJECT_CLASSES},
        small_object_ap=0.0,
        night_ap=0.0,
        operational_precision=0.0,
        operational_recall=0.0,
        operational_f1=0.0,
        operational_threshold=operational_threshold,
    )

    log.info("evaluation complete: mAP@0.5=%.4f mAP@0.5:0.95=%.4f",
             result.map50, result.map50_95)
    return result


def load_baseline_from_mlflow(
    tracking_uri: str,
    run_id: str,
) -> dict[str, float]:
    """Fetch baseline metrics from an MLflow run.

    Returns a flat dict of metric_name → value matching the slice names
    from EvalResult.all_slices().
    """
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.tracking.MlflowClient()
    run = client.get_run(run_id)

    metrics = run.data.metrics
    baseline: dict[str, float] = {}

    # Map MLflow metric names to our slice names
    metric_mapping = {
        "mAP@0.5": "mAP@0.5",
        "mAP@0.5:0.95": "mAP@0.5:0.95",
        "val_mAP@0.5": "mAP@0.5",
        "val_mAP@0.5:0.95": "mAP@0.5:0.95",
        "best_mAP@0.5:0.95": "mAP@0.5:0.95",
        "small_object_AP": "small_object_AP",
        "night_AP": "night_AP",
        "operational_precision": "operational_precision",
        "operational_recall": "operational_recall",
        "operational_f1": "operational_f1",
    }

    for mlflow_name, slice_name in metric_mapping.items():
        if mlflow_name in metrics:
            baseline[slice_name] = metrics[mlflow_name]

    # Per-class AP
    for cls in OBJECT_CLASSES:
        for prefix in [f"AP_{cls}", f"val_AP_{cls}"]:
            if prefix in metrics:
                baseline[f"AP_{cls}"] = metrics[prefix]
                break

    return baseline


# ---------------------------------------------------------------------------
# Regression check
# ---------------------------------------------------------------------------


@dataclass
class RegressionResult:
    slice_name: str
    baseline_value: float
    current_value: float
    delta: float
    threshold: float
    regressed: bool


def check_regressions(
    current: dict[str, float],
    baseline: dict[str, float],
    threshold: float,
) -> list[RegressionResult]:
    """Compare current metrics against baseline and flag regressions.

    A regression is detected when:
        baseline_value - current_value > threshold

    Returns results for all slices present in both current and baseline.
    """
    results: list[RegressionResult] = []

    for slice_name in sorted(set(current.keys()) & set(baseline.keys())):
        current_val = current[slice_name]
        baseline_val = baseline[slice_name]
        delta = current_val - baseline_val
        regressed = (baseline_val - current_val) > threshold

        results.append(RegressionResult(
            slice_name=slice_name,
            baseline_value=baseline_val,
            current_value=current_val,
            delta=delta,
            threshold=threshold,
            regressed=regressed,
        ))

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def generate_markdown_report(
    eval_result: EvalResult,
    regressions: list[RegressionResult],
    baseline_run_id: str | None,
) -> str:
    """Generate a Markdown comparison report."""
    lines: list[str] = []
    lines.append("# Model Evaluation Report")
    lines.append("")
    lines.append(f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}")
    if baseline_run_id:
        lines.append(f"**Baseline run:** `{baseline_run_id}`")
    lines.append("")

    # Overall metrics
    lines.append("## Overall Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| mAP@0.5 | {eval_result.map50:.4f} |")
    lines.append(f"| mAP@0.5:0.95 | {eval_result.map50_95:.4f} |")
    lines.append(f"| small_object_AP | {eval_result.small_object_ap:.4f} |")
    lines.append(f"| night_AP | {eval_result.night_ap:.4f} |")
    lines.append("")

    # Per-class AP
    lines.append("## Per-Class AP")
    lines.append("")
    lines.append("| Class | AP |")
    lines.append("|-------|-----|")
    for cls in OBJECT_CLASSES:
        ap = eval_result.per_class_ap.get(cls, 0.0)
        lines.append(f"| {cls} | {ap:.4f} |")
    lines.append("")

    # Operational slice
    lines.append(f"## Operational Slice (threshold={eval_result.operational_threshold})")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Precision | {eval_result.operational_precision:.4f} |")
    lines.append(f"| Recall | {eval_result.operational_recall:.4f} |")
    lines.append(f"| F1 | {eval_result.operational_f1:.4f} |")
    lines.append("")

    # Regression comparison
    if regressions:
        lines.append("## Regression Check")
        lines.append("")
        any_regressed = any(r.regressed for r in regressions)
        if any_regressed:
            lines.append("**REGRESSION DETECTED** on the following slices:")
        else:
            lines.append("No regressions detected.")
        lines.append("")
        lines.append("| Slice | Baseline | Current | Delta | Status |")
        lines.append("|-------|----------|---------|-------|--------|")
        for r in regressions:
            status = "REGRESSED" if r.regressed else "OK"
            lines.append(
                f"| {r.slice_name} | {r.baseline_value:.4f} | "
                f"{r.current_value:.4f} | {r.delta:+.4f} | {status} |"
            )
        lines.append("")

    return "\n".join(lines)


def log_results_to_mlflow(
    tracking_uri: str,
    eval_result: EvalResult,
    regressions: list[RegressionResult],
    report_path: str | None,
) -> None:
    """Log evaluation results to MLflow (within the current or new run)."""
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)

    metrics = eval_result.to_dict()
    flat_metrics: dict[str, float] = {}
    for k, v in metrics.items():
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                flat_metrics[f"{k}_{sub_k}"] = float(sub_v)
        elif isinstance(v, (int, float)):
            flat_metrics[k] = float(v)

    mlflow.log_metrics(flat_metrics)

    if any(r.regressed for r in regressions):
        mlflow.set_tag("regression_detected", "true")
    else:
        mlflow.set_tag("regression_detected", "false")
    mlflow.set_tag("eval_gate", "passed" if not any(r.regressed for r in regressions) else "failed")

    if report_path and Path(report_path).exists():
        mlflow.log_artifact(report_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkpoint",
        default="models/checkpoints/best.pt",
        help="Path to model checkpoint.",
    )
    parser.add_argument(
        "--test-manifest",
        default="data/training/current/test.json",
        help="Path to test manifest JSON.",
    )
    parser.add_argument(
        "--baseline-run-id",
        default=None,
        help="MLflow run ID of the production baseline for regression comparison.",
    )
    parser.add_argument(
        "--mlflow-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000"),
        help="MLflow tracking URI.",
    )
    parser.add_argument(
        "--regression-threshold",
        type=float,
        default=DEFAULT_REGRESSION_THRESHOLD,
        help=f"Max allowed regression on any slice (default: {DEFAULT_REGRESSION_THRESHOLD}).",
    )
    parser.add_argument(
        "--operational-threshold",
        type=float,
        default=DEFAULT_OPERATIONAL_THRESHOLD,
        help=f"Confidence threshold for operational metrics (default: {DEFAULT_OPERATIONAL_THRESHOLD}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("models/evaluation/eval_results.json"),
        help="Output JSON path.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("models/evaluation/eval_report.md"),
        help="Output Markdown report path.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()

    # Run evaluation
    eval_result = run_evaluation(
        args.checkpoint,
        args.test_manifest,
        args.operational_threshold,
    )

    # Compare with baseline if provided
    regressions: list[RegressionResult] = []
    if args.baseline_run_id:
        log.info("loading baseline from MLflow run: %s", args.baseline_run_id)
        baseline = load_baseline_from_mlflow(args.mlflow_uri, args.baseline_run_id)
        current = eval_result.all_slices()
        regressions = check_regressions(current, baseline, args.regression_threshold)

        regressed_count = sum(1 for r in regressions if r.regressed)
        if regressed_count > 0:
            log.warning(
                "REGRESSION DETECTED: %d slices regressed beyond %.2f threshold",
                regressed_count, args.regression_threshold,
            )
            for r in regressions:
                if r.regressed:
                    log.warning(
                        "  %s: baseline=%.4f current=%.4f delta=%+.4f",
                        r.slice_name, r.baseline_value, r.current_value, r.delta,
                    )

    # Write results
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output_data = {
        "evaluation": eval_result.to_dict(),
        "baseline_run_id": args.baseline_run_id,
        "regressions": [
            {
                "slice": r.slice_name,
                "baseline": r.baseline_value,
                "current": r.current_value,
                "delta": r.delta,
                "regressed": r.regressed,
            }
            for r in regressions
        ],
        "gate_passed": not any(r.regressed for r in regressions),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    args.output.write_text(json.dumps(output_data, indent=2) + "\n", encoding="utf-8")

    # Write Markdown report
    report = generate_markdown_report(eval_result, regressions, args.baseline_run_id)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(report, encoding="utf-8")
    log.info("report written to %s", args.report_path)

    # Log to MLflow
    try:
        log_results_to_mlflow(
            args.mlflow_uri, eval_result, regressions, str(args.report_path)
        )
    except Exception as exc:
        log.warning("failed to log to MLflow (non-fatal): %s", exc)

    # Print summary
    print(json.dumps(output_data, indent=2))

    # Exit non-zero if regression detected
    if any(r.regressed for r in regressions):
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
