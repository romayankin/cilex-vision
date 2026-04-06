"""Bulk collector parsing tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import main
from config import Settings
from writer import AsyncpgBulkWriter


class FakeTimestamp:
    def __init__(self, seconds: int = 0, nanos: int = 0) -> None:
        self.seconds = seconds
        self.nanos = nanos


class FakeVideoTimestamp:
    def __init__(self) -> None:
        self.source_capture_ts = FakeTimestamp(1_700_000_000, 0)
        self.edge_receive_ts = FakeTimestamp(1_700_000_001, 500_000_000)
        self.core_ingest_ts = FakeTimestamp(1_700_000_002, 0)


class FakeBoundingBox:
    def __init__(self) -> None:
        self.x_min = 0.10
        self.y_min = 0.20
        self.x_max = 0.40
        self.y_max = 0.70


class FakeDetection:
    def __init__(self) -> None:
        self.camera_id = "cam-01"
        self.object_class = 1
        self.confidence = 0.91
        self.bbox = FakeBoundingBox()
        self.timestamps = FakeVideoTimestamp()
        self.model_version = "1.0.0"


@dataclass
class FakeDecoder:
    detection: object

    def decode_detection(self, payload: bytes, *, topic: str) -> object:
        return self.detection


def build_service(decoder: object) -> main.BulkCollectorService:
    settings = Settings()
    return main.BulkCollectorService(
        settings,
        decoder=decoder,
        writer=AsyncpgBulkWriter(dsn="postgresql://unused", pool=object()),
    )


def test_parse_detection_message_converts_bbox_to_xywh_and_track_observation() -> None:
    service = build_service(FakeDecoder(FakeDetection()))

    parsed = service.parse_detection_message(
        topic="bulk.detections",
        payload=b"unused",
        headers={
            "x-frame-seq": "42",
            "x-local-track-id": "12345678-1234-5678-1234-567812345678",
            "x-embedding-ref": "minio://embeddings/track.bin",
        },
    )

    detection = parsed.detection_rows[0]
    observation = parsed.track_observation_rows[0]

    assert detection.frame_seq == 42
    assert detection.bbox_x == pytest.approx(0.10)
    assert detection.bbox_y == pytest.approx(0.20)
    assert detection.bbox_w == pytest.approx(0.30)
    assert detection.bbox_h == pytest.approx(0.50)
    assert detection.time.isoformat() == "2023-11-14T22:13:21.500000+00:00"
    assert observation.centroid_x == pytest.approx(0.25)
    assert observation.centroid_y == pytest.approx(0.45)
    assert observation.bbox_area == pytest.approx(0.15)


def test_parse_detection_message_rejects_below_threshold() -> None:
    detection = FakeDetection()
    detection.confidence = 0.10
    service = build_service(FakeDecoder(detection))

    with pytest.raises(main.DecodeError) as exc_info:
        service.parse_detection_message(
            topic="bulk.detections",
            payload=b"unused",
            headers={"x-frame-seq": "1"},
        )

    assert exc_info.value.reason == "below_threshold"
