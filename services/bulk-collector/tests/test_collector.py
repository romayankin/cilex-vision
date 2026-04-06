"""Batch collector unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from collector import BatchCollector, BufferedMessage, KafkaOffsetCommit
from writer import DetectionRow, TrackObservationRow


class FakeClock:
    """Mutable monotonic clock for batch-age tests."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def make_message(seq: int) -> BufferedMessage:
    """Create one buffered message with both table row types."""
    now = datetime(2026, 4, 6, tzinfo=timezone.utc)
    track_id = uuid4()
    return BufferedMessage(
        commit=KafkaOffsetCommit(
            group_id="bulk-collector-detections",
            topic="bulk.detections",
            partition=0,
            offset=seq,
        ),
        detection_rows=[
            DetectionRow(
                time=now,
                camera_id="cam-01",
                frame_seq=seq,
                object_class="person",
                confidence=0.95,
                bbox_x=0.1,
                bbox_y=0.2,
                bbox_w=0.3,
                bbox_h=0.4,
                local_track_id=track_id,
                model_version="1.0.0",
            )
        ],
        track_observation_rows=[
            TrackObservationRow(
                time=now,
                camera_id="cam-01",
                frame_seq=seq,
                local_track_id=track_id,
                centroid_x=0.25,
                centroid_y=0.4,
                bbox_area=0.12,
                embedding_ref=None,
            )
        ],
    )


def test_batch_collector_flushes_on_batch_size() -> None:
    clock = FakeClock()
    collector = BatchCollector(batch_size=2, max_age_ms=500, clock=clock)

    assert collector.add(make_message(1)) is None
    batch = collector.add(make_message(2))

    assert batch is not None
    assert len(batch.messages) == 2
    assert len(batch.detection_rows) == 2
    assert len(batch.track_observation_rows) == 2
    assert collector.staged_messages() == 0


def test_batch_collector_flushes_on_age_and_can_requeue() -> None:
    clock = FakeClock()
    collector = BatchCollector(batch_size=10, max_age_ms=500, clock=clock)

    assert collector.add(make_message(1)) is None
    clock.value = 0.6

    batch = collector.flush_due()
    assert batch is not None
    assert len(batch.messages) == 1
    assert collector.staged_messages() == 0

    collector.requeue(batch)
    assert collector.staged_messages() == 1

    flushed = collector.flush_all()
    assert flushed is not None
    assert len(flushed.messages) == 1
    assert flushed.messages[0].commit.offset == 1

