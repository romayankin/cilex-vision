"""Tests for Kafka topic definitions in topics.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

TOPICS_FILE = Path(__file__).resolve().parent / "topics.yaml"


def _load_topic_names() -> list[str]:
    with open(TOPICS_FILE) as fh:
        data = yaml.safe_load(fh)
    return [t["name"] for t in data["topics"]]


def test_frames_decoded_refs_topic_exists() -> None:
    assert "frames.decoded.refs" in _load_topic_names()


def test_bulk_detections_topic_exists() -> None:
    assert "bulk.detections" in _load_topic_names()


def test_frames_decoded_refs_config() -> None:
    with open(TOPICS_FILE) as fh:
        data = yaml.safe_load(fh)
    topic = next(t for t in data["topics"] if t["name"] == "frames.decoded.refs")
    assert topic["partitions"] == 12
    assert topic["key_schema"] == "string(camera_id)"
    assert topic["value_schema"] == "vidanalytics.v1.frame.FrameRef"


def test_bulk_detections_config() -> None:
    with open(TOPICS_FILE) as fh:
        data = yaml.safe_load(fh)
    topic = next(t for t in data["topics"] if t["name"] == "bulk.detections")
    assert topic["partitions"] == 12
    assert topic["key_schema"] == "string(camera_id)"
    assert topic["value_schema"] == "vidanalytics.v1.detection.Detection"
