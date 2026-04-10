"""Tests for the cross-zone track associator.

Creates synthetic multi-zone datasets and verifies cross-zone matching,
class/version filtering, adjacency enforcement, DB persistence,
backward-compatibility, and 100-camera recall.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import numpy as np
import pytest

from config import MTMCSettings
from helpers import make_l2_normalised, make_similar_vector
from cross_zone_associator import CrossZoneAssociator
from zone_sharding import ZoneBoundaryEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: object) -> MTMCSettings:
    defaults = {
        "cross_zone_match_threshold": 0.55,
        "cross_zone_batch_interval_s": 1.0,
    }
    defaults.update(overrides)
    return MTMCSettings(**defaults)  # type: ignore[arg-type]


def _make_boundary_event(
    zone_id: str,
    camera_id: str,
    vector: np.ndarray,
    object_class: str = "person",
    model_version: str = "1.0.0",
    local_track_id: str | None = None,
    global_track_id: str | None = None,
) -> ZoneBoundaryEvent:
    return ZoneBoundaryEvent(
        local_track_id=local_track_id or str(uuid4()),
        camera_id=camera_id,
        zone_id=zone_id,
        embedding_vector=vector.tolist(),
        model_version=model_version,
        object_class=object_class,
        timestamp=time.time(),
        global_track_id=global_track_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cross_zone_match_known_identity(rng: np.random.Generator) -> None:
    """Same person crosses zones — matched by embedding similarity."""
    settings = _make_settings()
    adjacency = {"entrance": {"lobby"}, "lobby": {"entrance", "parking"}}
    assoc = CrossZoneAssociator(settings, zone_adjacency=adjacency)

    # Person identity
    identity = make_l2_normalised(rng=rng)

    # Track exits entrance zone
    ev_entrance = _make_boundary_event(
        zone_id="entrance",
        camera_id="cam-e2",
        vector=identity,
    )
    assoc.ingest_event(ev_entrance)
    # Drain the pending buffer so the entrance event is only in the index
    assoc.match_batch()

    # Same person enters lobby zone (similar embedding)
    similar = make_similar_vector(identity, similarity=0.92, rng=rng)
    ev_lobby = _make_boundary_event(
        zone_id="lobby",
        camera_id="cam-l1",
        vector=similar,
    )
    assoc.ingest_event(ev_lobby)

    matches = assoc.match_batch()
    assert len(matches) == 1
    m = matches[0]
    assert m.zone_a_track_id == ev_lobby.local_track_id
    assert m.zone_a_zone_id == "lobby"
    assert m.zone_b_track_id == ev_entrance.local_track_id
    assert m.zone_b_zone_id == "entrance"
    assert m.cosine_score >= 0.55


def test_cross_zone_rejects_different_class(rng: np.random.Generator) -> None:
    """Car track in zone A does NOT match person track in zone B."""
    settings = _make_settings()
    adjacency = {"entrance": {"lobby"}, "lobby": {"entrance"}}
    assoc = CrossZoneAssociator(settings, zone_adjacency=adjacency)

    identity = make_l2_normalised(rng=rng)

    # Person in entrance
    ev_person = _make_boundary_event(
        zone_id="entrance",
        camera_id="cam-e2",
        vector=identity,
        object_class="person",
    )
    assoc.ingest_event(ev_person)
    assoc.match_batch()

    # Car in lobby — very similar embedding but different class
    similar = make_similar_vector(identity, similarity=0.98, rng=rng)
    ev_car = _make_boundary_event(
        zone_id="lobby",
        camera_id="cam-l1",
        vector=similar,
        object_class="car",
    )
    assoc.ingest_event(ev_car)

    matches = assoc.match_batch()
    assert len(matches) == 0


def test_cross_zone_respects_adjacency(rng: np.random.Generator) -> None:
    """Non-adjacent zones do NOT produce matches."""
    settings = _make_settings()
    # Entrance is adjacent to lobby only. Parking is adjacent to lobby only.
    # Entrance and parking are NOT adjacent.
    adjacency = {
        "entrance": {"lobby"},
        "lobby": {"entrance", "parking"},
        "parking": {"lobby"},
    }
    assoc = CrossZoneAssociator(settings, zone_adjacency=adjacency)

    identity = make_l2_normalised(rng=rng)

    # Track in entrance
    ev_entrance = _make_boundary_event(
        zone_id="entrance",
        camera_id="cam-e2",
        vector=identity,
    )
    assoc.ingest_event(ev_entrance)
    assoc.match_batch()

    # Same identity appears in parking (NOT adjacent to entrance)
    similar = make_similar_vector(identity, similarity=0.95, rng=rng)
    ev_parking = _make_boundary_event(
        zone_id="parking",
        camera_id="cam-p1",
        vector=similar,
    )
    assoc.ingest_event(ev_parking)

    matches = assoc.match_batch()
    # Parking searches lobby (adjacent) — but entrance event is in entrance index
    # Parking does NOT search entrance (non-adjacent) — no match expected
    assert len(matches) == 0


@pytest.mark.asyncio
async def test_site_global_link_created(rng: np.random.Generator) -> None:
    """Successful match persists a site_global_link record via db_writer."""
    db_writer = MagicMock()
    db_writer.create_site_global_link = AsyncMock()

    settings = _make_settings()
    adjacency = {"entrance": {"lobby"}, "lobby": {"entrance"}}
    assoc = CrossZoneAssociator(
        settings, zone_adjacency=adjacency, db_writer=db_writer
    )

    identity = make_l2_normalised(rng=rng)

    ev_a = _make_boundary_event(
        zone_id="entrance",
        camera_id="cam-e2",
        vector=identity,
    )
    assoc.ingest_event(ev_a)
    assoc.match_batch()

    similar = make_similar_vector(identity, similarity=0.93, rng=rng)
    ev_b = _make_boundary_event(
        zone_id="lobby",
        camera_id="cam-l1",
        vector=similar,
    )
    assoc.ingest_event(ev_b)

    matches = assoc.match_batch()
    assert len(matches) == 1

    await assoc.persist_matches(matches)

    db_writer.create_site_global_link.assert_awaited_once()
    call_kwargs = db_writer.create_site_global_link.call_args.kwargs
    assert call_kwargs["zone_a_track_id"] == ev_b.local_track_id
    assert call_kwargs["zone_b_track_id"] == ev_a.local_track_id
    assert call_kwargs["confidence"] >= 0.55


def test_backward_compatible_no_zones(rng: np.random.Generator) -> None:
    """Single-zone site with no adjacency — cross-zone associator is a no-op."""
    settings = _make_settings()
    # Empty adjacency — no cross-zone connections
    assoc = CrossZoneAssociator(settings, zone_adjacency={})

    identity = make_l2_normalised(rng=rng)

    ev = _make_boundary_event(
        zone_id="default",
        camera_id="cam-1",
        vector=identity,
    )
    assoc.ingest_event(ev)

    matches = assoc.match_batch()
    assert len(matches) == 0


def test_100_camera_synthetic(rng: np.random.Generator) -> None:
    """100 cameras across 5 zones, 10 known cross-zone identities — verify recall."""
    settings = _make_settings(cross_zone_match_threshold=0.50)

    zones = ["zone-A", "zone-B", "zone-C", "zone-D", "zone-E"]
    # Linear adjacency: A<->B, B<->C, C<->D, D<->E
    adjacency: dict[str, set[str]] = {}
    for i, z in enumerate(zones):
        adj: set[str] = set()
        if i > 0:
            adj.add(zones[i - 1])
        if i < len(zones) - 1:
            adj.add(zones[i + 1])
        adjacency[z] = adj

    assoc = CrossZoneAssociator(settings, zone_adjacency=adjacency)

    # 100 cameras: 20 per zone, boundary cameras are cam-{zone}-19 (last in zone)
    for zi, zone in enumerate(zones):
        for ci in range(20):
            cam_id = f"cam-{zone}-{ci}"
            vec = make_l2_normalised(rng=rng)
            ev = _make_boundary_event(
                zone_id=zone,
                camera_id=cam_id,
                vector=vec,
            )
            assoc.ingest_event(ev)

    # Flush all background events
    assoc.match_batch()

    # Now create 10 known cross-zone identities crossing adjacent zones
    identities = [make_l2_normalised(rng=rng) for _ in range(10)]

    for i, identity in enumerate(identities):
        # Identity appears at boundary of zone-A and zone-B
        src_zone_idx = i % (len(zones) - 1)
        src_zone = zones[src_zone_idx]
        dst_zone = zones[src_zone_idx + 1]

        track_a_id = str(uuid4())
        ev_a = _make_boundary_event(
            zone_id=src_zone,
            camera_id=f"cam-{src_zone}-19",
            vector=identity,
            local_track_id=track_a_id,
        )
        assoc.ingest_event(ev_a)

    # Flush source events into indices
    assoc.match_batch()

    # Now add the matching events in adjacent zones
    for i, identity in enumerate(identities):
        src_zone_idx = i % (len(zones) - 1)
        dst_zone = zones[src_zone_idx + 1]

        similar = make_similar_vector(identity, similarity=0.90, rng=rng)
        track_b_id = str(uuid4())
        ev_b = _make_boundary_event(
            zone_id=dst_zone,
            camera_id=f"cam-{dst_zone}-0",
            vector=similar,
            local_track_id=track_b_id,
        )
        assoc.ingest_event(ev_b)

    matches = assoc.match_batch()

    # At least 7 out of 10 cross-zone identities should be recalled
    # (some may fail due to noise from the 100 background embeddings)
    assert len(matches) >= 7, (
        f"Only {len(matches)}/10 cross-zone identities recalled"
    )

    # Verify all matches have correct structure
    for m in matches:
        assert m.cosine_score >= 0.50
        assert m.zone_a_zone_id != m.zone_b_zone_id
        assert m.object_class == "person"
