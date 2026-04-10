#!/usr/bin/env python3
"""Evaluate MTMC Re-ID accuracy against ground truth annotations.

Usage:
    python run_mtmc_eval.py --ground-truth data/eval/reid/ground_truth.json \
        --db-dsn postgresql://localhost:5432/vidanalytics \
        --site-id site-01 --mlflow-uri http://localhost:5000
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from reid_metrics import (
    IdentityGroup,
    PredictedAssociation,
    ReIDMetrics,
    compute_reid_metrics,
    identity_groups_from_payload,
    metrics_to_json_dict,
)


DEFAULT_MLFLOW_URI = "http://127.0.0.1:5000"
DEFAULT_OUTPUT_DIR = Path("artifacts/evaluation/mtmc")


@dataclass(frozen=True)
class GroundTruthDataset:
    identity_groups: list[IdentityGroup]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LocalTrackRow:
    local_track_id: str
    camera_id: str
    object_class: str
    start_time: datetime
    end_time: datetime | None


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
        "--ground-truth",
        type=Path,
        default=Path("data/eval/reid/ground_truth.json"),
        help="Evaluation-ready ground truth JSON produced by export_reid_gt.py.",
    )
    parser.add_argument(
        "--db-dsn",
        default=os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN"),
        help="PostgreSQL / TimescaleDB DSN.",
    )
    parser.add_argument(
        "--site-id",
        default=os.environ.get("SITE_ID", "site-01"),
        help="Site identifier used for MLflow tagging.",
    )
    parser.add_argument(
        "--start-time",
        help="Optional inclusive evaluation window start timestamp (ISO-8601).",
    )
    parser.add_argument(
        "--end-time",
        help="Optional inclusive evaluation window end timestamp (ISO-8601).",
    )
    parser.add_argument(
        "--mlflow-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", DEFAULT_MLFLOW_URI),
        help="MLflow tracking URI.",
    )
    parser.add_argument(
        "--mlflow-experiment",
        default="mtmc-evaluation",
        help="MLflow experiment name.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for local evaluation artifacts.",
    )
    parser.add_argument(
        "--go-live-rank1-threshold",
        type=float,
        default=0.70,
        help="Rank-1 accuracy threshold for a go-live recommendation.",
    )
    return parser.parse_args()


def load_ground_truth(path: Path) -> GroundTruthDataset:
    if not path.exists():
        raise RuntimeError(f"ground truth file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("ground truth JSON must be a top-level object")
    identity_groups = identity_groups_from_payload(payload)
    validate_ground_truth_track_ids(identity_groups)
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return GroundTruthDataset(identity_groups=identity_groups, metadata=metadata)


def validate_ground_truth_track_ids(identity_groups: list[IdentityGroup]) -> None:
    for identity_group in identity_groups:
        for sighting in identity_group.sightings:
            try:
                UUID(sighting.local_track_id)
            except ValueError as exc:
                raise RuntimeError(
                    f"ground truth contains non-UUID local_track_id {sighting.local_track_id!r}; "
                    "export_reid_gt.py should be used to validate and normalize the source data"
                ) from exc


def parse_iso_datetime(value: str, flag_name: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"{flag_name} cannot be empty")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"invalid {flag_name}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{flag_name} must include a timezone offset")
    return parsed


def determine_requested_window(args: argparse.Namespace) -> tuple[datetime | None, datetime | None]:
    if args.start_time is None and args.end_time is None:
        return None, None
    if args.start_time is None or args.end_time is None:
        raise RuntimeError("--start-time and --end-time must be provided together")
    start_time = parse_iso_datetime(args.start_time, "--start-time")
    end_time = parse_iso_datetime(args.end_time, "--end-time")
    if end_time < start_time:
        raise RuntimeError("--end-time must be greater than or equal to --start-time")
    return start_time, end_time


async def fetch_local_tracks(
    connection: Any,
    local_track_ids: list[str],
) -> dict[str, LocalTrackRow]:
    rows = await connection.fetch(
        """
        SELECT lt.local_track_id, lt.camera_id, lt.object_class, lt.start_time, lt.end_time
        FROM local_tracks lt
        WHERE lt.local_track_id = ANY($1::uuid[])
        """,
        local_track_ids,
    )
    result: dict[str, LocalTrackRow] = {}
    for row in rows:
        result[str(row["local_track_id"])] = LocalTrackRow(
            local_track_id=str(row["local_track_id"]),
            camera_id=str(row["camera_id"]),
            object_class=str(row["object_class"]),
            start_time=row["start_time"],
            end_time=row["end_time"],
        )
    return result


def derive_window_from_local_tracks(local_tracks: dict[str, LocalTrackRow]) -> tuple[datetime, datetime]:
    if not local_tracks:
        raise RuntimeError("cannot derive an evaluation window because no local tracks were found")
    start_time = min(track.start_time for track in local_tracks.values())
    end_time = max(track.end_time or track.start_time for track in local_tracks.values())
    return start_time, end_time


async def fetch_predictions(
    connection: Any,
    *,
    annotated_track_ids: list[str],
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[PredictedAssociation], int]:
    global_track_rows = await connection.fetch(
        """
        SELECT gt.global_track_id, gt.object_class, gt.first_seen, gt.last_seen
        FROM global_tracks gt
        WHERE gt.first_seen <= $2 AND gt.last_seen >= $1
        """,
        start_time,
        end_time,
    )
    if not global_track_rows:
        return [], 0

    global_track_ids = [str(row["global_track_id"]) for row in global_track_rows]
    link_rows = await connection.fetch(
        """
        SELECT gtl.global_track_id, gtl.local_track_id, gtl.camera_id, gtl.confidence, gtl.linked_at
        FROM global_track_links gtl
        WHERE gtl.global_track_id = ANY($1::uuid[])
        """,
        global_track_ids,
    )
    if not link_rows:
        return [], len(global_track_rows)

    annotated_track_id_set = set(annotated_track_ids)
    predicted_associations: list[PredictedAssociation] = []
    for row in link_rows:
        local_track_id = str(row["local_track_id"])
        if local_track_id not in annotated_track_id_set:
            continue
        predicted_associations.append(
            PredictedAssociation(
                local_track_id=local_track_id,
                global_track_id=str(row["global_track_id"]),
                camera_id=str(row["camera_id"]),
                confidence=float(row["confidence"]),
                linked_at=row["linked_at"].isoformat()
                if row["linked_at"] is not None
                else None,
            )
        )
    return predicted_associations, len(global_track_rows)


def build_per_camera_pair_report(metrics: ReIDMetrics) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for key, value in metrics.per_camera_pair.items():
        report[key] = {
            "camera_a": value.camera_a,
            "camera_b": value.camera_b,
            "true_pairs": value.true_pairs,
            "predicted_pairs": value.predicted_pairs,
            "correct": value.correct,
            "precision": value.precision,
            "recall": value.recall,
        }
    return report


def build_report_markdown(
    *,
    site_id: str,
    ground_truth_path: Path,
    output_dir: Path,
    evaluation_window: tuple[datetime, datetime],
    dataset: GroundTruthDataset,
    metrics: ReIDMetrics,
    go_live_threshold: float,
    global_tracks_in_window: int,
    predicted_association_count: int,
) -> str:
    start_time, end_time = evaluation_window
    go_live_result = "PASS" if metrics.rank1_accuracy >= go_live_threshold else "FAIL"
    lines = [
        "# MTMC Evaluation Report",
        "",
        "## Configuration",
        "",
        f"- Site ID: `{site_id}`",
        f"- Ground truth: `{ground_truth_path}`",
        f"- Output dir: `{output_dir}`",
        f"- Evaluation window: `{start_time.isoformat()}` to `{end_time.isoformat()}`",
        f"- Ground-truth identities: `{len(dataset.identity_groups)}`",
        f"- Ground-truth source: `{dataset.metadata.get('source', 'unknown')}`",
        f"- Global tracks in DB window: `{global_tracks_in_window}`",
        f"- Predicted associations evaluated: `{predicted_association_count}`",
        "",
        "## Go / No-Go",
        "",
        "| Metric | Threshold | Measured | Result |",
        "|--------|-----------|----------|--------|",
        f"| Rank-1 accuracy | > {go_live_threshold:.2f} | {metrics.rank1_accuracy:.4f} | {go_live_result} |",
        "",
        "## Core Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Rank-1 accuracy | {metrics.rank1_accuracy:.4f} |",
        f"| Rank-5 accuracy | {metrics.rank5_accuracy:.4f} |",
        f"| Mean average precision | {metrics.mean_average_precision:.4f} |",
        f"| Precision | {metrics.precision:.4f} |",
        f"| Recall | {metrics.recall:.4f} |",
        f"| F1 | {metrics.f1:.4f} |",
        f"| False positive rate | {metrics.false_positive_rate:.4f} |",
        f"| False negative rate | {metrics.false_negative_rate:.4f} |",
        f"| Total queries | {metrics.total_queries} |",
        f"| Total true pairs | {metrics.total_true_pairs} |",
        f"| Total predicted pairs | {metrics.total_predicted_pairs} |",
        "",
        "## Per-Camera Pair Breakdown",
        "",
        "| Camera Pair | True Pairs | Predicted Pairs | Correct | Precision | Recall |",
        "|-------------|------------|-----------------|---------|-----------|--------|",
    ]
    for key, value in sorted(metrics.per_camera_pair.items()):
        lines.append(
            f"| {key} | {value.true_pairs} | {value.predicted_pairs} | "
            f"{value.correct} | {value.precision:.4f} | {value.recall:.4f} |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Rank-1, Rank-5, and mAP are computed from final MTMC assignments in the DB.",
            "- The MTMC service does not currently persist candidate-ranked retrieval lists, "
            "so these are assignment-derived proxies rather than full FAISS retrieval metrics.",
            "- Evaluation is restricted to annotated `local_track_id` values from ground truth. "
            "Unlabeled site traffic is excluded to avoid treating missing labels as false positives.",
        ]
    )
    return "\n".join(lines) + "\n"


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


def sanitize_metric_name(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)


def log_run_to_mlflow(
    *,
    args: argparse.Namespace,
    dataset: GroundTruthDataset,
    metrics: ReIDMetrics,
    evaluation_window: tuple[datetime, datetime],
    global_tracks_in_window: int,
    predicted_association_count: int,
    git_revision: str | None,
    git_dirty: bool | None,
) -> str:
    mlflow = require_module("mlflow", "mlflow")
    mlflow.set_tracking_uri(args.mlflow_uri)
    mlflow.set_experiment(args.mlflow_experiment)

    with tempfile.TemporaryDirectory(prefix="mtmc-eval-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        metrics_path = temp_dir / "metrics.json"
        per_camera_path = temp_dir / "per_camera_pair_report.json"
        report_path = temp_dir / "evaluation_report.md"

        metrics_path.write_text(
            json.dumps(metrics_to_json_dict(metrics), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        per_camera_path.write_text(
            json.dumps(build_per_camera_pair_report(metrics), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report_path.write_text(
            build_report_markdown(
                site_id=args.site_id,
                ground_truth_path=args.ground_truth,
                output_dir=args.output_dir,
                evaluation_window=evaluation_window,
                dataset=dataset,
                metrics=metrics,
                go_live_threshold=args.go_live_rank1_threshold,
                global_tracks_in_window=global_tracks_in_window,
                predicted_association_count=predicted_association_count,
            ),
            encoding="utf-8",
        )

        start_time, end_time = evaluation_window
        with mlflow.start_run(run_name=f"mtmc-eval-{args.site_id}") as run:
            mlflow.set_tags(
                {
                    "eval.phase": "mtmc",
                    "eval.site_id": args.site_id,
                    "eval.protocol_version": "1.0.0",
                }
            )
            params = {
                "site_id": args.site_id,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "ground_truth_path": str(args.ground_truth),
                "go_live_threshold": args.go_live_rank1_threshold,
                "identity_group_count": len(dataset.identity_groups),
                "global_tracks_in_window": global_tracks_in_window,
                "predicted_association_count": predicted_association_count,
            }
            source_project = dataset.metadata.get("source_project")
            if isinstance(source_project, str) and source_project.strip():
                params["source_project"] = source_project.strip()
            if git_revision is not None:
                params["git_revision"] = git_revision
            if git_dirty is not None:
                params["git_dirty"] = str(git_dirty).lower()
            mlflow.log_params(params)
            mlflow.log_metrics(
                {
                    "rank1_accuracy": metrics.rank1_accuracy,
                    "rank5_accuracy": metrics.rank5_accuracy,
                    "mean_average_precision": metrics.mean_average_precision,
                    "false_positive_rate": metrics.false_positive_rate,
                    "false_negative_rate": metrics.false_negative_rate,
                    "precision": metrics.precision,
                    "recall": metrics.recall,
                    "f1": metrics.f1,
                    "total_queries": float(metrics.total_queries),
                    "total_true_pairs": float(metrics.total_true_pairs),
                    "total_predicted_pairs": float(metrics.total_predicted_pairs),
                }
            )
            for key, value in metrics.per_camera_pair.items():
                suffix = sanitize_metric_name(key)
                mlflow.log_metric(f"reid_precision_{suffix}", value.precision)
                mlflow.log_metric(f"reid_recall_{suffix}", value.recall)
            mlflow.log_artifact(str(metrics_path))
            mlflow.log_artifact(str(per_camera_path))
            mlflow.log_artifact(str(report_path))
            return run.info.run_id


async def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    if not args.db_dsn:
        raise RuntimeError("--db-dsn is required")

    dataset = load_ground_truth(args.ground_truth)
    annotated_track_ids = sorted(
        {
            sighting.local_track_id
            for identity_group in dataset.identity_groups
            for sighting in identity_group.sightings
        }
    )

    asyncpg = require_module("asyncpg", "asyncpg")
    requested_window = determine_requested_window(args)
    connection = await asyncpg.connect(args.db_dsn)
    try:
        gt_local_tracks = await fetch_local_tracks(connection, annotated_track_ids)
        missing_track_ids = sorted(set(annotated_track_ids) - set(gt_local_tracks))
        if missing_track_ids:
            preview = ", ".join(missing_track_ids[:5])
            raise RuntimeError(
                "ground truth references local_tracks that were not found in the DB: "
                f"{preview}"
            )

        evaluation_window = (
            requested_window
            if requested_window != (None, None)
            else derive_window_from_local_tracks(gt_local_tracks)
        )
        start_time, end_time = evaluation_window

        predicted_associations, global_tracks_in_window = await fetch_predictions(
            connection,
            annotated_track_ids=annotated_track_ids,
            start_time=start_time,
            end_time=end_time,
        )
    finally:
        await connection.close()

    metrics = compute_reid_metrics(dataset.identity_groups, predicted_associations)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    local_metrics_path = args.output_dir / "metrics.json"
    local_per_camera_path = args.output_dir / "per_camera_pair_report.json"
    local_report_path = args.output_dir / "evaluation_report.md"
    local_summary_path = args.output_dir / "evaluation_summary.json"

    local_metrics_path.write_text(
        json.dumps(metrics_to_json_dict(metrics), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    local_per_camera_path.write_text(
        json.dumps(build_per_camera_pair_report(metrics), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    local_report_path.write_text(
        build_report_markdown(
            site_id=args.site_id,
            ground_truth_path=args.ground_truth,
            output_dir=args.output_dir,
            evaluation_window=evaluation_window,
            dataset=dataset,
            metrics=metrics,
            go_live_threshold=args.go_live_rank1_threshold,
            global_tracks_in_window=global_tracks_in_window,
            predicted_association_count=len(predicted_associations),
        ),
        encoding="utf-8",
    )

    git_revision, git_dirty = detect_git_state()
    run_id = log_run_to_mlflow(
        args=args,
        dataset=dataset,
        metrics=metrics,
        evaluation_window=evaluation_window,
        global_tracks_in_window=global_tracks_in_window,
        predicted_association_count=len(predicted_associations),
        git_revision=git_revision,
        git_dirty=git_dirty,
    )

    summary_payload = {
        "site_id": args.site_id,
        "ground_truth_path": str(args.ground_truth),
        "evaluation_window": {
            "start_time": evaluation_window[0].isoformat(),
            "end_time": evaluation_window[1].isoformat(),
        },
        "metrics": metrics_to_json_dict(metrics),
        "global_tracks_in_window": global_tracks_in_window,
        "predicted_association_count": len(predicted_associations),
        "mlflow_run_id": run_id,
    }
    local_summary_path.write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary_payload


def main() -> None:
    args = parse_args()
    summary = asyncio.run(evaluate(args))
    print(
        json.dumps(
            {
                "site_id": summary["site_id"],
                "rank1_accuracy": round(summary["metrics"]["rank1_accuracy"], 6),
                "rank5_accuracy": round(summary["metrics"]["rank5_accuracy"], 6),
                "mean_average_precision": round(
                    summary["metrics"]["mean_average_precision"],
                    6,
                ),
                "mlflow_run_id": summary["mlflow_run_id"],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
