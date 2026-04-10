"""Adaptive transit-time learning from observed MTMC transitions.

Reads from the ``transit_time_stats`` materialized view, blends learned
distributions with static priors from ``TransitionEdge.default_distributions``,
and updates the ``topology_edges.transit_distributions`` JSONB column.

The blending uses a linear ramp: weight = min(sample_count / min_samples, 1.0).
With 0 samples the prior is returned unchanged; at ``min_samples`` or above the
learned distribution fully replaces the prior.

Usage::

    python adaptive_transit.py --db-dsn postgresql://... --min-samples 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Lazy import — asyncpg is only needed at runtime.
asyncpg: Any = None


def _ensure_asyncpg() -> Any:
    global asyncpg  # noqa: PLW0603
    if asyncpg is None:
        import asyncpg as _apg  # noqa: PLC0415

        asyncpg = _apg
    return asyncpg


# ---------------------------------------------------------------------------
# Models (from services/topology/models.py — imported at runtime)
# ---------------------------------------------------------------------------

try:
    from models import TransitTimeDistribution, TransitionEdge
except ImportError:
    # Fallback: inline minimal definitions for standalone use
    from pydantic import BaseModel

    class TransitTimeDistribution(BaseModel):  # type: ignore[no-redef]
        object_class: str
        p50_ms: float
        p90_ms: float
        p99_ms: float
        sample_count: int = 0
        last_updated: datetime | None = None

    class TransitionEdge(BaseModel):  # type: ignore[no-redef]
        edge_id: str | None = None
        camera_a_id: str = ""
        camera_b_id: str = ""
        transition_time_s: float = 0.0

        @staticmethod
        def default_distributions(
            transition_time_s: float,
        ) -> list[TransitTimeDistribution]:
            _SPEED = {
                "person": 1.0, "car": 0.3, "truck": 0.5, "bus": 0.4,
                "bicycle": 0.6, "motorcycle": 0.35, "animal": 0.8,
            }
            dists: list[TransitTimeDistribution] = []
            for cls, factor in _SPEED.items():
                base_ms = transition_time_s * 1000.0 * factor
                dists.append(TransitTimeDistribution(
                    object_class=cls,
                    p50_ms=round(base_ms, 1),
                    p90_ms=round(base_ms * 1.5, 1),
                    p99_ms=round(base_ms * 2.5, 1),
                ))
            return dists


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LearnedDistribution:
    """One row from ``transit_time_stats``."""

    from_camera: str
    to_camera: str
    object_class: str
    p50_ms: float
    p90_ms: float
    p99_ms: float
    sample_count: int


@dataclass
class UpdateResult:
    """Summary of a single adaptive update run."""

    edges_updated: int
    distributions_blended: int
    total_learned_rows: int


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


async def refresh_transit_stats(conn: Any) -> None:
    """Non-blocking refresh of the ``transit_time_stats`` materialized view."""
    await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY transit_time_stats")
    logger.info("Refreshed transit_time_stats materialized view")


async def fetch_learned_distributions(conn: Any) -> list[LearnedDistribution]:
    """Read all rows from ``transit_time_stats``."""
    rows = await conn.fetch(
        "SELECT from_camera, to_camera, object_class, "
        "p50_ms, p90_ms, p99_ms, sample_count "
        "FROM transit_time_stats"
    )
    return [
        LearnedDistribution(
            from_camera=r["from_camera"],
            to_camera=r["to_camera"],
            object_class=r["object_class"],
            p50_ms=float(r["p50_ms"]),
            p90_ms=float(r["p90_ms"]),
            p99_ms=float(r["p99_ms"]),
            sample_count=int(r["sample_count"]),
        )
        for r in rows
    ]


def blend_distribution(
    prior: TransitTimeDistribution,
    learned: LearnedDistribution,
    min_samples: int = 100,
) -> TransitTimeDistribution:
    """Blend a static prior with a learned distribution.

    The blend weight ramps linearly from 0 (no samples) to 1.0
    (``min_samples`` or more).  This ensures the prior dominates when
    data is sparse and learned values dominate once enough Re-ID
    matches have been observed.
    """
    weight = min(learned.sample_count / min_samples, 1.0) if min_samples > 0 else 1.0
    return TransitTimeDistribution(
        object_class=learned.object_class,
        p50_ms=round(prior.p50_ms * (1 - weight) + learned.p50_ms * weight, 1),
        p90_ms=round(prior.p90_ms * (1 - weight) + learned.p90_ms * weight, 1),
        p99_ms=round(prior.p99_ms * (1 - weight) + learned.p99_ms * weight, 1),
        sample_count=learned.sample_count,
        last_updated=datetime.now(timezone.utc),
    )


async def update_edge_distributions(
    conn: Any,
    edge_id: str,
    blended: list[TransitTimeDistribution],
) -> None:
    """Persist blended distributions to ``topology_edges.transit_distributions``."""
    payload = json.dumps([
        {
            "object_class": d.object_class,
            "p50_ms": d.p50_ms,
            "p90_ms": d.p90_ms,
            "p99_ms": d.p99_ms,
            "sample_count": d.sample_count,
            "last_updated": d.last_updated.isoformat() if d.last_updated else None,
        }
        for d in blended
    ])
    await conn.execute(
        "UPDATE topology_edges SET transit_distributions = $1::jsonb "
        "WHERE edge_id = $2",
        payload,
        edge_id,
    )


async def run_adaptive_update(
    dsn: str,
    min_samples: int = 100,
) -> UpdateResult:
    """Full pipeline: refresh view, fetch learned stats, blend, update edges.

    1. Refresh the materialized view (non-blocking).
    2. Fetch all learned distributions.
    3. For each topology edge, blend learned distributions with the
       static prior derived from ``transition_time_s``.
    4. Write the blended distributions back to the JSONB column.
    """
    apg = _ensure_asyncpg()
    conn = await apg.connect(dsn)

    try:
        # Step 1: refresh the materialized view
        await refresh_transit_stats(conn)

        # Step 2: fetch learned distributions
        learned_rows = await fetch_learned_distributions(conn)
        logger.info("Fetched %d learned distribution rows", len(learned_rows))

        # Index learned rows by (from_camera, to_camera)
        learned_by_edge: dict[tuple[str, str], list[LearnedDistribution]] = {}
        for row in learned_rows:
            key = (row.from_camera, row.to_camera)
            learned_by_edge.setdefault(key, []).append(row)

        # Step 3: load all topology edges
        edge_rows = await conn.fetch(
            "SELECT edge_id, camera_a_id, camera_b_id, transition_time_s "
            "FROM topology_edges WHERE enabled = true"
        )

        edges_updated = 0
        distributions_blended = 0

        for edge_row in edge_rows:
            edge_id = str(edge_row["edge_id"])
            cam_a = edge_row["camera_a_id"]
            cam_b = edge_row["camera_b_id"]
            transition_time_s = float(edge_row["transition_time_s"])

            # Get static priors
            priors = TransitionEdge.default_distributions(transition_time_s)
            prior_by_class = {d.object_class: d for d in priors}

            # Check both directions for learned data
            learned_forward = {
                r.object_class: r
                for r in learned_by_edge.get((cam_a, cam_b), [])
            }
            learned_reverse = {
                r.object_class: r
                for r in learned_by_edge.get((cam_b, cam_a), [])
            }

            blended: list[TransitTimeDistribution] = []
            edge_had_updates = False

            for obj_class, prior in prior_by_class.items():
                # Prefer forward direction; fall back to reverse
                learned = learned_forward.get(obj_class) or learned_reverse.get(obj_class)
                if learned is not None:
                    blended.append(blend_distribution(prior, learned, min_samples))
                    edge_had_updates = True
                    distributions_blended += 1
                else:
                    blended.append(prior)

            # Step 4: write back if anything changed
            if edge_had_updates:
                await update_edge_distributions(conn, edge_id, blended)
                edges_updated += 1

        result = UpdateResult(
            edges_updated=edges_updated,
            distributions_blended=distributions_blended,
            total_learned_rows=len(learned_rows),
        )
        logger.info(
            "Adaptive update complete: %d edges updated, %d distributions blended",
            result.edges_updated,
            result.distributions_blended,
        )
        return result

    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run adaptive transit-time learning from MTMC observations."
    )
    parser.add_argument(
        "--db-dsn",
        default="postgresql://cilex:cilex@localhost:5432/cilex",
        help="asyncpg connection string (default: %(default)s)",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=100,
        help="Sample count for full blend weight (default: %(default)s)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = asyncio.run(run_adaptive_update(args.db_dsn, args.min_samples))
    print(  # noqa: T201
        f"Done: {result.edges_updated} edges updated, "
        f"{result.distributions_blended} distributions blended "
        f"(from {result.total_learned_rows} learned rows)"
    )


if __name__ == "__main__":
    main()
