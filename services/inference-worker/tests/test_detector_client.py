"""Tests for detector_client — preprocessing, NMS, and post-processing.

Triton is mocked so these tests run without a live Triton server.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from config import DetectorConfig, TritonConfig
from detector_client import (
    CLASS_INDEX_TO_NAME,
    DetectorClient,
    LetterboxInfo,
    RawDetection,
    _nms,
    _per_class_nms,
)


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

@pytest.fixture
def triton_cfg() -> TritonConfig:
    return TritonConfig()


@pytest.fixture
def det_cfg() -> DetectorConfig:
    return DetectorConfig()


@pytest.fixture
def client(triton_cfg: TritonConfig, det_cfg: DetectorConfig) -> DetectorClient:
    return DetectorClient(triton_cfg, det_cfg)


@pytest.fixture
def sample_frame() -> np.ndarray:
    """720p RGB frame."""
    return np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)


# ---------------------------------------------------------------
# Preprocessing tests
# ---------------------------------------------------------------

class TestPreprocessing:

    def test_letterbox_shape(self, client: DetectorClient, sample_frame: np.ndarray) -> None:
        tensor, lb = client._preprocess(sample_frame)
        assert tensor.shape == (1, 3, 640, 640)
        assert tensor.dtype == np.float32

    def test_letterbox_values_in_range(self, client: DetectorClient, sample_frame: np.ndarray) -> None:
        tensor, _ = client._preprocess(sample_frame)
        assert tensor.min() >= 0.0
        assert tensor.max() <= 1.0

    def test_letterbox_info(self, client: DetectorClient, sample_frame: np.ndarray) -> None:
        _, lb = client._preprocess(sample_frame)
        assert lb.orig_w == 1280
        assert lb.orig_h == 720
        assert lb.scale > 0
        assert lb.pad_w >= 0
        assert lb.pad_h >= 0

    def test_square_frame(self, client: DetectorClient) -> None:
        frame = np.random.randint(0, 255, (640, 640, 3), dtype=np.uint8)
        tensor, lb = client._preprocess(frame)
        assert tensor.shape == (1, 3, 640, 640)
        assert lb.pad_w == pytest.approx(0, abs=1)
        assert lb.pad_h == pytest.approx(0, abs=1)


# ---------------------------------------------------------------
# NMS tests
# ---------------------------------------------------------------

class TestNMS:

    def test_nms_removes_overlapping(self) -> None:
        x1 = np.array([0.0, 0.01, 100.0])
        y1 = np.array([0.0, 0.01, 100.0])
        x2 = np.array([50.0, 50.0, 150.0])
        y2 = np.array([50.0, 50.0, 150.0])
        scores = np.array([0.9, 0.8, 0.7])

        keep = _nms(x1, y1, x2, y2, scores, iou_thresh=0.5)
        # Box 0 and 1 overlap heavily; 0 wins. Box 2 is separate.
        assert 0 in keep
        assert 2 in keep
        assert len(keep) == 2

    def test_nms_keeps_non_overlapping(self) -> None:
        x1 = np.array([0.0, 100.0])
        y1 = np.array([0.0, 100.0])
        x2 = np.array([10.0, 110.0])
        y2 = np.array([10.0, 110.0])
        scores = np.array([0.9, 0.8])

        keep = _nms(x1, y1, x2, y2, scores, iou_thresh=0.5)
        assert len(keep) == 2

    def test_per_class_nms_separates_classes(self) -> None:
        # Two overlapping boxes but different classes → both kept
        x1 = np.array([0.0, 0.01])
        y1 = np.array([0.0, 0.01])
        x2 = np.array([50.0, 50.0])
        y2 = np.array([50.0, 50.0])
        scores = np.array([0.9, 0.8])
        classes = np.array([0, 1])  # person vs car

        keep = _per_class_nms(x1, y1, x2, y2, scores, classes, iou_thresh=0.5)
        assert len(keep) == 2

    def test_empty_input(self) -> None:
        keep = _nms(
            np.array([]), np.array([]), np.array([]), np.array([]),
            np.array([]), iou_thresh=0.5,
        )
        assert len(keep) == 0


# ---------------------------------------------------------------
# Post-processing tests (mock Triton)
# ---------------------------------------------------------------

class TestPostprocessing:

    def test_postprocess_produces_detections(self, client: DetectorClient) -> None:
        # Simulate YOLOv8 output: [1, 11, 8400]
        raw = np.zeros((1, 11, 8400), dtype=np.float32)

        # Place one high-confidence person detection at center
        anchor_idx = 100
        raw[0, 0, anchor_idx] = 320.0  # cx
        raw[0, 1, anchor_idx] = 320.0  # cy
        raw[0, 2, anchor_idx] = 100.0  # w
        raw[0, 3, anchor_idx] = 200.0  # h
        raw[0, 4, anchor_idx] = 0.95   # person score

        lb = LetterboxInfo(
            scale=0.5, pad_w=0, pad_h=80, orig_w=1280, orig_h=720
        )

        dets = client._postprocess(raw, lb)
        assert len(dets) >= 1
        det = dets[0]
        assert det.class_name == "person"
        assert det.confidence == pytest.approx(0.95)
        assert 0 <= det.x_min <= det.x_max <= 1
        assert 0 <= det.y_min <= det.y_max <= 1

    def test_low_confidence_filtered(self, client: DetectorClient) -> None:
        raw = np.zeros((1, 11, 8400), dtype=np.float32)
        # All scores below 0.40 threshold
        raw[0, 4, 0] = 0.1

        lb = LetterboxInfo(scale=1.0, pad_w=0, pad_h=0, orig_w=640, orig_h=640)
        dets = client._postprocess(raw, lb)
        assert len(dets) == 0

    @pytest.mark.asyncio
    async def test_detect_end_to_end_mocked(self, client: DetectorClient, sample_frame: np.ndarray) -> None:
        """Full detect() with mocked Triton."""
        # Simulate output with one detection
        fake_output = np.zeros((1, 11, 8400), dtype=np.float32)
        fake_output[0, 0, 50] = 320.0
        fake_output[0, 1, 50] = 320.0
        fake_output[0, 2, 50] = 80.0
        fake_output[0, 3, 50] = 160.0
        fake_output[0, 4, 50] = 0.85  # person

        with patch.object(client, "_infer", return_value=fake_output):
            dets = await client.detect(sample_frame)

        assert len(dets) >= 1
        assert all(isinstance(d, RawDetection) for d in dets)


# ---------------------------------------------------------------
# Class mapping
# ---------------------------------------------------------------

class TestClassMapping:

    def test_seven_classes(self) -> None:
        assert len(CLASS_INDEX_TO_NAME) == 7

    def test_class_names(self) -> None:
        expected = {"person", "car", "truck", "bus", "bicycle", "motorcycle", "animal"}
        assert set(CLASS_INDEX_TO_NAME.values()) == expected
