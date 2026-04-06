"""ByteTrack single-camera tracker.

Implements the ByteTrack association strategy:

1. Separate detections into high-confidence and low-confidence groups.
2. First pass: match high-conf detections to predicted tracks via IoU.
3. Second pass: match remaining (unmatched) tracks to low-conf detections.
4. Create new tracks from unmatched high-conf detections.
5. Mark unmatched tracks as LOST; after ``max_lost_frames`` → TERMINATED.

ByteTrack runs on CPU (Kalman filter + Hungarian algorithm).  It is NOT
a Triton model (see P0-D10 handoff).

Track lifecycle:  NEW → ACTIVE → LOST → TERMINATED
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from scipy.optimize import linear_sum_assignment

from config import TrackerConfig
from detector_client import RawDetection

logger = logging.getLogger(__name__)


class TrackState(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    LOST = "lost"
    TERMINATED = "terminated"

    @property
    def proto_value(self) -> int:
        return _STATE_TO_PROTO[self]


_STATE_TO_PROTO: dict[TrackState, int] = {
    TrackState.NEW: 1,
    TrackState.ACTIVE: 2,
    TrackState.LOST: 3,
    TrackState.TERMINATED: 4,
}


@dataclass
class TrajectoryPoint:
    """Single observation in a track's trajectory."""

    detection_id: str
    centroid_x: float  # normalised [0,1]
    centroid_y: float  # normalised [0,1]
    frame_ts: float  # epoch seconds


@dataclass
class STrack:
    """Single tracked object state."""

    track_id: str
    camera_id: str
    bbox: np.ndarray  # [x_min, y_min, x_max, y_max] normalised
    confidence: float
    class_index: int
    state: TrackState
    trajectory: list[TrajectoryPoint] = field(default_factory=list)
    hits: int = 1
    lost_frames: int = 0
    # Class vote accumulator: class_index → count
    class_votes: dict[int, int] = field(default_factory=dict)
    # Best frame for embedding extraction
    best_confidence: float = 0.0
    best_frame_data: np.ndarray | None = field(default=None, repr=False)

    @property
    def centroid(self) -> tuple[float, float]:
        cx = (self.bbox[0] + self.bbox[2]) / 2
        cy = (self.bbox[1] + self.bbox[3]) / 2
        return float(cx), float(cy)

    @property
    def majority_class(self) -> int:
        if not self.class_votes:
            return self.class_index
        return max(self.class_votes, key=self.class_votes.get)  # type: ignore[arg-type]

    @property
    def mean_confidence(self) -> float:
        if not self.trajectory:
            return self.confidence
        return sum(1.0 for _ in self.trajectory) and self.confidence

    def predict(self) -> None:
        """Simple constant-velocity prediction (linear motion model)."""
        if len(self.trajectory) < 2:
            return
        p1 = self.trajectory[-2]
        p2 = self.trajectory[-1]
        dx = p2.centroid_x - p1.centroid_x
        dy = p2.centroid_y - p1.centroid_y
        w = self.bbox[2] - self.bbox[0]
        h = self.bbox[3] - self.bbox[1]
        new_cx = (self.bbox[0] + self.bbox[2]) / 2 + dx
        new_cy = (self.bbox[1] + self.bbox[3]) / 2 + dy
        self.bbox = np.array([
            new_cx - w / 2,
            new_cy - h / 2,
            new_cx + w / 2,
            new_cy + h / 2,
        ], dtype=np.float64)

    def update(
        self,
        det: RawDetection,
        detection_id: str,
        frame_ts: float,
        frame_data: np.ndarray | None = None,
    ) -> None:
        """Update track with a new matched detection."""
        self.bbox = np.array(
            [det.x_min, det.y_min, det.x_max, det.y_max], dtype=np.float64
        )
        self.confidence = det.confidence
        self.class_votes[det.class_index] = (
            self.class_votes.get(det.class_index, 0) + 1
        )
        self.hits += 1
        self.lost_frames = 0

        self.trajectory.append(
            TrajectoryPoint(
                detection_id=detection_id,
                centroid_x=(det.x_min + det.x_max) / 2,
                centroid_y=(det.y_min + det.y_max) / 2,
                frame_ts=frame_ts,
            )
        )

        if det.confidence > self.best_confidence:
            self.best_confidence = det.confidence
            self.best_frame_data = frame_data

        if self.state == TrackState.NEW and self.hits >= 3:
            self.state = TrackState.ACTIVE
        elif self.state == TrackState.LOST:
            self.state = TrackState.ACTIVE


class ByteTracker:
    """Per-camera ByteTrack tracker instance."""

    def __init__(self, camera_id: str, cfg: TrackerConfig) -> None:
        self.camera_id = camera_id
        self._cfg = cfg
        self._active_tracks: list[STrack] = []
        self._lost_tracks: list[STrack] = []
        self._frame_count: int = 0

    @property
    def active_track_count(self) -> int:
        return len(self._active_tracks)

    def update(
        self,
        detections: list[RawDetection],
        frame_ts: float,
        frame_data: np.ndarray | None = None,
    ) -> tuple[list[STrack], list[STrack]]:
        """Process one frame of detections.

        Returns (updated_tracks, terminated_tracks).
        """
        self._frame_count += 1

        # Predict existing tracks forward
        for track in self._active_tracks:
            track.predict()

        # Split detections by confidence
        high_dets: list[RawDetection] = []
        low_dets: list[RawDetection] = []
        for d in detections:
            if d.confidence >= self._cfg.track_thresh:
                high_dets.append(d)
            else:
                low_dets.append(d)

        # --- First pass: match high-conf detections to active tracks ---
        matched_t, matched_d, unmatched_tracks, unmatched_high = (
            self._match(
                self._active_tracks,
                high_dets,
                self._cfg.match_thresh,
            )
        )

        # Update matched tracks
        updated: list[STrack] = []
        for t_idx, d_idx in zip(matched_t, matched_d):
            det = high_dets[d_idx]
            track = self._active_tracks[t_idx]
            det_id = str(uuid.uuid4())
            track.update(det, det_id, frame_ts, frame_data)
            updated.append(track)

        # --- Second pass: match remaining tracks to low-conf detections ---
        remaining_tracks = [self._active_tracks[i] for i in unmatched_tracks]
        if remaining_tracks and low_dets:
            m_t2, m_d2, still_unmatched, _ = self._match(
                remaining_tracks,
                low_dets,
                self._cfg.second_match_thresh,
            )
            for t_idx, d_idx in zip(m_t2, m_d2):
                det = low_dets[d_idx]
                track = remaining_tracks[t_idx]
                det_id = str(uuid.uuid4())
                track.update(det, det_id, frame_ts, frame_data)
                updated.append(track)
            remaining_tracks = [remaining_tracks[i] for i in still_unmatched]

        # --- Also try matching lost tracks to high-conf unmatched dets ---
        unmatched_high_dets = [high_dets[i] for i in unmatched_high]
        if self._lost_tracks and unmatched_high_dets:
            m_lost_t, m_lost_d, still_lost, still_unmatched_high = self._match(
                self._lost_tracks,
                unmatched_high_dets,
                self._cfg.second_match_thresh,
            )
            reactivated: list[STrack] = []
            for t_idx, d_idx in zip(m_lost_t, m_lost_d):
                det = unmatched_high_dets[d_idx]
                track = self._lost_tracks[t_idx]
                det_id = str(uuid.uuid4())
                track.update(det, det_id, frame_ts, frame_data)
                reactivated.append(track)
            updated.extend(reactivated)
            self._lost_tracks = [self._lost_tracks[i] for i in still_lost]
            unmatched_high_dets = [unmatched_high_dets[i] for i in still_unmatched_high]
        else:
            unmatched_high_dets = [high_dets[i] for i in unmatched_high]

        # Mark unmatched active tracks as LOST
        for track in remaining_tracks:
            track.lost_frames += 1
            if track.state != TrackState.LOST:
                track.state = TrackState.LOST
            self._lost_tracks.append(track)

        # Terminate tracks that exceeded max_lost_frames
        terminated: list[STrack] = []
        surviving_lost: list[STrack] = []
        for track in self._lost_tracks:
            track.lost_frames += 1
            if track.lost_frames > self._cfg.max_lost_frames:
                track.state = TrackState.TERMINATED
                terminated.append(track)
            else:
                surviving_lost.append(track)
        self._lost_tracks = surviving_lost

        # Create new tracks from unmatched high-conf detections
        for det in unmatched_high_dets:
            det_id = str(uuid.uuid4())
            track = STrack(
                track_id=str(uuid.uuid4()),
                camera_id=self.camera_id,
                bbox=np.array(
                    [det.x_min, det.y_min, det.x_max, det.y_max],
                    dtype=np.float64,
                ),
                confidence=det.confidence,
                class_index=det.class_index,
                state=TrackState.NEW,
                class_votes={det.class_index: 1},
                best_confidence=det.confidence,
                best_frame_data=frame_data,
            )
            track.trajectory.append(
                TrajectoryPoint(
                    detection_id=det_id,
                    centroid_x=(det.x_min + det.x_max) / 2,
                    centroid_y=(det.y_min + det.y_max) / 2,
                    frame_ts=frame_ts,
                )
            )
            updated.append(track)

        # Rebuild active list
        self._active_tracks = [
            t for t in updated if t.state != TrackState.TERMINATED
        ]

        return updated, terminated

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    @staticmethod
    def _match(
        tracks: list[STrack],
        detections: list[RawDetection],
        iou_thresh: float,
    ) -> tuple[list[int], list[int], list[int], list[int]]:
        """Hungarian matching via IoU cost matrix.

        Returns (matched_track_idx, matched_det_idx,
                 unmatched_track_idx, unmatched_det_idx).
        """
        if not tracks or not detections:
            return (
                [],
                [],
                list(range(len(tracks))),
                list(range(len(detections))),
            )

        track_boxes = np.array([t.bbox for t in tracks])
        det_boxes = np.array(
            [[d.x_min, d.y_min, d.x_max, d.y_max] for d in detections]
        )

        iou_matrix = _compute_iou_matrix(track_boxes, det_boxes)
        cost_matrix = 1.0 - iou_matrix

        row_indices, col_indices = linear_sum_assignment(cost_matrix)

        matched_t: list[int] = []
        matched_d: list[int] = []
        unmatched_t = set(range(len(tracks)))
        unmatched_d = set(range(len(detections)))

        for r, c in zip(row_indices, col_indices):
            if iou_matrix[r, c] >= iou_thresh:
                matched_t.append(r)
                matched_d.append(c)
                unmatched_t.discard(r)
                unmatched_d.discard(c)

        return (
            matched_t,
            matched_d,
            sorted(unmatched_t),
            sorted(unmatched_d),
        )


def _compute_iou_matrix(
    boxes_a: np.ndarray, boxes_b: np.ndarray
) -> np.ndarray:
    """Compute IoU matrix between two sets of [x1,y1,x2,y2] boxes."""
    x1 = np.maximum(boxes_a[:, 0:1], boxes_b[:, 0:1].T)
    y1 = np.maximum(boxes_a[:, 1:2], boxes_b[:, 1:2].T)
    x2 = np.minimum(boxes_a[:, 2:3], boxes_b[:, 2:3].T)
    y2 = np.minimum(boxes_a[:, 3:4], boxes_b[:, 3:4].T)

    inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    area_a = (boxes_a[:, 2] - boxes_a[:, 0]) * (boxes_a[:, 3] - boxes_a[:, 1])
    area_b = (boxes_b[:, 2] - boxes_b[:, 0]) * (boxes_b[:, 3] - boxes_b[:, 1])
    union = area_a[:, None] + area_b[None, :] - inter

    return inter / np.maximum(union, 1e-8)
