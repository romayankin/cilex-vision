"""Tests for the confidence-weighted track aggregator.

Verifies single observation, accumulation, mixed-color voting,
no-observation fallback, and flush cleanup.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aggregator import TrackAggregator


@pytest.fixture
def aggregator() -> TrackAggregator:
    return TrackAggregator()


def test_single_observation(aggregator: TrackAggregator) -> None:
    """A single observation returns that color."""
    aggregator.add_observation("t1", "vehicle_color", "red", 0.9, 0.8)
    result = aggregator.get_result("t1", "vehicle_color")
    assert result is not None
    color, confidence = result
    assert color == "red"
    assert confidence > 0.0


def test_multiple_same_color_increases_confidence(aggregator: TrackAggregator) -> None:
    """Multiple observations of the same color should dominate."""
    aggregator.add_observation("t1", "vehicle_color", "blue", 0.8, 0.9)
    aggregator.add_observation("t1", "vehicle_color", "blue", 0.9, 0.85)
    aggregator.add_observation("t1", "vehicle_color", "blue", 0.7, 0.9)

    result = aggregator.get_result("t1", "vehicle_color")
    assert result is not None
    color, confidence = result
    assert color == "blue"
    assert confidence > 0.9  # All votes are for blue, so ratio ~1.0


def test_mixed_colors_highest_weight_wins(aggregator: TrackAggregator) -> None:
    """With mixed colors, the one with highest weighted sum wins."""
    # High-confidence red observations
    aggregator.add_observation("t1", "vehicle_color", "red", 0.95, 0.9)
    aggregator.add_observation("t1", "vehicle_color", "red", 0.90, 0.85)
    # Lower-confidence blue
    aggregator.add_observation("t1", "vehicle_color", "blue", 0.4, 0.5)

    result = aggregator.get_result("t1", "vehicle_color")
    assert result is not None
    color, confidence = result
    assert color == "red"


def test_no_observations_returns_none(aggregator: TrackAggregator) -> None:
    """No observations should return None."""
    result = aggregator.get_result("t_nonexistent", "vehicle_color")
    assert result is None


def test_flush_returns_aggregated(aggregator: TrackAggregator) -> None:
    """Flush should return one AggregatedAttribute per type with observations."""
    now = datetime.now(tz=timezone.utc)
    aggregator.add_observation("t1", "vehicle_color", "black", 0.8, 0.9, now)
    aggregator.add_observation("t1", "vehicle_color", "black", 0.9, 0.95, now)

    results = aggregator.flush_track("t1")
    assert len(results) == 1
    attr = results[0]
    assert attr.track_id == "t1"
    assert attr.attribute_type == "vehicle_color"
    assert attr.color_value == "black"
    assert attr.confidence > 0.0


def test_flush_cleans_up_state(aggregator: TrackAggregator) -> None:
    """After flush, track state should be gone."""
    aggregator.add_observation("t1", "vehicle_color", "white", 0.8, 0.9)
    aggregator.flush_track("t1")

    assert not aggregator.has_track("t1")
    assert aggregator.get_result("t1", "vehicle_color") is None
    # Flush again returns empty
    assert aggregator.flush_track("t1") == []


def test_multiple_attribute_types(aggregator: TrackAggregator) -> None:
    """Person tracks have both upper and lower color attributes."""
    now = datetime.now(tz=timezone.utc)
    aggregator.add_observation("t1", "person_upper_color", "red", 0.9, 0.8, now)
    aggregator.add_observation("t1", "person_lower_color", "blue", 0.85, 0.9, now)

    results = aggregator.flush_track("t1")
    assert len(results) == 2

    types = {r.attribute_type: r.color_value for r in results}
    assert types["person_upper_color"] == "red"
    assert types["person_lower_color"] == "blue"


def test_observation_count(aggregator: TrackAggregator) -> None:
    """observation_count returns total across all attribute types."""
    aggregator.add_observation("t1", "person_upper_color", "red", 0.9, 0.8)
    aggregator.add_observation("t1", "person_upper_color", "red", 0.8, 0.9)
    aggregator.add_observation("t1", "person_lower_color", "blue", 0.85, 0.9)

    assert aggregator.observation_count("t1") == 3
    assert aggregator.observation_count("t_nonexistent") == 0


def test_zero_weight_observation(aggregator: TrackAggregator) -> None:
    """Zero-weight observations should be handled gracefully."""
    aggregator.add_observation("t1", "vehicle_color", "red", 0.0, 0.0)
    result = aggregator.get_result("t1", "vehicle_color")
    assert result is not None
    color, confidence = result
    # With zero total weight, should return unknown
    assert color == "unknown"
    assert confidence == 0.0


def test_flush_nonexistent_returns_empty(aggregator: TrackAggregator) -> None:
    """Flushing a non-existent track returns empty list."""
    assert aggregator.flush_track("nope") == []
