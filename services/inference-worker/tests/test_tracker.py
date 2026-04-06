"""Tests for tracker.ByteTracker — synthetic detection sequences."""

from __future__ import annotations

import numpy as np
import pytest

from config import TrackerConfig
from detector_client import RawDetection
from tracker import ByteTracker, STrack, TrackState, _compute_iou_matrix


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

@pytest.fixture
def cfg() -> TrackerConfig:
    return TrackerConfig(
        track_thresh=0.5,
        match_thresh=0.3,
        second_match_thresh=0.3,
        max_lost_frames=5,
        min_hits=3,
    )


@pytest.fixture
def tracker(cfg: TrackerConfig) -> ByteTracker:
    return ByteTracker("cam-1", cfg)


def _det(
    x1: float, y1: float, x2: float, y2: float,
    conf: float = 0.8, cls: int = 0,
) -> RawDetection:
    return RawDetection(x_min=x1, y_min=y1, x_max=x2, y_max=y2,
                        confidence=conf, class_index=cls)


# ---------------------------------------------------------------
# IoU tests
# ---------------------------------------------------------------

class TestIoU:

    def test_identical_boxes(self) -> None:
        a = np.array([[0.0, 0.0, 1.0, 1.0]])
        iou = _compute_iou_matrix(a, a)
        assert iou[0, 0] == pytest.approx(1.0)

    def test_non_overlapping(self) -> None:
        a = np.array([[0.0, 0.0, 0.5, 0.5]])
        b = np.array([[0.6, 0.6, 1.0, 1.0]])
        iou = _compute_iou_matrix(a, b)
        assert iou[0, 0] == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        a = np.array([[0.0, 0.0, 0.5, 0.5]])
        b = np.array([[0.25, 0.25, 0.75, 0.75]])
        iou = _compute_iou_matrix(a, b)
        assert 0 < iou[0, 0] < 1


# ---------------------------------------------------------------
# Track creation and lifecycle
# ---------------------------------------------------------------

class TestTrackLifecycle:

    def test_new_track_created(self, tracker: ByteTracker) -> None:
        dets = [_det(0.1, 0.1, 0.3, 0.3)]
        updated, terminated = tracker.update(dets, frame_ts=1.0)
        assert len(updated) == 1
        assert updated[0].state == TrackState.NEW
        assert len(terminated) == 0

    def test_track_becomes_active(self, tracker: ByteTracker) -> None:
        det = _det(0.1, 0.1, 0.3, 0.3)
        for i in range(4):
            updated, _ = tracker.update([det], frame_ts=float(i))
        # After min_hits=3, track should be ACTIVE
        active = [t for t in updated if t.state == TrackState.ACTIVE]
        assert len(active) >= 1

    def test_track_lost_when_no_detections(self, tracker: ByteTracker) -> None:
        det = _det(0.1, 0.1, 0.3, 0.3)
        tracker.update([det], frame_ts=1.0)

        # No detections for next frame
        updated, _ = tracker.update([], frame_ts=2.0)
        # The track should become LOST
        assert tracker.active_track_count == 0

    def test_track_terminates_after_max_lost(self, tracker: ByteTracker) -> None:
        det = _det(0.1, 0.1, 0.3, 0.3)
        tracker.update([det], frame_ts=0.0)

        # Send empty frames beyond max_lost_frames
        terminated_all: list[STrack] = []
        for i in range(1, 10):
            _, terminated = tracker.update([], frame_ts=float(i))
            terminated_all.extend(terminated)

        assert len(terminated_all) >= 1
        assert all(t.state == TrackState.TERMINATED for t in terminated_all)


# ---------------------------------------------------------------
# Multi-object tracking
# ---------------------------------------------------------------

class TestMultiObject:

    def test_two_separate_tracks(self, tracker: ByteTracker) -> None:
        d1 = _det(0.1, 0.1, 0.2, 0.2)
        d2 = _det(0.7, 0.7, 0.9, 0.9)
        updated, _ = tracker.update([d1, d2], frame_ts=1.0)
        assert len(updated) == 2
        track_ids = {t.track_id for t in updated}
        assert len(track_ids) == 2

    def test_track_continuity(self, tracker: ByteTracker) -> None:
        """Same detection in consecutive frames should keep the same track_id."""
        det = _det(0.1, 0.1, 0.3, 0.3)
        updated1, _ = tracker.update([det], frame_ts=1.0)
        tid1 = updated1[0].track_id

        updated2, _ = tracker.update([det], frame_ts=2.0)
        tid2 = updated2[0].track_id
        assert tid1 == tid2

    def test_moving_object_tracked(self, tracker: ByteTracker) -> None:
        """An object moving slightly should maintain the same track."""
        positions = [(0.1, 0.1, 0.2, 0.2), (0.12, 0.12, 0.22, 0.22),
                     (0.14, 0.14, 0.24, 0.24)]
        tid = None
        for i, (x1, y1, x2, y2) in enumerate(positions):
            updated, _ = tracker.update(
                [_det(x1, y1, x2, y2)], frame_ts=float(i)
            )
            if tid is None:
                tid = updated[0].track_id
            else:
                assert updated[0].track_id == tid

    def test_trajectory_accumulates(self, tracker: ByteTracker) -> None:
        det = _det(0.1, 0.1, 0.3, 0.3)
        for i in range(5):
            updated, _ = tracker.update([det], frame_ts=float(i))

        track = updated[0]
        assert len(track.trajectory) >= 5


# ---------------------------------------------------------------
# Class voting
# ---------------------------------------------------------------

class TestClassVoting:

    def test_majority_class(self, tracker: ByteTracker) -> None:
        # First detection as person, next 3 as car
        det_person = _det(0.1, 0.1, 0.3, 0.3, cls=0)
        det_car = _det(0.1, 0.1, 0.3, 0.3, cls=1)

        tracker.update([det_person], frame_ts=0.0)
        tracker.update([det_car], frame_ts=1.0)
        tracker.update([det_car], frame_ts=2.0)
        updated, _ = tracker.update([det_car], frame_ts=3.0)

        assert updated[0].majority_class == 1  # car


# ---------------------------------------------------------------
# Low-confidence second-pass matching
# ---------------------------------------------------------------

class TestLowConfidenceMatching:

    def test_low_conf_matches_existing_track(self, tracker: ByteTracker) -> None:
        """A low-confidence detection near an existing track should match."""
        high = _det(0.1, 0.1, 0.3, 0.3, conf=0.8)
        tracker.update([high], frame_ts=0.0)

        low = _det(0.1, 0.1, 0.3, 0.3, conf=0.3)
        updated, _ = tracker.update([low], frame_ts=1.0)
        # Should match the existing track, not create a new one
        assert len(updated) == 1
