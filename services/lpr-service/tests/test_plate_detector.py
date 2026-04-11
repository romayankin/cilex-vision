"""Tests for plate_detector preprocessing and postprocessing."""

from __future__ import annotations

import numpy as np
import pytest

from plate_detector import PlateDetectorClient, PlateDetection, _nms


def _client() -> PlateDetectorClient:
    return PlateDetectorClient(
        triton_url="localhost:8001",
        model_name="plate_detector",
        input_name="images",
        output_name="plate_detections",
        input_size=640,
        confidence_threshold=0.35,
        nms_iou_threshold=0.40,
    )


def test_preprocess_resize_and_normalize() -> None:
    client = _client()
    vehicle_crop = np.random.randint(0, 255, size=(80, 160, 3), dtype=np.uint8)

    tensor = client._preprocess(vehicle_crop)

    assert tensor.shape == (1, 3, 640, 640)
    assert tensor.dtype == np.float32
    assert 0.0 <= float(tensor.min()) <= float(tensor.max()) <= 1.0


def test_postprocess_extracts_and_filters_boxes() -> None:
    client = _client()
    raw = np.array(
        [
            [
                [0.50, 0.60, 0.40, 0.20, 0.95],
                [0.52, 0.60, 0.40, 0.20, 0.70],
                [0.20, 0.25, 0.15, 0.08, 0.10],
            ]
        ],
        dtype=np.float32,
    )

    detections = client._postprocess(raw)

    assert len(detections) == 1
    det = detections[0]
    assert isinstance(det, PlateDetection)
    assert det.confidence == pytest.approx(0.95)
    assert 0.0 <= det.x <= 1.0
    assert 0.0 <= det.y <= 1.0
    assert 0.0 < det.w <= 1.0
    assert 0.0 < det.h <= 1.0


def test_nms_removes_overlapping_candidates() -> None:
    x1 = np.array([0.10, 0.12, 0.70], dtype=np.float32)
    y1 = np.array([0.10, 0.11, 0.70], dtype=np.float32)
    x2 = np.array([0.50, 0.51, 0.90], dtype=np.float32)
    y2 = np.array([0.22, 0.22, 0.82], dtype=np.float32)
    scores = np.array([0.95, 0.85, 0.60], dtype=np.float32)

    keep = _nms(x1, y1, x2, y2, scores, iou_thresh=0.4)

    assert 0 in keep
    assert 2 in keep
    assert len(keep) == 2


def test_empty_and_no_plate_cases() -> None:
    client = _client()

    assert client._postprocess(np.empty((1, 0, 5), dtype=np.float32)) == []

    low_conf = np.array([[[0.50, 0.50, 0.40, 0.20, 0.10]]], dtype=np.float32)
    assert client._postprocess(low_conf) == []
