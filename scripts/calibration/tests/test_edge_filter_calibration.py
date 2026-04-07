from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

calibration = importlib.import_module("edge_filter_calibration")


def _result(
    *,
    pass_through_rate: float,
    miss_rate: float,
    false_trigger_rate: float,
    is_baseline: bool = False,
) -> calibration.CandidateResult:
    total_frames = 100
    object_positive_frames = 20
    object_negative_frames = total_frames - object_positive_frames
    motion_positive_frames = int(round(pass_through_rate * total_frames))
    false_negative_frames = int(round(miss_rate * object_positive_frames))
    false_positive_frames = int(round(false_trigger_rate * object_negative_frames))
    true_positive_frames = max(object_positive_frames - false_negative_frames, 0)
    true_negative_frames = max(object_negative_frames - false_positive_frames, 0)
    scene_change_frames = 5
    metrics = calibration.CandidateMetrics(
        total_frames=total_frames,
        object_positive_frames=object_positive_frames,
        object_negative_frames=object_negative_frames,
        motion_positive_frames=motion_positive_frames,
        true_positive_frames=true_positive_frames,
        true_negative_frames=true_negative_frames,
        false_positive_frames=false_positive_frames,
        false_negative_frames=false_negative_frames,
        scene_change_frames=scene_change_frames,
        miss_rate=miss_rate,
        false_trigger_rate=false_trigger_rate,
        pass_through_rate=pass_through_rate,
        scene_change_rate=scene_change_frames / total_frames,
    )
    normalized_distance = min(
        abs(pass_through_rate - calibration.DEFAULT_TARGET_PASS_THROUGH_RATE)
        / calibration.DEFAULT_TARGET_PASS_THROUGH_RATE,
        1.0,
    )
    score = (
        0.60 * (1.0 - miss_rate)
        + 0.25 * (1.0 - false_trigger_rate)
        + 0.15 * (1.0 - normalized_distance)
    )
    return calibration.CandidateResult(
        motion_config=calibration.MotionConfig(
            pixel_threshold=25 if is_baseline else 30,
            motion_threshold=0.02 if is_baseline else 0.03,
            scene_change_threshold=0.80,
            reference_update_interval_s=300,
        ),
        metrics=metrics,
        score=score,
        is_baseline=is_baseline,
    )


def test_choose_recommended_candidate_prefers_in_tolerance_window() -> None:
    baseline = _result(
        pass_through_rate=0.40,
        miss_rate=0.00,
        false_trigger_rate=0.00,
        is_baseline=True,
    )
    target_fit = _result(
        pass_through_rate=0.16,
        miss_rate=0.05,
        false_trigger_rate=0.10,
    )
    recommended, note = calibration.choose_recommended_candidate(
        [baseline, target_fit],
        target_pass_through_rate=0.15,
        pass_through_tolerance=0.05,
    )
    assert recommended == target_fit
    assert "Preferred candidates" in note


def test_build_metrics_text_emits_nan_for_uncalibrated_inventory() -> None:
    payload = calibration.load_params_document(Path("does-not-exist.yaml"))
    text = calibration.build_metrics_text(
        payload,
        inventory=[("site-a", "cam-01")],
        now_epoch=0.0,
    )
    assert 'per_camera_pass_through_rate{site_id="site-a",camera_id="cam-01"} NaN' in text
    assert 'calibration_freshness_hours{site_id="site-a",camera_id="cam-01"} NaN' in text


def test_update_params_document_writes_camera_entry(tmp_path: Path) -> None:
    params_path = tmp_path / "params.yaml"
    baseline = _result(
        pass_through_rate=0.22,
        miss_rate=0.04,
        false_trigger_rate=0.09,
        is_baseline=True,
    )
    recommended = _result(
        pass_through_rate=0.17,
        miss_rate=0.03,
        false_trigger_rate=0.08,
    )
    payload = calibration.update_params_document(
        params_path,
        site_id="site-a",
        camera_id="cam-01",
        capture_window_s=600,
        detector_summary={
            "model_name": "yolov8l",
            "confidence_threshold": 0.40,
            "nms_iou_threshold": 0.45,
            "triton_url": "localhost:8001",
            "analysis_runtime_s": 1.0,
            "effective_fps": 10.0,
            "recommendation_note": "test",
        },
        baseline=baseline,
        recommended=recommended,
        scorecard_json_path=tmp_path / "scorecard.json",
        capture_manifest_path=tmp_path / "capture-manifest.json",
    )
    entry = payload["cameras"]["site-a/cam-01"]
    assert entry["pass_through_rate"] == pytest.approx(0.17)
    assert entry["recommended_motion"]["motion_threshold"] == pytest.approx(0.03)
