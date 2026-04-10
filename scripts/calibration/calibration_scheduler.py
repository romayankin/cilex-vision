#!/usr/bin/env python3
"""DB-driven calibration scheduler — rotates through all cameras weekly.

Reads online cameras from PostgreSQL, shells out to edge_filter_calibration.py,
persists successful results back to PostgreSQL, and refreshes the shared
calibration params / Prometheus textfile artifacts.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import importlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import edge_filter_calibration as calibration  # noqa: E402


LOGGER = logging.getLogger("calibration_scheduler")
DEFAULT_STATE_PATH = calibration.DEFAULT_OUTPUT_DIR / "schedule-state.json"
DEFAULT_INTERVAL_DAYS = 7.0
DEFAULT_MIN_GAP_HOURS = 1.0
CREATE_RESULTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS calibration_results (
    result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id),
    calibrated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    pass_through_rate DOUBLE PRECISION NOT NULL,
    miss_rate DOUBLE PRECISION NOT NULL,
    false_trigger_rate DOUBLE PRECISION NOT NULL,
    recommended_pixel_threshold INTEGER,
    recommended_motion_threshold DOUBLE PRECISION,
    recommended_scene_change_threshold DOUBLE PRECISION,
    scorecard_json JSONB,
    status TEXT NOT NULL DEFAULT 'success'
)
"""
CREATE_RESULTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_calibration_camera_time
ON calibration_results (camera_id, calibrated_at DESC)
"""


@dataclass(frozen=True)
class DatabaseCamera:
    camera_id: str
    db_site_id: str
    name: str
    status: str
    order_index: int


@dataclass(frozen=True)
class CameraTarget:
    site_id: str
    db_site_id: str
    camera_id: str
    name: str
    order_index: int
    last_completed_epoch: float | None


@dataclass(frozen=True)
class SchedulerSelection:
    target: CameraTarget | None
    reason: str
    skipped_camera_ids: tuple[str, ...]


@dataclass(frozen=True)
class PersistedResult:
    calibrated_at: datetime
    pass_through_rate: float
    miss_rate: float
    false_trigger_rate: float
    recommended_pixel_threshold: int | None
    recommended_motion_threshold: float | None
    recommended_scene_change_threshold: float | None
    scorecard_path: Path


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
        "--db-dsn",
        default=os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN"),
        help="PostgreSQL / TimescaleDB DSN.",
    )
    parser.add_argument(
        "--edge-config",
        type=Path,
        required=True,
        help="Edge-agent YAML config forwarded to edge_filter_calibration.py.",
    )
    parser.add_argument(
        "--inference-config",
        type=Path,
        help="Optional inference-worker YAML config forwarded to edge_filter_calibration.py.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=calibration.DEFAULT_OUTPUT_DIR,
        help="Base directory for calibration artifacts.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Scheduler state file tracking last runs and per-camera status.",
    )
    parser.add_argument(
        "--params-yaml",
        type=Path,
        default=calibration.DEFAULT_PARAMS_PATH,
        help="Shared calibration params YAML updated by the worker script.",
    )
    parser.add_argument(
        "--metrics-output",
        type=Path,
        default=calibration.DEFAULT_METRICS_OUTPUT,
        help="Prometheus textfile written after each scheduler invocation.",
    )
    parser.add_argument(
        "--capture-window-s",
        type=int,
        default=calibration.DEFAULT_WINDOW_S,
        help="Capture duration forwarded to edge_filter_calibration.py.",
    )
    parser.add_argument(
        "--interval-days",
        type=float,
        default=DEFAULT_INTERVAL_DAYS,
        help="Target calibration interval per camera.",
    )
    parser.add_argument(
        "--min-gap-hours",
        type=float,
        default=DEFAULT_MIN_GAP_HOURS,
        help="Minimum gap between any two calibration starts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the next eligible camera without launching calibration.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the interval and minimum-gap checks.",
    )
    parser.add_argument(
        "--now-iso",
        help="Override the scheduler clock for testing (ISO-8601 UTC).",
    )
    parser.add_argument(
        "--python-bin",
        default=sys.executable,
        help="Python interpreter used to run edge_filter_calibration.py.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level.",
    )
    return parser.parse_args()


def current_epoch(now_iso: str | None) -> float:
    if now_iso is None:
        return time.time()
    return calibration.iso_to_epoch(now_iso)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "last_run_started_at": None,
            "last_run": {},
            "cameras": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("last_run_started_at", None)
    payload.setdefault("last_run", {})
    payload.setdefault("cameras", {})
    return payload


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@contextlib.contextmanager
def scheduler_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another calibration scheduler run is already active: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_metrics_inventory(
    *,
    edge_settings: calibration.EdgeSettings,
    db_cameras: list[DatabaseCamera],
) -> list[tuple[str, str]]:
    return [(edge_settings.site_id, camera.camera_id) for camera in db_cameras]


def last_completed_epoch_for_camera(
    *,
    site_id: str,
    camera_id: str,
    params_document: dict[str, Any],
    state: dict[str, Any],
    db_last_completed: dict[str, float],
) -> float | None:
    last_completed_epoch = db_last_completed.get(camera_id)
    params_entry = (params_document.get("cameras") or {}).get(
        calibration.camera_key(site_id, camera_id),
        {},
    )
    measured_at = params_entry.get("measured_at")
    if measured_at:
        params_epoch = calibration.iso_to_epoch(str(measured_at))
        if last_completed_epoch is None or params_epoch > last_completed_epoch:
            last_completed_epoch = params_epoch

    state_entry = (state.get("cameras") or {}).get(
        calibration.camera_key(site_id, camera_id),
        {},
    )
    completed_at = state_entry.get("last_completed_at")
    if completed_at:
        state_epoch = calibration.iso_to_epoch(str(completed_at))
        if last_completed_epoch is None or state_epoch > last_completed_epoch:
            last_completed_epoch = state_epoch
    return last_completed_epoch


def choose_next_camera(
    *,
    edge_settings: calibration.EdgeSettings,
    db_cameras: list[DatabaseCamera],
    params_document: dict[str, Any],
    state: dict[str, Any],
    db_last_completed: dict[str, float],
    now_epoch: float,
    interval_days: float,
    min_gap_hours: float,
    force: bool,
) -> SchedulerSelection:
    if not db_cameras:
        raise RuntimeError("database returned no online cameras")

    last_started_at = state.get("last_run_started_at")
    if not force and last_started_at:
        elapsed = now_epoch - calibration.iso_to_epoch(str(last_started_at))
        min_gap_s = min_gap_hours * 3600.0
        if elapsed < min_gap_s:
            remaining_minutes = max(min_gap_s - elapsed, 0.0) / 60.0
            return SchedulerSelection(
                target=None,
                reason=f"rate limit active; next slot opens in {remaining_minutes:.1f} minutes",
                skipped_camera_ids=(),
            )

    enabled_edge_cameras = {
        camera.camera_id
        for camera in edge_settings.cameras
        if camera.enabled
    }
    targets: list[CameraTarget] = []
    skipped_camera_ids: list[str] = []
    for camera in db_cameras:
        if camera.camera_id not in enabled_edge_cameras:
            skipped_camera_ids.append(camera.camera_id)
            continue
        targets.append(
            CameraTarget(
                site_id=edge_settings.site_id,
                db_site_id=camera.db_site_id,
                camera_id=camera.camera_id,
                name=camera.name,
                order_index=camera.order_index,
                last_completed_epoch=last_completed_epoch_for_camera(
                    site_id=edge_settings.site_id,
                    camera_id=camera.camera_id,
                    params_document=params_document,
                    state=state,
                    db_last_completed=db_last_completed,
                ),
            )
        )

    if not targets:
        skipped = ", ".join(sorted(skipped_camera_ids))
        raise RuntimeError(
            "database returned online cameras, but none are enabled in the edge config: "
            f"{skipped}"
        )

    ordered = sorted(
        targets,
        key=lambda target: (
            0 if target.last_completed_epoch is None else 1,
            target.last_completed_epoch or 0.0,
            target.order_index,
        ),
    )
    selected = ordered[0]
    if force:
        return SchedulerSelection(
            target=selected,
            reason="forced run; interval and minimum-gap checks bypassed",
            skipped_camera_ids=tuple(sorted(skipped_camera_ids)),
        )
    if selected.last_completed_epoch is None:
        return SchedulerSelection(
            target=selected,
            reason="selected camera has no completed calibration yet",
            skipped_camera_ids=tuple(sorted(skipped_camera_ids)),
        )

    interval_s = interval_days * 86400.0
    age_s = max(now_epoch - selected.last_completed_epoch, 0.0)
    if age_s < interval_s:
        remaining_hours = max(interval_s - age_s, 0.0) / 3600.0
        return SchedulerSelection(
            target=None,
            reason=(
                f"no camera is due for recalibration yet; next due camera is "
                f"{selected.camera_id} in {remaining_hours:.1f} hours"
            ),
            skipped_camera_ids=tuple(sorted(skipped_camera_ids)),
        )
    age_days = age_s / 86400.0
    return SchedulerSelection(
        target=selected,
        reason=f"selected stalest camera ({age_days:.2f} days since last completion)",
        skipped_camera_ids=tuple(sorted(skipped_camera_ids)),
    )


def run_id_for_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_worker_command(
    args: argparse.Namespace,
    *,
    target: CameraTarget,
    run_id: str,
) -> list[str]:
    worker_script = SCRIPT_DIR / "edge_filter_calibration.py"
    command = [
        args.python_bin,
        str(worker_script),
        "--edge-config",
        str(args.edge_config),
        "--site-id",
        target.site_id,
        "--camera-id",
        target.camera_id,
        "--capture-window-s",
        str(args.capture_window_s),
        "--output-dir",
        str(args.output_dir),
        "--params-yaml",
        str(args.params_yaml),
        "--metrics-output",
        str(args.metrics_output),
        "--run-id",
        run_id,
    ]
    if args.inference_config is not None:
        command.extend(["--inference-config", str(args.inference_config)])
    return command


def build_run_paths(
    *,
    output_dir: Path,
    target: CameraTarget,
    run_id: str,
) -> calibration.RunPaths:
    return calibration.build_run_paths(
        output_dir=output_dir,
        site_id=target.site_id,
        camera_id=target.camera_id,
        run_id=run_id,
        capture_manifest=None,
        analysis_only=False,
    )


def parse_iso_datetime(value: str, field_name: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"{field_name} cannot be empty")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"invalid {field_name}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{field_name} must include a timezone offset")
    return parsed


def load_scorecard_result(scorecard_path: Path) -> PersistedResult:
    if not scorecard_path.exists():
        raise RuntimeError(f"expected scorecard JSON was not written: {scorecard_path}")
    payload = json.loads(scorecard_path.read_text(encoding="utf-8"))
    recommended = payload.get("recommended")
    if not isinstance(recommended, dict):
        raise RuntimeError(f"scorecard JSON is missing 'recommended': {scorecard_path}")
    metrics = recommended.get("metrics")
    motion_config = recommended.get("motion_config")
    if not isinstance(metrics, dict) or not isinstance(motion_config, dict):
        raise RuntimeError(
            f"scorecard JSON is missing recommended metrics or motion_config: {scorecard_path}"
        )
    calibrated_at_raw = payload.get("captured_at") or payload.get("generated_at")
    if not isinstance(calibrated_at_raw, str):
        raise RuntimeError(f"scorecard JSON is missing calibrated timestamp: {scorecard_path}")
    return PersistedResult(
        calibrated_at=parse_iso_datetime(calibrated_at_raw, "captured_at"),
        pass_through_rate=float(metrics["pass_through_rate"]),
        miss_rate=float(metrics["miss_rate"]),
        false_trigger_rate=float(metrics["false_trigger_rate"]),
        recommended_pixel_threshold=(
            int(motion_config["pixel_threshold"])
            if "pixel_threshold" in motion_config
            else None
        ),
        recommended_motion_threshold=(
            float(motion_config["motion_threshold"])
            if "motion_threshold" in motion_config
            else None
        ),
        recommended_scene_change_threshold=(
            float(motion_config["scene_change_threshold"])
            if "scene_change_threshold" in motion_config
            else None
        ),
        scorecard_path=scorecard_path,
    )


def update_camera_state(
    state: dict[str, Any],
    *,
    target: CameraTarget,
    run_id: str,
    started_at: str,
    selection_reason: str,
    status: str,
    completed_at: str | None = None,
    returncode: int | None = None,
    error_excerpt: str | None = None,
    result: PersistedResult | None = None,
) -> None:
    cameras = state.setdefault("cameras", {})
    key = calibration.camera_key(target.site_id, target.camera_id)
    entry = cameras.setdefault(
        key,
        {
            "site_id": target.site_id,
            "camera_id": target.camera_id,
            "camera_name": target.name,
            "db_site_id": target.db_site_id,
        },
    )
    entry["last_run_id"] = run_id
    entry["last_started_at"] = started_at
    entry["last_selection_reason"] = selection_reason
    entry["last_status"] = status
    if completed_at is not None:
        entry["last_completed_at"] = completed_at
    if returncode is not None:
        entry["last_returncode"] = returncode
    if error_excerpt:
        entry["last_error_excerpt"] = error_excerpt
    elif status == "success":
        entry.pop("last_error_excerpt", None)
    if result is not None:
        entry["pass_through_rate"] = result.pass_through_rate
        entry["miss_rate"] = result.miss_rate
        entry["false_trigger_rate"] = result.false_trigger_rate
        entry["scorecard_json_path"] = str(result.scorecard_path)

    state["last_run_started_at"] = started_at
    state["last_run"] = {
        "site_id": target.site_id,
        "camera_id": target.camera_id,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "selection_reason": selection_reason,
        "returncode": returncode,
        "scorecard_json_path": str(result.scorecard_path) if result is not None else None,
    }
    if error_excerpt:
        state["last_run"]["error_excerpt"] = error_excerpt


async def ensure_calibration_results_table(connection: Any) -> None:
    await connection.execute(CREATE_RESULTS_TABLE_SQL)
    await connection.execute(CREATE_RESULTS_INDEX_SQL)


async def fetch_online_cameras(connection: Any) -> list[DatabaseCamera]:
    rows = await connection.fetch(
        """
        SELECT camera_id, site_id::text AS db_site_id, name, status
        FROM cameras
        WHERE status = 'online'
        ORDER BY camera_id
        """
    )
    return [
        DatabaseCamera(
            camera_id=str(row["camera_id"]),
            db_site_id=str(row["db_site_id"]),
            name=str(row["name"]),
            status=str(row["status"]),
            order_index=index,
        )
        for index, row in enumerate(rows)
    ]


async def fetch_latest_completed_epochs(connection: Any) -> dict[str, float]:
    rows = await connection.fetch(
        """
        SELECT camera_id, MAX(calibrated_at) AS last_calibrated
        FROM calibration_results
        WHERE status = 'success'
        GROUP BY camera_id
        """
    )
    result: dict[str, float] = {}
    for row in rows:
        calibrated_at = row["last_calibrated"]
        if calibrated_at is not None:
            result[str(row["camera_id"])] = float(calibrated_at.timestamp())
    return result


async def insert_calibration_result(
    connection: Any,
    *,
    target: CameraTarget,
    result: PersistedResult,
) -> None:
    scorecard_payload = json.loads(result.scorecard_path.read_text(encoding="utf-8"))
    await connection.execute(
        """
        INSERT INTO calibration_results (
            camera_id,
            calibrated_at,
            pass_through_rate,
            miss_rate,
            false_trigger_rate,
            recommended_pixel_threshold,
            recommended_motion_threshold,
            recommended_scene_change_threshold,
            scorecard_json,
            status
        ) VALUES (
            $1,
            $2,
            $3,
            $4,
            $5,
            $6,
            $7,
            $8,
            $9::jsonb,
            'success'
        )
        """,
        target.camera_id,
        result.calibrated_at,
        result.pass_through_rate,
        result.miss_rate,
        result.false_trigger_rate,
        result.recommended_pixel_threshold,
        result.recommended_motion_threshold,
        result.recommended_scene_change_threshold,
        json.dumps(scorecard_payload, sort_keys=True),
    )


def refresh_metrics(
    *,
    args: argparse.Namespace,
    edge_settings: calibration.EdgeSettings,
    db_cameras: list[DatabaseCamera],
    now_epoch: float,
) -> None:
    params_document = calibration.load_params_document(args.params_yaml)
    calibration.write_metrics_textfile(
        args.metrics_output,
        params_document,
        inventory=build_metrics_inventory(edge_settings=edge_settings, db_cameras=db_cameras),
        now_epoch=now_epoch,
    )


async def gather_scheduler_context(
    args: argparse.Namespace,
) -> tuple[calibration.EdgeSettings, list[DatabaseCamera], dict[str, float]]:
    if not args.db_dsn:
        raise RuntimeError("--db-dsn is required")

    edge_settings = calibration.load_edge_settings(args.edge_config)
    asyncpg = require_module("asyncpg", "asyncpg")
    connection = await asyncpg.connect(args.db_dsn)
    try:
        await ensure_calibration_results_table(connection)
        db_cameras = await fetch_online_cameras(connection)
        db_last_completed = await fetch_latest_completed_epochs(connection)
    finally:
        await connection.close()
    return edge_settings, db_cameras, db_last_completed


async def persist_worker_result(
    args: argparse.Namespace,
    *,
    target: CameraTarget,
    result: PersistedResult,
) -> None:
    asyncpg = require_module("asyncpg", "asyncpg")
    connection = await asyncpg.connect(args.db_dsn)
    try:
        await ensure_calibration_results_table(connection)
        await insert_calibration_result(connection, target=target, result=result)
    finally:
        await connection.close()


async def run_scheduler(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.interval_days <= 0:
        raise RuntimeError("--interval-days must be greater than zero")
    if args.min_gap_hours < 0:
        raise RuntimeError("--min-gap-hours must be greater than or equal to zero")
    if args.capture_window_s <= 0:
        raise RuntimeError("--capture-window-s must be greater than zero")
    now_epoch = current_epoch(args.now_iso)
    now_iso = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )

    edge_settings, db_cameras, db_last_completed = await gather_scheduler_context(args)
    params_document = calibration.load_params_document(args.params_yaml)
    state = load_state(args.state_path)

    selection = choose_next_camera(
        edge_settings=edge_settings,
        db_cameras=db_cameras,
        params_document=params_document,
        state=state,
        db_last_completed=db_last_completed,
        now_epoch=now_epoch,
        interval_days=args.interval_days,
        min_gap_hours=args.min_gap_hours,
        force=args.force,
    )
    if selection.skipped_camera_ids:
        LOGGER.warning(
            "Skipping DB cameras not enabled in edge config: %s",
            ", ".join(selection.skipped_camera_ids),
        )

    if selection.target is None:
        refresh_metrics(
            args=args,
            edge_settings=edge_settings,
            db_cameras=db_cameras,
            now_epoch=now_epoch,
        )
        print(selection.reason)
        return

    target = selection.target
    run_id = run_id_for_epoch(now_epoch)
    run_paths = build_run_paths(output_dir=args.output_dir, target=target, run_id=run_id)

    if args.dry_run:
        print(f"{target.site_id} {target.camera_id} {run_id} {selection.reason}")
        return

    update_camera_state(
        state,
        target=target,
        run_id=run_id,
        started_at=now_iso,
        selection_reason=selection.reason,
        status="running",
    )
    save_state(args.state_path, state)

    command = build_worker_command(args, target=target, run_id=run_id)
    LOGGER.info("Launching calibration worker for %s", target.camera_id)
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    completed_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if result.returncode != 0:
        update_camera_state(
            state,
            target=target,
            run_id=run_id,
            started_at=now_iso,
            selection_reason=selection.reason,
            completed_at=completed_at,
            status="failed",
            returncode=result.returncode,
            error_excerpt=(result.stderr or result.stdout or "").strip()[:500] or None,
        )
        save_state(args.state_path, state)
        refresh_metrics(
            args=args,
            edge_settings=edge_settings,
            db_cameras=db_cameras,
            now_epoch=current_epoch(None),
        )
        raise SystemExit(result.returncode)

    persisted_result = load_scorecard_result(run_paths.scorecard_json_path)
    try:
        await persist_worker_result(args, target=target, result=persisted_result)
    except Exception as exc:
        error_excerpt = str(exc)[:500]
        update_camera_state(
            state,
            target=target,
            run_id=run_id,
            started_at=now_iso,
            selection_reason=selection.reason,
            completed_at=completed_at,
            status="failed",
            returncode=1,
            error_excerpt=error_excerpt,
        )
        save_state(args.state_path, state)
        refresh_metrics(
            args=args,
            edge_settings=edge_settings,
            db_cameras=db_cameras,
            now_epoch=current_epoch(None),
        )
        raise

    update_camera_state(
        state,
        target=target,
        run_id=run_id,
        started_at=now_iso,
        selection_reason=selection.reason,
        completed_at=completed_at,
        status="success",
        returncode=0,
        result=persisted_result,
    )
    save_state(args.state_path, state)
    refresh_metrics(
        args=args,
        edge_settings=edge_settings,
        db_cameras=db_cameras,
        now_epoch=current_epoch(None),
    )
    print(
        f"Scheduled calibration completed for {target.site_id}/{target.camera_id}: "
        f"{selection.reason}"
    )


def main() -> None:
    args = parse_args()
    lock_path = args.state_path.with_suffix(args.state_path.suffix + ".lock")
    with scheduler_lock(lock_path):
        asyncio.run(run_scheduler(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:  # pragma: no cover - CLI boundary
        raise SystemExit(130) from None
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc
