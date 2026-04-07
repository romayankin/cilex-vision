from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

calibration = importlib.import_module("edge_filter_calibration")
scheduler = importlib.import_module("schedule_calibration")


def _edge_settings() -> calibration.EdgeSettings:
    return calibration.EdgeSettings(
        site_id="site-a",
        cameras=[
            calibration.CameraConfig(camera_id="cam-01", rtsp_url="rtsp://cam-01"),
            calibration.CameraConfig(camera_id="cam-02", rtsp_url="rtsp://cam-02"),
        ],
    )


def test_choose_next_camera_prefers_uncalibrated_camera() -> None:
    settings = _edge_settings()
    params_document = {
        "cameras": {
            "site-a/cam-01": {
                "site_id": "site-a",
                "camera_id": "cam-01",
                "measured_at": "2026-04-07T08:00:00Z",
            }
        }
    }
    target, reason = scheduler.choose_next_camera(
        edge_settings=settings,
        params_document=params_document,
        state={"last_run_started_at": None, "cameras": {}},
        now_epoch=calibration.iso_to_epoch("2026-04-07T12:00:00Z"),
        min_interval_s=3600,
        force=False,
    )
    assert target is not None
    assert target.camera_id == "cam-02"
    assert "no completed calibration" in reason


def test_choose_next_camera_respects_one_per_hour_rate_limit() -> None:
    settings = _edge_settings()
    target, reason = scheduler.choose_next_camera(
        edge_settings=settings,
        params_document={"cameras": {}},
        state={"last_run_started_at": "2026-04-07T11:30:00Z", "cameras": {}},
        now_epoch=calibration.iso_to_epoch("2026-04-07T12:00:00Z"),
        min_interval_s=3600,
        force=False,
    )
    assert target is None
    assert "rate limit active" in reason


def test_build_worker_command_forwards_core_paths(tmp_path: Path) -> None:
    namespace = argparse.Namespace(
        edge_config=tmp_path / "edge.yaml",
        inference_config=tmp_path / "inference.yaml",
        capture_window_s=600,
        output_dir=tmp_path / "artifacts",
        params_yaml=tmp_path / "params.yaml",
        metrics_output=tmp_path / "metrics.prom",
        python_bin="/usr/bin/python3",
    )

    target = scheduler.CameraTarget(
        site_id="site-a",
        camera_id="cam-01",
        order_index=0,
        last_completed_epoch=None,
    )
    command = scheduler.build_worker_command(namespace, target, "20260407T120000Z")
    assert "--camera-id" in command
    assert "cam-01" in command
    assert "--run-id" in command
    assert "20260407T120000Z" in command
