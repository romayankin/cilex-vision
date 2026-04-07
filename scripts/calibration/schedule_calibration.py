#!/usr/bin/env python3
"""Rotate edge-filter calibrations across cameras at most once per hour."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import edge_filter_calibration as calibration


DEFAULT_STATE_PATH = calibration.DEFAULT_OUTPUT_DIR / "schedule-state.json"


@dataclass(frozen=True)
class CameraTarget:
    site_id: str
    camera_id: str
    order_index: int
    last_completed_epoch: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--edge-config",
        type=Path,
        required=True,
        help="Edge-agent YAML config used to discover enabled cameras.",
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
        help="Scheduler state file tracking camera rotation and last run status.",
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
        help="Capture duration forwarded to the worker script.",
    )
    parser.add_argument(
        "--min-interval-s",
        type=int,
        default=3600,
        help="Minimum wall-clock gap between calibration starts.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the one-calibration-per-hour rate limit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the next eligible camera without launching the worker.",
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
    return parser.parse_args()


def current_epoch(now_iso: str | None) -> float:
    if now_iso is None:
        return time.time()
    return calibration.iso_to_epoch(now_iso)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "last_run_started_at": None,
            "cameras": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("last_run_started_at", None)
    payload.setdefault("cameras", {})
    return payload


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_inventory(edge_settings: calibration.EdgeSettings) -> list[tuple[str, str]]:
    return [
        (edge_settings.site_id, camera.camera_id)
        for camera in edge_settings.cameras
        if camera.enabled
    ]


def choose_next_camera(
    *,
    edge_settings: calibration.EdgeSettings,
    params_document: dict[str, Any],
    state: dict[str, Any],
    now_epoch: float,
    min_interval_s: int,
    force: bool,
) -> tuple[CameraTarget | None, str]:
    enabled_cameras = [camera for camera in edge_settings.cameras if camera.enabled]
    if not enabled_cameras:
        raise ValueError("edge config does not define any enabled cameras")

    last_started_at = state.get("last_run_started_at")
    if not force and last_started_at:
        elapsed = now_epoch - calibration.iso_to_epoch(str(last_started_at))
        if elapsed < min_interval_s:
            remaining = max(min_interval_s - elapsed, 0.0)
            return None, f"rate limit active; next slot opens in {remaining / 60.0:.1f} minutes"

    params_cameras = params_document.get("cameras") or {}
    state_cameras = state.get("cameras") or {}
    targets: list[CameraTarget] = []
    for index, camera in enumerate(enabled_cameras):
        key = calibration.camera_key(edge_settings.site_id, camera.camera_id)
        last_completed_epoch: float | None = None

        params_entry = params_cameras.get(key) or {}
        if params_entry.get("measured_at"):
            last_completed_epoch = calibration.iso_to_epoch(str(params_entry["measured_at"]))

        state_entry = state_cameras.get(key) or {}
        if state_entry.get("last_completed_at"):
            state_epoch = calibration.iso_to_epoch(str(state_entry["last_completed_at"]))
            if last_completed_epoch is None or state_epoch > last_completed_epoch:
                last_completed_epoch = state_epoch

        targets.append(
            CameraTarget(
                site_id=edge_settings.site_id,
                camera_id=camera.camera_id,
                order_index=index,
                last_completed_epoch=last_completed_epoch,
            )
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
    if selected.last_completed_epoch is None:
        return selected, "selected camera has no completed calibration yet"
    age_hours = (now_epoch - selected.last_completed_epoch) / 3600.0
    return selected, f"selected stalest camera ({age_hours:.2f} hours since last completion)"


def run_id_for_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_worker_command(args: argparse.Namespace, target: CameraTarget, run_id: str) -> list[str]:
    worker_script = Path(__file__).with_name("edge_filter_calibration.py")
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


def update_camera_state(
    state: dict[str, Any],
    *,
    target: CameraTarget,
    run_id: str,
    started_at: str,
    completed_at: str | None = None,
    status: str | None = None,
    returncode: int | None = None,
    error_excerpt: str | None = None,
) -> None:
    cameras = state.setdefault("cameras", {})
    key = calibration.camera_key(target.site_id, target.camera_id)
    entry = cameras.setdefault(
        key,
        {
            "site_id": target.site_id,
            "camera_id": target.camera_id,
        },
    )
    entry["last_run_id"] = run_id
    entry["last_started_at"] = started_at
    if completed_at is not None:
        entry["last_completed_at"] = completed_at
    if status is not None:
        entry["last_status"] = status
    if returncode is not None:
        entry["last_returncode"] = returncode
    if error_excerpt:
        entry["last_error_excerpt"] = error_excerpt
    elif "last_error_excerpt" in entry and status == "success":
        entry.pop("last_error_excerpt")


def refresh_metrics(args: argparse.Namespace, edge_settings: calibration.EdgeSettings, now_epoch: float) -> None:
    params_document = calibration.load_params_document(args.params_yaml)
    calibration.write_metrics_textfile(
        args.metrics_output,
        params_document,
        inventory=build_inventory(edge_settings),
        now_epoch=now_epoch,
    )


def main() -> None:
    args = parse_args()
    now_epoch = current_epoch(args.now_iso)
    now_iso = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    edge_settings = calibration.load_edge_settings(args.edge_config)
    params_document = calibration.load_params_document(args.params_yaml)
    state = load_state(args.state_path)

    target, reason = choose_next_camera(
        edge_settings=edge_settings,
        params_document=params_document,
        state=state,
        now_epoch=now_epoch,
        min_interval_s=args.min_interval_s,
        force=args.force,
    )

    if target is None:
        refresh_metrics(args, edge_settings, now_epoch)
        print(reason)
        return

    run_id = run_id_for_epoch(now_epoch)
    if args.dry_run:
        print(f"{target.site_id} {target.camera_id} {run_id} {reason}")
        return

    state["last_run_started_at"] = now_iso
    update_camera_state(
        state,
        target=target,
        run_id=run_id,
        started_at=now_iso,
        status="running",
    )
    save_state(args.state_path, state)

    command = build_worker_command(args, target, run_id)
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
    update_camera_state(
        state,
        target=target,
        run_id=run_id,
        started_at=now_iso,
        completed_at=completed_at,
        status="success" if result.returncode == 0 else "failed",
        returncode=result.returncode,
        error_excerpt=(result.stderr or result.stdout or "").strip()[:500] or None,
    )
    save_state(args.state_path, state)

    refresh_metrics(args, edge_settings, current_epoch(None))

    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print(f"Scheduled calibration completed for {target.site_id}/{target.camera_id}: {reason}")


if __name__ == "__main__":
    main()
