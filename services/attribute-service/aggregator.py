"""Confidence-weighted voting aggregator for track-level attributes.

Accumulates per-observation (color, confidence * quality) scores over
the lifetime of a track.  On flush (track TERMINATED or observation
threshold), emits the final aggregated attribute per type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class AggregatedAttribute:
    """Final aggregated attribute for a track."""

    track_id: str
    attribute_type: str
    color_value: str
    confidence: float
    observed_at: datetime


@dataclass
class _Observation:
    """Single classification observation."""

    color: str
    weight: float  # confidence * quality_score


@dataclass
class _TrackState:
    """Accumulated state for one track across attribute types."""

    # attribute_type -> list of observations
    observations: dict[str, list[_Observation]] = field(default_factory=dict)
    last_seen: datetime | None = None


class TrackAggregator:
    """Aggregates color classifications over a track's lifetime."""

    def __init__(self) -> None:
        self._tracks: dict[str, _TrackState] = {}

    def add_observation(
        self,
        track_id: str,
        attr_type: str,
        color: str,
        confidence: float,
        quality: float,
        observed_at: datetime | None = None,
    ) -> None:
        """Record a single classification observation."""
        state = self._tracks.setdefault(track_id, _TrackState())
        obs_list = state.observations.setdefault(attr_type, [])
        obs_list.append(_Observation(color=color, weight=confidence * quality))
        if observed_at is not None:
            state.last_seen = observed_at

    def get_result(
        self,
        track_id: str,
        attr_type: str,
    ) -> tuple[str, float] | None:
        """Get the current best color for a track + attribute type.

        Returns (color, confidence) or None if no observations.
        """
        state = self._tracks.get(track_id)
        if state is None:
            return None
        obs_list = state.observations.get(attr_type)
        if not obs_list:
            return None

        return self._vote(obs_list)

    def flush_track(self, track_id: str) -> list[AggregatedAttribute]:
        """Flush all aggregated attributes for a track and clean up state.

        Returns one AggregatedAttribute per attribute type that has observations.
        """
        state = self._tracks.pop(track_id, None)
        if state is None:
            return []

        results: list[AggregatedAttribute] = []
        now = state.last_seen or datetime.now(tz=timezone.utc)

        for attr_type, obs_list in state.observations.items():
            if not obs_list:
                continue
            color, confidence = self._vote(obs_list)
            results.append(AggregatedAttribute(
                track_id=track_id,
                attribute_type=attr_type,
                color_value=color,
                confidence=confidence,
                observed_at=now,
            ))

        return results

    def observation_count(self, track_id: str) -> int:
        """Return total observations across all attribute types for a track."""
        state = self._tracks.get(track_id)
        if state is None:
            return 0
        return sum(len(obs) for obs in state.observations.values())

    def has_track(self, track_id: str) -> bool:
        """Check if the aggregator has state for a track."""
        return track_id in self._tracks

    @staticmethod
    def _vote(obs_list: list[_Observation]) -> tuple[str, float]:
        """Confidence-weighted voting. Returns (color, normalized_confidence)."""
        scores: dict[str, float] = {}
        for obs in obs_list:
            scores[obs.color] = scores.get(obs.color, 0.0) + obs.weight

        if not scores:
            return ("unknown", 0.0)

        total_weight = sum(scores.values())
        if total_weight <= 0:
            return ("unknown", 0.0)

        best_color = max(scores, key=scores.get)  # type: ignore[arg-type]
        confidence = scores[best_color] / total_weight

        return (best_color, confidence)
