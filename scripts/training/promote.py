#!/usr/bin/env python3
"""Register an approved model in the MLflow Model Registry.

Transitions a trained model through registry stages:
    None -> Staging -> Production

Verifies the evaluation gate passed (checks MLflow run tags) before
allowing promotion to Production.

Usage:
    python promote.py --run-id abc123 --model-name yolov8l-detector --stage Staging
    python promote.py --run-id abc123 --model-name yolov8l-detector --stage Production
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

VALID_STAGES = ("None", "Staging", "Production", "Archived")


def verify_eval_gate(
    client: Any,
    run_id: str,
) -> bool:
    """Check that the evaluation gate passed for this run.

    Looks for the 'eval_gate' tag set by evaluate.py.
    Returns True if the gate passed, False otherwise.
    """
    run = client.get_run(run_id)
    tags = run.data.tags

    gate_value = tags.get("eval_gate")
    if gate_value is None:
        log.warning("run %s has no eval_gate tag — evaluation may not have been run", run_id)
        return False

    if gate_value != "passed":
        log.warning("run %s eval_gate=%s (not 'passed')", run_id, gate_value)
        return False

    regression = tags.get("regression_detected", "unknown")
    if regression == "true":
        log.warning("run %s has regression_detected=true", run_id)
        return False

    return True


def build_description(client: Any, run_id: str) -> str:
    """Build a model description from the run's params and metrics."""
    run = client.get_run(run_id)
    params = run.data.params
    metrics = run.data.metrics

    lines = [
        f"Source run: {run_id}",
        f"Model: {params.get('model.name', 'unknown')}",
        f"Classes: {params.get('model.num_classes', '?')}",
        f"Epochs: {metrics.get('total_epochs', '?')}",
        f"Best mAP@0.5:0.95: {metrics.get('best_mAP@0.5:0.95', '?')}",
        f"Best epoch: {metrics.get('best_epoch', '?')}",
    ]

    # Add eval metrics if available
    for key in ["mAP@0.5", "mAP@0.5:0.95", "operational_f1"]:
        if key in metrics:
            lines.append(f"Eval {key}: {metrics[key]}")

    return "\n".join(lines)


def register_model(
    tracking_uri: str,
    run_id: str,
    model_name: str,
    stage: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Register a model version and transition to the requested stage."""
    import mlflow
    from mlflow.exceptions import MlflowException

    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.tracking.MlflowClient()

    # Verify run exists
    try:
        client.get_run(run_id)
    except MlflowException as exc:
        raise RuntimeError(f"MLflow run {run_id} not found: {exc}") from exc

    # Gate check for Production promotion
    if stage == "Production" and not force:
        if not verify_eval_gate(client, run_id):
            raise RuntimeError(
                f"evaluation gate not passed for run {run_id}; "
                "run evaluate.py first or use --force to bypass"
            )

    # Build description
    description = build_description(client, run_id)

    # Find model artifacts in the run
    artifact_uri = f"runs:/{run_id}/model"

    # Register model version
    try:
        model_version = mlflow.register_model(artifact_uri, model_name)
        version = model_version.version
        log.info("registered %s version %s from run %s", model_name, version, run_id)
    except MlflowException as exc:
        # Model might already exist; try to create a new version
        raise RuntimeError(f"failed to register model: {exc}") from exc

    # Update description
    client.update_model_version(
        name=model_name,
        version=version,
        description=description,
    )

    # Transition stage
    if stage != "None":
        client.transition_model_version_stage(
            name=model_name,
            version=version,
            stage=stage,
        )
        log.info("transitioned %s v%s to stage: %s", model_name, version, stage)

    return {
        "model_name": model_name,
        "version": version,
        "stage": stage,
        "run_id": run_id,
        "artifact_uri": artifact_uri,
        "description": description,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        required=True,
        help="MLflow run ID of the trained model.",
    )
    parser.add_argument(
        "--model-name",
        default="yolov8l-detector",
        help="Model name in the MLflow registry (default: yolov8l-detector).",
    )
    parser.add_argument(
        "--stage",
        choices=list(VALID_STAGES),
        default="Staging",
        help="Target stage (default: Staging).",
    )
    parser.add_argument(
        "--mlflow-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000"),
        help="MLflow tracking URI.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass evaluation gate check (use with caution).",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()

    if args.stage not in VALID_STAGES:
        raise SystemExit(f"invalid stage: {args.stage}; must be one of {VALID_STAGES}")

    result = register_model(
        args.mlflow_uri,
        args.run_id,
        args.model_name,
        args.stage,
        force=args.force,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
