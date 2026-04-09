#!/usr/bin/env python3
"""Sample candidate cross-camera identity pairs from the database.

Queries local_tracks and topology_edges to find track exits at camera A
paired with track entries at camera B within the transit-time window.

Query logic:
1. Load topology edges for the site from topology_edges + cameras
2. For each directed edge (A→B): find terminated tracks at camera A where
   end_time is within the query window
3. For each exit track: find tracks at camera B where start_time is within
   [exit_time, exit_time + transit_window]
4. Filter: same object_class, both mean_confidence > threshold
5. Rank by temporal plausibility (closer to expected transit time = higher)
6. Output top N candidates as JSON

Output JSON:
{
  "candidates": [
    {
      "pair_id": "...",
      "camera_a": {
        "camera_id": "...",
        "local_track_id": "...",
        "object_class": "...",
        "end_time": "...",
        "mean_confidence": 0.85
      },
      "camera_b": {
        "camera_id": "...",
        "local_track_id": "...",
        "object_class": "...",
        "start_time": "...",
        "mean_confidence": 0.78
      },
      "transit_time_s": 12.3,
      "expected_transit_range_s": [5.0, 45.0],
      "object_class": "person"
    }
  ]
}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)

# Per-class speed factors relative to person baseline (from topology docs).
# Lower factor = faster transit = shorter expected time.
CLASS_SPEED_FACTORS: dict[str, float] = {
    "person": 1.0,
    "car": 0.3,
    "truck": 0.5,
    "bus": 0.4,
    "bicycle": 0.6,
    "motorcycle": 0.35,
    "animal": 0.8,
}


@dataclass(frozen=True)
class TopologyEdge:
    camera_a: str
    camera_b: str
    transition_time_s: float
    transit_distributions: dict[str, Any] | None


@dataclass(frozen=True)
class TrackSummary:
    local_track_id: str
    camera_id: str
    object_class: str
    start_time: datetime
    end_time: datetime | None
    mean_confidence: float | None


@dataclass
class CandidatePair:
    pair_id: str
    camera_a: TrackSummary
    camera_b: TrackSummary
    transit_time_s: float
    expected_transit_range_s: tuple[float, float]
    object_class: str
    plausibility_score: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN (e.g. postgresql://user:pass@host:5432/db).",
    )
    parser.add_argument(
        "--site-id",
        default=os.environ.get("SITE_ID"),
        help="Site UUID for filtering topology edges.",
    )
    parser.add_argument(
        "--start",
        help="Query window start (ISO 8601).",
    )
    parser.add_argument(
        "--end",
        help="Query window end (ISO 8601).",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="Minimum mean_confidence for candidate tracks (default: 0.5).",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=500,
        help="Maximum number of candidate pairs to output (default: 500).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/annotation/reid-candidates.json"),
        help="Output JSON path.",
    )
    return parser.parse_args()


def parse_timestamp(value: str) -> datetime:
    """Parse ISO 8601 timestamp to timezone-aware datetime."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def get_transit_window(
    edge: TopologyEdge, object_class: str
) -> tuple[float, float]:
    """Return (min_s, max_s) transit window for the given edge and class.

    Uses p99 from transit_distributions if available for the object class,
    otherwise falls back to transition_time_s * 3 as the upper bound.
    Minimum is always 0.
    """
    speed_factor = CLASS_SPEED_FACTORS.get(object_class, 1.0)
    base_time = edge.transition_time_s * speed_factor

    if edge.transit_distributions and object_class in edge.transit_distributions:
        dist = edge.transit_distributions[object_class]
        p99 = dist.get("p99")
        if p99 is not None:
            return (0.0, float(p99) * speed_factor)

    return (0.0, base_time * 3.0)


def compute_plausibility(
    transit_time_s: float,
    expected_range: tuple[float, float],
    base_transit_s: float,
) -> float:
    """Score how plausible a transit time is. 1.0 = perfect, 0.0 = at boundary.

    Uses a simple linear decay from the expected midpoint to the window edge.
    """
    midpoint = base_transit_s
    max_time = expected_range[1]
    if max_time <= 0:
        return 0.0
    if transit_time_s < 0:
        return 0.0
    if transit_time_s > max_time:
        return 0.0

    distance_from_mid = abs(transit_time_s - midpoint)
    max_distance = max(midpoint, max_time - midpoint)
    if max_distance <= 0:
        return 1.0
    return max(0.0, 1.0 - distance_from_mid / max_distance)


async def load_edges(dsn: str, site_id: str) -> list[TopologyEdge]:
    """Load topology edges for the site."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT te.camera_a_id, te.camera_b_id, te.transition_time_s,
                   te.transit_distributions
            FROM topology_edges te
            JOIN cameras ca ON ca.camera_id = te.camera_a_id
            WHERE ca.site_id = $1
            """,
            site_id,
        )
        return [
            TopologyEdge(
                camera_a=row["camera_a_id"],
                camera_b=row["camera_b_id"],
                transition_time_s=float(row["transition_time_s"]),
                transit_distributions=(
                    json.loads(row["transit_distributions"])
                    if isinstance(row["transit_distributions"], str)
                    else row["transit_distributions"]
                ),
            )
            for row in rows
        ]
    finally:
        await conn.close()


async def find_exit_tracks(
    dsn: str,
    camera_id: str,
    start: datetime,
    end: datetime,
    min_confidence: float,
) -> list[TrackSummary]:
    """Find terminated tracks at a camera with end_time in the query window."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT local_track_id, camera_id, object_class,
                   start_time, end_time, mean_confidence
            FROM local_tracks
            WHERE camera_id = $1
              AND state = 'terminated'
              AND end_time IS NOT NULL
              AND end_time >= $2
              AND end_time <= $3
              AND (mean_confidence IS NULL OR mean_confidence >= $4)
            ORDER BY end_time
            """,
            camera_id,
            start,
            end,
            min_confidence,
        )
        return [
            TrackSummary(
                local_track_id=str(row["local_track_id"]),
                camera_id=row["camera_id"],
                object_class=row["object_class"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                mean_confidence=float(row["mean_confidence"])
                if row["mean_confidence"] is not None
                else None,
            )
            for row in rows
        ]
    finally:
        await conn.close()


async def find_entry_tracks(
    dsn: str,
    camera_id: str,
    after: datetime,
    before: datetime,
    object_class: str,
    min_confidence: float,
) -> list[TrackSummary]:
    """Find tracks at camera B whose start_time falls within the transit window."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT local_track_id, camera_id, object_class,
                   start_time, end_time, mean_confidence
            FROM local_tracks
            WHERE camera_id = $1
              AND start_time >= $2
              AND start_time <= $3
              AND object_class = $4
              AND (mean_confidence IS NULL OR mean_confidence >= $5)
            ORDER BY start_time
            """,
            camera_id,
            after,
            before,
            object_class,
            min_confidence,
        )
        return [
            TrackSummary(
                local_track_id=str(row["local_track_id"]),
                camera_id=row["camera_id"],
                object_class=row["object_class"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                mean_confidence=float(row["mean_confidence"])
                if row["mean_confidence"] is not None
                else None,
            )
            for row in rows
        ]
    finally:
        await conn.close()


def format_ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


async def sample_candidates(args: argparse.Namespace) -> list[CandidatePair]:
    """Main sampling logic: for each edge, find exit→entry pairs."""
    if not args.dsn:
        raise SystemExit("--dsn is required")
    if not args.site_id:
        raise SystemExit("--site-id is required")
    if not args.start or not args.end:
        raise SystemExit("--start and --end are required")

    start = parse_timestamp(args.start)
    end = parse_timestamp(args.end)

    edges = await load_edges(args.dsn, args.site_id)
    if not edges:
        raise SystemExit(f"no topology edges found for site {args.site_id}")

    all_candidates: list[CandidatePair] = []

    for edge in edges:
        exit_tracks = await find_exit_tracks(
            args.dsn, edge.camera_a, start, end, args.min_confidence
        )

        for exit_track in exit_tracks:
            if exit_track.end_time is None:
                continue

            speed_factor = CLASS_SPEED_FACTORS.get(exit_track.object_class, 1.0)
            base_transit = edge.transition_time_s * speed_factor
            window = get_transit_window(edge, exit_track.object_class)

            from datetime import timedelta

            window_start = exit_track.end_time
            window_end = exit_track.end_time + timedelta(seconds=window[1])

            entry_tracks = await find_entry_tracks(
                args.dsn,
                edge.camera_b,
                window_start,
                window_end,
                exit_track.object_class,
                args.min_confidence,
            )

            for entry_track in entry_tracks:
                transit_s = (
                    entry_track.start_time - exit_track.end_time
                ).total_seconds()

                plausibility = compute_plausibility(transit_s, window, base_transit)

                all_candidates.append(
                    CandidatePair(
                        pair_id=str(uuid.uuid4()),
                        camera_a=exit_track,
                        camera_b=entry_track,
                        transit_time_s=round(transit_s, 2),
                        expected_transit_range_s=(round(window[0], 1), round(window[1], 1)),
                        object_class=exit_track.object_class,
                        plausibility_score=plausibility,
                    )
                )

    # Sort by plausibility descending, take top N
    all_candidates.sort(key=lambda c: c.plausibility_score, reverse=True)
    return all_candidates[: args.max_candidates]


def serialize_candidates(candidates: list[CandidatePair]) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "pair_id": c.pair_id,
                "camera_a": {
                    "camera_id": c.camera_a.camera_id,
                    "local_track_id": c.camera_a.local_track_id,
                    "object_class": c.camera_a.object_class,
                    "end_time": format_ts(c.camera_a.end_time),
                    "mean_confidence": c.camera_a.mean_confidence,
                },
                "camera_b": {
                    "camera_id": c.camera_b.camera_id,
                    "local_track_id": c.camera_b.local_track_id,
                    "object_class": c.camera_b.object_class,
                    "start_time": format_ts(c.camera_b.start_time),
                    "mean_confidence": c.camera_b.mean_confidence,
                },
                "transit_time_s": c.transit_time_s,
                "expected_transit_range_s": list(c.expected_transit_range_s),
                "object_class": c.object_class,
            }
            for c in candidates
        ]
    }


def main() -> None:
    args = parse_args()
    candidates = asyncio.run(sample_candidates(args))
    output = serialize_candidates(candidates)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
