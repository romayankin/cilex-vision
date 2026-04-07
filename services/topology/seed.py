"""Seed a 4-camera site with plausible topology edges.

Usage::

    python seed.py                     # print JSON to stdout
    python seed.py --apply --dsn ...   # insert into database

The seed creates a single site ``demo-site-1`` with cameras:

- cam-entrance  (zone: entrance)
- cam-lobby     (zone: lobby)
- cam-corridor  (zone: corridor)
- cam-parking   (zone: parking)

Edges have broad transit-time windows (1 s – 120 s for person baseline)
that the adaptive MTMC learner will narrow over time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from models import (
    CameraNode,
    TopologyGraph,
    TransitionEdge,
)

SITE_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
SITE_NAME = "Demo Site — 4-Camera Layout"

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

CAMERAS: list[CameraNode] = [
    CameraNode(
        camera_id="cam-entrance",
        site_id=SITE_ID,
        name="Main Entrance",
        zone_id="entrance",
        latitude=40.7128,
        longitude=-74.0060,
        status="online",
        location_description="Front door facing the street",
    ),
    CameraNode(
        camera_id="cam-lobby",
        site_id=SITE_ID,
        name="Lobby Interior",
        zone_id="lobby",
        latitude=40.7129,
        longitude=-74.0058,
        status="online",
        location_description="Lobby area, wide-angle ceiling mount",
    ),
    CameraNode(
        camera_id="cam-corridor",
        site_id=SITE_ID,
        name="Main Corridor",
        zone_id="corridor",
        latitude=40.7130,
        longitude=-74.0055,
        status="online",
        location_description="Long corridor between lobby and parking garage",
    ),
    CameraNode(
        camera_id="cam-parking",
        site_id=SITE_ID,
        name="Parking Garage Entry",
        zone_id="parking",
        latitude=40.7131,
        longitude=-74.0050,
        status="online",
        location_description="Parking garage ramp, ground floor",
    ),
]

# Edges with plausible person transit times (seconds).
# Each edge also gets per-class distributions via default_distributions().
_EDGE_DEFS: list[dict] = [
    # entrance → lobby: short walk through the door
    {"a": "cam-entrance", "b": "cam-lobby", "t": 5.0, "conf": 0.95},
    # lobby → corridor: across the lobby
    {"a": "cam-lobby", "b": "cam-corridor", "t": 12.0, "conf": 0.90},
    # corridor → parking: down the corridor
    {"a": "cam-corridor", "b": "cam-parking", "t": 25.0, "conf": 0.85},
    # entrance → parking: long walk around the building
    {"a": "cam-entrance", "b": "cam-parking", "t": 90.0, "conf": 0.60},
    # lobby → parking: shortcut through side door
    {"a": "cam-lobby", "b": "cam-parking", "t": 45.0, "conf": 0.70},
    # corridor → lobby: reverse direction
    {"a": "cam-corridor", "b": "cam-lobby", "t": 10.0, "conf": 0.88},
]

EDGES: list[TransitionEdge] = [
    TransitionEdge(
        edge_id=str(uuid.uuid4()),
        camera_a_id=e["a"],
        camera_b_id=e["b"],
        transition_time_s=e["t"],
        confidence=e["conf"],
        enabled=True,
        transit_distributions=TransitionEdge.default_distributions(e["t"]),
    )
    for e in _EDGE_DEFS
]


def build_topology() -> TopologyGraph:
    """Build the seed topology graph."""
    return TopologyGraph(
        site_id=SITE_ID,
        cameras=CAMERAS,
        edges=EDGES,
    )


# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------


async def apply_to_db(dsn: str) -> None:
    """Insert seed data into PostgreSQL via asyncpg."""
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("asyncpg required for --apply") from exc

    conn = await asyncpg.connect(dsn)
    try:
        # Create site if not exists
        await conn.execute(
            "INSERT INTO sites (site_id, name) VALUES ($1, $2) "
            "ON CONFLICT (site_id) DO NOTHING",
            SITE_ID,
            SITE_NAME,
        )

        # Insert cameras
        for cam in CAMERAS:
            config = json.dumps({"zone_id": cam.zone_id}) if cam.zone_id else None
            await conn.execute(
                "INSERT INTO cameras "
                "(camera_id, site_id, name, latitude, longitude, "
                "location_description, status, config_json) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb) "
                "ON CONFLICT (camera_id) DO UPDATE SET "
                "name = EXCLUDED.name, latitude = EXCLUDED.latitude, "
                "longitude = EXCLUDED.longitude, "
                "location_description = EXCLUDED.location_description, "
                "config_json = EXCLUDED.config_json",
                cam.camera_id,
                cam.site_id,
                cam.name,
                cam.latitude,
                cam.longitude,
                cam.location_description,
                cam.status,
                config,
            )

        # Insert edges
        for edge in EDGES:
            await conn.execute(
                "INSERT INTO topology_edges "
                "(edge_id, camera_a_id, camera_b_id, "
                "transition_time_s, confidence, enabled) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (edge_id) DO UPDATE SET "
                "transition_time_s = EXCLUDED.transition_time_s, "
                "confidence = EXCLUDED.confidence, "
                "enabled = EXCLUDED.enabled",
                edge.edge_id,
                edge.camera_a_id,
                edge.camera_b_id,
                edge.transition_time_s,
                edge.confidence,
                edge.enabled,
            )

        print(  # noqa: T201
            f"Seeded site {SITE_ID}: "
            f"{len(CAMERAS)} cameras, {len(EDGES)} edges"
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write seed data to the database (requires --dsn).",
    )
    parser.add_argument(
        "--dsn",
        default="postgresql://cilex:cilex@localhost:5432/cilex",
        help="asyncpg connection string.",
    )
    args = parser.parse_args()

    topo = build_topology()

    if args.apply:
        asyncio.run(apply_to_db(args.dsn))
    else:
        # Print the full topology JSON to stdout
        print(topo.model_dump_json(indent=2))  # noqa: T201


if __name__ == "__main__":
    main()
